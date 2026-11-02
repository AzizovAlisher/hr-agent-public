"""First-class applications and native assessment HTTP APIs."""

from datetime import datetime
from pathlib import PurePath
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import FieldClass
from app.auth.dependencies import current_user
from app.db import get_session
from app.models.user import User
from app.schemas.applications import (
    ApplicationAnalyticsOut,
    ApplicationAnalyticsReconciliation,
    ApplicationCreate,
    ApplicationOut,
    ApplicationPage,
    ApplicationStage,
    ApplicationStageTransition,
    ApplicationUpdate,
)
from app.schemas.assessments import (
    AssessmentAttemptOut,
    AssessmentAttemptSummaryOut,
    AssessmentAutosaveRequest,
    AssessmentFileUploadOut,
    AssessmentInvitationCreate,
    AssessmentInvitationCreated,
    AssessmentInvitationOut,
    AssessmentInvitationReissueRequest,
    AssessmentManualGradeRequest,
    AssessmentSubmissionOut,
    AssessmentTemplateCreate,
    AssessmentTemplateOut,
    AssessmentTemplateUpdate,
    AssessmentVersionCreate,
    AssessmentVersionOut,
    PublicAssessmentLaunch,
    PublicAssessmentSubmissionOut,
)
from app.schemas.pagination import CursorPage
from app.services import applications as application_service
from app.services import assessments as assessment_service
from app.services.application_analytics import application_analytics
from app.services.enterprise import (
    can_access_field,
    require_field_access,
    require_permission,
)
from app.services.uploads import (
    private_blob_response,
    safe_download_filename,
    validated_private_media_type,
)

router = APIRouter(tags=["applications"])
public_router = APIRouter(prefix="/public/assessments", tags=["public-assessments"])


def _protect_token_response(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"


@router.get("/analytics/applications", response_model=ApplicationAnalyticsOut)
async def get_application_analytics(
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    timezone: str = Query(default="UTC", min_length=1, max_length=64),
    position_id: UUID | None = None,
    source: str | None = Query(default=None, min_length=1, max_length=32),
    owner_user_id: UUID | None = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ApplicationAnalyticsOut:
    await require_permission(db, user, "candidates.read")
    quality_score_authorized = await can_access_field(
        db, user, FieldClass.ASSESSMENT_RESULT
    )
    ai_decisions_authorized = await can_access_field(
        db, user, FieldClass.AI_CONTEXT
    )
    return await application_analytics(
        db,
        user.organization_id,
        start_at=start_at,
        end_at=end_at,
        timezone_name=timezone,
        position_id=position_id,
        source=source,
        owner_user_id=owner_user_id,
        include_quality_scores=quality_score_authorized,
        include_ai_decisions=ai_decisions_authorized,
    )


@router.get(
    "/analytics/applications/reconciliation",
    response_model=ApplicationAnalyticsReconciliation,
)
async def reconcile_application_analytics(
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    timezone: str = Query(default="UTC", min_length=1, max_length=64),
    position_id: UUID | None = None,
    source: str | None = Query(default=None, min_length=1, max_length=32),
    owner_user_id: UUID | None = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ApplicationAnalyticsReconciliation:
    await require_permission(db, user, "candidates.read")
    result = await application_analytics(
        db,
        user.organization_id,
        start_at=start_at,
        end_at=end_at,
        timezone_name=timezone,
        position_id=position_id,
        source=source,
        owner_user_id=owner_user_id,
    )
    return result.reconciliation


@router.get("/applications", response_model=ApplicationPage)
async def list_applications(
    candidate_id: UUID | None = None,
    position_id: UUID | None = None,
    stage_filter: ApplicationStage | None = Query(default=None, alias="stage"),
    include_closed: bool = False,
    include_archived: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ApplicationPage:
    await require_permission(db, user, "candidates.read")
    return await application_service.list_applications(
        db,
        user.organization_id,
        candidate_id=candidate_id,
        position_id=position_id,
        stage=stage_filter,
        include_closed=include_closed,
        include_archived=include_archived,
        limit=limit,
        cursor=cursor,
    )


@router.post(
    "/applications",
    response_model=ApplicationOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_application(
    body: ApplicationCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ApplicationOut:
    await require_permission(db, user, "candidates.write")
    return await application_service.create_application(db, user, body)


@router.get("/applications/{application_id}", response_model=ApplicationOut)
async def get_application(
    application_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ApplicationOut:
    await require_permission(db, user, "candidates.read")
    application = await application_service.get_application(
        db, user.organization_id, application_id
    )
    return await application_service.application_out(
        db, application, include_events=True
    )


@router.patch("/applications/{application_id}", response_model=ApplicationOut)
async def update_application(
    application_id: UUID,
    body: ApplicationUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ApplicationOut:
    await require_permission(db, user, "candidates.write")
    return await application_service.update_application(db, user, application_id, body)


@router.delete("/applications/{application_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_application(
    application_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> None:
    await require_permission(db, user, "candidates.write")
    await application_service.delete_application(db, user, application_id)


@router.post("/applications/{application_id}/stage", response_model=ApplicationOut)
async def transition_application_stage(
    application_id: UUID,
    body: ApplicationStageTransition,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ApplicationOut:
    return await application_service.transition_application_stage(
        db, user, application_id, body
    )


@router.get(
    "/assessment-templates",
    response_model=CursorPage[AssessmentTemplateOut],
)
async def list_assessment_templates(
    include_archived: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[AssessmentTemplateOut]:
    await require_permission(db, user, "assessments.read")
    return await assessment_service.list_templates(
        db,
        user.organization_id,
        include_archived=include_archived,
        limit=limit,
        cursor=cursor,
    )


@router.post(
    "/assessment-templates",
    response_model=AssessmentTemplateOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_assessment_template(
    body: AssessmentTemplateCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> AssessmentTemplateOut:
    await require_permission(db, user, "assessments.manage")
    return await assessment_service.create_template(db, user, body)


@router.get("/assessment-templates/{template_id}", response_model=AssessmentTemplateOut)
async def get_assessment_template(
    template_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> AssessmentTemplateOut:
    await require_permission(db, user, "assessments.read")
    template = await assessment_service.get_template(
        db, user.organization_id, template_id
    )
    return await assessment_service.template_out(db, template, include_versions=True)


@router.patch(
    "/assessment-templates/{template_id}", response_model=AssessmentTemplateOut
)
async def update_assessment_template(
    template_id: UUID,
    body: AssessmentTemplateUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> AssessmentTemplateOut:
    await require_permission(db, user, "assessments.manage")
    return await assessment_service.update_template(db, user, template_id, body)


@router.post(
    "/assessment-templates/{template_id}/versions",
    response_model=AssessmentVersionOut,
    status_code=status.HTTP_201_CREATED,
)
async def publish_assessment_version(
    template_id: UUID,
    body: AssessmentVersionCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> AssessmentVersionOut:
    await require_permission(db, user, "assessments.manage")
    return await assessment_service.create_version(db, user, template_id, body)


@router.post(
    "/applications/{application_id}/assessment-invitations",
    response_model=AssessmentInvitationCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_assessment_invitation(
    application_id: UUID,
    body: AssessmentInvitationCreate,
    response: Response,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> AssessmentInvitationCreated:
    _protect_token_response(response)
    await require_permission(db, user, "assessments.manage")
    return await assessment_service.create_invitation(db, user, application_id, body)


@router.get(
    "/applications/{application_id}/assessment-invitations",
    response_model=CursorPage[AssessmentInvitationOut],
)
async def list_assessment_invitations(
    application_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[AssessmentInvitationOut]:
    await require_permission(db, user, "assessments.read")
    page = await assessment_service.list_invitations(
        db,
        user.organization_id,
        application_id,
        limit=limit,
        cursor=cursor,
    )
    if await can_access_field(db, user, FieldClass.CONTACT):
        return page
    return page.model_copy(
        update={
            "items": [
                invitation.model_copy(
                    update={"recipient_email": None, "accommodations": None}
                )
                for invitation in page.items
            ]
        }
    )


@router.post(
    "/assessment-invitations/{invitation_id}/revoke",
    response_model=AssessmentInvitationOut,
)
async def revoke_assessment_invitation(
    invitation_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> AssessmentInvitationOut:
    await require_permission(db, user, "assessments.manage")
    return await assessment_service.revoke_invitation(db, user, invitation_id)


@router.post(
    "/assessment-invitations/{invitation_id}/reissue",
    response_model=AssessmentInvitationCreated,
)
async def reissue_assessment_invitation(
    invitation_id: UUID,
    body: AssessmentInvitationReissueRequest,
    response: Response,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> AssessmentInvitationCreated:
    _protect_token_response(response)
    await require_permission(db, user, "assessments.manage")
    return await assessment_service.reissue_invitation(db, user, invitation_id, body)


@router.post(
    "/assessment-invitations/{invitation_id}/retakes",
    response_model=AssessmentInvitationOut,
)
async def approve_assessment_retake(
    invitation_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> AssessmentInvitationOut:
    await require_permission(db, user, "assessments.manage")
    return await assessment_service.approve_retake(db, user, invitation_id)


@router.get(
    "/assessment-invitations/{invitation_id}/attempts",
    response_model=CursorPage[AssessmentAttemptSummaryOut],
)
async def list_assessment_invitation_attempts(
    invitation_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[AssessmentAttemptSummaryOut]:
    await require_permission(db, user, "assessments.results")
    await require_field_access(db, user, FieldClass.ASSESSMENT_RESULT)
    include_files = await can_access_field(
        db, user, FieldClass.CANDIDATE_DOCUMENT
    )
    return await assessment_service.list_invitation_attempts(
        db,
        user.organization_id,
        invitation_id,
        limit=limit,
        cursor=cursor,
        include_files=include_files,
    )


@router.get("/assessment-files/{file_id}/download")
async def download_assessment_file(
    file_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> StreamingResponse:
    await require_permission(db, user, "assessments.results")
    file, key = await assessment_service.reviewer_assessment_file(db, user, file_id)
    suffix = PurePath(key).suffix.lower() or ".bin"
    media_type = validated_private_media_type(
        key,
        allowed_suffixes={".pdf", ".docx", ".txt", ".png", ".jpg", ".jpeg"},
    )
    return await private_blob_response(
        key,
        media_type=media_type,
        filename=safe_download_filename(
            (
                PurePath(key).stem
                if not file.original_filename
                else file.original_filename.rsplit(".", 1)[0]
            ),
            fallback="assessment-file",
            suffix=suffix,
        ),
    )


@router.get("/assessment-attempts/{attempt_id}", response_model=AssessmentSubmissionOut)
async def get_assessment_attempt_result(
    attempt_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> AssessmentSubmissionOut:
    await require_permission(db, user, "assessments.results")
    return await assessment_service.get_attempt_result(
        db, user.organization_id, attempt_id
    )


@router.post(
    "/assessment-attempts/{attempt_id}/manual-grade",
    response_model=AssessmentSubmissionOut,
)
async def grade_assessment_attempt_manually(
    attempt_id: UUID,
    body: AssessmentManualGradeRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> AssessmentSubmissionOut:
    await require_permission(db, user, "assessments.grade")
    return await assessment_service.grade_attempt_manually(db, user, attempt_id, body)


@public_router.get("/{token}", response_model=PublicAssessmentLaunch)
async def get_public_assessment(
    token: str,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> PublicAssessmentLaunch:
    _protect_token_response(response)
    return await assessment_service.public_launch(db, token)


@public_router.post(
    "/{token}/attempts",
    response_model=AssessmentAttemptOut,
)
async def start_public_assessment_attempt(
    token: str,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> AssessmentAttemptOut:
    _protect_token_response(response)
    return await assessment_service.start_or_resume_attempt(db, token)


@public_router.patch(
    "/{token}/attempts/{attempt_id}/answers", response_model=AssessmentAttemptOut
)
async def autosave_public_assessment_answers(
    token: str,
    attempt_id: UUID,
    body: AssessmentAutosaveRequest,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> AssessmentAttemptOut:
    _protect_token_response(response)
    return await assessment_service.autosave_answers(db, token, attempt_id, body)


@public_router.post(
    "/{token}/attempts/{attempt_id}/questions/{question_id}/files",
    response_model=AssessmentFileUploadOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_public_assessment_file(
    token: str,
    attempt_id: UUID,
    question_id: UUID,
    response: Response,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session),
) -> AssessmentFileUploadOut:
    _protect_token_response(response)
    return await assessment_service.save_assessment_file(
        db, token, attempt_id, question_id, file
    )


@public_router.delete(
    "/{token}/attempts/{attempt_id}/files/{file_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_public_assessment_file(
    token: str,
    attempt_id: UUID,
    file_id: UUID,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> None:
    _protect_token_response(response)
    await assessment_service.delete_assessment_file(db, token, attempt_id, file_id)


@public_router.post(
    "/{token}/attempts/{attempt_id}/submit",
    response_model=PublicAssessmentSubmissionOut,
)
async def submit_public_assessment_attempt(
    token: str,
    attempt_id: UUID,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> PublicAssessmentSubmissionOut:
    _protect_token_response(response)
    return await assessment_service.submit_public_attempt(db, token, attempt_id)
