"""Focused integration coverage for applications and native assessments."""

import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from app.config import get_settings
from app.models.application import Application, ApplicationStageEvent
from app.models.assessment import (
    AssessmentAnswer,
    AssessmentAttempt,
    AssessmentFile,
    AssessmentInvitation,
    AssessmentResult,
    AssessmentTemplateVersion,
)
from app.models.candidate import Candidate
from app.models.enterprise import AuditLog
from app.models.operations import OrganizationEntitlement, UsageLedgerEntry
from app.models.organization import Organization
from app.models.position import Position
from app.models.private_object import PrivateObjectDeletion
from app.models.score import Score
from app.models.user import User
from app.routers.applications import (
    grade_assessment_attempt_manually as grade_assessment_attempt_route,
)
from app.schemas.applications import (
    ApplicationCreate,
    ApplicationStageTransition,
    ApplicationUpdate,
)
from app.schemas.assessments import (
    AssessmentAccommodations,
    AssessmentAutosaveRequest,
    AssessmentCondition,
    AssessmentConditionGroup,
    AssessmentInvitationCreate,
    AssessmentInvitationReissueRequest,
    AssessmentKnockoutRule,
    AssessmentManualGradeRequest,
    AssessmentOption,
    AssessmentQuestionCreate,
    AssessmentScoringRule,
    AssessmentSectionCreate,
    AssessmentTemplateCreate,
    AssessmentValidationRule,
    AssessmentVersionCreate,
)
from app.services.application_analytics import application_analytics
from app.services.applications import (
    create_application,
    get_application,
    list_applications,
    transition_application_stage,
    update_application,
)
from app.services.assessments import (
    approve_retake,
    autosave_answers,
    create_invitation,
    create_template,
    create_version,
    grade_attempt_manually,
    list_invitation_attempts,
    public_launch,
    reissue_invitation,
    reviewer_assessment_file,
    revoke_invitation,
    save_assessment_file,
    start_or_resume_attempt,
    submit_attempt,
    submit_public_attempt,
)
from app.storage import (
    DeletionSettlement,
    get_blob_store,
    settle_private_object_deletion,
)
from fastapi import HTTPException
from pydantic import ValidationError
from pypdf import PdfWriter
from sqlalchemy import func, select
from starlette.datastructures import Headers, UploadFile

pytestmark = pytest.mark.asyncio


async def _settle_private_object(db, *, private_object_id, organization_id, store):
    deletion = (
        await db.execute(
            select(PrivateObjectDeletion).where(
                PrivateObjectDeletion.organization_id == organization_id,
                PrivateObjectDeletion.private_object_id == private_object_id,
                PrivateObjectDeletion.completed_at.is_(None),
            )
        )
    ).scalar_one()
    token = uuid4()
    deletion.claimed_at = datetime.now(UTC)
    deletion.claim_token = token
    deletion.attempt_count += 1
    await db.flush()
    settled = await settle_private_object_deletion(
        db,
        deletion_id=deletion.id,
        organization_id=organization_id,
        claim_token=token,
        store=store,
    )
    await db.commit()
    assert settled == DeletionSettlement.DELETED


async def seed_actor(db, suffix: str):
    organization = Organization(
        name=f"Application org {suffix}",
        slug=f"application-{suffix}-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    user = User(
        organization_id=organization.id,
        email=f"application-{suffix}-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"candidate-{suffix}",
        raw_cv="A sufficiently detailed candidate profile for assessment tests. " * 2,
    )
    position = Position(
        organization_id=organization.id,
        title=f"Engineer {suffix}",
        status="open",
        created_by=None,
    )
    db.add_all([user, candidate, position])
    await db.flush()
    position.created_by = user.id
    await db.commit()
    return organization, user, candidate, position


async def create_native_application(db, user, candidate, position):
    return await create_application(
        db,
        user,
        ApplicationCreate(
            candidate_id=candidate.id,
            position_id=position.id,
            owner_user_id=user.id,
            source="manual",
        ),
    )


async def test_application_is_first_class_score_compatible_and_tenant_scoped(db):
    org, actor, candidate, position = await seed_actor(db, "primary")
    other_org, other_actor, _other_candidate, _other_position = await seed_actor(
        db, "other"
    )
    created = await create_native_application(db, actor, candidate, position)

    with pytest.raises(ValidationError):
        ApplicationCreate.model_validate(
            {
                "candidate_id": str(candidate.id),
                "position_id": str(position.id),
                "stage": "hired",
            }
        )

    application = await db.get(Application, created.id)
    assert application is not None
    created_version = application.updated_at
    score = (
        await db.execute(
            select(Score).where(
                Score.candidate_id == candidate.id,
                Score.position_id == position.id,
            )
        )
    ).scalar_one()
    assert application.legacy_score_id == score.id
    assert score.owner_user_id == actor.id
    assert score.stage == application.stage == "new"
    events = list(
        (
            await db.execute(
                select(ApplicationStageEvent).where(
                    ApplicationStageEvent.application_id == application.id
                )
            )
        ).scalars()
    )
    assert [(event.from_stage, event.to_stage) for event in events] == [(None, "new")]

    transitioned = await transition_application_stage(
        db,
        actor,
        application.id,
        ApplicationStageTransition(
            stage="screening",
            expected_version=application.updated_at,
            reason="CV received",
        ),
    )
    await db.refresh(score)
    assert transitioned.stage == score.stage == "screening"
    assert [
        (event.from_stage, event.to_stage) for event in transitioned.stage_events
    ] == [
        (None, "new"),
        ("new", "screening"),
    ]
    with pytest.raises(HTTPException) as stale_transition:
        await transition_application_stage(
            db,
            actor,
            application.id,
            ApplicationStageTransition(
                stage="interview",
                expected_version=created_version,
                reason="Stale browser intent",
            ),
        )
    assert stale_transition.value.status_code == 409
    await db.refresh(score)
    assert score.stage == "screening"

    assert (await list_applications(db, org.id)).items != []
    assert (await list_applications(db, other_org.id)).items == []
    with pytest.raises(HTTPException) as hidden:
        await get_application(db, other_actor.organization_id, application.id)
    assert hidden.value.status_code == 404


async def test_application_field_updates_are_versioned_and_do_not_rewrite_cohorts(db):
    organization, actor, candidate, position = await seed_actor(db, "field-history")
    replacement = User(
        organization_id=organization.id,
        email=f"replacement-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    db.add(replacement)
    await db.commit()

    created = await create_native_application(db, actor, candidate, position)
    original_version = created.updated_at
    updated = await update_application(
        db,
        actor,
        created.id,
        ApplicationUpdate(
            expected_version=original_version,
            source="referral",
            owner_user_id=replacement.id,
        ),
    )
    assert updated.source == "referral"
    assert updated.owner_user_id == replacement.id
    change_event = next(
        event
        for event in updated.stage_events
        if event.details.get("event_kind") == "application_fields_updated"
    )
    assert change_event.from_stage == change_event.to_stage == "new"
    assert change_event.details["changes"]["source"] == {
        "before": "manual",
        "after": "referral",
    }
    assert change_event.details["changes"]["owner_user_id"] == {
        "before": str(actor.id),
        "after": str(replacement.id),
    }

    with pytest.raises(HTTPException) as stale:
        await update_application(
            db,
            actor,
            created.id,
            ApplicationUpdate(
                expected_version=original_version,
                next_action_due_date=datetime.now(UTC).date(),
            ),
        )
    assert stale.value.status_code == 409

    report = await application_analytics(
        db,
        organization.id,
        start_at=created.applied_at - timedelta(minutes=1),
        end_at=datetime.now(UTC) + timedelta(minutes=1),
        timezone_name="UTC",
    )
    assert {item.key: item.count for item in report.by_source} == {"manual": 1}
    assert {item.key: item.count for item in report.by_owner} == {str(actor.id): 1}


def assessment_version_body() -> AssessmentVersionCreate:
    return AssessmentVersionCreate(
        title="Engineering screen",
        instructions="Answer every visible required question.",
        passing_percentage=Decimal("70"),
        time_limit_minutes=60,
        sections=[
            AssessmentSectionCreate(
                title="Core",
                questions=[
                    AssessmentQuestionCreate(
                        key="consent",
                        question_type="boolean",
                        prompt="Can you work in this role?",
                        required=True,
                        scoring=AssessmentScoringRule(correct=True),
                        knockout=AssessmentKnockoutRule(
                            operator="equals", value=False, reason="Role constraint"
                        ),
                        max_score=1,
                    ),
                    AssessmentQuestionCreate(
                        key="track",
                        question_type="single",
                        prompt="Choose a track",
                        required=True,
                        options=[
                            AssessmentOption(value="backend", label="Backend"),
                            AssessmentOption(value="frontend", label="Frontend"),
                        ],
                        scoring=AssessmentScoringRule(correct="backend"),
                        max_score=2,
                    ),
                    AssessmentQuestionCreate(
                        key="skills",
                        question_type="multi",
                        prompt="Choose languages",
                        required=True,
                        options=[
                            AssessmentOption(value="python", label="Python"),
                            AssessmentOption(value="sql", label="SQL"),
                            AssessmentOption(value="go", label="Go"),
                        ],
                        scoring=AssessmentScoringRule(
                            correct=["python", "sql"], partial_credit=True
                        ),
                        max_score=2,
                    ),
                    AssessmentQuestionCreate(
                        key="years",
                        question_type="number",
                        prompt="Years of experience",
                        required=True,
                        condition=AssessmentConditionGroup(
                            mode="all",
                            rules=[
                                AssessmentCondition(
                                    question_key="consent",
                                    operator="equals",
                                    value=True,
                                ),
                                AssessmentCondition(
                                    question_key="track",
                                    operator="equals",
                                    value="backend",
                                ),
                            ],
                        ),
                        validation=AssessmentValidationRule(min_value=0, max_value=80),
                        scoring=AssessmentScoringRule(min_value=3, max_value=10),
                        max_score=2,
                    ),
                    AssessmentQuestionCreate(
                        key="summary",
                        question_type="long",
                        prompt="Summarize your experience",
                        required=True,
                        condition=AssessmentCondition(
                            question_key="track", operator="equals", value="backend"
                        ),
                    ),
                    AssessmentQuestionCreate(
                        key="frontend_note",
                        question_type="short",
                        prompt="Describe your UI specialty",
                        required=True,
                        condition=AssessmentCondition(
                            question_key="track", operator="equals", value="frontend"
                        ),
                    ),
                    AssessmentQuestionCreate(
                        key="portfolio",
                        question_type="url",
                        prompt="Portfolio URL",
                        validation=AssessmentValidationRule(
                            min_length=20,
                            max_length=2_048,
                        ),
                        scoring=AssessmentScoringRule(
                            accepted_values=["https://example.com/portfolio"]
                        ),
                        max_score=1,
                    ),
                    AssessmentQuestionCreate(
                        key="sample",
                        question_type="file",
                        prompt="Optional work sample",
                        validation=AssessmentValidationRule(
                            allowed_file_extensions=["pdf"],
                            max_file_size_bytes=1_000_000,
                        ),
                        max_score=1,
                    ),
                ],
            )
        ],
    )


async def prepare_assessment(
    db,
    suffix: str = "flow",
    *,
    max_attempts: int = 1,
    accommodations: AssessmentAccommodations | None = None,
):
    _org, actor, candidate, position = await seed_actor(db, suffix)
    application = await create_native_application(db, actor, candidate, position)
    template = await create_template(
        db,
        actor,
        AssessmentTemplateCreate(name=f"Native assessment {suffix}"),
    )
    version = await create_version(db, actor, template.id, assessment_version_body())
    invitation = await create_invitation(
        db,
        actor,
        application.id,
        AssessmentInvitationCreate(
            template_version_id=version.id,
            expires_at=datetime.now(UTC) + timedelta(days=2),
            max_attempts=max_attempts,
            accommodations=accommodations or AssessmentAccommodations(),
        ),
    )
    return actor, application, template, version, invitation


async def test_public_launch_exposes_authoritative_retake_eligibility(db):
    actor, _application, _template, _version, invitation = await prepare_assessment(
        db, "retake-launch", max_attempts=2
    )

    launch = await public_launch(db, invitation.token)
    assert launch.max_attempts == 2
    assert launch.authorized_attempts == 1
    assert launch.attempts_started == 0
    assert launch.attempts_remaining == 1
    assert launch.can_start_attempt is True
    questions = {
        question.key: question
        for section in launch.sections
        for question in section.questions
    }

    first = await start_or_resume_attempt(db, invitation.token)
    in_progress = await public_launch(db, invitation.token)
    assert in_progress.attempts_started == 1
    assert in_progress.attempts_remaining == 0
    assert in_progress.can_start_attempt is False
    await autosave_answers(
        db,
        invitation.token,
        first.id,
        AssessmentAutosaveRequest(
            answers=[
                {"question_id": questions["consent"].id, "value": True},
                {"question_id": questions["track"].id, "value": "backend"},
                {
                    "question_id": questions["skills"].id,
                    "value": ["python", "sql"],
                },
                {"question_id": questions["years"].id, "value": 5},
                {
                    "question_id": questions["summary"].id,
                    "value": "I build reliable services and APIs.",
                },
            ]
        ),
    )
    await submit_attempt(db, invitation.token, first.id)

    retake = await public_launch(db, invitation.token)
    assert retake.attempt is None
    assert retake.result is None
    assert retake.attempts_started == 1
    assert retake.attempts_remaining == 0
    assert retake.can_start_attempt is False
    with pytest.raises(HTTPException) as replayed_submission:
        await submit_attempt(db, invitation.token, first.id)
    assert replayed_submission.value.status_code == 409

    with pytest.raises(HTTPException) as unauthorized_retake:
        await start_or_resume_attempt(db, invitation.token)
    assert unauthorized_retake.value.status_code == 403
    assert unauthorized_retake.value.detail == {"code": "retake_approval_required"}

    approved = await approve_retake(db, actor, invitation.id)
    assert approved.authorized_attempts == 2
    assert (await approve_retake(db, actor, invitation.id)).authorized_attempts == 2
    approved_launch = await public_launch(db, invitation.token)
    assert approved_launch.attempts_remaining == 1
    assert approved_launch.can_start_attempt is True

    second = await start_or_resume_attempt(db, invitation.token)
    assert second.sequence == 2
    second_launch = await public_launch(db, invitation.token)
    assert second_launch.attempt is not None
    assert second_launch.attempt.id == second.id
    assert second_launch.attempts_started == 2
    assert second_launch.attempts_remaining == 0
    assert second_launch.can_start_attempt is False
    approval_audits = list(
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.organization_id == actor.organization_id,
                    AuditLog.resource_id == invitation.id,
                    AuditLog.action == "approve_assessment_retake",
                )
            )
        ).scalars()
    )
    assert len(approval_audits) == 1


async def test_reissuing_invitation_atomically_invalidates_old_token_and_audits(db):
    actor, application, _template, _version, invitation = await prepare_assessment(
        db, "reissue"
    )
    original_token = invitation.token
    replacement_expiry = datetime.now(UTC) + timedelta(days=7)

    replacement = await reissue_invitation(
        db,
        actor,
        invitation.id,
        AssessmentInvitationReissueRequest(expires_at=replacement_expiry),
    )

    assert replacement.id == invitation.id
    assert replacement.token != original_token
    assert replacement.status == "active"
    assert replacement.expires_at == replacement_expiry
    stored = await db.get(AssessmentInvitation, invitation.id)
    assert stored is not None
    assert stored.token_digest != ""
    assert original_token not in stored.token_digest
    assert replacement.token not in stored.token_digest
    assert stored.token_prefix == replacement.token[:12]

    with pytest.raises(HTTPException) as old_link:
        await public_launch(db, original_token)
    assert old_link.value.status_code == 404
    launch = await public_launch(db, replacement.token)
    assert launch.invitation_id == invitation.id
    assert launch.sections

    audit = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.organization_id == actor.organization_id,
                AuditLog.action == "reissue_assessment_invitation",
                AuditLog.resource_id == invitation.id,
            )
        )
    ).scalar_one()
    assert audit.actor_user_id == actor.id
    assert audit.metadata_json["application_id"] == str(application.id)
    serialized_audit = str(audit.metadata_json)
    assert original_token not in serialized_audit
    assert replacement.token not in serialized_audit


async def test_reissue_enforces_fresh_bounded_expiry_and_terminal_state(db):
    actor, _application, _template, _version, invitation = await prepare_assessment(
        db, "reissue-bounds"
    )
    with pytest.raises(HTTPException) as past:
        await reissue_invitation(
            db,
            actor,
            invitation.id,
            AssessmentInvitationReissueRequest(
                expires_at=datetime.now(UTC) - timedelta(seconds=1)
            ),
        )
    assert past.value.status_code == 422

    with pytest.raises(HTTPException) as too_long:
        await reissue_invitation(
            db,
            actor,
            invitation.id,
            AssessmentInvitationReissueRequest(
                expires_at=datetime.now(UTC) + timedelta(days=366)
            ),
        )
    assert too_long.value.status_code == 422

    await revoke_invitation(db, actor, invitation.id)
    with pytest.raises(HTTPException) as revoked:
        await reissue_invitation(
            db,
            actor,
            invitation.id,
            AssessmentInvitationReissueRequest(
                expires_at=datetime.now(UTC) + timedelta(days=7)
            ),
        )
    assert revoked.value.status_code == 409
    assert revoked.value.detail["code"] == "assessment_invitation_not_reissuable"


async def test_assessment_accommodation_extends_timer_without_exceeding_invitation(db):
    accommodations = AssessmentAccommodations(
        extra_time_minutes=30,
        breaks_allowed=True,
        screen_reader_optimized=True,
    )
    _actor, _application, _template, _version, invitation = await prepare_assessment(
        db,
        "accommodations",
        accommodations=accommodations,
    )
    assert invitation.accommodations == accommodations
    assert invitation.authorized_attempts == 1

    launch = await public_launch(db, invitation.token)
    assert launch.accommodations == accommodations
    assert launch.time_limit_minutes == 60

    before_start = datetime.now(UTC)
    attempt = await start_or_resume_attempt(db, invitation.token)
    effective_duration = attempt.expires_at - before_start
    assert timedelta(minutes=89) < effective_duration <= timedelta(minutes=91)


async def test_assessment_autosave_resume_conditionals_and_deterministic_grading(db):
    actor, application, _template, version, invitation = await prepare_assessment(db)
    stored_invitation = await db.get(AssessmentInvitation, invitation.id)
    assert stored_invitation is not None
    assert invitation.token not in stored_invitation.token_digest
    assert len(stored_invitation.token_digest) == 64

    launch = await public_launch(db, invitation.token)
    serialized = launch.model_dump(mode="json")
    serialized_text = str(serialized)
    assert "scoring" not in serialized_text
    assert "knockout" not in serialized_text
    questions = {
        question.key: question
        for section in launch.sections
        for question in section.questions
    }
    assert set(questions) == {
        "consent",
        "track",
        "skills",
        "years",
        "summary",
        "frontend_note",
        "portfolio",
        "sample",
    }

    attempt = await start_or_resume_attempt(db, invitation.token)
    resumed = await start_or_resume_attempt(db, invitation.token)
    assert resumed.id == attempt.id
    with pytest.raises(HTTPException) as short_url:
        await autosave_answers(
            db,
            invitation.token,
            attempt.id,
            AssessmentAutosaveRequest(
                answers=[
                    {
                        "question_id": questions["portfolio"].id,
                        "value": "https://x.co",
                    }
                ]
            ),
        )
    assert short_url.value.status_code == 422
    await autosave_answers(
        db,
        invitation.token,
        attempt.id,
        AssessmentAutosaveRequest(
            answers=[
                {"question_id": questions["consent"].id, "value": True},
                {"question_id": questions["track"].id, "value": "backend"},
                {"question_id": questions["skills"].id, "value": ["python"]},
                {"question_id": questions["years"].id, "value": 5},
                {
                    "question_id": questions["portfolio"].id,
                    "value": "https://example.com/portfolio",
                },
            ]
        ),
    )
    with pytest.raises(HTTPException) as required:
        await submit_attempt(db, invitation.token, attempt.id)
    assert required.value.status_code == 422
    assert "summary" in required.value.detail["question_keys"]
    assert "frontend_note" not in required.value.detail["question_keys"]

    await autosave_answers(
        db,
        invitation.token,
        attempt.id,
        AssessmentAutosaveRequest(
            answers=[
                {
                    "question_id": questions["summary"].id,
                    "value": "I build reliable services and APIs.",
                }
            ]
        ),
    )
    submitted = await submit_attempt(db, invitation.token, attempt.id)
    assert submitted.result.application_id == application.id
    assert submitted.result.raw_score == Decimal("7.0000")
    assert submitted.result.max_score == Decimal("9.0000")
    assert submitted.result.percentage == Decimal("77.7778")
    assert submitted.result.passed is True
    assert submitted.result.knocked_out is False
    assert submitted.result.needs_manual_review is False
    with pytest.raises(HTTPException) as completed_launch:
        await public_launch(db, invitation.token)
    assert completed_launch.value.status_code == 410
    with pytest.raises(HTTPException) as duplicate:
        await submit_attempt(db, invitation.token, attempt.id)
    assert duplicate.value.status_code == 410
    revoked = await revoke_invitation(db, actor, invitation.id)
    assert revoked.status == "revoked"
    persisted_version = await db.get(AssessmentTemplateVersion, version.id)
    assert persisted_version is not None
    persisted_version.title = "Mutated title"
    with pytest.raises(ValueError, match="immutable"):
        await db.commit()
    await db.rollback()


async def test_assessment_any_condition_uses_decimal_numeric_equality_at_runtime(db):
    _organization, actor, candidate, position = await seed_actor(db, "any-numeric")
    application = await create_native_application(db, actor, candidate, position)
    template = await create_template(
        db,
        actor,
        AssessmentTemplateCreate(name="Numeric any-condition assessment"),
    )
    version = await create_version(
        db,
        actor,
        template.id,
        AssessmentVersionCreate(
            title="Numeric branch",
            passing_percentage=0,
            sections=[
                AssessmentSectionCreate(
                    title="Core",
                    questions=[
                        AssessmentQuestionCreate(
                            key="consent",
                            question_type="boolean",
                            prompt="Consent",
                            required=True,
                            scoring=AssessmentScoringRule(correct=True),
                            max_score=1,
                        ),
                        AssessmentQuestionCreate(
                            key="years",
                            question_type="number",
                            prompt="Years",
                            required=True,
                        ),
                        AssessmentQuestionCreate(
                            key="numeric_detail",
                            question_type="short",
                            prompt="Detail for two years",
                            required=True,
                            condition=AssessmentConditionGroup(
                                mode="any",
                                rules=[
                                    AssessmentCondition(
                                        question_key="years",
                                        operator="equals",
                                        value=Decimal("2.0"),
                                    ),
                                    AssessmentCondition(
                                        question_key="consent",
                                        operator="equals",
                                        value=False,
                                    ),
                                ],
                            ),
                        ),
                    ],
                )
            ],
        ),
    )
    invitation = await create_invitation(
        db,
        actor,
        application.id,
        AssessmentInvitationCreate(
            template_version_id=version.id,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        ),
    )
    launch = await public_launch(db, invitation.token)
    questions = {
        question.key: question
        for section in launch.sections
        for question in section.questions
    }
    attempt = await start_or_resume_attempt(db, invitation.token)
    await autosave_answers(
        db,
        invitation.token,
        attempt.id,
        AssessmentAutosaveRequest(
            answers=[
                {"question_id": questions["consent"].id, "value": True},
                {"question_id": questions["years"].id, "value": 2},
            ]
        ),
    )

    with pytest.raises(HTTPException) as missing_branch:
        await submit_attempt(db, invitation.token, attempt.id)
    assert missing_branch.value.status_code == 422
    assert missing_branch.value.detail["question_keys"] == ["numeric_detail"]

    await autosave_answers(
        db,
        invitation.token,
        attempt.id,
        AssessmentAutosaveRequest(
            answers=[
                {
                    "question_id": questions["numeric_detail"].id,
                    "value": "A deterministic example.",
                }
            ]
        ),
    )
    submitted = await submit_attempt(db, invitation.token, attempt.id)
    assert submitted.result.passed is True


async def test_manual_grading_is_bounded_audited_and_optimistically_locked(db):
    _organization, actor, candidate, position = await seed_actor(db, "manual-grade")
    application = await create_native_application(db, actor, candidate, position)
    template = await create_template(
        db,
        actor,
        AssessmentTemplateCreate(name="Manual grade assessment"),
    )
    body = assessment_version_body()
    summary = next(
        question
        for section in body.sections
        for question in section.questions
        if question.key == "summary"
    )
    summary.max_score = Decimal("3")
    version = await create_version(db, actor, template.id, body)
    invitation = await create_invitation(
        db,
        actor,
        application.id,
        AssessmentInvitationCreate(
            template_version_id=version.id,
            expires_at=datetime.now(UTC) + timedelta(days=2),
        ),
    )
    launch = await public_launch(db, invitation.token)
    questions = {
        question.key: question
        for section in launch.sections
        for question in section.questions
    }
    attempt = await start_or_resume_attempt(db, invitation.token)
    await autosave_answers(
        db,
        invitation.token,
        attempt.id,
        AssessmentAutosaveRequest(
            answers=[
                {"question_id": questions["consent"].id, "value": True},
                {"question_id": questions["track"].id, "value": "backend"},
                {
                    "question_id": questions["skills"].id,
                    "value": ["python", "sql"],
                },
                {"question_id": questions["years"].id, "value": 5},
                {
                    "question_id": questions["summary"].id,
                    "value": "I build secure and reliable backend services.",
                },
            ]
        ),
    )
    submitted = await submit_attempt(db, invitation.token, attempt.id)
    assert submitted.result.raw_score == Decimal("7.0000")
    assert submitted.result.max_score == Decimal("12.0000")
    assert submitted.result.needs_manual_review is True
    assert submitted.result.passed is False
    summary_breakdown = next(
        item
        for item in submitted.result.breakdown
        if item.get("question_id") == str(questions["summary"].id)
    )
    assert summary_breakdown["needs_manual_review"] is True

    grade_request = AssessmentManualGradeRequest(
        expected_graded_at=submitted.result.graded_at,
        grades=[
            {
                "question_id": questions["summary"].id,
                "score": Decimal("2.5"),
                "evidence_comment": "The answer cites concrete reliability work.",
            }
        ],
    )
    graded = await grade_attempt_manually(db, actor, attempt.id, grade_request)
    assert graded.result.raw_score == Decimal("9.5000")
    assert graded.result.max_score == Decimal("12.0000")
    assert graded.result.percentage == Decimal("79.1667")
    assert graded.result.passed is True
    assert graded.result.needs_manual_review is False
    assert graded.result.knocked_out is False
    graded_summary = next(
        item
        for item in graded.result.breakdown
        if item.get("question_id") == str(questions["summary"].id)
    )
    assert graded_summary["score"] == "2.5000"
    assert graded_summary["manual_grade"] == {
        "score": "2.5000",
        "evidence_comment": "The answer cites concrete reliability work.",
        "reviewed_by_user_id": str(actor.id),
        "reviewed_at": graded.result.graded_at.isoformat(),
    }

    audit = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.organization_id == actor.organization_id,
                AuditLog.resource_id == graded.result.id,
                AuditLog.action == "grade_assessment_manually",
            )
        )
    ).scalar_one()
    assert audit.actor_user_id == actor.id
    assert audit.metadata_json["prior"]["needs_manual_review"] is True
    assert audit.metadata_json["new"]["needs_manual_review"] is False
    assert audit.metadata_json["new"]["manual_grades"][0] == {
        "question_id": str(questions["summary"].id),
        "score": "2.5000",
        "evidence_comment": "The answer cites concrete reliability work.",
    }

    with pytest.raises(HTTPException) as stale:
        await grade_attempt_manually(db, actor, attempt.id, grade_request)
    assert stale.value.status_code == 409
    assert stale.value.detail["code"] == "stale_assessment_result"

    with pytest.raises(HTTPException) as excessive:
        await grade_attempt_manually(
            db,
            actor,
            attempt.id,
            AssessmentManualGradeRequest(
                expected_graded_at=graded.result.graded_at,
                grades=[
                    {
                        "question_id": questions["summary"].id,
                        "score": Decimal("3.0001"),
                        "evidence_comment": "This exceeds the stored question cap.",
                    }
                ],
            ),
        )
    assert excessive.value.status_code == 422
    assert excessive.value.detail["question_id"] == str(questions["summary"].id)

    _other_org, other_actor, _other_candidate, _other_position = await seed_actor(
        db, "manual-grade-other-tenant"
    )
    with pytest.raises(HTTPException) as hidden:
        await grade_attempt_manually(
            db,
            other_actor,
            attempt.id,
            AssessmentManualGradeRequest(
                expected_graded_at=graded.result.graded_at,
                grades=[
                    {
                        "question_id": questions["summary"].id,
                        "score": Decimal("2"),
                        "evidence_comment": "Cross-tenant grading must remain hidden.",
                    }
                ],
            ),
        )
    assert hidden.value.status_code == 404


async def _prepare_knockout_adjudication(
    db,
    suffix: str,
    *,
    include_manual_question: bool,
):
    organization, actor, candidate, position = await seed_actor(db, suffix)
    application = await create_native_application(db, actor, candidate, position)
    template = await create_template(
        db,
        actor,
        AssessmentTemplateCreate(name=f"Knockout adjudication {suffix}"),
    )
    questions = [
        AssessmentQuestionCreate(
            key="eligibility",
            question_type="boolean",
            prompt="Does the rule match?",
            required=True,
            scoring=AssessmentScoringRule(correct=False),
            knockout=AssessmentKnockoutRule(
                operator="equals",
                value=False,
                reason="Rule evidence retained for human review",
            ),
            max_score=5,
        )
    ]
    if include_manual_question:
        questions.append(
            AssessmentQuestionCreate(
                key="work_sample",
                question_type="long",
                prompt="Describe the relevant work sample",
                required=True,
                max_score=5,
            )
        )
    version = await create_version(
        db,
        actor,
        template.id,
        AssessmentVersionCreate(
            title="Adjudication contract",
            passing_percentage=Decimal("80"),
            sections=[AssessmentSectionCreate(title="Review", questions=questions)],
        ),
    )
    invitation = await create_invitation(
        db,
        actor,
        application.id,
        AssessmentInvitationCreate(
            template_version_id=version.id,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        ),
    )
    launch = await public_launch(db, invitation.token)
    question_by_key = {
        question.key: question
        for section in launch.sections
        for question in section.questions
    }
    attempt = await start_or_resume_attempt(db, invitation.token)
    answers = [
        {"question_id": question_by_key["eligibility"].id, "value": False}
    ]
    if include_manual_question:
        answers.append(
            {
                "question_id": question_by_key["work_sample"].id,
                "value": "A reviewer-verifiable work sample with concrete evidence.",
            }
        )
    await autosave_answers(
        db,
        invitation.token,
        attempt.id,
        AssessmentAutosaveRequest(answers=answers),
    )
    submitted = await submit_attempt(db, invitation.token, attempt.id)
    assert submitted.result.knocked_out is True
    assert submitted.result.knockout_review is None
    assert submitted.result.needs_manual_review is True
    assert submitted.result.passed is False
    return (
        organization,
        actor,
        application,
        invitation,
        attempt,
        question_by_key,
        submitted,
    )


async def test_knockout_only_review_can_be_cleared_or_confirmed_with_audit_and_lock(
    db,
):
    (
        organization,
        actor,
        _application,
        invitation,
        attempt,
        _questions,
        submitted,
    ) = await _prepare_knockout_adjudication(
        db,
        "knockout-only-review",
        include_manual_question=False,
    )

    cleared = await grade_attempt_manually(
        db,
        actor,
        attempt.id,
        AssessmentManualGradeRequest(
            expected_graded_at=submitted.result.graded_at,
            knockout_review={
                "outcome": "cleared",
                "reason": "  The source record shows this rule was a false positive.  ",
            },
        ),
    )
    assert cleared.result.knocked_out is True
    assert cleared.result.knockout_reason == "Rule evidence retained for human review"
    assert cleared.result.knockout_review is not None
    assert cleared.result.knockout_review.outcome == "cleared"
    assert (
        cleared.result.knockout_review.reason
        == "The source record shows this rule was a false positive."
    )
    assert cleared.result.knockout_review.reviewed_by_user_id == actor.id
    assert cleared.result.knockout_review.reviewed_at == cleared.result.graded_at
    assert cleared.result.needs_manual_review is False
    assert cleared.result.passed is True

    attempts_page = await list_invitation_attempts(
        db,
        organization.id,
        invitation.id,
    )
    assert attempts_page.items[0].result is not None
    assert attempts_page.items[0].result.knockout_review is not None
    assert attempts_page.items[0].result.knockout_review.outcome == "cleared"

    first_audit = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.organization_id == organization.id,
                AuditLog.resource_id == cleared.result.id,
                AuditLog.action == "grade_assessment_manually",
            )
        )
    ).scalar_one()
    assert first_audit.metadata_json["prior"]["knockout_review"] is None
    assert first_audit.metadata_json["new"]["knockout_review"]["outcome"] == "cleared"
    assert first_audit.metadata_json["new"]["manual_grades"] == []

    with pytest.raises(HTTPException) as stale:
        await grade_attempt_manually(
            db,
            actor,
            attempt.id,
            AssessmentManualGradeRequest(
                expected_graded_at=submitted.result.graded_at,
                knockout_review={
                    "outcome": "confirmed",
                    "reason": "A stale reviewer must not overwrite adjudication.",
                },
            ),
        )
    assert stale.value.status_code == 409
    assert stale.value.detail["code"] == "stale_assessment_result"

    confirmed = await grade_attempt_manually(
        db,
        actor,
        attempt.id,
        AssessmentManualGradeRequest(
            expected_graded_at=cleared.result.graded_at,
            knockout_review={
                "outcome": "confirmed",
                "reason": "The original evidence confirms that the rule applies.",
            },
        ),
    )
    assert confirmed.result.knockout_review is not None
    assert confirmed.result.knockout_review.outcome == "confirmed"
    assert confirmed.result.needs_manual_review is False
    assert confirmed.result.passed is False

    audits = list(
        (
            await db.execute(
                select(AuditLog)
                .where(
                    AuditLog.organization_id == organization.id,
                    AuditLog.resource_id == confirmed.result.id,
                    AuditLog.action == "grade_assessment_manually",
                )
                .order_by(AuditLog.occurred_at, AuditLog.id)
            )
        ).scalars()
    )
    assert len(audits) == 2
    assert audits[-1].metadata_json["prior"]["knockout_review"]["outcome"] == "cleared"
    assert audits[-1].metadata_json["new"]["knockout_review"]["outcome"] == "confirmed"


async def test_manual_and_knockout_review_resolve_atomically_and_remain_tenant_scoped(
    db,
):
    (
        organization,
        actor,
        _application,
        _invitation,
        attempt,
        questions,
        submitted,
    ) = await _prepare_knockout_adjudication(
        db,
        "combined-review",
        include_manual_question=True,
    )
    assert submitted.result.raw_score == Decimal("5.0000")
    assert submitted.result.max_score == Decimal("10.0000")

    combined = await grade_attempt_manually(
        db,
        actor,
        attempt.id,
        AssessmentManualGradeRequest(
            expected_graded_at=submitted.result.graded_at,
            grades=[
                {
                    "question_id": questions["work_sample"].id,
                    "score": Decimal("5"),
                    "evidence_comment": "The answer contains specific, verifiable evidence.",
                }
            ],
            knockout_review={
                "outcome": "cleared",
                "reason": "The documented accommodation makes the rule inapplicable.",
            },
        ),
    )
    assert combined.result.raw_score == Decimal("10.0000")
    assert combined.result.percentage == Decimal("100.0000")
    assert combined.result.needs_manual_review is False
    assert combined.result.passed is True
    assert combined.result.knockout_review is not None
    assert combined.result.knockout_review.outcome == "cleared"
    manual_item = next(
        item
        for item in combined.result.breakdown
        if item.get("question_id") == str(questions["work_sample"].id)
    )
    assert manual_item["needs_manual_review"] is False
    assert manual_item["manual_grade"]["reviewed_by_user_id"] == str(actor.id)

    audit = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.organization_id == organization.id,
                AuditLog.resource_id == combined.result.id,
                AuditLog.action == "grade_assessment_manually",
            )
        )
    ).scalar_one()
    assert audit.metadata_json["new"]["manual_grades"][0]["question_id"] == str(
        questions["work_sample"].id
    )
    assert audit.metadata_json["new"]["knockout_review"]["outcome"] == "cleared"

    _other_org, other_actor, _candidate, _position = await seed_actor(
        db,
        "combined-review-other-tenant",
    )
    with pytest.raises(HTTPException) as hidden:
        await grade_attempt_manually(
            db,
            other_actor,
            attempt.id,
            AssessmentManualGradeRequest(
                expected_graded_at=combined.result.graded_at,
                knockout_review={
                    "outcome": "confirmed",
                    "reason": "Cross-tenant adjudication must not reveal this attempt.",
                },
            ),
        )
    assert hidden.value.status_code == 404

    restricted = User(
        organization_id=organization.id,
        email=f"restricted-review-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="restricted",
        locale="en",
    )
    db.add(restricted)
    await db.commit()
    with pytest.raises(HTTPException) as denied:
        await grade_assessment_attempt_route(
            attempt_id=attempt.id,
            body=AssessmentManualGradeRequest(
                expected_graded_at=combined.result.graded_at,
                knockout_review={
                    "outcome": "confirmed",
                    "reason": "A restricted user must not adjudicate results.",
                },
            ),
            db=db,
            user=restricted,
        )
    assert denied.value.status_code == 403


async def test_assessment_knockout_file_validation_and_expiry(db):
    actor, application, _template, version, invitation = await prepare_assessment(
        db, "knockout"
    )
    launch = await public_launch(db, invitation.token)
    questions = {
        question.key: question
        for section in launch.sections
        for question in section.questions
    }
    attempt = await start_or_resume_attempt(db, invitation.token)
    with pytest.raises(HTTPException) as invalid_file:
        await autosave_answers(
            db,
            invitation.token,
            attempt.id,
            AssessmentAutosaveRequest(
                answers=[
                    {
                        "question_id": questions["sample"].id,
                        "value": {
                            "file_id": "upload_123",
                            "filename": "payload.exe",
                            "size_bytes": 50,
                        },
                    }
                ]
            ),
        )
    assert invalid_file.value.status_code == 422

    await autosave_answers(
        db,
        invitation.token,
        attempt.id,
        AssessmentAutosaveRequest(
            answers=[
                {"question_id": questions["consent"].id, "value": False},
                {"question_id": questions["track"].id, "value": "frontend"},
                {"question_id": questions["skills"].id, "value": ["python", "sql"]},
                {
                    "question_id": questions["frontend_note"].id,
                    "value": "Accessible component systems",
                },
            ]
        ),
    )
    public_submission = await submit_public_attempt(db, invitation.token, attempt.id)
    public_result = public_submission.result.model_dump(mode="json")
    assert set(public_result) == {
        "attempt_id",
        "submitted_at",
        "percentage",
        "passed",
        "needs_manual_review",
    }
    assert public_submission.result.passed is False
    assert public_submission.result.needs_manual_review is True
    stored_result = (
        await db.execute(
            select(AssessmentResult).where(AssessmentResult.attempt_id == attempt.id)
        )
    ).scalar_one()
    assert stored_result.knocked_out is True
    assert stored_result.knockout_reason == "Role constraint"
    refreshed_application = await db.get(Application, application.id)
    assert refreshed_application is not None
    assert refreshed_application.stage == "new"

    with pytest.raises(HTTPException) as past_expiry:
        await create_invitation(
            db,
            actor,
            application.id,
            AssessmentInvitationCreate(
                template_version_id=version.id,
                expires_at=datetime.now(UTC) - timedelta(minutes=1),
            ),
        )
    assert past_expiry.value.status_code == 422


def _upload(filename: str, content: bytes, media_type: str) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": media_type}),
    )


def _pdf(label: str) -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Subject": label})
    writer.write(output)
    return output.getvalue()


async def test_assessment_file_ownership_cleanup_reviewer_access_and_safe_refresh(
    db,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "storage_dir", str(tmp_path / "objects"))
    actor, _application, _template, _version, invitation = await prepare_assessment(
        db, "files"
    )
    launch = await public_launch(db, invitation.token)
    questions = {
        question.key: question
        for section in launch.sections
        for question in section.questions
    }
    attempt = await start_or_resume_attempt(db, invitation.token)

    with pytest.raises(HTTPException) as unsafe_type:
        await save_assessment_file(
            db,
            invitation.token,
            attempt.id,
            questions["sample"].id,
            _upload("payload.exe", b"MZ-not-an-assessment", "application/octet-stream"),
        )
    assert unsafe_type.value.status_code == 415
    with pytest.raises(HTTPException) as oversized:
        await save_assessment_file(
            db,
            invitation.token,
            attempt.id,
            questions["sample"].id,
            _upload(
                "oversized.pdf",
                b"%PDF-1.4\n" + b"x" * 1_000_000,
                "application/pdf",
            ),
        )
    assert oversized.value.status_code == 413

    orphan_content = _pdf("orphan")
    retained_content = _pdf("portfolio")
    orphan = await save_assessment_file(
        db,
        invitation.token,
        attempt.id,
        questions["sample"].id,
        _upload("orphan.pdf", orphan_content, "application/pdf"),
    )
    retained = await save_assessment_file(
        db,
        invitation.token,
        attempt.id,
        questions["sample"].id,
        _upload("work-sample.pdf", retained_content, "application/pdf"),
    )

    (
        _other_actor,
        _other_app,
        _other_template,
        _other_version,
        other_invitation,
    ) = await prepare_assessment(db, "files-other")
    other_launch = await public_launch(db, other_invitation.token)
    other_questions = {
        question.key: question
        for section in other_launch.sections
        for question in section.questions
    }
    other_attempt = await start_or_resume_attempt(db, other_invitation.token)
    with pytest.raises(HTTPException) as cross_scope:
        await autosave_answers(
            db,
            other_invitation.token,
            other_attempt.id,
            AssessmentAutosaveRequest(
                answers=[
                    {
                        "question_id": other_questions["sample"].id,
                        "value": retained.model_dump(mode="json"),
                    }
                ]
            ),
        )
    assert cross_scope.value.status_code == 422
    assert cross_scope.value.detail == "Invalid file reference"

    await autosave_answers(
        db,
        invitation.token,
        attempt.id,
        AssessmentAutosaveRequest(
            answers=[
                {"question_id": questions["consent"].id, "value": True},
                {"question_id": questions["track"].id, "value": "backend"},
                {
                    "question_id": questions["skills"].id,
                    "value": ["python", "sql"],
                },
                {"question_id": questions["years"].id, "value": 5},
                {
                    "question_id": questions["summary"].id,
                    "value": "I build secure and reliable backend services.",
                },
                {
                    "question_id": questions["sample"].id,
                    "value": retained.model_dump(mode="json"),
                },
            ]
        ),
    )
    submitted = await submit_attempt(db, invitation.token, attempt.id)
    assert submitted.result.needs_manual_review is True
    assert submitted.result.passed is False

    orphan_record = await db.get(AssessmentFile, orphan.file_id)
    retained_record = await db.get(AssessmentFile, retained.file_id)
    assert orphan_record is not None and orphan_record.status == "deleted"
    assert retained_record is not None and retained_record.status == "ready"
    assert orphan_record.private_object_id is not None
    assert retained_record.private_object_id is not None
    store = get_blob_store()
    assert await store.stat(orphan_record.storage_path) is not None
    await _settle_private_object(
        db,
        private_object_id=orphan_record.private_object_id,
        organization_id=actor.organization_id,
        store=store,
    )
    assert await store.stat(orphan_record.storage_path) is None
    assert await store.stat(retained_record.storage_path) is not None

    summaries = await list_invitation_attempts(db, actor.organization_id, invitation.id)
    assert summaries.next_cursor is None
    assert len(summaries.items) == 1
    assert summaries.items[0].id == attempt.id
    assert summaries.items[0].result is not None
    assert summaries.items[0].result.needs_manual_review is True
    assert summaries.items[0].result.knocked_out is False
    assert [file.file_id for file in summaries.items[0].files] == [retained.file_id]
    assert summaries.items[0].files[0].download_url == (
        f"/assessment-files/{retained.file_id}/download"
    )

    reviewed_file, reviewed_key = await reviewer_assessment_file(
        db, actor, retained.file_id
    )
    assert reviewed_file.id == retained.file_id
    assert reviewed_key == retained_record.storage_path
    assert b"".join([chunk async for chunk in store.iter_bytes(reviewed_key)]) == (
        retained_content
    )
    audit = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.organization_id == actor.organization_id,
                AuditLog.resource_id == retained.file_id,
                AuditLog.action == "download_assessment_file",
            )
        )
    ).scalar_one()
    assert audit.metadata_json["sensitive_read"] is True

    with pytest.raises(HTTPException) as hidden_from_other_tenant:
        await reviewer_assessment_file(db, _other_actor, retained.file_id)
    assert hidden_from_other_tenant.value.status_code == 404

    with pytest.raises(HTTPException) as consumed:
        await public_launch(db, invitation.token)
    assert consumed.value.status_code == 410


async def test_assessment_start_entitlement_is_atomic_and_resume_is_not_metered(db):
    actor, _application, _template, _version, invitation = await prepare_assessment(
        db, "entitlement"
    )
    stored_invitation = await db.get(AssessmentInvitation, invitation.id)
    assert stored_invitation is not None
    organization_id = actor.organization_id
    invitation_id = invitation.id
    stored_invitation.max_attempts = 2
    stored_invitation.authorized_attempts = 2
    db.add(
        OrganizationEntitlement(
            organization_id=organization_id,
            metric="assessment_starts",
            limit_value=1,
            period="contract",
        )
    )
    await db.commit()

    first = await start_or_resume_attempt(db, invitation.token)
    resumed = await start_or_resume_attempt(db, invitation.token)
    assert resumed.id == first.id
    assert (
        int(
            (
                await db.execute(
                    select(func.count(UsageLedgerEntry.id)).where(
                        UsageLedgerEntry.organization_id == organization_id,
                        UsageLedgerEntry.metric == "assessment_starts",
                    )
                )
            ).scalar_one()
        )
        == 1
    )
    assert (
        int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(AssessmentAttempt)
                    .where(AssessmentAttempt.invitation_id == invitation_id)
                )
            ).scalar_one()
        )
        == 1
    )
    stored_attempt = await db.get(AssessmentAttempt, first.id)
    assert stored_attempt is not None
    stored_attempt.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    with pytest.raises(HTTPException) as blocked:
        await start_or_resume_attempt(db, invitation.token)
    assert blocked.value.status_code == 403
    assert blocked.value.detail == {
        "code": "entitlement_exceeded",
        "metric": "assessment_starts",
        "used": 1,
        "limit": 1,
        "requested": 1,
    }
    assert (
        int(
            (
                await db.execute(
                    select(func.count(UsageLedgerEntry.id)).where(
                        UsageLedgerEntry.organization_id == organization_id,
                        UsageLedgerEntry.metric == "assessment_starts",
                    )
                )
            ).scalar_one()
        )
        == 1
    )
    assert (
        int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(AssessmentAttempt)
                    .where(AssessmentAttempt.invitation_id == invitation_id)
                )
            ).scalar_one()
        )
        == 1
    )


async def test_assessment_autosave_null_clears_scalar_answer(db):
    _actor, _application, _template, version, invitation = await prepare_assessment(
        db, "clear-answer"
    )
    attempt = await start_or_resume_attempt(db, invitation.token)
    consent = next(
        question
        for section in version.sections
        for question in section.questions
        if question.key == "consent"
    )
    saved = await autosave_answers(
        db,
        invitation.token,
        attempt.id,
        AssessmentAutosaveRequest(answers=[{"question_id": consent.id, "value": True}]),
    )
    assert [(answer.question_id, answer.value) for answer in saved.answers] == [
        (consent.id, True)
    ]

    cleared = await autosave_answers(
        db,
        invitation.token,
        attempt.id,
        AssessmentAutosaveRequest(answers=[{"question_id": consent.id, "value": None}]),
    )
    assert cleared.answers == []
    assert (
        await db.execute(
            select(AssessmentAnswer).where(
                AssessmentAnswer.attempt_id == attempt.id,
                AssessmentAnswer.question_id == consent.id,
            )
        )
    ).scalar_one_or_none() is None
