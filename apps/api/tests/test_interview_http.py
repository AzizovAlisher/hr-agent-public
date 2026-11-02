"""Real HTTP coverage for manual interview kits and assigned feedback."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from app.auth.jwt import encode_access_token
from app.main import app
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.interview import InterviewKitVersion
from app.models.organization import Organization
from app.models.position import Position
from app.models.user import User

pytestmark = pytest.mark.asyncio


def _auth(user: User) -> dict[str, str]:
    token = encode_access_token(
        user.id,
        user.role,
        user.locale,
        token_version=user.token_version,
    )
    return {"Authorization": f"Bearer {token}"}


async def _seed(db):
    organization = Organization(
        name="Manual interview tenant",
        slug=f"manual-interviews-{uuid4().hex[:10]}",
    )
    other_organization = Organization(
        name="Other manual interview tenant",
        slug=f"other-manual-interviews-{uuid4().hex[:10]}",
    )
    db.add_all([organization, other_organization])
    await db.flush()
    recruiter = User(
        organization_id=organization.id,
        email=f"interview-author-{uuid4().hex[:10]}@example.test",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    interviewer = User(
        organization_id=organization.id,
        email=f"assigned-interviewer-{uuid4().hex[:10]}@example.test",
        password_hash="unused",
        role="interviewer",
        locale="en",
    )
    unassigned = User(
        organization_id=organization.id,
        email=f"unassigned-interviewer-{uuid4().hex[:10]}@example.test",
        password_hash="unused",
        role="interviewer",
        locale="en",
    )
    outsider = User(
        organization_id=other_organization.id,
        email=f"outside-interview-author-{uuid4().hex[:10]}@example.test",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    db.add_all([recruiter, interviewer, unassigned, outsider])
    await db.flush()
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="manual-interview-candidate",
        raw_cv="Python and PostgreSQL delivery evidence",
    )
    position = Position(
        organization_id=organization.id,
        title="Platform engineer",
        status="open",
        created_by=recruiter.id,
    )
    wrong_position = Position(
        organization_id=organization.id,
        title="Unrelated role",
        status="open",
        created_by=recruiter.id,
    )
    other_candidate = Candidate(
        organization_id=other_organization.id,
        source="manual",
        source_ref="other-manual-interview-candidate",
    )
    other_position = Position(
        organization_id=other_organization.id,
        title="Other platform engineer",
        status="open",
        created_by=outsider.id,
    )
    db.add_all([candidate, position, wrong_position, other_candidate, other_position])
    await db.flush()
    application = Application(
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        stage="interview",
        source="manual",
        cycle_number=1,
        created_by_user_id=recruiter.id,
    )
    historical_application = Application(
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        stage="rejected",
        source="manual",
        cycle_number=2,
        closed_at=datetime.now(UTC),
        created_by_user_id=recruiter.id,
    )
    other_application = Application(
        organization_id=other_organization.id,
        candidate_id=other_candidate.id,
        position_id=other_position.id,
        stage="interview",
        source="manual",
        created_by_user_id=outsider.id,
    )
    db.add_all([application, historical_application, other_application])
    await db.commit()
    return {
        "recruiter": recruiter,
        "interviewer": interviewer,
        "unassigned": unassigned,
        "outsider": outsider,
        "candidate": candidate,
        "position": position,
        "wrong_position": wrong_position,
        "application": application,
        "historical_application": historical_application,
        "other_application": other_application,
    }


def _questions(prefix: str) -> list[dict]:
    return [
        {
            "category": "technical",
            "question_text": f"{prefix}: explain a concrete PostgreSQL tradeoff",
            "rationale": "Tests the role requirement",
            "expected_signals": "Specific evidence and constraints",
            "is_required": True,
        },
        {
            "category": "behavioral",
            "question_text": f"{prefix}: describe a production incident",
            "is_required": True,
        },
        {
            "category": "culture",
            "question_text": f"{prefix}: describe a preferred feedback practice",
            "is_required": False,
        },
    ]


async def test_manual_versioned_kit_and_assigned_feedback_over_http(db):
    seeded = await _seed(db)
    recruiter = seeded["recruiter"]
    interviewer = seeded["interviewer"]
    unassigned = seeded["unassigned"]
    outsider = seeded["outsider"]
    candidate = seeded["candidate"]
    position = seeded["position"]
    application = seeded["application"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        closed_cycle = await client.post(
            f"/candidates/{candidate.id}/interviews",
            headers=_auth(recruiter),
            json={
                "application_id": str(seeded["historical_application"].id),
                "position_id": str(position.id),
                "interviewer_user_ids": [str(interviewer.id)],
                "questions": _questions("closed"),
            },
        )
        mismatched_position = await client.post(
            f"/candidates/{candidate.id}/interviews",
            headers=_auth(recruiter),
            json={
                "application_id": str(application.id),
                "position_id": str(seeded["wrong_position"].id),
                "interviewer_user_ids": [str(interviewer.id)],
                "questions": _questions("wrong-position"),
            },
        )
        cross_tenant_application = await client.post(
            f"/candidates/{candidate.id}/interviews",
            headers=_auth(recruiter),
            json={
                "application_id": str(seeded["other_application"].id),
                "position_id": str(position.id),
                "interviewer_user_ids": [str(interviewer.id)],
                "questions": _questions("cross-tenant"),
            },
        )
        created = await client.post(
            f"/candidates/{candidate.id}/interviews",
            headers=_auth(recruiter),
            json={
                "application_id": str(application.id),
                "position_id": str(position.id),
                "interviewer_user_ids": [str(interviewer.id)],
                "questions": _questions("version-one"),
                "notes": "Structured interview plan",
            },
        )

        assert closed_cycle.status_code == 409
        assert mismatched_position.status_code == 409
        assert cross_tenant_application.status_code == 404
        assert created.status_code == 201, created.text
        created_body = created.json()
        interview_id = created_body["id"]
        assert created_body["application_id"] == str(application.id)
        assert created_body["kit_version"] == 1
        assert created_body["interviewer_user_ids"] == [str(interviewer.id)]

        revised = await client.post(
            f"/interviews/{interview_id}/kit-versions",
            headers=_auth(recruiter),
            json={"expected_version": 1, "questions": _questions("version-two")},
        )
        stale_revision = await client.post(
            f"/interviews/{interview_id}/kit-versions",
            headers=_auth(recruiter),
            json={"expected_version": 1, "questions": _questions("stale")},
        )
        assert revised.status_code == 201, revised.text
        assert revised.json()["kit_version"] == 2
        assert stale_revision.status_code == 409

        assigned_view = await client.get(
            f"/interviews/{interview_id}", headers=_auth(interviewer)
        )
        unassigned_view = await client.get(
            f"/interviews/{interview_id}", headers=_auth(unassigned)
        )
        cross_tenant_view = await client.get(
            f"/interviews/{interview_id}", headers=_auth(outsider)
        )
        assert assigned_view.status_code == 200
        assert unassigned_view.status_code == 404
        assert cross_tenant_view.status_code == 404
        assert all(
            question["question_text"].startswith("version-two:")
            for question in assigned_view.json()["questions"]
        )

        interviewer_cannot_author = await client.patch(
            f"/interviews/{interview_id}",
            headers=_auth(interviewer),
            json={"notes": "Must not bypass authoring policy"},
        )
        incomplete = await client.post(
            f"/interviews/{interview_id}/complete",
            headers=_auth(interviewer),
            json={"notes": "Incomplete"},
        )
        unassigned_score = await client.patch(
            f"/interviews/{interview_id}/questions/"
            f"{assigned_view.json()['questions'][0]['id']}",
            headers=_auth(unassigned),
            json={"score": 5, "notes": "Must remain hidden"},
        )
        assert interviewer_cannot_author.status_code == 403
        assert incomplete.status_code == 422
        assert unassigned_score.status_code == 404

        for question in assigned_view.json()["questions"]:
            if not question["is_required"]:
                continue
            scored = await client.patch(
                f"/interviews/{interview_id}/questions/{question['id']}",
                headers=_auth(interviewer),
                json={"score": 4, "notes": "Observed structured evidence"},
            )
            assert scored.status_code == 200, scored.text

        completed = await client.post(
            f"/interviews/{interview_id}/complete",
            headers=_auth(interviewer),
            json={"notes": "All required evidence reviewed"},
        )
        idempotent_complete = await client.post(
            f"/interviews/{interview_id}/complete",
            headers=_auth(interviewer),
            json={"notes": "All required evidence reviewed"},
        )
        changed_complete = await client.post(
            f"/interviews/{interview_id}/complete",
            headers=_auth(interviewer),
            json={"notes": "Changed after submission"},
        )
        rescore = await client.patch(
            f"/interviews/{interview_id}/questions/"
            f"{assigned_view.json()['questions'][0]['id']}",
            headers=_auth(interviewer),
            json={"score": 1},
        )
        revise_after_feedback = await client.post(
            f"/interviews/{interview_id}/kit-versions",
            headers=_auth(recruiter),
            json={"expected_version": 2, "questions": _questions("too-late")},
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "scored"
        assert completed.json()["overall_score"] == 4.0
        assert idempotent_complete.status_code == 200
        assert changed_complete.status_code == 409
        assert rescore.status_code == 409
        assert revise_after_feedback.status_code == 409

    db.expire_all()
    versions = list(
        (
            await db.execute(
                select(InterviewKitVersion)
                .where(InterviewKitVersion.interview_id == UUID(interview_id))
                .order_by(InterviewKitVersion.version)
            )
        ).scalars()
    )
    assert [version.version for version in versions] == [1, 2]
    assert versions[0].questions_snapshot[0]["question_text"].startswith("version-one:")
    assert versions[1].questions_snapshot[0]["question_text"].startswith("version-two:")
