"""Native assessment authoring, invitation, autosave, and grading services."""

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import PurePath
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import FieldClass
from app.db import forced_tenant_rls, resolve_capability, set_tenant_context
from app.models.application import Application
from app.models.assessment import (
    AssessmentAnswer,
    AssessmentAttempt,
    AssessmentFile,
    AssessmentInvitation,
    AssessmentQuestion,
    AssessmentResult,
    AssessmentSection,
    AssessmentTemplate,
    AssessmentTemplateVersion,
)
from app.models.enterprise import AuditLog
from app.models.user import User
from app.schemas.assessments import (
    AssessmentAccommodations,
    AssessmentAttemptOut,
    AssessmentAttemptSummaryOut,
    AssessmentAutosaveRequest,
    AssessmentFileUploadOut,
    AssessmentInvitationCreate,
    AssessmentInvitationCreated,
    AssessmentInvitationOut,
    AssessmentInvitationReissueRequest,
    AssessmentManualGradeRequest,
    AssessmentQuestionOut,
    AssessmentResultOut,
    AssessmentReviewerFileOut,
    AssessmentReviewerResultSummary,
    AssessmentSectionOut,
    AssessmentSubmissionOut,
    AssessmentTemplateCreate,
    AssessmentTemplateOut,
    AssessmentTemplateUpdate,
    AssessmentVersionCreate,
    AssessmentVersionOut,
    PublicAssessmentLaunch,
    PublicAssessmentQuestion,
    PublicAssessmentResultOut,
    PublicAssessmentSection,
    PublicAssessmentSubmissionOut,
)
from app.schemas.pagination import CursorPage
from app.services.enterprise import require_field_access
from app.services.entitlements import EntitlementExceeded, record_usage
from app.services.pagination import (
    collection_cursor_scope,
    cursor_datetime,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)
from app.services.uploads import (
    cleanup_promoted_upload_after_db_failure,
    enqueue_or_delete_private_object,
    resolve_resource_private_blob_reference,
    store_private_upload,
)
from app.storage import BlobStoreError, get_blob_store
from app.storage.registry import register_ready_private_object

_MISSING = object()
_MAX_ANSWER_BYTES = 256 * 1024
_MAX_ASSESSMENT_FILE_BYTES = 15 * 1024 * 1024
_ASSESSMENT_FILE_SUFFIXES = {".pdf", ".docx", ".txt", ".png", ".jpg", ".jpeg"}


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _json_config(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    return value.model_dump(mode="json", exclude_none=True)


async def _load_version(
    db: AsyncSession,
    version_id: UUID,
    *,
    organization_id: UUID | None = None,
) -> tuple[
    AssessmentTemplateVersion, list[tuple[AssessmentSection, list[AssessmentQuestion]]]
]:
    stmt = select(AssessmentTemplateVersion).where(
        AssessmentTemplateVersion.id == version_id
    )
    if organization_id is not None:
        stmt = stmt.where(AssessmentTemplateVersion.organization_id == organization_id)
    version = (await db.execute(stmt)).scalar_one_or_none()
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assessment version not found")
    sections = list(
        (
            await db.execute(
                select(AssessmentSection)
                .where(AssessmentSection.version_id == version.id)
                .order_by(AssessmentSection.position, AssessmentSection.id)
            )
        ).scalars()
    )
    questions = list(
        (
            await db.execute(
                select(AssessmentQuestion)
                .where(AssessmentQuestion.version_id == version.id)
                .order_by(
                    AssessmentQuestion.section_id,
                    AssessmentQuestion.position,
                    AssessmentQuestion.id,
                )
            )
        ).scalars()
    )
    by_section: dict[UUID, list[AssessmentQuestion]] = {
        section.id: [] for section in sections
    }
    for question in questions:
        by_section.setdefault(question.section_id, []).append(question)
    return version, [(section, by_section.get(section.id, [])) for section in sections]


def _question_out(question: AssessmentQuestion) -> AssessmentQuestionOut:
    return AssessmentQuestionOut(
        id=question.id,
        key=question.key,
        question_type=question.question_type,
        prompt=question.prompt,
        description=question.description,
        required=question.required,
        position=question.position,
        options=question.options,
        validation=question.validation,
        condition=question.condition,
        scoring=question.scoring,
        knockout=question.knockout,
        max_score=question.max_score,
    )


async def version_out(
    db: AsyncSession,
    version: AssessmentTemplateVersion,
    hierarchy: list[tuple[AssessmentSection, list[AssessmentQuestion]]] | None = None,
) -> AssessmentVersionOut:
    if hierarchy is None:
        _, hierarchy = await _load_version(db, version.id)
    return AssessmentVersionOut(
        id=version.id,
        template_id=version.template_id,
        version=version.version,
        title=version.title,
        instructions=version.instructions,
        passing_percentage=version.passing_percentage,
        time_limit_minutes=version.time_limit_minutes,
        published_at=version.published_at,
        sections=[
            AssessmentSectionOut(
                id=section.id,
                title=section.title,
                description=section.description,
                position=section.position,
                questions=[_question_out(question) for question in questions],
            )
            for section, questions in hierarchy
        ],
    )


async def template_out(
    db: AsyncSession, template: AssessmentTemplate, *, include_versions: bool
) -> AssessmentTemplateOut:
    versions: list[AssessmentVersionOut] = []
    if include_versions:
        rows = list(
            (
                await db.execute(
                    select(AssessmentTemplateVersion)
                    .where(AssessmentTemplateVersion.template_id == template.id)
                    .order_by(AssessmentTemplateVersion.version.desc())
                )
            ).scalars()
        )
        versions = [await version_out(db, version) for version in rows]
    return AssessmentTemplateOut(
        id=template.id,
        name=template.name,
        description=template.description,
        status=template.status,
        created_at=template.created_at,
        updated_at=template.updated_at,
        versions=versions,
    )


async def create_template(
    db: AsyncSession, user: User, body: AssessmentTemplateCreate
) -> AssessmentTemplateOut:
    template = AssessmentTemplate(
        organization_id=user.organization_id,
        created_by_user_id=user.id,
        name=body.name,
        description=body.description,
    )
    db.add(template)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "An assessment template with this name already exists",
        ) from exc
    await db.refresh(template)
    return await template_out(db, template, include_versions=False)


async def list_templates(
    db: AsyncSession,
    organization_id: UUID,
    *,
    include_archived: bool = False,
    limit: int = 100,
    cursor: str | None = None,
) -> CursorPage[AssessmentTemplateOut]:
    stmt = select(AssessmentTemplate).where(
        AssessmentTemplate.organization_id == organization_id
    )
    if not include_archived:
        stmt = stmt.where(AssessmentTemplate.status == "active")
    namespace = "assessments.templates"
    scope = collection_cursor_scope(
        namespace,
        organization_id=organization_id,
        filters={"include_archived": include_archived},
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"name", "id"},
            detail="Invalid assessment templates cursor",
        )
        cursor_name = payload["name"]
        if not isinstance(cursor_name, str):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"code": "cursor_invalid", "detail": "Invalid assessment templates cursor"},
            )
        cursor_id = cursor_uuid(payload, detail="Invalid assessment templates cursor")
        stmt = stmt.where(
            or_(
                AssessmentTemplate.name > cursor_name,
                and_(AssessmentTemplate.name == cursor_name, AssessmentTemplate.id > cursor_id),
            )
        )
    rows = list(
        (
            await db.execute(
                stmt.order_by(AssessmentTemplate.name, AssessmentTemplate.id).limit(
                    limit + 1
                )
            )
        ).scalars()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return CursorPage(
        items=[await template_out(db, row, include_versions=False) for row in rows],
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={"name": rows[-1].name, "id": str(rows[-1].id)},
            )
            if has_more and rows
            else None
        ),
    )


async def get_template(
    db: AsyncSession, organization_id: UUID, template_id: UUID
) -> AssessmentTemplate:
    template = (
        await db.execute(
            select(AssessmentTemplate).where(
                AssessmentTemplate.id == template_id,
                AssessmentTemplate.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if template is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assessment template not found")
    return template


async def update_template(
    db: AsyncSession,
    user: User,
    template_id: UUID,
    body: AssessmentTemplateUpdate,
) -> AssessmentTemplateOut:
    template = await get_template(db, user.organization_id, template_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(template, field, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "An assessment template with this name already exists",
        ) from exc
    await db.refresh(template)
    return await template_out(db, template, include_versions=False)


async def create_version(
    db: AsyncSession,
    user: User,
    template_id: UUID,
    body: AssessmentVersionCreate,
) -> AssessmentVersionOut:
    template = (
        await db.execute(
            select(AssessmentTemplate)
            .where(
                AssessmentTemplate.id == template_id,
                AssessmentTemplate.organization_id == user.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if template is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assessment template not found")
    if template.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "Assessment template is archived")
    latest = (
        await db.execute(
            select(func.coalesce(func.max(AssessmentTemplateVersion.version), 0)).where(
                AssessmentTemplateVersion.template_id == template.id
            )
        )
    ).scalar_one()
    version = AssessmentTemplateVersion(
        organization_id=user.organization_id,
        template_id=template.id,
        version=int(latest) + 1,
        title=body.title,
        instructions=body.instructions,
        passing_percentage=body.passing_percentage,
        time_limit_minutes=body.time_limit_minutes,
        created_by_user_id=user.id,
    )
    db.add(version)
    await db.flush()
    for section_position, section_body in enumerate(body.sections):
        section = AssessmentSection(
            organization_id=user.organization_id,
            version_id=version.id,
            title=section_body.title,
            description=section_body.description,
            position=section_position,
        )
        db.add(section)
        await db.flush()
        for question_position, question_body in enumerate(section_body.questions):
            db.add(
                AssessmentQuestion(
                    organization_id=user.organization_id,
                    version_id=version.id,
                    section_id=section.id,
                    key=question_body.key,
                    question_type=question_body.question_type,
                    prompt=question_body.prompt,
                    description=question_body.description,
                    required=question_body.required,
                    position=question_position,
                    options=[
                        option.model_dump(mode="json")
                        for option in question_body.options
                    ],
                    validation=_json_config(question_body.validation),
                    condition=_json_config(question_body.condition),
                    scoring=_json_config(question_body.scoring),
                    knockout=_json_config(question_body.knockout),
                    max_score=question_body.max_score,
                )
            )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Assessment version could not be published",
        ) from exc
    await db.refresh(version)
    return await version_out(db, version)


def _invitation_out(invitation: AssessmentInvitation) -> AssessmentInvitationOut:
    return AssessmentInvitationOut(
        id=invitation.id,
        application_id=invitation.application_id,
        template_version_id=invitation.template_version_id,
        token_prefix=invitation.token_prefix,
        recipient_email=invitation.recipient_email,
        status=invitation.status,
        expires_at=invitation.expires_at,
        max_attempts=invitation.max_attempts,
        authorized_attempts=invitation.authorized_attempts,
        accommodations=AssessmentAccommodations.model_validate(
            invitation.accommodations or {}
        ),
        first_opened_at=invitation.first_opened_at,
        completed_at=invitation.completed_at,
        created_at=invitation.created_at,
    )


async def create_invitation(
    db: AsyncSession,
    user: User,
    application_id: UUID,
    body: AssessmentInvitationCreate,
) -> AssessmentInvitationCreated:
    application = (
        await db.execute(
            select(Application).where(
                Application.id == application_id,
                Application.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if application is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application not found")
    version, _ = await _load_version(
        db, body.template_version_id, organization_id=user.organization_id
    )
    template = await get_template(db, user.organization_id, version.template_id)
    if template.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "Assessment template is archived")
    expires_at = body.expires_at.astimezone(UTC)
    now = _now()
    if expires_at <= now:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Expiry must be in the future"
        )
    if expires_at > now + timedelta(days=365):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Expiry cannot be more than 365 days in the future",
        )
    token = "asm_" + secrets.token_urlsafe(32)
    invitation = AssessmentInvitation(
        organization_id=user.organization_id,
        application_id=application.id,
        template_version_id=version.id,
        token_digest=_token_digest(token),
        token_prefix=token[:12],
        recipient_email=(
            body.recipient_email.strip().lower() if body.recipient_email else None
        ),
        expires_at=expires_at,
        max_attempts=body.max_attempts,
        authorized_attempts=1,
        accommodations=body.accommodations.model_dump(mode="json"),
        invited_by_user_id=user.id,
    )
    db.add(invitation)
    await db.commit()
    await db.refresh(invitation)
    return AssessmentInvitationCreated(
        **_invitation_out(invitation).model_dump(), token=token
    )


async def list_invitations(
    db: AsyncSession,
    organization_id: UUID,
    application_id: UUID,
    *,
    limit: int = 100,
    cursor: str | None = None,
) -> CursorPage[AssessmentInvitationOut]:
    application = (
        await db.execute(
            select(Application.id).where(
                Application.id == application_id,
                Application.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if application is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application not found")
    namespace = "assessments.invitations"
    scope = collection_cursor_scope(
        namespace,
        organization_id=organization_id,
        filters={"application_id": str(application_id)},
    )
    stmt = select(AssessmentInvitation).where(
        AssessmentInvitation.organization_id == organization_id,
        AssessmentInvitation.application_id == application_id,
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"created_at", "id"},
            detail="Invalid assessment invitations cursor",
        )
        cursor_time = cursor_datetime(
            payload, "created_at", detail="Invalid assessment invitations cursor"
        )
        cursor_id = cursor_uuid(payload, detail="Invalid assessment invitations cursor")
        stmt = stmt.where(
            or_(
                AssessmentInvitation.created_at < cursor_time,
                and_(AssessmentInvitation.created_at == cursor_time, AssessmentInvitation.id < cursor_id),
            )
        )
    rows = list(
        (
            await db.execute(
                stmt.order_by(
                    AssessmentInvitation.created_at.desc(), AssessmentInvitation.id.desc()
                ).limit(limit + 1)
            )
        ).scalars()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    now = _now()
    changed = False
    for invitation in rows:
        if invitation.status == "active" and invitation.expires_at <= now:
            invitation.status = "expired"
            changed = True
    if changed:
        await db.commit()
    return CursorPage(
        items=[_invitation_out(row) for row in rows],
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "created_at": rows[-1].created_at.astimezone(UTC).isoformat(),
                    "id": str(rows[-1].id),
                },
            )
            if has_more and rows
            else None
        ),
    )


async def revoke_invitation(
    db: AsyncSession, user: User, invitation_id: UUID
) -> AssessmentInvitationOut:
    invitation = (
        await db.execute(
            select(AssessmentInvitation)
            .where(
                AssessmentInvitation.id == invitation_id,
                AssessmentInvitation.organization_id == user.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if invitation is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Assessment invitation not found"
        )
    if invitation.status == "revoked":
        return _invitation_out(invitation)
    invitation.status = "revoked"
    pending_files = list(
        (
            await db.execute(
                select(AssessmentFile)
                .join(
                    AssessmentAttempt, AssessmentAttempt.id == AssessmentFile.attempt_id
                )
                .where(
                    AssessmentFile.invitation_id == invitation.id,
                    AssessmentFile.organization_id == user.organization_id,
                    AssessmentFile.status == "ready",
                    AssessmentAttempt.status == "in_progress",
                )
                .with_for_update()
            )
        ).scalars()
    )
    for file in pending_files:
        await _erase_assessment_file(
            db,
            file,
            reason="resource_deleted",
            clear_answer=True,
        )
    await db.commit()
    await db.refresh(invitation)
    return _invitation_out(invitation)


async def reissue_invitation(
    db: AsyncSession,
    user: User,
    invitation_id: UUID,
    body: AssessmentInvitationReissueRequest,
) -> AssessmentInvitationCreated:
    """Atomically replace a candidate capability token and its bounded expiry."""

    invitation = (
        await db.execute(
            select(AssessmentInvitation)
            .where(
                AssessmentInvitation.id == invitation_id,
                AssessmentInvitation.organization_id == user.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if invitation is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Assessment invitation not found"
        )
    if invitation.status in {"revoked", "completed"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "assessment_invitation_not_reissuable"},
        )
    now = _now()
    expires_at = body.expires_at.astimezone(UTC)
    if expires_at <= now:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Expiry must be in the future"
        )
    if expires_at > now + timedelta(days=365):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Expiry cannot be more than 365 days in the future",
        )

    token = "asm_" + secrets.token_urlsafe(32)
    previous_expiry = invitation.expires_at
    previous_status = invitation.status
    invitation.token_digest = _token_digest(token)
    invitation.token_prefix = token[:12]
    invitation.expires_at = expires_at
    invitation.status = "active"
    db.add(
        AuditLog(
            organization_id=user.organization_id,
            actor_user_id=user.id,
            actor_email_snapshot=user.email,
            action="reissue_assessment_invitation",
            resource_type="assessment_invitation",
            resource_id=invitation.id,
            path_template="/assessment-invitations/{invitation_id}/reissue",
            method="POST",
            status_code=200,
            request_id=uuid4(),
            metadata_json={
                "application_id": str(invitation.application_id),
                "previous_status": previous_status,
                "previous_expiry": previous_expiry.isoformat(),
                "expires_at": expires_at.isoformat(),
            },
            occurred_at=now,
        )
    )
    await db.commit()
    await db.refresh(invitation)
    return AssessmentInvitationCreated(
        **_invitation_out(invitation).model_dump(), token=token
    )


async def approve_retake(
    db: AsyncSession, user: User, invitation_id: UUID
) -> AssessmentInvitationOut:
    """Authorize exactly one additional attempt after a submitted attempt.

    Retakes are deliberately granted by a hiring-team actor instead of being
    self-service through the public bearer token. Repeated approval requests
    are idempotent until the candidate consumes the authorization.
    """

    invitation = (
        await db.execute(
            select(AssessmentInvitation)
            .where(
                AssessmentInvitation.id == invitation_id,
                AssessmentInvitation.organization_id == user.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if invitation is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Assessment invitation not found"
        )
    now = _now()
    if invitation.status in {"revoked", "expired"} or invitation.expires_at <= now:
        if invitation.status == "active" and invitation.expires_at <= now:
            invitation.status = "expired"
            await db.commit()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "assessment_invitation_inactive"},
        )

    attempts = list(
        (
            await db.execute(
                select(AssessmentAttempt)
                .where(AssessmentAttempt.invitation_id == invitation.id)
                .order_by(AssessmentAttempt.sequence.desc())
                .with_for_update()
            )
        ).scalars()
    )
    attempts_started = len(attempts)
    if attempts_started == 0 or attempts[0].status != "submitted":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "submitted_attempt_required"},
        )
    if attempts_started >= invitation.max_attempts:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "assessment_attempt_limit_reached"},
        )
    if invitation.authorized_attempts > attempts_started:
        return _invitation_out(invitation)

    previous_authorized = invitation.authorized_attempts
    invitation.authorized_attempts = attempts_started + 1
    db.add(
        AuditLog(
            organization_id=user.organization_id,
            actor_user_id=user.id,
            actor_email_snapshot=user.email,
            action="approve_assessment_retake",
            resource_type="assessment_invitation",
            resource_id=invitation.id,
            path_template="/assessment-invitations/{invitation_id}/retakes",
            method="POST",
            status_code=200,
            request_id=uuid4(),
            metadata_json={
                "application_id": str(invitation.application_id),
                "attempts_started": attempts_started,
                "previous_authorized_attempts": previous_authorized,
                "authorized_attempts": invitation.authorized_attempts,
            },
            occurred_at=now,
        )
    )
    await db.commit()
    await db.refresh(invitation)
    return _invitation_out(invitation)


async def _invitation_by_token(
    db: AsyncSession, token: str, *, lock: bool = False
) -> AssessmentInvitation:
    if not token.startswith("asm_") or len(token) > 256:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Assessment invitation not found"
        )
    digest = _token_digest(token)
    bootstrap = None
    if forced_tenant_rls():
        bootstrap = await resolve_capability(db, kind="assessment", digest=digest)
        if bootstrap is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "Assessment invitation not found"
            )
        await set_tenant_context(
            db,
            organization_id=bootstrap.organization_id,
            actor_id=None,
            identity_kind="public",
        )
    stmt = select(AssessmentInvitation).where(
        AssessmentInvitation.token_digest == digest
    )
    if bootstrap is not None:
        stmt = stmt.where(
            AssessmentInvitation.id == bootstrap.capability_id,
            AssessmentInvitation.organization_id == bootstrap.organization_id,
        )
    if lock:
        stmt = stmt.with_for_update()
    invitation = (await db.execute(stmt)).scalar_one_or_none()
    if invitation is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Assessment invitation not found"
        )
    if invitation.status in {"revoked", "completed"}:
        raise HTTPException(
            status.HTTP_410_GONE, "Assessment invitation is no longer active"
        )
    if invitation.expires_at <= _now() or invitation.status == "expired":
        if invitation.status == "active":
            invitation.status = "expired"
            await db.commit()
        raise HTTPException(status.HTTP_410_GONE, "Assessment invitation has expired")
    return invitation


async def _attempt_out(
    db: AsyncSession, attempt: AssessmentAttempt
) -> AssessmentAttemptOut:
    answers = list(
        (
            await db.execute(
                select(AssessmentAnswer)
                .where(AssessmentAnswer.attempt_id == attempt.id)
                .order_by(AssessmentAnswer.created_at, AssessmentAnswer.id)
            )
        ).scalars()
    )
    return AssessmentAttemptOut(
        id=attempt.id,
        invitation_id=attempt.invitation_id,
        sequence=attempt.sequence,
        status=attempt.status,
        started_at=attempt.started_at,
        last_saved_at=attempt.last_saved_at,
        expires_at=attempt.expires_at,
        submitted_at=attempt.submitted_at,
        answers=[
            {
                "question_id": answer.question_id,
                "value": answer.value,
                "updated_at": answer.updated_at,
            }
            for answer in answers
        ],
    )


async def public_launch(db: AsyncSession, token: str) -> PublicAssessmentLaunch:
    invitation = await _invitation_by_token(db, token)
    version, hierarchy = await _load_version(db, invitation.template_version_id)
    now = _now()
    attempt = (
        await db.execute(
            select(AssessmentAttempt)
            .where(AssessmentAttempt.invitation_id == invitation.id)
            .order_by(AssessmentAttempt.sequence.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if (
        attempt is not None
        and attempt.status == "in_progress"
        and attempt.expires_at <= now
    ):
        attempt.status = "expired"
        await db.commit()
    if invitation.first_opened_at is None:
        invitation.first_opened_at = now
        await db.commit()
    attempts_started = int(
        (
            await db.execute(
                select(func.count(AssessmentAttempt.id)).where(
                    AssessmentAttempt.invitation_id == invitation.id
                )
            )
        ).scalar_one()
    )
    attempts_remaining = max(0, invitation.authorized_attempts - attempts_started)
    has_active_attempt = (
        attempt is not None
        and attempt.status == "in_progress"
        and attempt.expires_at > now
    )
    can_start_attempt = (
        invitation.status == "active"
        and invitation.expires_at > now
        and not has_active_attempt
        and attempts_remaining > 0
    )
    # Submitted attempts and results are reviewer records. The public bearer
    # may start an explicitly provisioned retake, but replaying it must never
    # disclose a prior answer set or outcome.
    public_attempt = (
        attempt
        if attempt is not None and attempt.status == "in_progress"
        else None
    )
    return PublicAssessmentLaunch(
        invitation_id=invitation.id,
        title=version.title,
        instructions=version.instructions,
        expires_at=invitation.expires_at,
        time_limit_minutes=version.time_limit_minutes,
        max_attempts=invitation.max_attempts,
        authorized_attempts=invitation.authorized_attempts,
        attempts_started=attempts_started,
        attempts_remaining=attempts_remaining,
        can_start_attempt=can_start_attempt,
        accommodations=AssessmentAccommodations.model_validate(
            invitation.accommodations or {}
        ),
        sections=[
            PublicAssessmentSection(
                id=section.id,
                title=section.title,
                description=section.description,
                position=section.position,
                questions=[
                    PublicAssessmentQuestion(
                        id=question.id,
                        key=question.key,
                        question_type=question.question_type,
                        prompt=question.prompt,
                        description=question.description,
                        required=question.required,
                        position=question.position,
                        options=question.options,
                        validation=question.validation,
                        condition=question.condition,
                    )
                    for question in questions
                ],
            )
            for section, questions in hierarchy
        ],
        attempt=(
            await _attempt_out(db, public_attempt)
            if public_attempt is not None
            else None
        ),
        result=None,
    )


async def start_or_resume_attempt(db: AsyncSession, token: str) -> AssessmentAttemptOut:
    invitation = await _invitation_by_token(db, token, lock=True)
    active = (
        await db.execute(
            select(AssessmentAttempt)
            .where(
                AssessmentAttempt.invitation_id == invitation.id,
                AssessmentAttempt.status == "in_progress",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    now = _now()
    if active is not None and active.expires_at > now:
        return await _attempt_out(db, active)
    if active is not None:
        active.status = "expired"
    if invitation.status == "completed":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Assessment invitation is completed"
        )
    attempts_started = int(
        (
            await db.execute(
                select(func.count(AssessmentAttempt.id)).where(
                    AssessmentAttempt.invitation_id == invitation.id
                )
            )
        ).scalar_one()
    )
    if attempts_started >= invitation.max_attempts:
        invitation.status = "completed"
        invitation.completed_at = now
        await db.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, "No assessment attempts remain")
    if attempts_started >= invitation.authorized_attempts:
        if active is not None:
            await db.commit()
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"code": "retake_approval_required"},
        )
    version = (
        await db.execute(
            select(AssessmentTemplateVersion).where(
                AssessmentTemplateVersion.id == invitation.template_version_id
            )
        )
    ).scalar_one()
    expires_at = invitation.expires_at
    if version.time_limit_minutes is not None:
        accommodations = AssessmentAccommodations.model_validate(
            invitation.accommodations or {}
        )
        expires_at = min(
            expires_at,
            now
            + timedelta(
                minutes=(
                    version.time_limit_minutes + accommodations.extra_time_minutes
                )
            ),
        )
    attempt = AssessmentAttempt(
        organization_id=invitation.organization_id,
        invitation_id=invitation.id,
        application_id=invitation.application_id,
        template_version_id=invitation.template_version_id,
        sequence=attempts_started + 1,
        expires_at=expires_at,
        started_at=now,
        last_saved_at=now,
    )
    db.add(attempt)
    await db.flush()
    try:
        await record_usage(
            db,
            organization_id=invitation.organization_id,
            metric="assessment_starts",
            amount=1,
            kind="usage",
            idempotency_key=f"assessment:{invitation.id}:{attempt.id}",
            resource_type="assessment_attempt",
            resource_id=attempt.id,
            metadata={"invitation_id": str(invitation.id)},
        )
        await db.commit()
    except EntitlementExceeded as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={
                "code": "entitlement_exceeded",
                "metric": exc.metric,
                "used": exc.used,
                "limit": exc.limit,
                "requested": exc.requested,
            },
        ) from exc
    await db.refresh(attempt)
    return await _attempt_out(db, attempt)


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"{label} must be a number"
        )
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"{label} must be a number"
        ) from None
    if not number.is_finite():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"{label} must be finite"
        )
    return number


def _option_values(question: AssessmentQuestion) -> set[str]:
    return {str(option["value"]) for option in question.options}


def _validate_answer(question: AssessmentQuestion, value: Any) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    except (TypeError, ValueError):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Answer must be JSON"
        ) from None
    if len(encoded) > _MAX_ANSWER_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Answer is too large"
        )
    rules = question.validation or {}
    kind = question.question_type
    if kind == "boolean":
        if not isinstance(value, bool):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "Boolean answer required"
            )
        return value
    if kind == "single":
        if not isinstance(value, str) or value not in _option_values(question):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid option")
        return value
    if kind == "multi":
        if not isinstance(value, list) or any(
            not isinstance(item, str) for item in value
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "Option list required"
            )
        if len(value) != len(set(value)) or not set(value).issubset(
            _option_values(question)
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid options"
            )
        return value
    if kind == "number":
        number = _decimal(value, "Answer")
        if rules.get("min_value") is not None and number < Decimal(
            str(rules["min_value"])
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "Answer is below minimum"
            )
        if rules.get("max_value") is not None and number > Decimal(
            str(rules["max_value"])
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "Answer exceeds maximum"
            )
        return value
    if kind in {"short", "long"}:
        if not isinstance(value, str):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "Text answer required"
            )
        max_default = 10_000 if kind == "short" else 100_000
        minimum = int(rules.get("min_length", 0))
        maximum = int(rules.get("max_length", max_default))
        if len(value) < minimum or len(value) > maximum:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid answer length"
            )
        return value
    if kind == "url":
        minimum = int(rules.get("min_length", 0))
        maximum = int(rules.get("max_length", 2_048))
        if not isinstance(value, str) or len(value) < minimum or len(value) > maximum:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid URL")
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "HTTP(S) URL required"
            )
        return value
    if kind == "file":
        if not isinstance(value, dict):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "File reference required"
            )
        file_id = value.get("file_id")
        filename = value.get("filename")
        size_bytes = value.get("size_bytes")
        if not isinstance(file_id, str) or not file_id or len(file_id) > 512:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid file reference"
            )
        if not isinstance(filename, str) or not filename or len(filename) > 255:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid filename"
            )
        if (
            not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid file size"
            )
        maximum = int(rules.get("max_file_size_bytes", 25 * 1024 * 1024))
        if size_bytes > maximum:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "File exceeds size limit"
            )
        allowed = {
            str(item).lower().lstrip(".")
            for item in rules.get("allowed_file_extensions", [])
        }
        extension = PurePath(filename).suffix.lower().lstrip(".")
        if allowed and extension not in allowed:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "File type is not allowed"
            )
        return {"file_id": file_id, "filename": filename, "size_bytes": size_bytes}
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT, "Unsupported question type"
    )


async def _active_attempt(
    db: AsyncSession, invitation: AssessmentInvitation, attempt_id: UUID, *, lock: bool
) -> AssessmentAttempt:
    stmt = select(AssessmentAttempt).where(
        AssessmentAttempt.id == attempt_id,
        AssessmentAttempt.invitation_id == invitation.id,
    )
    if lock:
        stmt = stmt.with_for_update()
    attempt = (await db.execute(stmt)).scalar_one_or_none()
    if attempt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assessment attempt not found")
    if attempt.status != "in_progress":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Assessment attempt is not editable"
        )
    if attempt.expires_at <= _now():
        attempt.status = "expired"
        await db.commit()
        raise HTTPException(status.HTTP_410_GONE, "Assessment attempt has expired")
    return attempt


def _assessment_file_out(file: AssessmentFile) -> AssessmentFileUploadOut:
    return AssessmentFileUploadOut(
        file_id=file.id,
        question_id=file.question_id,
        filename=file.original_filename,
        media_type=file.media_type,
        size_bytes=file.size_bytes,
        sha256=file.sha256,
        status="ready",
    )


async def _file_question(
    db: AsyncSession, attempt: AssessmentAttempt, question_id: UUID
) -> AssessmentQuestion:
    question = (
        await db.execute(
            select(AssessmentQuestion).where(
                AssessmentQuestion.id == question_id,
                AssessmentQuestion.version_id == attempt.template_version_id,
                AssessmentQuestion.organization_id == attempt.organization_id,
                AssessmentQuestion.question_type == "file",
            )
        )
    ).scalar_one_or_none()
    if question is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File question not found")
    return question


async def save_assessment_file(
    db: AsyncSession,
    token: str,
    attempt_id: UUID,
    question_id: UUID,
    upload: UploadFile,
) -> AssessmentFileUploadOut:
    """Stream, quarantine, validate, and bind one private candidate upload."""

    invitation = await _invitation_by_token(db, token, lock=True)
    organization_id = invitation.organization_id
    attempt = await _active_attempt(db, invitation, attempt_id, lock=True)
    question = await _file_question(db, attempt, question_id)
    rules = question.validation or {}
    configured_suffixes = {
        "." + str(value).casefold().lstrip(".")
        for value in rules.get("allowed_file_extensions", [])
    }
    allowed_suffixes = configured_suffixes or _ASSESSMENT_FILE_SUFFIXES
    if not allowed_suffixes.issubset(_ASSESSMENT_FILE_SUFFIXES):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Assessment file configuration is unsupported",
        )
    maximum = min(
        _MAX_ASSESSMENT_FILE_BYTES,
        int(rules.get("max_file_size_bytes", _MAX_ASSESSMENT_FILE_BYTES)),
    )
    if maximum < 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Assessment file size configuration is unsupported",
        )
    stored = await store_private_upload(
        upload,
        organization_id=organization_id,
        category="assessments",
        allowed_suffixes=allowed_suffixes,
        max_bytes=maximum,
        label="Assessment file",
    )
    file_id = uuid4()
    record = AssessmentFile(
        id=file_id,
        organization_id=organization_id,
        invitation_id=invitation.id,
        attempt_id=attempt.id,
        question_id=question.id,
        original_filename=stored.original_filename,
        media_type=stored.media_type,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
        storage_path=stored.key,
        status="ready",
        scan_result=f"{stored.scan.engine}:{stored.scan.result}"[:64],
        ready_at=_now(),
    )
    private_object_id: UUID | None = None
    try:
        private_object = await register_ready_private_object(
            db,
            organization_id=organization_id,
            resource_type="assessment_file",
            resource_id=file_id,
            stored=stored,
        )
        private_object_id = private_object.id
        record.private_object_id = private_object.id
        db.add(record)
        await db.flush()
        await db.commit()
    except Exception:
        await db.rollback()
        await cleanup_promoted_upload_after_db_failure(
            stored.key,
            organization_id=organization_id,
            category="assessments",
            private_object_id=private_object_id,
        )
        raise
    await db.refresh(record)
    return _assessment_file_out(record)


async def _owned_assessment_file(
    db: AsyncSession,
    *,
    invitation: AssessmentInvitation,
    attempt: AssessmentAttempt,
    question: AssessmentQuestion,
    value: Any,
    lock: bool = False,
) -> AssessmentFile:
    try:
        file_id = UUID(str(value.get("file_id")))
    except (AttributeError, TypeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid file reference"
        ) from exc
    stmt = select(AssessmentFile).where(
        AssessmentFile.id == file_id,
        AssessmentFile.organization_id == invitation.organization_id,
        AssessmentFile.invitation_id == invitation.id,
        AssessmentFile.attempt_id == attempt.id,
        AssessmentFile.question_id == question.id,
        AssessmentFile.status == "ready",
    )
    if lock:
        stmt = stmt.with_for_update()
    record = (await db.execute(stmt)).scalar_one_or_none()
    if record is None:
        # The response is deliberately identical for another tenant, token,
        # attempt, question, erased file, and an invented UUID.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid file reference"
        )
    return record


def _file_answer_value(record: AssessmentFile) -> dict[str, Any]:
    return {
        "file_id": str(record.id),
        "filename": record.original_filename,
        "media_type": record.media_type,
        "size_bytes": record.size_bytes,
        "sha256": record.sha256,
    }


async def _erase_assessment_file(
    db: AsyncSession,
    record: AssessmentFile,
    *,
    reason: str,
    clear_answer: bool = False,
) -> None:
    if clear_answer:
        answer = (
            await db.execute(
                select(AssessmentAnswer).where(
                    AssessmentAnswer.attempt_id == record.attempt_id,
                    AssessmentAnswer.question_id == record.question_id,
                )
            )
        ).scalar_one_or_none()
        answer_value = answer.value if answer is not None else None
        if (
            answer is not None
            and isinstance(answer_value, dict)
            and str(answer_value.get("file_id")) == str(record.id)
        ):
            await db.delete(answer)
    await enqueue_or_delete_private_object(
        db,
        organization_id=record.organization_id,
        category="assessments",
        resource_type="assessment_file",
        resource_id=record.id,
        private_object_id=record.private_object_id,
        legacy_reference=record.storage_path,
        reason=reason,
    )
    record.status = "deleted"
    record.deleted_at = _now()
    record.original_filename = "deleted"
    record.media_type = "application/octet-stream"
    record.scan_result = None


async def delete_assessment_file(
    db: AsyncSession,
    token: str,
    attempt_id: UUID,
    file_id: UUID,
) -> None:
    invitation = await _invitation_by_token(db, token, lock=True)
    attempt = await _active_attempt(db, invitation, attempt_id, lock=True)
    record = (
        await db.execute(
            select(AssessmentFile)
            .where(
                AssessmentFile.id == file_id,
                AssessmentFile.organization_id == invitation.organization_id,
                AssessmentFile.invitation_id == invitation.id,
                AssessmentFile.attempt_id == attempt.id,
                AssessmentFile.status == "ready",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assessment file not found")
    answer = (
        await db.execute(
            select(AssessmentAnswer).where(
                AssessmentAnswer.attempt_id == attempt.id,
                AssessmentAnswer.question_id == record.question_id,
            )
        )
    ).scalar_one_or_none()
    if answer is not None and str((answer.value or {}).get("file_id")) == str(
        record.id
    ):
        await db.delete(answer)
    await _erase_assessment_file(db, record, reason="resource_deleted")
    await db.commit()


async def autosave_answers(
    db: AsyncSession,
    token: str,
    attempt_id: UUID,
    body: AssessmentAutosaveRequest,
) -> AssessmentAttemptOut:
    invitation = await _invitation_by_token(db, token, lock=True)
    attempt = await _active_attempt(db, invitation, attempt_id, lock=True)
    _, hierarchy = await _load_version(db, attempt.template_version_id)
    questions = {
        question.id: question
        for _, section_questions in hierarchy
        for question in section_questions
    }
    now = _now()
    for incoming in body.answers:
        question = questions.get(incoming.question_id)
        if question is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Question is not in this assessment",
            )
        answer = (
            await db.execute(
                select(AssessmentAnswer).where(
                    AssessmentAnswer.attempt_id == attempt.id,
                    AssessmentAnswer.question_id == question.id,
                )
            )
        ).scalar_one_or_none()
        if incoming.value is None:
            if question.question_type == "file":
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    "Use the assessment file delete endpoint to clear a file answer",
                )
            if answer is not None:
                await db.delete(answer)
            continue
        value = _validate_answer(question, incoming.value)
        if question.question_type == "file":
            owned_file = await _owned_assessment_file(
                db,
                invitation=invitation,
                attempt=attempt,
                question=question,
                value=value,
                lock=True,
            )
            value = _file_answer_value(owned_file)
            previous_file_id = (
                str((answer.value or {}).get("file_id"))
                if answer is not None and isinstance(answer.value, dict)
                else None
            )
            if previous_file_id and previous_file_id != str(owned_file.id):
                try:
                    previous_uuid = UUID(previous_file_id)
                except ValueError:
                    previous_uuid = None
                if previous_uuid is not None:
                    previous = (
                        await db.execute(
                            select(AssessmentFile)
                            .where(
                                AssessmentFile.id == previous_uuid,
                                AssessmentFile.organization_id
                                == invitation.organization_id,
                                AssessmentFile.invitation_id == invitation.id,
                                AssessmentFile.attempt_id == attempt.id,
                                AssessmentFile.question_id == question.id,
                                AssessmentFile.status == "ready",
                            )
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if previous is not None:
                        await _erase_assessment_file(db, previous, reason="replacement")
        if answer is None:
            db.add(
                AssessmentAnswer(
                    organization_id=invitation.organization_id,
                    attempt_id=attempt.id,
                    question_id=question.id,
                    value=value,
                    answered_at=now,
                )
            )
        else:
            answer.value = value
            answer.answered_at = now
    attempt.last_saved_at = now
    await db.commit()
    await db.refresh(attempt)
    return await _attempt_out(db, attempt)


def _is_answered(value: Any) -> bool:
    if value is _MISSING or value is None or value in ([], {}):
        return False
    return not (isinstance(value, str) and not value.strip())


def _matches(
    operator: str,
    actual: Any,
    expected: Any,
    *,
    question_type: str | None = None,
) -> bool:
    if operator == "answered":
        return _is_answered(actual)
    if operator == "not_answered":
        return not _is_answered(actual)
    if actual is _MISSING:
        return False
    if operator in {"equals", "not_equals"}:
        if question_type == "number":
            try:
                matched = Decimal(str(actual)) == Decimal(str(expected))
            except (InvalidOperation, ValueError):
                matched = False
        else:
            matched = actual == expected
        return matched if operator == "equals" else not matched
    if operator == "contains":
        return expected in actual if isinstance(actual, (list, str)) else False
    if operator == "not_contains":
        return expected not in actual if isinstance(actual, (list, str)) else False
    if operator == "in":
        return actual in expected if isinstance(expected, list) else False
    if operator in {"gt", "gte", "lt", "lte"}:
        try:
            left, right = Decimal(str(actual)), Decimal(str(expected))
        except (InvalidOperation, ValueError):
            return False
        return {
            "gt": left > right,
            "gte": left >= right,
            "lt": left < right,
            "lte": left <= right,
        }[operator]
    return False


def _condition_matches(
    condition: dict[str, Any],
    *,
    answers_by_key: dict[str, Any],
    questions_by_key: dict[str, AssessmentQuestion],
) -> bool:
    raw_rules = condition.get("rules")
    if isinstance(raw_rules, list):
        if not raw_rules:
            return False
        results: list[bool] = []
        for raw_rule in raw_rules:
            if not isinstance(raw_rule, dict):
                return False
            source_key = raw_rule.get("question_key")
            if not isinstance(source_key, str):
                return False
            source_question = questions_by_key.get(source_key)
            results.append(
                _matches(
                    str(raw_rule.get("operator", "equals")),
                    answers_by_key.get(source_key, _MISSING),
                    raw_rule.get("value"),
                    question_type=(
                        source_question.question_type
                        if source_question is not None
                        else None
                    ),
                )
            )
        return any(results) if condition.get("mode") == "any" else all(results)

    source_key = condition.get("question_key")
    if not isinstance(source_key, str):
        return False
    source_question = questions_by_key.get(source_key)
    return _matches(
        str(condition.get("operator", "equals")),
        answers_by_key.get(source_key, _MISSING),
        condition.get("value"),
        question_type=(
            source_question.question_type if source_question is not None else None
        ),
    )


def _score_question(question: AssessmentQuestion, value: Any) -> tuple[Decimal, bool]:
    maximum = Decimal(question.max_score)
    scoring = question.scoring or {}
    if maximum == 0:
        return Decimal("0"), False
    if not scoring or question.question_type == "file":
        return Decimal("0"), True
    kind = question.question_type
    correct = scoring.get("correct")
    matched = False
    if kind in {"boolean", "single"}:
        matched = value == correct
    elif kind == "multi":
        actual_set, correct_set = set(value), set(correct or [])
        if scoring.get("partial_credit") and correct_set:
            hits = len(actual_set & correct_set)
            wrong = len(actual_set - correct_set)
            ratio = Decimal(max(0, hits - wrong)) / Decimal(len(correct_set))
            return (maximum * ratio).quantize(Decimal("0.0001")), False
        matched = actual_set == correct_set
    elif kind == "number":
        actual = Decimal(str(value))
        if correct is not None:
            tolerance = Decimal(str(scoring.get("tolerance", 0)))
            matched = abs(actual - Decimal(str(correct))) <= tolerance
        elif (
            scoring.get("min_value") is not None or scoring.get("max_value") is not None
        ):
            minimum = Decimal(str(scoring.get("min_value", actual)))
            maximum_value = Decimal(str(scoring.get("max_value", actual)))
            matched = minimum <= actual <= maximum_value
        else:
            return Decimal("0"), True
    elif kind in {"short", "long", "url"}:
        accepted = scoring.get("accepted_values", [])
        if not accepted:
            return Decimal("0"), True
        if scoring.get("case_sensitive"):
            matched = value in accepted
        else:
            matched = str(value).casefold() in {
                str(item).casefold() for item in accepted
            }
    else:
        return Decimal("0"), True
    return (maximum if matched else Decimal("0")), False


async def _result_out(result: AssessmentResult) -> AssessmentResultOut:
    return AssessmentResultOut(
        id=result.id,
        attempt_id=result.attempt_id,
        application_id=result.application_id,
        raw_score=result.raw_score,
        max_score=result.max_score,
        percentage=result.percentage,
        passed=result.passed,
        knocked_out=result.knocked_out,
        knockout_question_id=result.knockout_question_id,
        knockout_reason=result.knockout_reason,
        knockout_review=result.knockout_review,
        needs_manual_review=result.needs_manual_review,
        breakdown=result.breakdown,
        graded_at=result.graded_at,
    )


async def submit_attempt(
    db: AsyncSession, token: str, attempt_id: UUID
) -> AssessmentSubmissionOut:
    invitation = await _invitation_by_token(db, token, lock=True)
    attempt = (
        await db.execute(
            select(AssessmentAttempt)
            .where(
                AssessmentAttempt.id == attempt_id,
                AssessmentAttempt.invitation_id == invitation.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if attempt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assessment attempt not found")
    existing_result = (
        await db.execute(
            select(AssessmentResult).where(AssessmentResult.attempt_id == attempt.id)
        )
    ).scalar_one_or_none()
    if attempt.status == "submitted" and existing_result is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Assessment attempt was already submitted"
        )
    if attempt.status != "in_progress":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Assessment attempt cannot be submitted"
        )
    if attempt.expires_at <= _now():
        attempt.status = "expired"
        await db.commit()
        raise HTTPException(status.HTTP_410_GONE, "Assessment attempt has expired")

    version, hierarchy = await _load_version(db, attempt.template_version_id)
    questions = [
        question for _, section_questions in hierarchy for question in section_questions
    ]
    saved = list(
        (
            await db.execute(
                select(AssessmentAnswer).where(
                    AssessmentAnswer.attempt_id == attempt.id
                )
            )
        ).scalars()
    )
    answers_by_question = {answer.question_id: answer for answer in saved}
    by_question = {answer.question_id: answer.value for answer in saved}
    questions_by_key = {question.key: question for question in questions}
    by_key: dict[str, Any] = {}
    visible: dict[UUID, bool] = {}
    missing_required: list[str] = []
    referenced_file_ids: set[UUID] = set()
    for question in questions:
        condition = question.condition or {}
        is_visible = True
        if condition:
            is_visible = _condition_matches(
                condition,
                answers_by_key=by_key,
                questions_by_key=questions_by_key,
            )
        visible[question.id] = is_visible
        value = by_question.get(question.id, _MISSING)
        if is_visible and question.required and not _is_answered(value):
            missing_required.append(question.key)
        if is_visible and _is_answered(value):
            value = _validate_answer(question, value)
            if question.question_type == "file":
                record = await _owned_assessment_file(
                    db,
                    invitation=invitation,
                    attempt=attempt,
                    question=question,
                    value=value,
                    lock=True,
                )
                referenced_file_ids.add(record.id)
                value = _file_answer_value(record)
                by_question[question.id] = value
                answers_by_question[question.id].value = value
            by_key[question.key] = value
        else:
            by_key[question.key] = _MISSING
    if missing_required:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "Required answers are missing",
                "question_keys": missing_required,
            },
        )

    # Hidden conditional answers and uploads that were never attached (or were
    # superseded) are private intake debris, not reviewer-visible evidence.
    for question in questions:
        if not visible[question.id] and question.id in answers_by_question:
            await db.delete(answers_by_question[question.id])
            by_question.pop(question.id, None)
    attempt_files = list(
        (
            await db.execute(
                select(AssessmentFile)
                .where(
                    AssessmentFile.organization_id == invitation.organization_id,
                    AssessmentFile.invitation_id == invitation.id,
                    AssessmentFile.attempt_id == attempt.id,
                    AssessmentFile.status == "ready",
                )
                .with_for_update()
            )
        ).scalars()
    )
    for record in attempt_files:
        if record.id not in referenced_file_ids:
            await _erase_assessment_file(db, record, reason="resource_deleted")

    total = Decimal("0")
    maximum = Decimal("0")
    needs_review = False
    knocked_out = False
    knockout_question_id: UUID | None = None
    knockout_reason: str | None = None
    breakdown: list[dict[str, Any]] = []
    for question in questions:
        if not visible[question.id]:
            breakdown.append(
                {"question_id": str(question.id), "key": question.key, "visible": False}
            )
            continue
        value = by_question.get(question.id, _MISSING)
        maximum += Decimal(question.max_score)
        score = Decimal("0")
        manual = False
        if _is_answered(value):
            score, manual = _score_question(question, value)
            knockout = question.knockout or {}
            if knockout and _matches(
                knockout.get("operator", "equals"),
                value,
                knockout.get("value"),
                question_type=question.question_type,
            ):
                knocked_out = True
                if knockout_question_id is None:
                    knockout_question_id = question.id
                    knockout_reason = knockout.get("reason") or "Knockout rule matched"
        elif Decimal(question.max_score) > 0 and not question.required:
            # Optional unanswered questions contribute no points and need no review.
            manual = False
        total += score
        needs_review = needs_review or manual
        breakdown.append(
            {
                "question_id": str(question.id),
                "key": question.key,
                "visible": True,
                "score": str(score),
                "max_score": str(question.max_score),
                "needs_manual_review": manual,
            }
        )
    percentage = Decimal("0")
    if maximum > 0:
        percentage = ((total / maximum) * Decimal("100")).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
    # A knockout is a review flag, never an automatic hiring decision. Keep
    # it in the manual-review queue even when every answer is deterministic.
    needs_review = needs_review or knocked_out
    passed = (
        not knocked_out
        and not needs_review
        and percentage >= Decimal(version.passing_percentage)
    )
    now = _now()
    result = AssessmentResult(
        organization_id=attempt.organization_id,
        attempt_id=attempt.id,
        application_id=attempt.application_id,
        raw_score=total,
        max_score=maximum,
        percentage=percentage,
        passed=passed,
        knocked_out=knocked_out,
        knockout_question_id=knockout_question_id,
        knockout_reason=knockout_reason,
        needs_manual_review=needs_review,
        breakdown=breakdown,
        graded_at=now,
    )
    db.add(result)
    attempt.status = "submitted"
    attempt.submitted_at = now
    attempt.last_saved_at = now
    attempts_count = int(
        (
            await db.execute(
                select(func.count(AssessmentAttempt.id)).where(
                    AssessmentAttempt.invitation_id == invitation.id
                )
            )
        ).scalar_one()
    )
    if attempts_count >= invitation.max_attempts:
        invitation.status = "completed"
        invitation.completed_at = now
    await db.commit()
    await db.refresh(attempt)
    await db.refresh(result)
    return AssessmentSubmissionOut(
        attempt=await _attempt_out(db, attempt), result=await _result_out(result)
    )


async def submit_public_attempt(
    db: AsyncSession, token: str, attempt_id: UUID
) -> PublicAssessmentSubmissionOut:
    """Submit an attempt and return only candidate-safe outcome fields."""

    submission = await submit_attempt(db, token, attempt_id)
    submitted_at = submission.attempt.submitted_at
    if submitted_at is None:  # Defensive invariant: submit_attempt sets this atomically.
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Submitted assessment is missing its completion timestamp",
        )
    return PublicAssessmentSubmissionOut(
        attempt=submission.attempt,
        result=PublicAssessmentResultOut(
            attempt_id=submission.attempt.id,
            submitted_at=submitted_at,
            percentage=submission.result.percentage,
            passed=submission.result.passed,
            needs_manual_review=submission.result.needs_manual_review,
        ),
    )


async def get_attempt_result(
    db: AsyncSession, organization_id: UUID, attempt_id: UUID
) -> AssessmentSubmissionOut:
    attempt = (
        await db.execute(
            select(AssessmentAttempt).where(
                AssessmentAttempt.id == attempt_id,
                AssessmentAttempt.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if attempt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assessment attempt not found")
    result = (
        await db.execute(
            select(AssessmentResult).where(
                AssessmentResult.attempt_id == attempt.id,
                AssessmentResult.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assessment result not found")
    return AssessmentSubmissionOut(
        attempt=await _attempt_out(db, attempt), result=await _result_out(result)
    )


def _breakdown_decimal(value: Any, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Stored assessment {label} is invalid",
        ) from exc
    if not number.is_finite() or number < 0:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Stored assessment {label} is invalid",
        )
    return number


async def grade_attempt_manually(
    db: AsyncSession,
    user: User,
    attempt_id: UUID,
    body: AssessmentManualGradeRequest,
) -> AssessmentSubmissionOut:
    """Resolve manual questions and/or adjudicate a knockout review flag.

    ``graded_at`` is the result's optimistic version. The result row is locked
    before comparison so concurrent reviewers cannot silently overwrite one
    another. The original knockout match is deliberately immutable; a human
    decision is stored alongside it with reviewer, reason, outcome, and time.
    """

    row = (
        await db.execute(
            select(
                AssessmentResult,
                AssessmentAttempt,
                AssessmentTemplateVersion,
                Application,
            )
            .join(
                AssessmentAttempt,
                AssessmentAttempt.id == AssessmentResult.attempt_id,
            )
            .join(
                AssessmentTemplateVersion,
                AssessmentTemplateVersion.id == AssessmentAttempt.template_version_id,
            )
            .join(Application, Application.id == AssessmentAttempt.application_id)
            .where(
                AssessmentResult.attempt_id == attempt_id,
                AssessmentResult.organization_id == user.organization_id,
                AssessmentAttempt.organization_id == user.organization_id,
                AssessmentTemplateVersion.organization_id == user.organization_id,
                Application.organization_id == user.organization_id,
                AssessmentResult.application_id == AssessmentAttempt.application_id,
            )
            .with_for_update(of=AssessmentResult)
        )
    ).one_or_none()
    if row is None:
        # Assignment loader criteria make unassigned and cross-tenant attempts
        # indistinguishable from an invented identifier.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assessment result not found")
    result, attempt, version, application = row
    if attempt.status != "submitted":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only submitted assessments can be graded"
        )
    expected = body.expected_graded_at.astimezone(UTC)
    current = result.graded_at.astimezone(UTC)
    if current != expected:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "stale_assessment_result",
                "message": "The assessment result changed; reload before grading.",
                "current_graded_at": result.graded_at.isoformat(),
            },
        )

    breakdown = [
        dict(item) for item in (result.breakdown or []) if isinstance(item, dict)
    ]
    manual_entries: dict[UUID, dict[str, Any]] = {}
    for item in breakdown:
        if not item.get("needs_manual_review") and not isinstance(
            item.get("manual_grade"), dict
        ):
            continue
        try:
            question_id = UUID(str(item.get("question_id")))
        except (TypeError, ValueError):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Stored manual-review question is invalid",
            ) from None
        manual_entries[question_id] = item
    grades = {grade.question_id: grade for grade in body.grades}
    if grades and not manual_entries:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This assessment has no manual questions to resolve",
        )
    if grades and set(grades) != set(manual_entries):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "Manual grades must cover every manual question",
                "question_ids": [
                    str(question_id) for question_id in sorted(manual_entries, key=str)
                ],
            },
        )

    prior_manual: list[dict[str, Any]] = []
    new_manual: list[dict[str, Any]] = []
    now = _now()
    for question_id, item in manual_entries.items():
        prior_grade = item.get("manual_grade")
        prior_manual.append(
            {
                "question_id": str(question_id),
                "score": (
                    str(prior_grade.get("score"))
                    if isinstance(prior_grade, dict)
                    else None
                ),
                "evidence_comment": (
                    prior_grade.get("evidence_comment")
                    if isinstance(prior_grade, dict)
                    else None
                ),
            }
        )
    for question_id, grade in grades.items():
        item = manual_entries[question_id]
        maximum = _breakdown_decimal(item.get("max_score", 0), "maximum score")
        score = Decimal(grade.score)
        if not score.is_finite() or score > maximum:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "message": "Manual score must be within the question maximum",
                    "question_id": str(question_id),
                    "max_score": str(maximum),
                },
            )
        normalized_score = score.quantize(Decimal("0.0001"))
        item["score"] = str(normalized_score)
        item["needs_manual_review"] = False
        item["manual_grade"] = {
            "score": str(normalized_score),
            "evidence_comment": grade.evidence_comment,
            "reviewed_by_user_id": str(user.id),
            "reviewed_at": now.isoformat(),
        }
        new_manual.append(
            {
                "question_id": str(question_id),
                "score": str(normalized_score),
                "evidence_comment": grade.evidence_comment,
            }
        )

    prior_knockout_review = (
        dict(result.knockout_review)
        if isinstance(result.knockout_review, dict)
        else None
    )
    knockout_review = prior_knockout_review
    if body.knockout_review is not None:
        if not result.knocked_out:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This assessment has no knockout match to adjudicate",
            )
        knockout_review = {
            "outcome": body.knockout_review.outcome,
            "reason": body.knockout_review.reason,
            "reviewed_by_user_id": str(user.id),
            "reviewed_at": now.isoformat(),
        }

    raw_score = Decimal("0")
    max_score = Decimal("0")
    for item in breakdown:
        if item.get("visible") is False:
            continue
        raw_score += _breakdown_decimal(item.get("score", 0), "question score")
        max_score += _breakdown_decimal(item.get("max_score", 0), "maximum score")
    raw_score = raw_score.quantize(Decimal("0.0001"))
    max_score = max_score.quantize(Decimal("0.0001"))
    if max_score <= 0:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Assessment result has no scorable questions",
        )
    percentage = ((raw_score / max_score) * Decimal("100")).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )
    unresolved_manual = any(
        item.get("needs_manual_review") is True for item in breakdown
    )
    knockout_unresolved = result.knocked_out and knockout_review is None
    knockout_blocks_pass = result.knocked_out and (
        knockout_review is None
        or knockout_review.get("outcome") == "confirmed"
    )
    needs_manual_review = unresolved_manual or knockout_unresolved
    passed = (
        not needs_manual_review
        and not knockout_blocks_pass
        and percentage >= Decimal(version.passing_percentage)
    )
    prior_result = {
        "raw_score": str(result.raw_score),
        "max_score": str(result.max_score),
        "percentage": str(result.percentage),
        "passed": result.passed,
        "needs_manual_review": result.needs_manual_review,
        "knockout_review": prior_knockout_review,
        "graded_at": result.graded_at.isoformat(),
        "manual_grades": prior_manual,
    }
    result.breakdown = breakdown
    result.raw_score = raw_score
    result.max_score = max_score
    result.percentage = percentage
    result.passed = passed
    result.knockout_review = knockout_review
    result.needs_manual_review = needs_manual_review
    result.graded_at = now
    db.add(
        AuditLog(
            organization_id=user.organization_id,
            actor_user_id=user.id,
            actor_email_snapshot=user.email,
            action="grade_assessment_manually",
            resource_type="assessment_result",
            resource_id=result.id,
            path_template="/assessment-attempts/{attempt_id}/manual-grade",
            method="POST",
            status_code=200,
            request_id=uuid4(),
            metadata_json={
                "attempt_id": str(attempt.id),
                "application_id": str(application.id),
                "prior": prior_result,
                "new": {
                    "raw_score": str(raw_score),
                    "max_score": str(max_score),
                    "percentage": str(percentage),
                    "passed": passed,
                    "needs_manual_review": needs_manual_review,
                    "knockout_review": knockout_review,
                    "graded_at": now.isoformat(),
                    "manual_grades": new_manual,
                },
            },
            occurred_at=now,
        )
    )
    await db.commit()
    await db.refresh(result)
    return AssessmentSubmissionOut(
        attempt=await _attempt_out(db, attempt), result=await _result_out(result)
    )


async def list_invitation_attempts(
    db: AsyncSession,
    organization_id: UUID,
    invitation_id: UUID,
    *,
    limit: int = 100,
    cursor: str | None = None,
    include_files: bool = True,
) -> CursorPage[AssessmentAttemptSummaryOut]:
    invitation = (
        await db.execute(
            select(AssessmentInvitation).where(
                AssessmentInvitation.id == invitation_id,
                AssessmentInvitation.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if invitation is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Assessment invitation not found"
        )
    namespace = "assessments.invitation-attempts"
    scope = collection_cursor_scope(
        namespace,
        organization_id=organization_id,
        filters={"invitation_id": str(invitation_id)},
    )
    stmt = select(AssessmentAttempt).where(
        AssessmentAttempt.invitation_id == invitation.id,
        AssessmentAttempt.organization_id == organization_id,
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"sequence", "id"},
            detail="Invalid assessment attempts cursor",
        )
        try:
            cursor_sequence = int(payload["sequence"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"code": "cursor_invalid", "detail": "Invalid assessment attempts cursor"},
            ) from exc
        cursor_id = cursor_uuid(payload, detail="Invalid assessment attempts cursor")
        stmt = stmt.where(
            or_(
                AssessmentAttempt.sequence > cursor_sequence,
                and_(AssessmentAttempt.sequence == cursor_sequence, AssessmentAttempt.id > cursor_id),
            )
        )
    attempts = list(
        (
            await db.execute(
                stmt.order_by(AssessmentAttempt.sequence, AssessmentAttempt.id).limit(
                    limit + 1
                )
            )
        ).scalars()
    )
    has_more = len(attempts) > limit
    attempts = attempts[:limit]
    if not attempts:
        return CursorPage(items=[], next_cursor=None)
    attempt_ids = [attempt.id for attempt in attempts]
    results = {
        result.attempt_id: result
        for result in (
            await db.execute(
                select(AssessmentResult).where(
                    AssessmentResult.organization_id == organization_id,
                    AssessmentResult.attempt_id.in_(attempt_ids),
                )
            )
        ).scalars()
    }
    files_by_attempt: dict[UUID, list[AssessmentFile]] = {
        attempt_id: [] for attempt_id in attempt_ids
    }
    if include_files:
        for file in (
            await db.execute(
                select(AssessmentFile)
                .where(
                    AssessmentFile.organization_id == organization_id,
                    AssessmentFile.attempt_id.in_(attempt_ids),
                    AssessmentFile.status == "ready",
                )
                .order_by(AssessmentFile.created_at, AssessmentFile.id)
            )
        ).scalars():
            files_by_attempt.setdefault(file.attempt_id, []).append(file)
    output: list[AssessmentAttemptSummaryOut] = []
    for attempt in attempts:
        result = results.get(attempt.id)
        output.append(
            AssessmentAttemptSummaryOut(
                id=attempt.id,
                invitation_id=attempt.invitation_id,
                sequence=attempt.sequence,
                status=attempt.status,
                started_at=attempt.started_at,
                last_saved_at=attempt.last_saved_at,
                expires_at=attempt.expires_at,
                submitted_at=attempt.submitted_at,
                result=(
                    AssessmentReviewerResultSummary(
                        id=result.id,
                        raw_score=result.raw_score,
                        max_score=result.max_score,
                        percentage=result.percentage,
                        passed=result.passed,
                        knocked_out=result.knocked_out,
                        knockout_question_id=result.knockout_question_id,
                        knockout_reason=result.knockout_reason,
                        knockout_review=result.knockout_review,
                        needs_manual_review=result.needs_manual_review,
                        graded_at=result.graded_at,
                    )
                    if result is not None
                    else None
                ),
                files=[
                    AssessmentReviewerFileOut(
                        **_assessment_file_out(file).model_dump(),
                        download_url=f"/assessment-files/{file.id}/download",
                    )
                    for file in files_by_attempt.get(attempt.id, [])
                ],
            )
        )
    return CursorPage(
        items=output,
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={"sequence": attempts[-1].sequence, "id": str(attempts[-1].id)},
            )
            if has_more
            else None
        ),
    )


async def reviewer_assessment_file(
    db: AsyncSession,
    user: User,
    file_id: UUID,
) -> tuple[AssessmentFile, str]:
    row = (
        await db.execute(
            select(AssessmentFile, AssessmentAttempt)
            .join(AssessmentAttempt, AssessmentAttempt.id == AssessmentFile.attempt_id)
            .where(
                AssessmentFile.id == file_id,
                AssessmentFile.organization_id == user.organization_id,
                AssessmentFile.status == "ready",
                AssessmentAttempt.organization_id == user.organization_id,
                AssessmentAttempt.status == "submitted",
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assessment file not found")
    file, attempt = row
    await require_field_access(db, user, FieldClass.ASSESSMENT_RESULT)
    await require_field_access(db, user, FieldClass.CANDIDATE_DOCUMENT)
    key = await resolve_resource_private_blob_reference(
        db,
        organization_id=user.organization_id,
        category="assessments",
        resource_type="assessment_file",
        resource_id=file.id,
        private_object_id=file.private_object_id,
        legacy_reference=file.storage_path,
    )
    if key is None:
        raise HTTPException(status.HTTP_410_GONE, "Assessment file is unavailable")
    try:
        metadata = await get_blob_store().stat(key)
    except BlobStoreError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Private file storage is temporarily unavailable.",
        ) from exc
    if metadata is None:
        raise HTTPException(status.HTTP_410_GONE, "Assessment file is unavailable")
    db.add(
        AuditLog(
            organization_id=user.organization_id,
            actor_user_id=user.id,
            actor_email_snapshot=user.email,
            action="download_assessment_file",
            resource_type="assessment_file",
            resource_id=file.id,
            path_template="/assessment-files/{file_id}/download",
            method="GET",
            status_code=200,
            request_id=uuid4(),
            metadata_json={
                "attempt_id": str(attempt.id),
                "question_id": str(file.question_id),
                "sensitive_read": True,
            },
            occurred_at=_now(),
        )
    )
    await db.commit()
    return file, key
