"""Shared application intake for every unauthenticated careers surface."""

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import FieldClass
from app.config import get_settings
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.notification import Notification
from app.models.position import Position
from app.models.score import Score
from app.models.sourcing import ReferralLink
from app.models.user import User
from app.services.activities import append_candidate_activity
from app.services.applications import ensure_application_link
from app.services.candidate_identity import (
    attach_candidate_identity,
    normalize_email,
    normalize_phone,
    resolve_candidate_identity,
)
from app.services.candidate_processing import (
    enqueue_candidate_parse_job,
    enqueue_candidate_scoring_job,
)
from app.services.entitlements import TECHNICAL_LIMITS, record_usage
from app.services.enterprise import can_access_field, has_permission
from app.services.source_provenance import (
    configured_source_slug,
    public_source_slug,
)
from app.services.uploads import (
    CandidateFileUpload,
    candidate_from_text,
    candidate_from_upload,
    cleanup_promoted_upload_after_db_failure,
    enqueue_or_delete_private_object,
)
from app.storage.registry import (
    enqueue_private_object_deletion,
    register_ready_private_object,
)

PUBLIC_CV_MAX_BYTES = TECHNICAL_LIMITS["candidate_document_bytes"]

def _identity_review_reference(
    organization_id: UUID,
    *,
    email: str | None,
    phone: str | None,
) -> str:
    """Create a stable opaque key for an unresolved contact combination."""

    settings = get_settings()
    configured = settings.public_rate_limit_hmac_secret
    secret = (
        configured.get_secret_value().encode()
        if configured is not None
        else hmac.new(
            settings.jwt_secret.encode(),
            b"public-identity-review-development-key",
            hashlib.sha256,
        ).digest()
    )
    material = f"{organization_id}\0{email or ''}\0{phone or ''}".encode()
    digest = hmac.new(secret, material, hashlib.sha256).hexdigest()
    return f"identity-review:{digest}"


def _public_idempotency_digest(
    organization_id: UUID,
    position_id: UUID,
    value: str,
) -> str:
    """Return an opaque, endpoint-scoped digest for a public request key."""

    cleaned = value.strip()
    if not cleaned or len(cleaned) > 160:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "idempotency_key_invalid", "detail": "Invalid Idempotency-Key"},
        )
    settings = get_settings()
    configured = settings.public_rate_limit_hmac_secret
    secret = (
        configured.get_secret_value().encode()
        if configured is not None
        else hmac.new(
            settings.jwt_secret.encode(),
            b"public-application-idempotency-development-key",
            hashlib.sha256,
        ).digest()
    )
    material = f"{organization_id}\0{position_id}\0{cleaned}".encode()
    return hmac.new(secret, material, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class PublicApplicationResult:
    candidate_id: UUID
    application_id: UUID
    position_id: UUID
    organization_id: UUID
    candidate_created: bool
    application_created: bool


async def submit_public_application(
    *,
    position: Position,
    name: str,
    phone: str,
    email: str | None,
    source: str | None,
    file: UploadFile | None,
    raw_text: str | None,
    referral_link: ReferralLink | None = None,
    idempotency_key: str | None = None,
    privacy_notice_version: str | None = None,
    privacy_acknowledged: bool = False,
    occurred_at: datetime | None = None,
    db: AsyncSession,
) -> PublicApplicationResult:
    """Idempotently accept one person into one position.

    Contact identity is tenant-scoped. A known person may apply to another
    position or start a new cycle after a genuinely closed prior application.
    A retry while a cycle is active returns the same generic accepted result
    without duplicating activity, notifications, or scoring. Reusing an
    Idempotency-Key always replays the exact original accepted result.
    """

    phone_norm = normalize_phone(phone)
    email_norm = normalize_email(email)
    organization_id = position.organization_id
    idempotency_digest = (
        _public_idempotency_digest(organization_id, position.id, idempotency_key)
        if idempotency_key is not None
        else None
    )
    if idempotency_digest is not None:
        # Serialize the full intake before parsing or candidate resolution so
        # concurrent multipart retries cannot duplicate expensive work.
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {
                "key": (
                    f"public-application:{organization_id}:{position.id}:"
                    f"{idempotency_digest}"
                )
            },
        )
        existing_application = (
            await db.execute(
                select(Application)
                .where(
                    Application.organization_id == organization_id,
                    Application.position_id == position.id,
                    Application.application_metadata.contains(
                        {"public_idempotency_keys": [idempotency_digest]}
                    ),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing_application is not None:
            return PublicApplicationResult(
                candidate_id=existing_application.candidate_id,
                application_id=existing_application.id,
                position_id=position.id,
                organization_id=organization_id,
                candidate_created=False,
                application_created=False,
            )
    # A referral-link source is authenticated tenant configuration and retains
    # its validated slug end-to-end. Direct public input is restricted to a
    # known alias or an opaque hash label.
    channel = (
        configured_source_slug(referral_link.source)
        if referral_link is not None
        else public_source_slug(source)
    )
    clean_name = name.strip()
    clean_phone = phone.strip()
    clean_email = email_norm
    # ``source_ref`` is broadly useful as a provenance label and therefore must
    # never double as a contact store.  Keep public contact identity solely in
    # the protected contact-point/parsed-CV stores and use an opaque intake
    # reference here so future rows cannot leak a name, phone, or email through
    # legacy candidate projections.
    source_ref = f"{channel}:public-application:{uuid4().hex}"
    identity = await resolve_candidate_identity(
        db,
        position.organization_id,
        email=email_norm,
        phone=phone_norm,
    )
    identity_review_required = identity.conflict
    identity_review_reference: str | None = None
    candidate = identity.candidate
    if identity_review_required:
        # Never guess which existing person owns conflicting contact signals.
        # Preserve the intake as an opaque, review-required candidate instead,
        # and reuse it for exact retries or applications to another position.
        identity_review_reference = _identity_review_reference(
            organization_id,
            email=email_norm,
            phone=phone_norm,
        )
        candidate = (
            await db.execute(
                select(Candidate)
                .where(
                    Candidate.organization_id == organization_id,
                    Candidate.source == "identity_review",
                    Candidate.source_ref == identity_review_reference,
                    Candidate.merged_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
    candidate_created = candidate is None
    if referral_link is not None:
        referral_link = (
            await db.execute(
                select(ReferralLink)
                .where(
                    ReferralLink.id == referral_link.id,
                    ReferralLink.organization_id == position.organization_id,
                    ReferralLink.position_id == position.id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if referral_link is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Referral link not found")
        if (
            referral_link.max_applications is not None
            and referral_link.application_count >= referral_link.max_applications
        ):
            existing_pair = False
            if candidate is not None:
                existing_pair = (
                    await db.execute(
                        select(Application.id).where(
                            Application.organization_id == position.organization_id,
                            Application.candidate_id == candidate.id,
                            Application.position_id == position.id,
                        )
                    )
                ).scalar_one_or_none() is not None
                if not existing_pair:
                    existing_pair = (
                        await db.execute(
                            select(Score.id).where(
                                Score.organization_id == position.organization_id,
                                Score.candidate_id == candidate.id,
                                Score.position_id == position.id,
                            )
                        )
                    ).scalar_one_or_none() is not None
            if not existing_pair:
                # This expected limit response occurs before any candidate,
                # upload, or application mutation, so callers need not roll
                # back and expire unrelated ORM state.
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND, "Referral link not found"
                )
    private_upload_path: str | None = None
    replaced_upload_path: str | None = None
    replaced_private_object_id: UUID | None = None
    candidate_upload: CandidateFileUpload | None = None
    new_private_object_id: UUID | None = None
    try:
        # A retry does not need to re-parse or replace the person's existing CV.
        # For a new person (or a contact-only legacy record), validate and keep
        # the supplied resume before creating the application.
        if candidate is None or not (
            (candidate.raw_cv or "").strip() or candidate.pdf_path
        ):
            if file is not None and file.filename:
                candidate_upload = await candidate_from_upload(
                    file,
                    organization_id=organization_id,
                    source_ref=source_ref,
                    max_bytes=PUBLIC_CV_MAX_BYTES,
                )
                incoming = candidate_upload.candidate
            elif raw_text and raw_text.strip():
                incoming = candidate_from_text(raw_text, source_ref=source_ref)
            else:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Attach your CV as a file (PDF / DOCX) or paste it as text.",
                )
            private_upload_path = incoming.pdf_path
            if candidate is None:
                candidate = incoming
                candidate.id = candidate.id or uuid4()
                candidate.source = (
                    "identity_review" if identity_review_required else "public"
                )
                if identity_review_reference is not None:
                    candidate.source_ref = identity_review_reference
                candidate.organization_id = position.organization_id
                await record_usage(
                    db,
                    organization_id=organization_id,
                    metric="candidates",
                    amount=1,
                    kind="usage",
                    idempotency_key=f"candidate:public:{candidate.id}",
                    resource_type="candidate",
                    resource_id=candidate.id,
                )
                db.add(candidate)
                await db.flush()
            else:
                replaced_upload_path = candidate.pdf_path
                replaced_private_object_id = candidate.private_object_id
                candidate.raw_cv = incoming.raw_cv
                candidate.pdf_path = incoming.pdf_path

            if candidate_upload is not None:
                private_object = await register_ready_private_object(
                    db,
                    organization_id=organization_id,
                    resource_type="candidate",
                    resource_id=candidate.id,
                    stored=candidate_upload.stored,
                )
                new_private_object_id = private_object.id
                candidate.private_object_id = private_object.id
                if replaced_private_object_id is not None:
                    await enqueue_private_object_deletion(
                        db,
                        organization_id=organization_id,
                        private_object_id=replaced_private_object_id,
                        reason="replacement",
                    )
            elif replaced_private_object_id is not None:
                # A pasted-text replacement also detaches and durably erases
                # the former binary object; never leave an authoritative
                # private_object_id pointing at a file the resource no longer
                # advertises.
                await enqueue_private_object_deletion(
                    db,
                    organization_id=organization_id,
                    private_object_id=replaced_private_object_id,
                    reason="replacement",
                )
                candidate.private_object_id = None
            if (
                replaced_private_object_id is None
                and replaced_upload_path
                and replaced_upload_path != private_upload_path
            ):
                # Pre-registry local paths use the same database transaction as
                # the replacement, so a committed candidate update can never
                # be followed by an unrecorded best-effort filesystem delete.
                await enqueue_or_delete_private_object(
                    db,
                    organization_id=organization_id,
                    category="candidates",
                    resource_type="candidate",
                    resource_id=candidate.id,
                    private_object_id=None,
                    legacy_reference=replaced_upload_path,
                    reason="replacement",
                )

        assert candidate is not None
        if not identity_review_required:
            await attach_candidate_identity(
                db,
                candidate,
                email=email_norm,
                phone=phone_norm,
                source=channel,
            )
        if candidate_created:
            candidate.parsed_cv = {
                "applicant_name": clean_name,
                "applicant_phone": clean_phone,
                "applicant_email": clean_email,
                "applied_via": channel,
            }
            if identity_review_required:
                candidate.parsed_cv["identity_review_required"] = True
            if referral_link is not None:
                candidate.parsed_cv.update(
                    {
                        "utm_campaign": referral_link.campaign,
                        "referral_link_id": str(referral_link.id),
                    }
                )

        application_metadata = {
            "campaign": referral_link.campaign if referral_link else None,
            "referral_link_id": str(referral_link.id) if referral_link else None,
            "identity_review_required": identity_review_required,
            "privacy_notice_version": privacy_notice_version,
            "privacy_acknowledged": privacy_acknowledged,
            "human_review_available": True,
        }
        if idempotency_digest is not None:
            application_metadata["public_idempotency_keys"] = [idempotency_digest]
        link = await ensure_application_link(
            db,
            organization_id=position.organization_id,
            candidate_id=candidate.id,
            position_id=position.id,
            source=channel,
            owner_user_id=position.created_by,
            actor_user_id=None,
            actor_kind="public",
            reason="public_application_received",
            metadata=application_metadata,
            occurred_at=occurred_at,
            allow_reapplication=True,
        )
        if idempotency_digest is not None:
            persisted_metadata = dict(link.application.application_metadata or {})
            persisted_keys = list(persisted_metadata.get("public_idempotency_keys") or [])
            if idempotency_digest not in persisted_keys:
                persisted_keys.append(idempotency_digest)
                persisted_metadata["public_idempotency_keys"] = persisted_keys[-20:]
                link.application.application_metadata = persisted_metadata
        if (
            link.new_pair
            and referral_link is not None
            and referral_link.max_applications is not None
            and referral_link.application_count >= referral_link.max_applications
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Referral link not found")

        if link.new_pair:
            append_candidate_activity(
                db,
                organization_id=position.organization_id,
                candidate_id=candidate.id,
                application_id=link.application.id,
                position_id=position.id,
                event_type="candidate_added"
                if candidate_created
                else "candidate_sourced",
                subject_type="candidate" if candidate_created else "application",
                subject_id=candidate.id if candidate_created else link.application.id,
                payload={
                    "source": "public",
                    "applied_via": channel,
                    "position_title": position.title,
                    **(
                        {"identity_review_required": True}
                        if identity_review_required
                        else {}
                    ),
                },
            )
            users = (
                (
                    await db.execute(
                        select(User).where(
                            User.organization_id == position.organization_id,
                            User.is_active.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for user in users:
                if not await has_permission(db, user, "candidates.read"):
                    continue
                if not await can_access_field(db, user, FieldClass.CONTACT):
                    continue
                db.add(
                    Notification(
                        organization_id=position.organization_id,
                        user_id=user.id,
                        type="candidate_applied",
                        payload={
                            "candidate_id": str(candidate.id),
                            "position_id": str(position.id),
                            "position_title": position.title,
                            "applied_via": channel,
                            "identity_review_required": identity_review_required,
                        },
                    )
                )
            if referral_link is not None:
                referral_link.application_count += 1
        if candidate_upload is not None and new_private_object_id is not None:
            await enqueue_candidate_parse_job(
                db,
                organization_id=position.organization_id,
                candidate_id=candidate.id,
                idempotency_key=(f"public-upload:{new_private_object_id}"),
                actor_user_id=None,
            )
        if link.new_pair:
            await enqueue_candidate_scoring_job(
                db,
                organization_id=position.organization_id,
                candidate_id=candidate.id,
                position_id=position.id,
                application_id=link.application.id,
                idempotency_key=f"public-application:{link.application.id}",
                actor_user_id=None,
            )
        await db.commit()
    except Exception:
        await db.rollback()
        await cleanup_promoted_upload_after_db_failure(
            private_upload_path,
            organization_id=organization_id,
            category="candidates",
            private_object_id=new_private_object_id,
        )
        raise

    return PublicApplicationResult(
        candidate_id=candidate.id,
        application_id=link.application.id,
        position_id=position.id,
        organization_id=position.organization_id,
        candidate_created=candidate_created,
        application_created=link.new_pair,
    )
