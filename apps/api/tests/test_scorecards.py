"""PostgreSQL integration tests for blind scorecards and hiring decisions."""

from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.auth.jwt import encode_access_token
from app.main import app
from app.models.application import Application, ApplicationStageEvent
from app.models.candidate import Candidate
from app.models.candidate_activity import CandidateActivity
from app.models.notification import Notification
from app.models.organization import Organization
from app.models.position import Position
from app.models.score import Score
from app.models.scorecard import (
    HiringDecision,
    ScorecardAssignment,
    ScorecardRating,
    ScorecardTemplate,
    ScorecardTemplateVersion,
)
from app.models.user import User
from app.routers.scorecards import (
    assign_candidate_scorecards,
    create_or_update_hiring_decision,
    create_scorecard_template,
    get_candidate_scorecards,
    submit_scorecard,
    update_scorecard_draft,
    update_scorecard_template,
    vote_hiring_decision,
)
from app.schemas.scorecards import (
    HiringDecisionCreate,
    HiringDecisionVoteRequest,
    ScorecardAssignRequest,
    ScorecardCriterionCreate,
    ScorecardDraftUpdate,
    ScorecardRatingInput,
    ScorecardTemplateCreate,
    ScorecardTemplateUpdate,
)
from app.services.applications import ensure_application_link
from app.services.candidate_merge import merge_candidates

pytestmark = pytest.mark.asyncio


def auth(user: User) -> dict[str, str]:
    token = encode_access_token(
        user.id,
        user.role,
        user.locale,
        token_version=user.token_version,
    )
    return {"Authorization": f"Bearer {token}"}


async def seed_pipeline(db, suffix: str):
    organization = Organization(
        name=f"Scorecard org {suffix}",
        slug=f"scorecards-{suffix}-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    creator = User(
        organization_id=organization.id,
        email=f"creator-{suffix}-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    reviewer_one = User(
        organization_id=organization.id,
        email=f"reviewer-one-{suffix}-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="hiring_manager",
        locale="en",
    )
    reviewer_two = User(
        organization_id=organization.id,
        email=f"reviewer-two-{suffix}-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="hiring_manager",
        locale="en",
    )
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"scorecard-candidate-{suffix}",
        raw_cv="A sufficiently detailed CV for scorecard integration tests. " * 2,
    )
    position = Position(
        organization_id=organization.id,
        title=f"Platform Engineer {suffix}",
        status="open",
        created_by=creator.id,
    )
    db.add_all([creator, reviewer_one, reviewer_two, candidate, position])
    await db.flush()
    application = Score(
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        overall_score=82,
        stage="screening",
    )
    db.add(application)
    await db.commit()
    return (
        organization,
        creator,
        reviewer_one,
        reviewer_two,
        candidate,
        position,
        application,
    )


async def make_template(db, user: User, *, name: str = "Engineering interview"):
    return await create_scorecard_template(
        ScorecardTemplateCreate(
            name=name,
            description="A structured rubric",
            criteria=[
                ScorecardCriterionCreate(
                    key="technical_depth",
                    label="Technical depth",
                    weight=2,
                    is_required=True,
                ),
                ScorecardCriterionCreate(
                    key="collaboration",
                    label="Collaboration",
                    weight=1,
                    is_required=True,
                ),
            ],
        ),
        db=db,
        user=user,
    )


async def assign_reviewers(
    db,
    *,
    candidate_id: UUID,
    position_id: UUID,
    template_id: UUID,
    reviewer_ids: list[UUID],
    actor: User,
):
    return await assign_candidate_scorecards(
        candidate_id,
        ScorecardAssignRequest(
            position_id=position_id,
            template_id=template_id,
            user_ids=reviewer_ids,
        ),
        db=db,
        user=actor,
    )


async def rate_assignment(
    db,
    *,
    assignment_id: UUID,
    criteria,
    reviewer: User,
    values: tuple[int, int],
    comment_prefix: str,
):
    return await update_scorecard_draft(
        assignment_id,
        ScorecardDraftUpdate(
            ratings=[
                ScorecardRatingInput(
                    criterion_id=criteria[0].id,
                    rating=values[0],
                    comment=f"{comment_prefix} technical comment",
                ),
                ScorecardRatingInput(
                    criterion_id=criteria[1].id,
                    rating=values[1],
                    comment=f"{comment_prefix} collaboration comment",
                ),
            ]
        ),
        db=db,
        user=reviewer,
    )


async def activity_count(db, candidate_id: UUID, *event_types: str) -> int:
    stmt = select(func.count(CandidateActivity.id)).where(
        CandidateActivity.candidate_id == candidate_id
    )
    if event_types:
        stmt = stmt.where(CandidateActivity.event_type.in_(event_types))
    return int((await db.execute(stmt)).scalar_one())


async def test_scorecard_tenant_isolation_and_org_scoped_template_names(db):
    (
        org_one,
        creator_one,
        _reviewer_one,
        _teammate_one,
        candidate_one,
        position_one,
        _application_one,
    ) = await seed_pipeline(db, "tenant-one")
    (
        org_two,
        creator_two,
        reviewer_two,
        _teammate_two,
        candidate_two,
        position_two,
        _application_two,
    ) = await seed_pipeline(db, "tenant-two")
    assert org_one.id != org_two.id

    template_one = await make_template(db, creator_one, name="Core rubric")
    template_two = await make_template(db, creator_two, name="Core rubric")
    assert template_one.id != template_two.id

    creator_one_id = creator_one.id
    creator_two_id = creator_two.id
    candidate_one_id = candidate_one.id
    candidate_two_id = candidate_two.id
    position_one_id = position_one.id
    position_two_id = position_two.id
    reviewer_two_id = reviewer_two.id
    org_one_id = org_one.id
    with pytest.raises(HTTPException) as duplicate_name:
        await make_template(db, creator_one, name="  core RUBRIC  ")
    assert duplicate_name.value.status_code == 409

    creator_one = await db.get(User, creator_one_id)
    creator_two = await db.get(User, creator_two_id)
    assert creator_one is not None and creator_two is not None
    with pytest.raises(HTTPException) as hidden_candidate:
        await get_candidate_scorecards(
            candidate_one_id,
            position_id=None,
            db=db,
            user=creator_two,
        )
    assert hidden_candidate.value.status_code == 404
    await db.rollback()

    creator_one = await db.get(User, creator_one_id)
    assert creator_one is not None
    with pytest.raises(HTTPException) as hidden_position:
        await assign_reviewers(
            db,
            candidate_id=candidate_one_id,
            position_id=position_two_id,
            template_id=template_one.id,
            reviewer_ids=[reviewer_two_id],
            actor=creator_one,
        )
    assert hidden_position.value.status_code == 404
    await db.rollback()

    creator_one = await db.get(User, creator_one_id)
    assert creator_one is not None
    with pytest.raises(HTTPException) as hidden_assignee:
        await assign_reviewers(
            db,
            candidate_id=candidate_one_id,
            position_id=position_one_id,
            template_id=template_one.id,
            reviewer_ids=[reviewer_two_id],
            actor=creator_one,
        )
    assert hidden_assignee.value.status_code == 404
    await db.rollback()

    assert (
        await db.execute(
            select(func.count(ScorecardAssignment.id)).where(
                ScorecardAssignment.candidate_id == candidate_one_id
            )
        )
    ).scalar_one() == 0
    assert await activity_count(db, candidate_one_id, "scorecard_assigned") == 0
    assert (
        await db.execute(
            select(func.count(Notification.id)).where(
                Notification.type == "scorecard_assigned",
                Notification.organization_id == org_one_id,
            )
        )
    ).scalar_one() == 0

    # Sanity check that the second tenant's own candidate remained accessible.
    creator_two = await db.get(User, creator_two_id)
    assert creator_two is not None
    own_panel = await get_candidate_scorecards(
        candidate_two_id,
        position_id=position_two_id,
        db=db,
        user=creator_two,
    )
    assert own_panel.assignment_count == 0


async def test_blind_feedback_required_validation_and_idempotent_submit(db):
    (
        _organization,
        creator,
        reviewer_one,
        reviewer_two,
        candidate,
        position,
        _application,
    ) = await seed_pipeline(db, "blind")
    template = await make_template(db, creator)
    candidate_id = candidate.id
    position_id = position.id
    panel = await assign_reviewers(
        db,
        candidate_id=candidate_id,
        position_id=position_id,
        template_id=template.id,
        reviewer_ids=[reviewer_one.id, reviewer_two.id],
        actor=creator,
    )
    assignments = {
        item.assigned_to_user_id: item for item in panel.assignments
    }
    assignment_one = assignments[reviewer_one.id]
    assignment_two = assignments[reviewer_two.id]

    await rate_assignment(
        db,
        assignment_id=assignment_one.id,
        criteria=template.criteria,
        reviewer=reviewer_one,
        values=(5, 4),
        comment_prefix="private-one",
    )
    own_panel = await submit_scorecard(
        assignment_one.id, db=db, user=reviewer_one
    )
    assert own_panel.revealed is False
    assert next(item for item in own_panel.assignments if item.id == assignment_one.id).ratings
    assert (
        next(item for item in own_panel.assignments if item.id == assignment_two.id).ratings
        is None
    )

    recruiter_panel = await get_candidate_scorecards(
        candidate_id,
        position_id=position_id,
        db=db,
        user=creator,
    )
    assert recruiter_panel.revealed is False
    assert all(item.ratings is None for item in recruiter_panel.assignments)

    await update_scorecard_draft(
        assignment_two.id,
        ScorecardDraftUpdate(
            ratings=[
                ScorecardRatingInput(
                    criterion_id=template.criteria[0].id,
                    rating=3,
                    comment="private-two partial",
                )
            ]
        ),
        db=db,
        user=reviewer_two,
    )
    reviewer_two_id = reviewer_two.id
    with pytest.raises(HTTPException) as incomplete:
        await submit_scorecard(assignment_two.id, db=db, user=reviewer_two)
    assert incomplete.value.status_code == 422
    await db.rollback()

    reviewer_two = await db.get(User, reviewer_two_id)
    assert reviewer_two is not None
    await update_scorecard_draft(
        assignment_two.id,
        ScorecardDraftUpdate(
            ratings=[
                ScorecardRatingInput(
                    criterion_id=template.criteria[1].id,
                    rating=2,
                    comment="private-two completed",
                )
            ]
        ),
        db=db,
        user=reviewer_two,
    )
    revealed = await submit_scorecard(
        assignment_two.id, db=db, user=reviewer_two
    )
    assert revealed.revealed is True
    assert revealed.completion_count == 2
    assert revealed.assignment_count == 2
    assert revealed.aggregate_average == 3.67
    assert all(item.ratings is not None for item in revealed.assignments)

    before_retry = await activity_count(db, candidate_id, "scorecard_submitted")
    retried = await submit_scorecard(
        assignment_two.id, db=db, user=reviewer_two
    )
    assert retried.revealed is True
    assert await activity_count(db, candidate_id, "scorecard_submitted") == before_retry

    activities = list(
        (
            await db.execute(
                select(CandidateActivity).where(
                    CandidateActivity.candidate_id == candidate_id
                )
            )
        ).scalars()
    )
    serialized_payloads = str([activity.payload for activity in activities]).lower()
    assert "private-one" not in serialized_payloads
    assert "private-two" not in serialized_payloads
    assert "comment" not in serialized_payloads


async def test_scorecard_application_links_are_tenant_and_pair_safe(db):
    (
        organization,
        creator,
        reviewer,
        _other_reviewer,
        candidate,
        position,
        _score,
    ) = await seed_pipeline(db, "application-link")
    (
        _other_organization,
        _other_creator,
        _other_reviewer_one,
        _other_reviewer_two,
        other_candidate,
        other_position,
        _other_score,
    ) = await seed_pipeline(db, "application-link-other-tenant")
    cross_tenant_link = await ensure_application_link(
        db,
        organization_id=other_candidate.organization_id,
        candidate_id=other_candidate.id,
        position_id=other_position.id,
        source="manual",
        owner_user_id=None,
        actor_user_id=None,
        actor_kind="system",
        reason="test_cross_tenant_link",
    )

    second_candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"second-{uuid4().hex}",
        raw_cv="Second candidate",
    )
    second_position = Position(
        organization_id=organization.id,
        title="Second position",
        status="open",
        created_by=creator.id,
    )
    db.add_all([second_candidate, second_position])
    await db.flush()
    db.add(
        Score(
            organization_id=organization.id,
            candidate_id=second_candidate.id,
            position_id=second_position.id,
            stage="screening",
        )
    )
    same_tenant_wrong_pair = await ensure_application_link(
        db,
        organization_id=organization.id,
        candidate_id=second_candidate.id,
        position_id=second_position.id,
        source="manual",
        owner_user_id=None,
        actor_user_id=creator.id,
        actor_kind="human",
        reason="test_wrong_pair_link",
    )
    template = await make_template(db, creator, name="Application-safe rubric")

    with pytest.raises(HTTPException) as cross_tenant:
        await assign_candidate_scorecards(
            candidate.id,
            ScorecardAssignRequest(
                position_id=position.id,
                application_id=cross_tenant_link.application.id,
                template_id=template.id,
                user_ids=[reviewer.id],
            ),
            db=db,
            user=creator,
        )
    assert cross_tenant.value.status_code == 404
    with pytest.raises(HTTPException) as wrong_pair:
        await assign_candidate_scorecards(
            candidate.id,
            ScorecardAssignRequest(
                position_id=position.id,
                application_id=same_tenant_wrong_pair.application.id,
                template_id=template.id,
                user_ids=[reviewer.id],
            ),
            db=db,
            user=creator,
        )
    assert wrong_pair.value.status_code == 409

    valid_link = await ensure_application_link(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        source="manual",
        owner_user_id=None,
        actor_user_id=creator.id,
        actor_kind="human",
        reason="test_valid_pair_link",
    )
    panel = await assign_candidate_scorecards(
        candidate.id,
        ScorecardAssignRequest(
            position_id=position.id,
            application_id=valid_link.application.id,
            template_id=template.id,
            user_ids=[reviewer.id],
        ),
        db=db,
        user=creator,
    )
    assert panel.assignments[0].application_id == valid_link.application.id


async def test_template_revisions_do_not_change_assigned_scorecards(db):
    (
        _organization,
        creator,
        reviewer,
        _other_reviewer,
        candidate,
        position,
        _score,
    ) = await seed_pipeline(db, "template-version")
    template = await make_template(db, creator, name="Versioned rubric")
    panel = await assign_reviewers(
        db,
        candidate_id=candidate.id,
        position_id=position.id,
        template_id=template.id,
        reviewer_ids=[reviewer.id],
        actor=creator,
    )
    assignment = panel.assignments[0]
    original_ids = [criterion.id for criterion in assignment.criteria]
    original_labels = [criterion.label for criterion in assignment.criteria]

    revised = await update_scorecard_template(
        template.id,
        ScorecardTemplateUpdate(
            name="Versioned rubric v2",
            criteria=[
                ScorecardCriterionCreate(
                    key="technical_depth",
                    label="Technical evidence",
                    weight=3,
                    is_required=True,
                ),
                ScorecardCriterionCreate(
                    key="communication",
                    label="Communication",
                    weight=1,
                    is_required=False,
                ),
            ],
        ),
        db=db,
        user=creator,
    )
    assert revised.current_version == 2
    assert [criterion.label for criterion in revised.criteria] != original_labels

    draft = await update_scorecard_draft(
        assignment.id,
        ScorecardDraftUpdate(
            ratings=[
                ScorecardRatingInput(
                    criterion_id=original_ids[0], rating=5, comment="Strong evidence"
                ),
                ScorecardRatingInput(
                    criterion_id=original_ids[1], rating=4, comment="Collaborative"
                ),
            ]
        ),
        db=db,
        user=reviewer,
    )
    assert draft.template_version == 1
    assert [criterion.label for criterion in draft.criteria] == original_labels
    submitted = await submit_scorecard(assignment.id, db=db, user=reviewer)
    assert submitted.revealed is True

    version_one = (
        await db.execute(
            select(ScorecardTemplateVersion).where(
                ScorecardTemplateVersion.template_id == template.id,
                ScorecardTemplateVersion.version == 1,
            )
        )
    ).scalar_one()
    version_one.name_snapshot = "tampered"
    with pytest.raises(ValueError, match="append-only"):
        await db.commit()
    await db.rollback()


async def test_decision_approval_history_no_ops_and_advance_stage_once(db):
    (
        _organization,
        creator,
        reviewer_one,
        reviewer_two,
        candidate,
        position,
        application,
    ) = await seed_pipeline(db, "decision")
    candidate_id = candidate.id
    position_id = position.id
    application_id = application.id
    creator_id = creator.id
    reviewer_two_id = reviewer_two.id
    template = await make_template(db, creator)
    panel = await assign_reviewers(
        db,
        candidate_id=candidate_id,
        position_id=position_id,
        template_id=template.id,
        reviewer_ids=[reviewer_one.id, reviewer_two.id],
        actor=creator,
    )
    by_user = {item.assigned_to_user_id: item for item in panel.assignments}
    for reviewer, values in ((reviewer_one, (5, 4)), (reviewer_two, (4, 4))):
        assignment = by_user[reviewer.id]
        await rate_assignment(
            db,
            assignment_id=assignment.id,
            criteria=template.criteria,
            reviewer=reviewer,
            values=values,
            comment_prefix=f"decision-{reviewer.id}",
        )
        await submit_scorecard(assignment.id, db=db, user=reviewer)

    request = HiringDecisionCreate(
        position_id=position_id,
        recommendation="advance",
        summary="Internal decision summary v1",
        required_approvals=2,
    )
    decision = await create_or_update_hiring_decision(
        candidate_id, request, db=db, user=creator
    )
    assert decision.status == "pending"
    created_count = await activity_count(
        db,
        candidate_id,
        "hiring_decision_created",
        "hiring_decision_updated",
    )
    same_decision = await create_or_update_hiring_decision(
        candidate_id, request, db=db, user=creator
    )
    assert same_decision.id == decision.id
    assert (
        await activity_count(
            db,
            candidate_id,
            "hiring_decision_created",
            "hiring_decision_updated",
        )
        == created_count
    )

    first_vote = await vote_hiring_decision(
        decision.id,
        HiringDecisionVoteRequest(vote="approve", comment="first approval"),
        db=db,
        user=reviewer_one,
    )
    assert first_vote.status == "pending"
    voted_count = await activity_count(
        db, candidate_id, "hiring_decision_voted", "hiring_decision_finalized"
    )
    repeated_vote = await vote_hiring_decision(
        decision.id,
        HiringDecisionVoteRequest(vote="approve", comment="first approval"),
        db=db,
        user=reviewer_one,
    )
    assert repeated_vote.status == "pending"
    assert (
        await activity_count(
            db,
            candidate_id,
            "hiring_decision_voted",
            "hiring_decision_finalized",
        )
        == voted_count
    )

    revised = await create_or_update_hiring_decision(
        candidate_id,
        HiringDecisionCreate(
            position_id=position_id,
            recommendation="advance",
            summary="Internal decision summary v2",
            required_approvals=2,
        ),
        db=db,
        user=creator,
    )
    assert len(revised.approvals) == 1
    assert revised.approvals[0].approval_cycle == 1
    assert revised.approval_cycle == 2
    assert revised.version == 2
    assert revised.status == "pending"

    await vote_hiring_decision(
        decision.id,
        HiringDecisionVoteRequest(vote="approve", comment="new first approval"),
        db=db,
        user=reviewer_one,
    )
    finalized = await vote_hiring_decision(
        decision.id,
        HiringDecisionVoteRequest(vote="approve", comment="second approval"),
        db=db,
        user=reviewer_two,
    )
    assert finalized.status == "approved"
    assert len(finalized.approvals) == 3
    assert [item.approval_cycle for item in finalized.approvals] == [1, 2, 2]
    db.expire_all()
    persisted_application = await db.get(Score, application_id)
    assert persisted_application is not None
    assert persisted_application.stage == "interview"
    assert await activity_count(db, candidate_id, "stage_changed") == 1

    reviewer_two = await db.get(User, reviewer_two_id)
    assert reviewer_two is not None
    finalized_events = await activity_count(
        db, candidate_id, "hiring_decision_finalized"
    )
    retried_final = await vote_hiring_decision(
        decision.id,
        HiringDecisionVoteRequest(vote="approve", comment="second approval"),
        db=db,
        user=reviewer_two,
    )
    assert retried_final.status == "approved"
    assert (
        await activity_count(db, candidate_id, "hiring_decision_finalized")
        == finalized_events
    )
    assert await activity_count(db, candidate_id, "stage_changed") == 1

    creator = await db.get(User, creator_id)
    assert creator is not None
    assignment_count = (
        await db.execute(
            select(func.count(ScorecardAssignment.id)).where(
                ScorecardAssignment.candidate_id == candidate_id
            )
        )
    ).scalar_one()
    assigned_events = await activity_count(db, candidate_id, "scorecard_assigned")
    with pytest.raises(HTTPException) as late_assignment:
        await assign_reviewers(
            db,
            candidate_id=candidate_id,
            position_id=position_id,
            template_id=template.id,
            reviewer_ids=[creator.id],
            actor=creator,
        )
    assert late_assignment.value.status_code == 409
    await db.rollback()
    assert (
        await db.execute(
            select(func.count(ScorecardAssignment.id)).where(
                ScorecardAssignment.candidate_id == candidate_id
            )
        )
    ).scalar_one() == assignment_count
    assert await activity_count(db, candidate_id, "scorecard_assigned") == assigned_events

    activities = list(
        (
            await db.execute(
                select(CandidateActivity).where(
                    CandidateActivity.candidate_id == candidate_id
                )
            )
        ).scalars()
    )
    payloads = str([activity.payload for activity in activities]).lower()
    assert "internal decision summary" not in payloads
    assert "first approval" not in payloads
    assert "second approval" not in payloads


async def test_deleted_children_and_users_leave_readable_snapshot_history(db):
    (
        _organization,
        creator,
        reviewer,
        surviving_viewer,
        candidate,
        position,
        _application,
    ) = await seed_pipeline(db, "retention")
    template = await make_template(db, creator, name="Retention rubric")
    panel = await assign_reviewers(
        db,
        candidate_id=candidate.id,
        position_id=position.id,
        template_id=template.id,
        reviewer_ids=[reviewer.id],
        actor=creator,
    )
    assignment = panel.assignments[0]
    await rate_assignment(
        db,
        assignment_id=assignment.id,
        criteria=template.criteria,
        reviewer=reviewer,
        values=(5, 3),
        comment_prefix="retained",
    )
    await submit_scorecard(assignment.id, db=db, user=reviewer)
    decision = await create_or_update_hiring_decision(
        candidate.id,
        HiringDecisionCreate(
            position_id=position.id,
            recommendation="advance",
            summary="Retained decision body",
            required_approvals=1,
        ),
        db=db,
            user=creator,
        )
    decision = await vote_hiring_decision(
        decision.id,
        HiringDecisionVoteRequest(vote="approve", comment="retained approval body"),
        db=db,
        user=reviewer,
    )

    assignment_id = assignment.id
    decision_id = decision.id
    approval_id = decision.approvals[0].id
    candidate_id = candidate.id
    surviving_viewer_id = surviving_viewer.id
    reviewer_email = reviewer.email
    creator_email = creator.email
    position_title = position.title
    rating_ids = [rating.id for rating in panel.assignments[0].ratings or []]
    # The assignment in the original panel predates draft ratings; fetch their IDs.
    rating_ids = list(
        (
            await db.execute(
                select(ScorecardRating.id).where(
                    ScorecardRating.assignment_id == assignment_id
                )
            )
        ).scalars()
    )
    assert len(rating_ids) == 2

    stored_template = await db.get(ScorecardTemplate, template.id)
    stored_reviewer = await db.get(User, reviewer.id)
    stored_creator = await db.get(User, creator.id)
    stored_position = await db.get(Position, position.id)
    assert all(
        item is not None
        for item in (stored_template, stored_reviewer, stored_creator, stored_position)
    )
    await db.delete(stored_template)
    await db.delete(stored_reviewer)
    await db.delete(stored_creator)
    await db.delete(stored_position)
    await db.commit()
    db.expire_all()

    retained_assignment = (
        await db.execute(
            select(ScorecardAssignment)
            .options(selectinload(ScorecardAssignment.ratings))
            .where(ScorecardAssignment.id == assignment_id)
        )
    ).scalar_one()
    assert retained_assignment.position_id is None
    assert retained_assignment.template_id is None
    assert retained_assignment.assigned_to_user_id is None
    assert retained_assignment.assigned_by_user_id is None
    assert retained_assignment.position_title_snapshot == position_title
    assert retained_assignment.template_name_snapshot == "Retention rubric"
    assert retained_assignment.assignee_email_snapshot == reviewer_email
    assert {rating.id for rating in retained_assignment.ratings} == set(rating_ids)
    assert all(rating.criterion_id is None for rating in retained_assignment.ratings)
    assert {rating.criterion_label_snapshot for rating in retained_assignment.ratings} == {
        "Technical depth",
        "Collaboration",
    }
    assert any(rating.comment == "retained technical comment" for rating in retained_assignment.ratings)

    retained_decision = (
        await db.execute(
            select(HiringDecision)
            .options(selectinload(HiringDecision.approvals))
            .where(HiringDecision.id == decision_id)
        )
    ).scalar_one()
    assert retained_decision.position_id is None
    assert retained_decision.created_by_user_id is None
    assert retained_decision.position_title_snapshot == position_title
    assert retained_decision.created_by_email_snapshot == creator_email
    assert retained_decision.summary == "Retained decision body"
    assert len(retained_decision.approvals) == 1
    assert retained_decision.approvals[0].id == approval_id
    assert retained_decision.approvals[0].user_id is None
    assert retained_decision.approvals[0].user_email_snapshot == reviewer_email
    assert retained_decision.approvals[0].comment == "retained approval body"

    surviving_viewer = await db.get(User, surviving_viewer_id)
    assert surviving_viewer is not None
    retained_panel = await get_candidate_scorecards(
        candidate_id,
        position_id=None,
        db=db,
        user=surviving_viewer,
    )
    assert retained_panel.revealed is True
    assert retained_panel.assignments[0].template_name == "Retention rubric"
    assert retained_panel.assignments[0].position_title == position_title
    assert retained_panel.decision is not None
    assert retained_panel.decision.position_title == position_title
    assert retained_panel.decision.created_by_email == creator_email

    retained_events = list(
        (
            await db.execute(
                select(CandidateActivity).where(
                    CandidateActivity.candidate_id == candidate_id
                )
            )
        ).scalars()
    )
    # Immutable scorecard/decision records retain their purpose-built snapshot
    # fields.  Generic activity payloads follow the d96 position-erasure
    # contract and are removed with the deleted position.
    assert retained_events == []


async def test_final_hire_and_reject_votes_use_canonical_application_transitions(db):
    with pytest.raises(ValidationError):
        HiringDecisionCreate(
            position_id=uuid4(),
            recommendation="hire",
            summary="   ",
        )
    (
        organization,
        creator,
        hire_reviewer,
        _reviewer_two,
        hire_candidate,
        hire_position,
        hire_score,
    ) = await seed_pipeline(db, "terminal-hire")
    hire_score.stage = "interview"
    hire_reviewer.role = "recruiter"
    hire_link = await ensure_application_link(
        db,
        organization_id=organization.id,
        candidate_id=hire_candidate.id,
        position_id=hire_position.id,
        source=hire_candidate.source,
        owner_user_id=hire_score.owner_user_id,
        actor_user_id=creator.id,
        actor_kind="human",
        reason="terminal_hire_test_setup",
    )
    hire_decision = HiringDecision(
        organization_id=organization.id,
        application_id=hire_link.application.id,
        candidate_id=hire_candidate.id,
        position_id=hire_position.id,
        created_by_user_id=creator.id,
        position_title_snapshot=hire_position.title,
        created_by_email_snapshot=creator.email,
        recommendation="hire",
        summary="Panel evidence supports preparing an offer",
        required_approvals=1,
    )
    db.add(hire_decision)
    await db.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        hire_response = await client.post(
            f"/hiring-decisions/{hire_decision.id}/vote",
            headers=auth(hire_reviewer),
            json={"vote": "approve", "comment": "Approved for offer"},
        )
    assert hire_response.status_code == 200
    assert hire_response.json()["status"] == "approved"
    await db.refresh(hire_score)
    await db.refresh(hire_link.application)
    assert hire_score.stage == "offer"
    hire_application = (
        await db.execute(
            select(Application).where(
                Application.organization_id == organization.id,
                Application.candidate_id == hire_candidate.id,
                Application.position_id == hire_position.id,
            )
        )
    ).scalar_one()
    assert hire_application.stage == "offer"
    hire_event = (
        await db.execute(
            select(ApplicationStageEvent).where(
                ApplicationStageEvent.application_id == hire_application.id,
                ApplicationStageEvent.to_stage == "offer",
            )
        )
    ).scalar_one()
    assert hire_event.reason == "Panel evidence supports preparing an offer"
    assert hire_event.details["hiring_decision_id"] == str(hire_decision.id)

    (
        reject_organization,
        reject_creator,
        reject_reviewer,
        _reject_reviewer_two,
        reject_candidate,
        reject_position,
        reject_score,
    ) = await seed_pipeline(db, "terminal-reject")
    reject_reviewer.role = "recruiter"
    reject_link = await ensure_application_link(
        db,
        organization_id=reject_organization.id,
        candidate_id=reject_candidate.id,
        position_id=reject_position.id,
        source=reject_candidate.source,
        owner_user_id=reject_score.owner_user_id,
        actor_user_id=reject_creator.id,
        actor_kind="human",
        reason="terminal_reject_test_setup",
    )
    reject_decision = HiringDecision(
        organization_id=reject_organization.id,
        application_id=reject_link.application.id,
        candidate_id=reject_candidate.id,
        position_id=reject_position.id,
        created_by_user_id=reject_creator.id,
        position_title_snapshot=reject_position.title,
        created_by_email_snapshot=reject_creator.email,
        recommendation="reject",
        summary="Required interview evidence did not meet the role bar",
        required_approvals=1,
    )
    db.add(reject_decision)
    await db.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        reject_response = await client.post(
            f"/hiring-decisions/{reject_decision.id}/vote",
            headers=auth(reject_reviewer),
            json={"vote": "approve", "comment": "Decision confirmed"},
        )
    assert reject_response.status_code == 200
    await db.refresh(reject_score)
    await db.refresh(reject_link.application)
    assert reject_score.stage == "rejected"
    reject_application = (
        await db.execute(
            select(Application).where(
                Application.organization_id == reject_organization.id,
                Application.candidate_id == reject_candidate.id,
                Application.position_id == reject_position.id,
            )
        )
    ).scalar_one()
    assert reject_application.stage == "rejected"
    reject_event = (
        await db.execute(
            select(ApplicationStageEvent).where(
                ApplicationStageEvent.application_id == reject_application.id,
                ApplicationStageEvent.to_stage == "rejected",
            )
        )
    ).scalar_one()
    assert reject_event.details["disposition_code"] == "other"
    assert reject_event.reason == "Required interview evidence did not meet the role bar"


async def test_assign_rollback_invalid_draft_atomicity_and_duplicate_no_op(
    db, monkeypatch
):
    (
        organization,
        creator,
        reviewer,
        _other_reviewer,
        candidate,
        position,
        _application,
    ) = await seed_pipeline(db, "rollback")
    template = await make_template(db, creator, name="Atomic rubric")
    creator_id = creator.id
    reviewer_id = reviewer.id
    organization_id = organization.id
    candidate_id = candidate.id
    position_id = position.id

    real_commit = db.commit

    async def fail_commit():
        raise RuntimeError("forced commit failure")

    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="forced commit failure"):
        await assign_reviewers(
            db,
            candidate_id=candidate_id,
            position_id=position_id,
            template_id=template.id,
            reviewer_ids=[reviewer_id],
            actor=creator,
        )
    await db.rollback()
    monkeypatch.setattr(db, "commit", real_commit)

    assert (
        await db.execute(
            select(func.count(ScorecardAssignment.id)).where(
                ScorecardAssignment.candidate_id == candidate_id
            )
        )
    ).scalar_one() == 0
    assert await activity_count(db, candidate_id, "scorecard_assigned") == 0
    assert (
        await db.execute(
            select(func.count(Notification.id)).where(
                Notification.organization_id == organization_id,
                Notification.type == "scorecard_assigned",
            )
        )
    ).scalar_one() == 0

    creator = await db.get(User, creator_id)
    reviewer = await db.get(User, reviewer_id)
    assert creator is not None and reviewer is not None
    first = await assign_reviewers(
        db,
        candidate_id=candidate_id,
        position_id=position_id,
        template_id=template.id,
        reviewer_ids=[reviewer_id],
        actor=creator,
    )
    second = await assign_reviewers(
        db,
        candidate_id=candidate_id,
        position_id=position_id,
        template_id=template.id,
        reviewer_ids=[reviewer_id],
        actor=creator,
    )
    assert first.assignment_count == second.assignment_count == 1
    assert await activity_count(db, candidate_id, "scorecard_assigned") == 1
    assert (
        await db.execute(
            select(func.count(Notification.id)).where(
                Notification.organization_id == organization_id,
                Notification.type == "scorecard_assigned",
            )
        )
    ).scalar_one() == 1

    assignment_id = first.assignments[0].id
    with pytest.raises(HTTPException) as invalid_criterion:
        await update_scorecard_draft(
            assignment_id,
            ScorecardDraftUpdate(
                ratings=[
                    ScorecardRatingInput(
                        criterion_id=template.criteria[0].id,
                        rating=5,
                        comment="must not be partially persisted",
                    ),
                    ScorecardRatingInput(
                        criterion_id=uuid4(),
                        rating=4,
                        comment="invalid criterion",
                    ),
                ]
            ),
            db=db,
            user=reviewer,
        )
    assert invalid_criterion.value.status_code == 422
    await db.rollback()
    assert (
        await db.execute(
            select(func.count(ScorecardRating.id)).where(
                ScorecardRating.assignment_id == assignment_id
            )
        )
    ).scalar_one() == 0


async def test_candidate_merge_rehomes_conflicting_scorecard_histories_losslessly(db):
    (
        organization,
        creator,
        reviewer,
        _viewer,
        primary,
        position,
        _primary_score,
    ) = await seed_pipeline(db, "merge")
    duplicate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="scorecard-merge-duplicate",
        raw_cv="A second detailed CV that will become a retained merge alias. " * 2,
    )
    db.add(duplicate)
    await db.flush()
    duplicate_score = Score(
        organization_id=organization.id,
        candidate_id=duplicate.id,
        position_id=position.id,
        overall_score=72,
        stage="screening",
    )
    db.add(duplicate_score)
    primary_link = await ensure_application_link(
        db,
        organization_id=organization.id,
        candidate_id=primary.id,
        position_id=position.id,
        source="manual",
        owner_user_id=None,
        actor_user_id=creator.id,
        actor_kind="human",
        reason="test_primary_application_link",
    )
    duplicate_link = await ensure_application_link(
        db,
        organization_id=organization.id,
        candidate_id=duplicate.id,
        position_id=position.id,
        source="manual",
        owner_user_id=None,
        actor_user_id=creator.id,
        actor_kind="human",
        reason="test_duplicate_application_link",
    )
    template = ScorecardTemplate(
        organization_id=organization.id,
        created_by_user_id=creator.id,
        name="Merge history rubric",
    )
    db.add(template)
    await db.flush()
    primary_assignment = ScorecardAssignment(
        organization_id=organization.id,
        application_id=primary_link.application.id,
        candidate_id=primary.id,
        position_id=position.id,
        template_id=template.id,
        assigned_to_user_id=reviewer.id,
        assigned_by_user_id=creator.id,
        template_name_snapshot=template.name,
        position_title_snapshot=position.title,
        assignee_email_snapshot=reviewer.email,
    )
    duplicate_assignment = ScorecardAssignment(
        organization_id=organization.id,
        application_id=duplicate_link.application.id,
        candidate_id=duplicate.id,
        position_id=position.id,
        template_id=template.id,
        assigned_to_user_id=reviewer.id,
        assigned_by_user_id=creator.id,
        template_name_snapshot=template.name,
        position_title_snapshot=position.title,
        assignee_email_snapshot=reviewer.email,
    )
    primary_decision = HiringDecision(
        organization_id=organization.id,
        application_id=primary_link.application.id,
        candidate_id=primary.id,
        position_id=position.id,
        created_by_user_id=creator.id,
        position_title_snapshot=position.title,
        created_by_email_snapshot=creator.email,
        recommendation="hold",
        summary="Primary retained history",
        required_approvals=1,
    )
    duplicate_decision = HiringDecision(
        organization_id=organization.id,
        application_id=duplicate_link.application.id,
        candidate_id=duplicate.id,
        position_id=position.id,
        created_by_user_id=creator.id,
        position_title_snapshot=position.title,
        created_by_email_snapshot=creator.email,
        recommendation="advance",
        summary="Duplicate retained history",
        required_approvals=1,
    )
    db.add_all(
        [
            primary_assignment,
            duplicate_assignment,
            primary_decision,
            duplicate_decision,
        ]
    )
    primary_application_id = primary_link.application.id
    duplicate_application_id = duplicate_link.application.id
    await db.commit()

    primary_id = primary.id
    duplicate_id = duplicate.id
    creator_id = creator.id
    position_id = position.id
    primary_assignment_id = primary_assignment.id
    duplicate_assignment_id = duplicate_assignment.id
    primary_decision_id = primary_decision.id
    duplicate_decision_id = duplicate_decision.id
    await merge_candidates(
        db,
        organization_id=organization.id,
        actor_user_id=creator.id,
        primary_candidate_id=primary_id,
        duplicate_candidate_ids=[duplicate_id],
        score_winners={position.id: primary_id},
    )
    db.expire_all()

    assignments = list(
        (
            await db.execute(
                select(ScorecardAssignment).where(
                    ScorecardAssignment.id.in_(
                        [primary_assignment_id, duplicate_assignment_id]
                    )
                )
            )
        ).scalars()
    )
    decisions = list(
        (
            await db.execute(
                select(HiringDecision).where(
                    HiringDecision.id.in_([primary_decision_id, duplicate_decision_id])
                )
            )
        ).scalars()
    )
    assert len(assignments) == 2
    assert len(decisions) == 2
    assert {item.candidate_id for item in assignments} == {primary_id}
    assert {item.candidate_id for item in decisions} == {primary_id}

    creator = await db.get(User, creator_id)
    assert creator is not None
    canonical_panel = await get_candidate_scorecards(
        primary_id,
        position_id=position_id,
        application_id=primary_application_id,
        db=db,
        user=creator,
    )
    alias_panel = await get_candidate_scorecards(
        duplicate_id,
        position_id=position_id,
        application_id=duplicate_application_id,
        db=db,
        user=creator,
    )
    assert canonical_panel.assignment_count == alias_panel.assignment_count == 1
    assert canonical_panel.decision is not None
    assert alias_panel.decision is not None
    assert canonical_panel.decision.id == primary_decision_id
    assert alias_panel.decision.id == duplicate_decision_id
