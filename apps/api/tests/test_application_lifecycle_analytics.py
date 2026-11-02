"""Reconciliation coverage for canonical person/application lifecycle analytics."""

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select

from app.auth.jwt import encode_access_token
from app.main import app
from app.models.application import Application, ApplicationStageEvent
from app.models.assessment import (
    AssessmentAttempt,
    AssessmentInvitation,
    AssessmentTemplate,
    AssessmentTemplateVersion,
)
from app.models.candidate import Candidate
from app.models.candidate_activity import CandidateActivity
from app.models.communication import CandidateContactPoint
from app.models.enterprise import AuditLog
from app.models.integration import IntegrationConnection, IntegrationJob
from app.models.notification import Notification
from app.models.operations import UsageLedgerEntry
from app.models.organization import Organization
from app.models.position import Position
from app.models.provider_assessment import ProviderAssessment
from app.models.requisition import CandidateOffer
from app.models.score import Score
from app.models.user import User
from app.schemas.applications import ApplicationStageTransition
from app.services.application_analytics import (
    application_analytics,
    application_analytics_csv,
)
from app.services.applications import transition_application_stage
from app.services.public_applications import submit_public_application
from app.services.source_provenance import public_source_slug

pytestmark = pytest.mark.asyncio


async def _seed(db):
    organization = Organization(
        name="Lifecycle Analytics",
        slug=f"lifecycle-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    user = User(
        organization_id=organization.id,
        email=f"lifecycle-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    db.add(user)
    await db.flush()
    first = Position(
        organization_id=organization.id,
        title="Platform Engineer",
        status="open",
        created_by=user.id,
    )
    second = Position(
        organization_id=organization.id,
        title="Data Engineer",
        status="open",
        created_by=user.id,
    )
    db.add_all([first, second])
    await db.commit()
    return organization, user, first, second


async def _apply(
    db,
    position,
    *,
    email: str,
    phone: str,
    source: str,
    occurred_at: datetime | None = None,
):
    return await submit_public_application(
        position=position,
        name="Canonical Candidate",
        phone=phone,
        email=email,
        source=source,
        file=None,
        raw_text="Experienced engineer building reliable distributed systems. " * 2,
        occurred_at=occurred_at,
        db=db,
    )


async def _transition(db, user, application_id, stage, *, occurred_at=None, **kwargs):
    application = await db.get(Application, application_id)
    assert application is not None
    await db.refresh(application)
    return await transition_application_stage(
        db,
        user,
        application_id,
        ApplicationStageTransition(
            stage=stage,
            expected_version=application.updated_at,
            **kwargs,
        ),
        occurred_at=occurred_at,
    )


async def test_identity_multi_position_idempotency_lifecycle_and_event_analytics(db):
    organization, user, first, second = await _seed(db)
    first_applied_at = datetime(2026, 1, 1, 23, 30, tzinfo=UTC)
    second_applied_at = datetime(2026, 1, 2, 20, 30, tzinfo=UTC)
    transition_times_by_stage = {
        "screening": datetime(2026, 1, 2, 0, 30, tzinfo=UTC),
        "interview": datetime(2026, 1, 2, 1, 30, tzinfo=UTC),
        "rejected": datetime(2026, 1, 2, 2, 30, tzinfo=UTC),
    }
    first_result = await _apply(
        db,
        first,
        email="Candidate.Person@Example.Test",
        phone="+992 (900) 555-101",
        source="company_website",
        occurred_at=first_applied_at,
    )
    retry = await _apply(
        db,
        first,
        email="candidate.person@example.test",
        phone="992-900-555-101",
        source="company_website",
        occurred_at=first_applied_at,
    )
    second_result = await _apply(
        db,
        second,
        email="CANDIDATE.PERSON@EXAMPLE.TEST",
        phone="+992900555101",
        source="linkedin",
        occurred_at=second_applied_at,
    )

    assert first_result.candidate_created is True
    assert retry.candidate_created is False
    assert retry.application_created is False
    assert retry.candidate_id == first_result.candidate_id
    assert retry.application_id == first_result.application_id
    assert second_result.candidate_id == first_result.candidate_id
    assert second_result.application_created is True
    assert second_result.application_id != first_result.application_id

    candidate = await db.get(Candidate, first_result.candidate_id)
    assert candidate is not None
    assert candidate.email_normalized == "candidate.person@example.test"
    assert candidate.phone_normalized == "+992900555101"
    assert int(
        (
            await db.execute(
                select(func.count(CandidateContactPoint.id)).where(
                    CandidateContactPoint.candidate_id == candidate.id
                )
            )
        ).scalar_one()
    ) == 2
    assert int(
        (
            await db.execute(
                select(func.count(Application.id)).where(
                    Application.candidate_id == candidate.id
                )
            )
        ).scalar_one()
    ) == 2
    assert int(
        (
            await db.execute(
                select(func.count(Score.id)).where(Score.candidate_id == candidate.id)
            )
        ).scalar_one()
    ) == 2
    assert int(
        (
            await db.execute(
                select(func.count(Notification.id)).where(
                    Notification.payload["candidate_id"].astext == str(candidate.id)
                )
            )
        ).scalar_one()
    ) == 2
    activities = list(
        (
            await db.execute(
                select(CandidateActivity).where(
                    CandidateActivity.candidate_id == candidate.id
                )
            )
        ).scalars()
    )
    assert sorted(activity.event_type for activity in activities) == [
        "candidate_added",
        "candidate_sourced",
    ]

    first_application = await db.get(Application, first_result.application_id)
    second_application = await db.get(Application, second_result.application_id)
    assert first_application is not None and second_application is not None

    with pytest.raises(HTTPException) as skipped_stage:
        await _transition(
            db,
            user,
            first_application.id,
            "offer",
        )
    assert skipped_stage.value.status_code == 409
    with pytest.raises(ValidationError):
        ApplicationStageTransition(
            stage="rejected",
            expected_version=first_application.updated_at,
        )

    for stage in ("screening", "interview"):
        await _transition(
            db,
            user,
            first_application.id,
            stage,
            occurred_at=transition_times_by_stage[stage],
        )
    await _transition(
        db,
        user,
        first_application.id,
        "rejected",
        disposition_code="not_qualified",
        reason="Experience did not meet the role bar",
        occurred_at=transition_times_by_stage["rejected"],
    )
    with pytest.raises(HTTPException) as terminal:
        await _transition(
            db,
            user,
            first_application.id,
            "new",
        )
    assert terminal.value.status_code == 409
    await _transition(
        db,
        user,
        second_application.id,
        "screening",
        occurred_at=datetime(2026, 1, 2, 21, 0, tzinfo=UTC),
    )

    first_initial = (
        await db.execute(
            select(ApplicationStageEvent).where(
                ApplicationStageEvent.application_id == first_application.id,
                ApplicationStageEvent.from_stage.is_(None),
            )
        )
    ).scalar_one()
    second_initial = (
        await db.execute(
            select(ApplicationStageEvent).where(
                ApplicationStageEvent.application_id == second_application.id,
                ApplicationStageEvent.from_stage.is_(None),
            )
        )
    ).scalar_one()
    assert first_initial.occurred_at == first_applied_at
    assert second_initial.occurred_at == second_applied_at
    db.add(
        ApplicationStageEvent(
            organization_id=organization.id,
            application_id=first_application.id,
            from_stage=None,
            to_stage="new",
            actor_kind="system",
            reason="duplicate_reconciliation_fixture",
            details={
                "source": first_application.source,
                "owner_user_id": str(user.id),
            },
            occurred_at=datetime(2026, 1, 1, 23, 31, tzinfo=UTC),
        )
    )
    # Current state is intentionally corrupted; event-derived analytics must
    # reconstruct screening and reconciliation must expose the mismatch.
    second_application.stage = "interview"
    await db.commit()

    report = await application_analytics(
        db,
        organization.id,
        start_at=datetime(2026, 1, 1, tzinfo=UTC),
        end_at=datetime(2026, 1, 4, tzinfo=UTC),
        timezone_name="Asia/Dushanbe",
    )
    assert report.total_applications == 2
    assert report.unique_candidates == 1
    assert {item.key: item.count for item in report.by_source} == {
        "company_website": 1,
        "linkedin": 1,
    }
    assert {item.key: item.count for item in report.by_owner} == {str(user.id): 2}
    assert {item.key: item.count for item in report.dispositions} == {
        "not_qualified": 1
    }
    stages = {item.stage: item for item in report.stages}
    assert stages["new"].entered == 2
    assert stages["screening"].entered == 2
    assert stages["screening"].conversion_from_previous_percent == 100
    assert stages["interview"].conversion_from_previous_percent == 50
    assert stages["offer"].conversion_from_previous_percent == 0
    assert stages["rejected"].conversion_from_previous_percent is None
    assert stages["new"].median_hours_in_stage == 0.75
    assert stages["rejected"].current == 1
    assert stages["screening"].current == 1
    local_application_day = next(
        item for item in report.daily if item.date.isoformat() == "2026-01-02"
    )
    assert local_application_day.applications == 1
    local_second_application_day = next(
        item for item in report.daily if item.date.isoformat() == "2026-01-03"
    )
    assert local_second_application_day.applications == 1
    assert report.daily[0].date.isoformat() == "2026-01-01"
    assert report.daily[-1].date.isoformat() == "2026-01-04"
    assert report.reconciliation.applications_checked == 2
    assert report.reconciliation.applications_without_initial_event == 0
    assert report.reconciliation.duplicate_initial_events == 1
    assert report.reconciliation.stage_mismatches == 1

    source_report = await application_analytics(
        db,
        organization.id,
        start_at=datetime(2026, 1, 1, tzinfo=UTC),
        end_at=datetime(2026, 1, 4, tzinfo=UTC),
        timezone_name="UTC",
        source="linkedin",
        owner_user_id=user.id,
    )
    assert source_report.total_applications == 1
    assert source_report.unique_candidates == 1

    first_page = await application_analytics(
        db,
        organization.id,
        start_at=datetime(2026, 1, 1, tzinfo=UTC),
        end_at=datetime(2026, 1, 4, tzinfo=UTC),
        timezone_name="UTC",
        drilldown_dimension="all",
        drilldown_limit=1,
    )
    assert first_page.drilldown is not None
    assert first_page.drilldown.total == 2
    assert len(first_page.drilldown.items) == 1
    assert first_page.drilldown.next_cursor is not None
    second_page = await application_analytics(
        db,
        organization.id,
        start_at=datetime(2026, 1, 1, tzinfo=UTC),
        end_at=datetime(2026, 1, 4, tzinfo=UTC),
        timezone_name="UTC",
        drilldown_dimension="all",
        drilldown_cursor=first_page.drilldown.next_cursor,
        drilldown_limit=1,
    )
    assert second_page.drilldown is not None
    assert len(second_page.drilldown.items) == 1
    assert (
        second_page.drilldown.items[0].application_id
        != first_page.drilldown.items[0].application_id
    )
    assert second_page.drilldown.next_cursor is None

    immutable_event = await db.get(ApplicationStageEvent, first_initial.id)
    assert immutable_event is not None
    immutable_event.reason = "tampered"
    with pytest.raises(ValueError, match="append-only"):
        await db.commit()
    await db.rollback()


async def test_offer_analytics_are_attributed_to_the_exact_application_cycle(db):
    organization, user, position, _unused = await _seed(db)
    first_result = await _apply(
        db,
        position,
        email="offer-cycle@example.test",
        phone="+992900000091",
        source="company_website",
        occurred_at=datetime(2026, 1, 1, 12, tzinfo=UTC),
    )
    await _transition(
        db,
        user,
        first_result.application_id,
        "rejected",
        disposition_code="not_qualified",
        reason="Closed before a later application cycle",
        occurred_at=datetime(2026, 1, 2, 12, tzinfo=UTC),
    )
    second_result = await _apply(
        db,
        position,
        email="OFFER-CYCLE@example.test",
        phone="992 900 000 091",
        source="referral",
        occurred_at=datetime(2026, 1, 5, 12, tzinfo=UTC),
    )
    assert second_result.application_id != first_result.application_id

    candidate = await db.get(Candidate, first_result.candidate_id)
    assert candidate is not None
    common = {
        "organization_id": organization.id,
        "candidate_id": candidate.id,
        "position_id": position.id,
        "created_by_user_id": user.id,
        "position_title_snapshot": position.title,
        "subject_template_snapshot": "Offer for {{position}}",
        "body_template_snapshot": "Welcome",
        "subject": "Offer",
        "body": "Welcome",
        "salary_currency": "TJS",
        "expires_at": datetime(2026, 1, 20, 12, tzinfo=UTC),
        "required_approvals": 1,
    }
    db.add_all(
        [
            CandidateOffer(
                **common,
                application_id=first_result.application_id,
                status="declined",
                sent_at=datetime(2026, 1, 2, 13, tzinfo=UTC),
                responded_at=datetime(2026, 1, 3, 13, tzinfo=UTC),
            ),
            CandidateOffer(
                **common,
                application_id=second_result.application_id,
                status="accepted",
                sent_at=datetime(2026, 1, 6, 13, tzinfo=UTC),
                responded_at=datetime(2026, 1, 7, 13, tzinfo=UTC),
            ),
        ]
    )
    await db.commit()

    report = await application_analytics(
        db,
        organization.id,
        start_at=datetime(2026, 1, 5, tzinfo=UTC),
        end_at=datetime(2026, 1, 10, tzinfo=UTC),
        timezone_name="UTC",
        position_id=position.id,
        source="referral",
    )
    assert report.total_applications == 1
    assert report.offers.model_dump() == {
        "sent": 1,
        "accepted": 1,
        "declined": 0,
        "response_rate_percent": 100.0,
        "acceptance_rate_percent": 100.0,
    }


async def test_event_cohort_reconciles_hiring_assessment_offer_and_operations(db):
    organization, user, position, _unused = await _seed(db)
    position.created_at = datetime(2025, 12, 31, 12, tzinfo=UTC)
    await db.commit()
    applied_at = datetime(2026, 1, 1, 12, tzinfo=UTC)
    public_source = public_source_slug("=2+2")
    result = await _apply(
        db,
        position,
        email="analytics.complete@example.test",
        phone="+992900000001",
        source="=2+2",
        occurred_at=applied_at,
    )
    application = await db.get(Application, result.application_id)
    candidate = await db.get(Candidate, result.candidate_id)
    assert application is not None and candidate is not None

    transition_at = {
        "screening": datetime(2026, 1, 2, 12, tzinfo=UTC),
        "interview": datetime(2026, 1, 3, 12, tzinfo=UTC),
        "offer": datetime(2026, 1, 4, 12, tzinfo=UTC),
        "hired": datetime(2026, 1, 5, 12, tzinfo=UTC),
    }
    for stage, occurred_at in transition_at.items():
        await _transition(db, user, application.id, stage, occurred_at=occurred_at)

    offer = CandidateOffer(
        organization_id=organization.id,
        application_id=application.id,
        candidate_id=candidate.id,
        position_id=position.id,
        created_by_user_id=user.id,
        position_title_snapshot=position.title,
        subject_template_snapshot="Offer for {{position}}",
        body_template_snapshot="Welcome",
        subject="Offer",
        body="Welcome",
        salary_currency="TJS",
        expires_at=datetime(2026, 1, 9, 12, tzinfo=UTC),
        required_approvals=1,
        status="accepted",
        sent_at=datetime(2026, 1, 4, 13, tzinfo=UTC),
        responded_at=datetime(2026, 1, 5, 13, tzinfo=UTC),
        signature_name="Candidate",
        created_at=datetime(2026, 1, 4, 12, tzinfo=UTC),
        updated_at=datetime(2026, 1, 5, 13, tzinfo=UTC),
    )
    template = AssessmentTemplate(
        organization_id=organization.id,
        created_by_user_id=user.id,
        name="Analytics assessment",
        status="active",
        created_at=datetime(2026, 1, 1, 13, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, 13, tzinfo=UTC),
    )
    db.add_all([offer, template])
    await db.flush()
    version = AssessmentTemplateVersion(
        organization_id=organization.id,
        template_id=template.id,
        version=1,
        title="Analytics assessment v1",
        passing_percentage=60,
        time_limit_minutes=60,
        created_by_user_id=user.id,
        published_at=datetime(2026, 1, 1, 14, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, 14, tzinfo=UTC),
    )
    db.add(version)
    await db.flush()
    invitation = AssessmentInvitation(
        organization_id=organization.id,
        application_id=application.id,
        template_version_id=version.id,
        token_digest="a" * 64,
        token_prefix="analytics",
        recipient_email="analytics.complete@example.test",
        status="completed",
        expires_at=datetime(2026, 1, 9, 12, tzinfo=UTC),
        max_attempts=1,
        authorized_attempts=1,
        accommodations={},
        invited_by_user_id=user.id,
        first_opened_at=datetime(2026, 1, 2, 9, tzinfo=UTC),
        completed_at=datetime(2026, 1, 2, 10, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, 15, tzinfo=UTC),
        updated_at=datetime(2026, 1, 2, 10, tzinfo=UTC),
    )
    db.add(invitation)
    await db.flush()
    attempt = AssessmentAttempt(
        organization_id=organization.id,
        invitation_id=invitation.id,
        application_id=application.id,
        template_version_id=version.id,
        sequence=1,
        status="submitted",
        started_at=datetime(2026, 1, 2, 9, tzinfo=UTC),
        last_saved_at=datetime(2026, 1, 2, 9, 30, tzinfo=UTC),
        expires_at=datetime(2026, 1, 2, 11, tzinfo=UTC),
        submitted_at=datetime(2026, 1, 2, 10, tzinfo=UTC),
        created_at=datetime(2026, 1, 2, 9, tzinfo=UTC),
        updated_at=datetime(2026, 1, 2, 10, tzinfo=UTC),
    )
    connected = IntegrationConnection(
        organization_id=organization.id,
        created_by_user_id=user.id,
        provider="google_workspace",
        name="Analytics Google",
        status="connected",
        capabilities=["calendar.write"],
    )
    degraded = IntegrationConnection(
        organization_id=organization.id,
        created_by_user_id=user.id,
        provider="telegram",
        name="Analytics Telegram",
        status="degraded",
        capabilities=["message.send"],
        consecutive_failures=2,
    )
    provider_connection = IntegrationConnection(
        organization_id=organization.id,
        created_by_user_id=user.id,
        provider="assessment_generic",
        name="Analytics assessment provider",
        status="connected",
        capabilities=["assessment.invite", "assessment.status", "assessment.result"],
        configuration={"provider_label": "Specialist analytics"},
    )
    db.add(provider_connection)
    await db.flush()
    provider_jobs = [
        IntegrationJob(
            organization_id=organization.id,
            connection_id=provider_connection.id,
            created_by_user_id=user.id,
            operation="assessment.invite",
            status="succeeded",
            idempotency_key=f"analytics-provider-{index}",
            request_fingerprint=(str(index) * 64)[:64],
            reference_payload={"application_id": str(application.id)},
            next_attempt_at=datetime(2026, 1, 1, 16 + index, tzinfo=UTC),
            completed_at=datetime(2026, 1, 1, 16 + index, tzinfo=UTC),
            created_at=datetime(2026, 1, 1, 16 + index, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, 16 + index, tzinfo=UTC),
        )
        for index in (1, 2)
    ]
    db.add_all(provider_jobs)
    await db.flush()
    provider_completed = ProviderAssessment(
        organization_id=organization.id,
        connection_id=provider_connection.id,
        application_id=application.id,
        candidate_id=candidate.id,
        invitation_job_id=provider_jobs[0].id,
        created_by_user_id=user.id,
        external_template_ref="analytics-provider-complete",
        status="graded",
        expires_at=datetime(2026, 1, 9, 12, tzinfo=UTC),
        provider_updated_at=datetime(2026, 1, 2, 12, tzinfo=UTC),
        started_at=datetime(2026, 1, 2, 9, 15, tzinfo=UTC),
        submitted_at=datetime(2026, 1, 2, 10, 15, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, 17, tzinfo=UTC),
        updated_at=datetime(2026, 1, 2, 12, tzinfo=UTC),
    )
    provider_dropoff = ProviderAssessment(
        organization_id=organization.id,
        connection_id=provider_connection.id,
        application_id=application.id,
        candidate_id=candidate.id,
        invitation_job_id=provider_jobs[1].id,
        created_by_user_id=user.id,
        external_template_ref="analytics-provider-dropoff",
        status="started",
        expires_at=datetime(2026, 1, 9, 12, tzinfo=UTC),
        provider_updated_at=datetime(2026, 1, 3, 9, tzinfo=UTC),
        started_at=datetime(2026, 1, 3, 9, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, 18, tzinfo=UTC),
        updated_at=datetime(2026, 1, 3, 9, tzinfo=UTC),
    )
    call_id = uuid4()
    token_usage = UsageLedgerEntry(
        organization_id=organization.id,
        metric="ai_tokens",
        amount=15,
        kind="settle",
        idempotency_key="analytics-ai-tokens",
        resource_type="llm_call",
        resource_id=call_id,
        metadata_json={"provider": "openrouter"},
        occurred_at=datetime(2026, 1, 3, 10, tzinfo=UTC),
    )
    cost_usage = UsageLedgerEntry(
        organization_id=organization.id,
        metric="ai_cost_microusd",
        amount=123,
        kind="settle",
        idempotency_key="analytics-ai-cost",
        resource_type="llm_call",
        resource_id=call_id,
        metadata_json={"provider": "openrouter"},
        occurred_at=datetime(2026, 1, 3, 10, tzinfo=UTC),
    )
    db.add_all(
        [
            attempt,
            connected,
            degraded,
            provider_completed,
            provider_dropoff,
            token_usage,
            cost_usage,
        ]
    )
    await db.commit()

    report = await application_analytics(
        db,
        organization.id,
        start_at=datetime(2026, 1, 1, tzinfo=UTC),
        end_at=datetime(2026, 1, 10, tzinfo=UTC),
        timezone_name="Asia/Dushanbe",
        position_id=position.id,
        source=public_source,
        owner_user_id=user.id,
        include_organization_operations=True,
        drilldown_dimension="stage_current",
        drilldown_key="hired",
    )
    assert report.total_applications == 1
    assert report.timing.hired_applications == 1
    assert report.timing.filled_positions == 1
    assert report.timing.median_hours_to_hire == 96
    assert report.timing.median_hours_to_fill == 120
    assert report.offers.model_dump() == {
        "sent": 1,
        "accepted": 1,
        "declined": 0,
        "response_rate_percent": 100.0,
        "acceptance_rate_percent": 100.0,
    }
    assert report.assessments.invitations == 3
    assert report.assessments.started == 3
    assert report.assessments.completed == 2
    assert report.assessments.dropped_off == 1
    assert report.owner_workload[0].active == 0
    assert report.owner_workload[0].terminal == 1
    assert report.integration_health.total == 3
    assert report.integration_health.connected == 2
    assert report.integration_health.attention_required == 1
    assert report.ai_usage.calls == 1
    assert report.ai_usage.tokens == 15
    assert report.ai_usage.cost_microusd == 123
    assert report.ai_usage.by_provider[0].provider == "openrouter"
    assert report.drilldown is not None
    assert report.drilldown.total == 1
    assert report.drilldown.items[0].application_id == application.id

    before_any_attempt = await application_analytics(
        db,
        organization.id,
        start_at=datetime(2026, 1, 1, tzinfo=UTC),
        end_at=datetime(2026, 1, 2, 8, tzinfo=UTC),
        timezone_name="UTC",
        position_id=position.id,
        source=public_source,
        owner_user_id=user.id,
    )
    assert before_any_attempt.assessments.invitations == 3
    assert before_any_attempt.assessments.started == 0
    assert before_any_attempt.assessments.completed == 0
    assert before_any_attempt.assessments.dropped_off == 0

    csv_export = application_analytics_csv(report)
    assert csv_export.startswith("\ufeffsection,key,label")
    assert "=2+2" not in csv_export
    assert public_source in csv_export
    assert str(application.id) in csv_export
    assert "native_and_provider" in csv_export


async def test_pipeline_http_filters_and_field_protected_export(db):
    organization, recruiter, position, _unused = await _seed(db)
    manager = User(
        organization_id=organization.id,
        email=f"analytics-manager-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="hiring_manager",
        locale="en",
    )
    db.add(manager)
    await db.commit()
    await _apply(
        db,
        position,
        email="analytics-http@example.test",
        phone="+992900000002",
        source="company_website",
        occurred_at=datetime(2026, 2, 10, 20, tzinfo=UTC),
    )
    direct = await application_analytics(
        db,
        organization.id,
        start_at=datetime(2026, 2, 9, 19, tzinfo=UTC),
        end_at=datetime(2026, 2, 11, 19, tzinfo=UTC),
        timezone_name="Asia/Dushanbe",
    )
    assert direct.total_applications == 1
    query = (
        "start_date=2026-02-10&end_date=2026-02-11&"
        "timezone=Asia%2FDushanbe&drilldown_dimension=all"
    )

    def authorization(user: User) -> dict[str, str]:
        return {
            "Authorization": "Bearer "
            + encode_access_token(user.id, user.role, user.locale)
        }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://analytics.test",
    ) as client:
        report = await client.get(
            f"/analytics/pipeline?{query}", headers=authorization(manager)
        )
        recruiter_report = await client.get(
            f"/analytics/pipeline?{query}", headers=authorization(recruiter)
        )
        denied_export = await client.get(
            f"/analytics/pipeline?{query}&format=csv",
            headers=authorization(manager),
        )
        export = await client.get(
            f"/analytics/pipeline?{query}&format=csv",
            headers=authorization(recruiter),
        )

    assert report.status_code == 200
    assert report.json()["total_applications"] == 0
    assert report.json()["integration_health"] is None
    assert report.json()["ai_usage"] is None
    assert recruiter_report.status_code == 200
    assert recruiter_report.json()["total_applications"] == 1
    assert recruiter_report.json()["drilldown"]["total"] == 1
    assert recruiter_report.json()["integration_health"] is not None
    assert recruiter_report.json()["ai_usage"] is not None
    assert denied_export.status_code == 403
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    assert export.headers["cache-control"] == "private, no-store, max-age=0"
    assert "attachment; filename=" in export.headers["content-disposition"]
    assert export.content.startswith(b"\xef\xbb\xbfsection,key,label")
    audit = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.organization_id == organization.id,
                AuditLog.action == "export_application_analytics",
            )
        )
    ).scalar_one()
    assert audit.actor_user_id == recruiter.id
    assert audit.metadata_json["application_count"] == 1
