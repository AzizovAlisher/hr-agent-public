"""Application-level source quality, AI review, and cohort reconciliation tests."""

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.auth.jwt import encode_access_token
from app.main import app
from app.models.application import Application, ApplicationEvaluation
from app.models.assessment import (
    AssessmentAttempt,
    AssessmentInvitation,
    AssessmentTemplate,
    AssessmentTemplateVersion,
)
from app.models.candidate import Candidate
from app.models.enterprise import PermissionOverride
from app.models.organization import Organization
from app.models.position import Position
from app.models.requisition import CandidateOffer
from app.models.user import User
from app.schemas.applications import ApplicationStageTransition
from app.services.application_analytics import (
    application_analytics,
    application_analytics_csv,
)
from app.services.applications import (
    ensure_application_link,
    transition_application_stage,
)
from app.services.public_applications import submit_public_application

pytestmark = pytest.mark.asyncio


async def _seed(db, label: str):
    organization = Organization(
        name=f"Analytics {label}",
        slug=f"analytics-{label}-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    user = User(
        organization_id=organization.id,
        email=f"analytics-{label}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    db.add(user)
    await db.flush()
    first_position = Position(
        organization_id=organization.id,
        title=f"{label} Platform Engineer",
        status="open",
        created_by=user.id,
    )
    second_position = Position(
        organization_id=organization.id,
        title=f"{label} Data Engineer",
        status="open",
        created_by=user.id,
    )
    db.add_all([first_position, second_position])
    await db.commit()
    return organization, user, first_position, second_position


async def _transition(db, user: User, application_id, stage: str, occurred_at):
    application = await db.get(Application, application_id)
    assert application is not None
    await db.refresh(application)
    rejection = (
        {
            "disposition_code": "not_qualified",
            "reason": "Closed before the next application cycle",
        }
        if stage == "rejected"
        else {}
    )
    return await transition_application_stage(
        db,
        user,
        application.id,
        ApplicationStageTransition(
            stage=stage,
            expected_version=application.updated_at,
            **rejection,
        ),
        occurred_at=occurred_at,
    )


async def test_source_quality_ai_review_filters_export_and_tenant_isolation(db):
    organization, user, position, second_position = await _seed(db, "primary")
    other_organization, other_user, other_position, _unused = await _seed(db, "other")
    window_start = datetime(2026, 7, 1, tzinfo=UTC)
    window_end = datetime(2026, 7, 15, tzinfo=UTC)

    first = await submit_public_application(
        position=position,
        name="Cycle Candidate",
        phone="+992900000701",
        email="cycle.analytics@example.test",
        source="company_website",
        file=None,
        raw_text="Experienced platform engineer with reliable systems work. " * 2,
        occurred_at=datetime(2026, 7, 2, 9, tzinfo=UTC),
        db=db,
    )
    retry = await submit_public_application(
        position=position,
        name="Cycle Candidate",
        phone="992 900 000 701",
        email="CYCLE.ANALYTICS@example.test",
        source="company_website",
        file=None,
        raw_text="Experienced platform engineer with reliable systems work. " * 2,
        occurred_at=datetime(2026, 7, 2, 9, tzinfo=UTC),
        db=db,
    )
    assert retry.application_id == first.application_id
    assert retry.application_created is False
    await _transition(
        db,
        user,
        first.application_id,
        "rejected",
        datetime(2026, 7, 3, 9, tzinfo=UTC),
    )

    second_link = await ensure_application_link(
        db,
        organization_id=organization.id,
        candidate_id=first.candidate_id,
        position_id=position.id,
        source="second_cycle",
        owner_user_id=user.id,
        actor_user_id=user.id,
        actor_kind="human",
        reason="analytics_reapplication",
        occurred_at=datetime(2026, 7, 4, 9, tzinfo=UTC),
        allow_reapplication=True,
    )
    second_application = second_link.application
    assert second_link.created is True
    assert second_application.cycle_number == 2
    await db.commit()
    for stage, occurred_at in (
        ("screening", datetime(2026, 7, 5, 9, tzinfo=UTC)),
        ("interview", datetime(2026, 7, 6, 9, tzinfo=UTC)),
        ("offer", datetime(2026, 7, 7, 9, tzinfo=UTC)),
        ("hired", datetime(2026, 7, 8, 9, tzinfo=UTC)),
    ):
        await _transition(db, user, second_application.id, stage, occurred_at)

    third_link = await ensure_application_link(
        db,
        organization_id=organization.id,
        candidate_id=first.candidate_id,
        position_id=second_position.id,
        source="referral",
        owner_user_id=user.id,
        actor_user_id=user.id,
        actor_kind="human",
        reason="analytics_multi_position",
        occurred_at=datetime(2026, 7, 5, 12, tzinfo=UTC),
    )
    third_application = third_link.application
    assert third_link.created is True

    candidate = await db.get(Candidate, first.candidate_id)
    assert candidate is not None
    template = AssessmentTemplate(
        organization_id=organization.id,
        created_by_user_id=user.id,
        name="Source quality assessment",
        status="active",
    )
    db.add(template)
    await db.flush()
    version = AssessmentTemplateVersion(
        organization_id=organization.id,
        template_id=template.id,
        version=1,
        title="Source quality assessment v1",
        passing_percentage=60,
        time_limit_minutes=60,
        created_by_user_id=user.id,
        published_at=datetime(2026, 7, 2, 10, tzinfo=UTC),
        created_at=datetime(2026, 7, 2, 10, tzinfo=UTC),
    )
    db.add(version)
    await db.flush()
    first_invitation = AssessmentInvitation(
        organization_id=organization.id,
        application_id=first.application_id,
        template_version_id=version.id,
        token_digest="1" * 64,
        token_prefix="first-cycle",
        recipient_email="cycle.analytics@example.test",
        status="active",
        expires_at=datetime(2026, 7, 12, tzinfo=UTC),
        max_attempts=1,
        authorized_attempts=1,
        accommodations={},
        invited_by_user_id=user.id,
        first_opened_at=datetime(2026, 7, 2, 11, tzinfo=UTC),
        created_at=datetime(2026, 7, 2, 10, tzinfo=UTC),
        updated_at=datetime(2026, 7, 2, 11, tzinfo=UTC),
    )
    second_invitation = AssessmentInvitation(
        organization_id=organization.id,
        application_id=second_application.id,
        template_version_id=version.id,
        token_digest="2" * 64,
        token_prefix="second-cycle",
        recipient_email="cycle.analytics@example.test",
        status="completed",
        expires_at=datetime(2026, 7, 12, tzinfo=UTC),
        max_attempts=1,
        authorized_attempts=1,
        accommodations={},
        invited_by_user_id=user.id,
        first_opened_at=datetime(2026, 7, 5, 10, tzinfo=UTC),
        completed_at=datetime(2026, 7, 5, 11, tzinfo=UTC),
        created_at=datetime(2026, 7, 4, 10, tzinfo=UTC),
        updated_at=datetime(2026, 7, 5, 11, tzinfo=UTC),
    )
    db.add_all([first_invitation, second_invitation])
    await db.flush()
    db.add_all(
        [
            AssessmentAttempt(
                organization_id=organization.id,
                invitation_id=first_invitation.id,
                application_id=first.application_id,
                template_version_id=version.id,
                sequence=1,
                status="in_progress",
                started_at=datetime(2026, 7, 2, 11, tzinfo=UTC),
                last_saved_at=datetime(2026, 7, 2, 11, 30, tzinfo=UTC),
                expires_at=datetime(2026, 7, 2, 12, tzinfo=UTC),
                created_at=datetime(2026, 7, 2, 11, tzinfo=UTC),
                updated_at=datetime(2026, 7, 2, 11, 30, tzinfo=UTC),
            ),
            AssessmentAttempt(
                organization_id=organization.id,
                invitation_id=second_invitation.id,
                application_id=second_application.id,
                template_version_id=version.id,
                sequence=1,
                status="submitted",
                started_at=datetime(2026, 7, 5, 10, tzinfo=UTC),
                last_saved_at=datetime(2026, 7, 5, 10, 30, tzinfo=UTC),
                expires_at=datetime(2026, 7, 5, 12, tzinfo=UTC),
                submitted_at=datetime(2026, 7, 5, 11, tzinfo=UTC),
                created_at=datetime(2026, 7, 5, 10, tzinfo=UTC),
                updated_at=datetime(2026, 7, 5, 11, tzinfo=UTC),
            ),
        ]
    )

    db.add(
        CandidateOffer(
            organization_id=organization.id,
            application_id=second_application.id,
            candidate_id=candidate.id,
            position_id=position.id,
            created_by_user_id=user.id,
            position_title_snapshot=position.title,
            subject_template_snapshot="Offer for {{position}}",
            body_template_snapshot="Welcome",
            subject="Offer",
            body="Welcome",
            salary_currency="TJS",
            expires_at=datetime(2026, 7, 12, tzinfo=UTC),
            required_approvals=1,
            status="accepted",
            sent_at=datetime(2026, 7, 7, 10, tzinfo=UTC),
            responded_at=datetime(2026, 7, 8, 10, tzinfo=UTC),
        )
    )

    first_ai = ApplicationEvaluation(
        organization_id=organization.id,
        application_id=first.application_id,
        kind="ai_scoring",
        status="completed",
        version=1,
        evidence={"raw_prompt_canary": "PRIVATE CANDIDATE PROMPT"},
        scores={"overall": 80},
        provenance={
            "provider": "private_vllm",
            "model": "private/model-a",
            "prompt_version": "score-v1",
            "raw_prompt": "PRIVATE CANDIDATE PROMPT",
        },
        actor_user_id=user.id,
        actor_kind="ai",
        evaluated_at=datetime(2026, 7, 2, 12, tzinfo=UTC),
        created_at=datetime(2026, 7, 2, 12, tzinfo=UTC),
    )
    second_ai = ApplicationEvaluation(
        organization_id=organization.id,
        application_id=second_application.id,
        kind="ai_scoring",
        status="completed",
        version=1,
        evidence={},
        scores={"overall": 90},
        provenance={
            "provider": "openrouter",
            "model": "managed/model-b",
            "prompt_version": "score-v2",
        },
        actor_user_id=user.id,
        actor_kind="ai",
        evaluated_at=datetime(2026, 7, 5, 13, tzinfo=UTC),
        created_at=datetime(2026, 7, 5, 13, tzinfo=UTC),
    )
    db.add_all([first_ai, second_ai])
    await db.flush()
    db.add(
        ApplicationEvaluation(
            organization_id=organization.id,
            application_id=second_application.id,
            kind="ai_scoring",
            status="reviewed",
            version=2,
            supersedes_evaluation_id=second_ai.id,
            evidence={},
            scores={"overall": 75},
            provenance={
                **second_ai.provenance,
                "review": {
                    "reviewed_evaluation_id": str(second_ai.id),
                    "reviewer_user_id": str(user.id),
                    "reason": "Structured human review",
                },
            },
            actor_user_id=user.id,
            actor_kind="human",
            evaluated_at=datetime(2026, 7, 6, 13, tzinfo=UTC),
            created_at=datetime(2026, 7, 6, 13, tzinfo=UTC),
        )
    )

    other = await submit_public_application(
        position=other_position,
        name="Cycle Candidate",
        phone="+992900000701",
        email="cycle.analytics@example.test",
        source="company_website",
        file=None,
        raw_text="Tenant-isolated candidate evidence. " * 3,
        occurred_at=datetime(2026, 7, 2, 9, tzinfo=UTC),
        db=db,
    )
    db.add(
        ApplicationEvaluation(
            organization_id=other_organization.id,
            application_id=other.application_id,
            kind="ai_scoring",
            status="completed",
            version=1,
            evidence={},
            scores={"overall": 5},
            provenance={
                "provider": "other_tenant_provider",
                "model": "other/model",
                "prompt_version": "other-v1",
            },
            actor_user_id=other_user.id,
            actor_kind="ai",
            evaluated_at=datetime(2026, 7, 3, 12, tzinfo=UTC),
            created_at=datetime(2026, 7, 3, 12, tzinfo=UTC),
        )
    )
    await db.commit()

    report = await application_analytics(
        db,
        organization.id,
        start_at=window_start,
        end_at=window_end,
        timezone_name="Asia/Dushanbe",
        include_quality_scores=True,
        include_ai_decisions=True,
        drilldown_dimension="all",
    )
    assert report.total_applications == 3
    assert report.unique_candidates == 1
    assert report.quality_score_authorized is True
    by_source = {item.key: item for item in report.by_source}
    assert by_source["company_website"].model_dump() == {
        "key": "company_website",
        "label": None,
        "count": 1,
        "applications": 1,
        "assessment_started": 1,
        "assessment_completed": 0,
        "assessment_dropped_off": 1,
        "interviews": 0,
        "offers": 0,
        "hires": 0,
        "assessment_completion_rate_percent": 0.0,
        "interview_rate_percent": 0.0,
        "offer_rate_percent": 0.0,
        "hire_rate_percent": 0.0,
        "scored_applications": 1,
        "average_quality_score": 80.0,
    }
    second_source = by_source["second_cycle"]
    assert second_source.applications == 1
    assert second_source.assessment_completed == 1
    assert second_source.assessment_dropped_off == 0
    assert second_source.interviews == 1
    assert second_source.offers == 1
    assert second_source.hires == 1
    assert second_source.average_quality_score == 75
    assert report.assessments.applications_invited == 2
    assert report.assessments.applications_started == 2
    assert report.assessments.applications_completed == 1
    assert report.assessments.applications_dropped_off == 1
    assert report.ai_decisions is not None
    assert report.ai_decisions.recommendations == 2
    assert report.ai_decisions.human_reviews == 1
    assert report.ai_decisions.human_overrides == 1
    assert report.ai_decisions.review_rate_percent == 50
    assert report.ai_decisions.override_rate_percent == 50
    provenance = {
        (item.provider, item.model, item.prompt_version): item
        for item in report.ai_decisions.by_provenance
    }
    assert provenance[("openrouter", "managed/model-b", "score-v2")].human_overrides == 1
    assert all(item.provider != "other_tenant_provider" for item in provenance.values())

    assert report.drilldown is not None
    assert report.drilldown.total == report.total_applications
    drilldown = {item.application_id: item for item in report.drilldown.items}
    assert drilldown[first.application_id].assessment_dropped_off is True
    assert drilldown[second_application.id].assessment_completed is True
    assert drilldown[second_application.id].offer_sent is True
    assert drilldown[second_application.id].hired is True
    assert drilldown[second_application.id].quality_score == 75
    assert drilldown[third_application.id].quality_score is None

    filtered = await application_analytics(
        db,
        organization.id,
        start_at=window_start,
        end_at=window_end,
        timezone_name="UTC",
        position_id=position.id,
        source="second_cycle",
        owner_user_id=user.id,
        include_quality_scores=True,
        include_ai_decisions=True,
        drilldown_dimension="source",
        drilldown_key="second_cycle",
    )
    assert filtered.total_applications == 1
    assert filtered.drilldown is not None
    assert filtered.drilldown.total == filtered.total_applications
    assert filtered.drilldown.items[0].application_id == second_application.id
    assert filtered.ai_decisions is not None
    assert filtered.ai_decisions.recommendations == 1
    assert filtered.ai_decisions.human_overrides == 1
    csv_export = application_analytics_csv(filtered)
    assert str(second_application.id) in csv_export
    assert str(first.application_id) not in csv_export
    assert str(third_application.id) not in csv_export
    assert "PRIVATE CANDIDATE PROMPT" not in csv_export
    assert "other_tenant_provider" not in csv_export

    redacted = await application_analytics(
        db,
        organization.id,
        start_at=window_start,
        end_at=window_end,
        timezone_name="UTC",
        include_quality_scores=False,
        include_ai_decisions=False,
        drilldown_dimension="all",
    )
    assert redacted.quality_score_authorized is False
    assert redacted.ai_decisions is None
    assert all(item.scored_applications is None for item in redacted.by_source)
    assert all(item.average_quality_score is None for item in redacted.by_source)
    assert redacted.drilldown is not None
    assert all(item.quality_score is None for item in redacted.drilldown.items)

    other_report = await application_analytics(
        db,
        other_organization.id,
        start_at=window_start,
        end_at=window_end,
        timezone_name="UTC",
        include_quality_scores=True,
        include_ai_decisions=True,
    )
    assert other_report.total_applications == 1
    assert other_report.unique_candidates == 1
    assert other_report.ai_decisions is not None
    assert other_report.ai_decisions.recommendations == 1
    assert other_report.by_source[0].average_quality_score == 5
    primary_candidates = list(
        (
            await db.execute(
                select(Candidate).where(Candidate.organization_id == organization.id)
            )
        ).scalars()
    )
    assert len(primary_candidates) == 1


async def test_pipeline_redacts_quality_and_ai_provenance_by_field_policy(db):
    organization, user, position, _second_position = await _seed(db, "policy")
    result = await submit_public_application(
        position=position,
        name="Policy Candidate",
        phone="+992900000702",
        email="policy.analytics@example.test",
        source="company_website",
        file=None,
        raw_text="Candidate evidence that must never appear in analytics. " * 2,
        occurred_at=datetime(2026, 7, 2, 9, tzinfo=UTC),
        db=db,
    )
    db.add(
        ApplicationEvaluation(
            organization_id=organization.id,
            application_id=result.application_id,
            kind="ai_scoring",
            status="completed",
            version=1,
            evidence={"raw_prompt": "HTTP PRIVATE PROMPT CANARY"},
            scores={"overall": 88},
            provenance={
                "provider": "private_vllm",
                "model": "private/policy-model",
                "prompt_version": "policy-v1",
                "raw_prompt": "HTTP PRIVATE PROMPT CANARY",
            },
            actor_user_id=user.id,
            actor_kind="ai",
            evaluated_at=datetime(2026, 7, 3, 9, tzinfo=UTC),
            created_at=datetime(2026, 7, 3, 9, tzinfo=UTC),
        )
    )
    await db.commit()
    authorization = {
        "Authorization": "Bearer "
        + encode_access_token(user.id, user.role, user.locale)
    }
    params = {
        "start_at": "2026-07-01T00:00:00+00:00",
        "end_at": "2026-07-15T00:00:00+00:00",
        "timezone": "UTC",
        "drilldown_dimension": "all",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://analytics.test",
    ) as client:
        permitted = await client.get(
            "/analytics/pipeline", params=params, headers=authorization
        )
    assert permitted.status_code == 200
    permitted_body = permitted.json()
    assert permitted_body["quality_score_authorized"] is True
    assert permitted_body["by_source"][0]["average_quality_score"] == 88
    assert permitted_body["ai_decisions"]["recommendations"] == 1
    assert "HTTP PRIVATE PROMPT CANARY" not in permitted.text

    db.add_all(
        [
            PermissionOverride(
                organization_id=organization.id,
                role="recruiter",
                permission="assessments.results",
                allowed=False,
            ),
            PermissionOverride(
                organization_id=organization.id,
                role="recruiter",
                permission="ai.sensitive_context",
                allowed=False,
            ),
        ]
    )
    await db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://analytics.test",
    ) as client:
        redacted = await client.get(
            "/analytics/pipeline", params=params, headers=authorization
        )
    assert redacted.status_code == 200
    redacted_body = redacted.json()
    assert redacted_body["quality_score_authorized"] is False
    assert redacted_body["by_source"][0]["scored_applications"] is None
    assert redacted_body["by_source"][0]["average_quality_score"] is None
    assert redacted_body["drilldown"]["items"][0]["quality_score"] is None
    assert redacted_body["ai_decisions"] is None
    assert "private/policy-model" not in redacted.text
    assert "HTTP PRIVATE PROMPT CANARY" not in redacted.text
