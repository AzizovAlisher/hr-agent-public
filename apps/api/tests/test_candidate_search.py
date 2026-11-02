"""PostgreSQL integration coverage for candidate discovery and bulk workflows."""

import csv
import hashlib
import io
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.application import Application, ApplicationStageEvent
from app.models.candidate import Candidate
from app.models.candidate_activity import CandidateActivity
from app.models.candidate_merge import CandidateMergeBatch
from app.models.candidate_note import CandidateNote, CandidateTag
from app.models.chat_session import ChatSession
from app.models.enterprise import AuditLog
from app.models.interview import Interview
from app.models.interview_recording import InterviewRecording
from app.models.notification import Notification
from app.models.organization import Organization
from app.models.position import Position
from app.models.private_object import LegacyPrivateObjectDeletion, PrivateObject
from app.models.score import Score
from app.models.task_submission import TaskSubmission
from app.models.user import User
from app.routers.activities import list_candidate_activities
from app.routers.candidate_search import (
    apply_candidate_bulk_action,
    create_candidate_view,
    download_merged_candidate_source_cv,
    export_candidates,
    get_merged_candidate_source_evidence,
    list_candidate_duplicates,
    list_candidate_views,
    list_merged_candidate_sources,
    merge_candidate_duplicates,
    preview_candidate_merge,
    search_candidates,
    update_candidate_view,
)
from app.schemas.candidate_search import (
    CANDIDATE_DUPLICATE_GROUP_MEMBER_LIMIT,
    CandidateBulkRequest,
    CandidateExportRequest,
    CandidateMergePreviewRequest,
    CandidateMergeRequest,
    CandidateMergeScoreWinner,
    CandidateSavedViewCreate,
    CandidateSavedViewUpdate,
    CandidateSearchFilters,
)
from app.services.activities import append_candidate_activity
from app.services.applications import ensure_application_link

pytestmark = pytest.mark.asyncio


async def seed_tenant(db, label: str) -> tuple[Organization, User]:
    token = uuid4().hex[:8]
    organization = Organization(
        name=f"Organization {label}",
        slug=f"organization-{label}-{token}",
    )
    db.add(organization)
    await db.flush()
    user = User(
        organization_id=organization.id,
        email=f"{label}-{token}@example.test",
        password_hash="not-used",
        role="recruiter",
        locale="en",
    )
    db.add(user)
    await db.flush()
    return organization, user


async def add_candidate(
    db,
    organization: Organization,
    label: str,
    *,
    source: str = "manual",
    raw_cv: str | None = None,
    source_ref: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    github: str | None = None,
    created_at: datetime | None = None,
) -> Candidate:
    candidate = Candidate(
        organization_id=organization.id,
        source=source,
        source_ref=source_ref or f"candidate-{label}-{uuid4().hex[:6]}",
        raw_cv=raw_cv
        or f"Candidate {label} has sufficient resume content for testing.",
        email_normalized=email,
        phone_normalized=phone,
        github_login=github,
        **({"created_at": created_at} if created_at else {}),
    )
    db.add(candidate)
    await db.flush()
    return candidate


async def add_position(
    db, organization: Organization, user: User, title: str
) -> Position:
    position = Position(
        organization_id=organization.id,
        title=title,
        status="open",
        created_by=user.id,
    )
    db.add(position)
    await db.flush()
    return position


async def add_score(
    db,
    organization: Organization,
    candidate: Candidate,
    position: Position,
    *,
    score: int | None,
    stage: str,
) -> Score:
    row = Score(
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        overall_score=score,
        stage=stage,
    )
    db.add(row)
    await db.flush()
    return row


async def run_search(db, user: User, **overrides):
    values = {
        "q": None,
        "score_min": None,
        "score_max": None,
        "source": [],
        "stage": [],
        "position_id": None,
        "tag": [],
        "favorites_only": False,
        "new_only": False,
        "unscored_only": False,
        "limit": 50,
        "cursor": None,
    }
    values.update(overrides)
    return await search_candidates(db=db, user=user, **values)


async def response_bytes(response) -> bytes:
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.encode() if isinstance(chunk, str) else chunk)
    return b"".join(chunks)


async def test_full_text_search_handles_russian_english_and_tenant_isolation(db):
    organization, user = await seed_tenant(db, "fts")
    russian = await add_candidate(
        db,
        organization,
        "russian",
        raw_cv="Опытный разработчик распределённых систем на Python.",
    )
    english = await add_candidate(
        db,
        organization,
        "english",
        raw_cv="Platform engineer with Kubernetes observability experience.",
    )
    other_organization, _other_user = await seed_tenant(db, "fts-other")
    outsider = await add_candidate(
        db,
        other_organization,
        "outsider",
        raw_cv="Опытный разработчик Kubernetes платформ.",
    )
    await db.commit()

    ru_page = await run_search(db, user, q="разработчик")
    assert [item.id for item in ru_page.items] == [russian.id]
    assert outsider.id not in {item.id for item in ru_page.items}

    en_page = await run_search(db, user, q="Kubernetes")
    assert [item.id for item in en_page.items] == [english.id]
    assert outsider.id not in {item.id for item in en_page.items}


async def test_source_score_and_stage_filters_match_the_same_application(db):
    organization, user = await seed_tenant(db, "filters")
    first_position = await add_position(db, organization, user, "Backend")
    second_position = await add_position(db, organization, user, "Platform")

    split_match = await add_candidate(db, organization, "split", source="referral")
    actual_match = await add_candidate(db, organization, "actual", source="referral")
    wrong_source = await add_candidate(db, organization, "wrong-source", source="hh")
    await add_score(
        db, organization, split_match, first_position, score=95, stage="new"
    )
    await add_score(
        db, organization, split_match, second_position, score=40, stage="interview"
    )
    await add_score(
        db, organization, actual_match, second_position, score=92, stage="interview"
    )
    await add_score(
        db, organization, wrong_source, second_position, score=96, stage="interview"
    )
    await db.commit()

    page = await run_search(
        db,
        user,
        source=["referral"],
        position_id=second_position.id,
        stage=["interview"],
        score_min=80,
    )
    assert [item.id for item in page.items] == [actual_match.id]

    without_position = await run_search(
        db,
        user,
        stage=["interview"],
        score_min=80,
    )
    assert split_match.id not in {item.id for item in without_position.items}
    assert actual_match.id in {item.id for item in without_position.items}


async def test_search_cursor_is_deterministic_and_bound_to_filters(db):
    organization, user = await seed_tenant(db, "cursor")
    happened = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)
    candidates = [
        await add_candidate(
            db,
            organization,
            f"cursor-{index}",
            source="manual",
            created_at=happened,
        )
        for index in range(3)
    ]
    await db.commit()

    first = await run_search(db, user, source=["manual"], limit=1)
    assert len(first.items) == 1
    assert first.next_cursor is not None
    second = await run_search(
        db,
        user,
        source=["manual"],
        limit=1,
        cursor=first.next_cursor,
    )
    assert len(second.items) == 1
    assert second.items[0].id != first.items[0].id
    assert {first.items[0].id, second.items[0].id}.issubset(
        {candidate.id for candidate in candidates}
    )

    with pytest.raises(HTTPException) as mismatch:
        await run_search(
            db,
            user,
            source=["hh"],
            limit=1,
            cursor=first.next_cursor,
        )
    assert mismatch.value.status_code == 422


async def test_saved_views_are_case_insensitive_and_private_to_the_user(db):
    organization, owner = await seed_tenant(db, "views")
    teammate = User(
        organization_id=organization.id,
        email=f"views-teammate-{uuid4().hex[:8]}@example.test",
        password_hash="not-used",
        role="recruiter",
        locale="en",
    )
    db.add(teammate)
    other_organization, outsider = await seed_tenant(db, "views-other")
    await db.commit()
    owner_id = owner.id
    teammate_id = teammate.id
    outsider_id = outsider.id
    organization_id = organization.id
    other_organization_id = other_organization.id

    created = await create_candidate_view(
        CandidateSavedViewCreate(
            name="Hot Leads",
            filters=CandidateSearchFilters(score_min=80, sources=["HH", "hh"]),
        ),
        db=db,
        user=owner,
    )
    view_id = created.id
    assert created.filters.sources == ["hh"]

    with pytest.raises(HTTPException) as duplicate:
        await create_candidate_view(
            CandidateSavedViewCreate(
                name="  hot leads  ", filters=CandidateSearchFilters()
            ),
            db=db,
            user=owner,
        )
    assert duplicate.value.status_code == 409

    # The expected IntegrityError rollback expires ORM instances in this
    # direct-router test. Real HTTP requests use a fresh session per request,
    # so reload the actors explicitly before simulating the next requests.
    owner = await db.get(User, owner_id)
    teammate = await db.get(User, teammate_id)
    outsider = await db.get(User, outsider_id)
    assert owner is not None and teammate is not None and outsider is not None

    teammate_view = await create_candidate_view(
        CandidateSavedViewCreate(
            name="HOT LEADS", filters=CandidateSearchFilters(stages=["screening"])
        ),
        db=db,
        user=teammate,
    )
    outsider_view = await create_candidate_view(
        CandidateSavedViewCreate(
            name="Hot Leads", filters=CandidateSearchFilters(tags=["private"])
        ),
        db=db,
        user=outsider,
    )
    assert teammate_view.id != view_id
    assert outsider_view.id != view_id
    assert other_organization_id != organization_id

    owner_views = await list_candidate_views(db=db, user=owner)
    teammate_views = await list_candidate_views(db=db, user=teammate)
    outsider_views = await list_candidate_views(db=db, user=outsider)
    assert [view.id for view in owner_views.items] == [
        view_id
    ]
    assert [view.id for view in teammate_views.items] == [
        teammate_view.id
    ]
    assert [view.id for view in outsider_views.items] == [
        outsider_view.id
    ]

    with pytest.raises(HTTPException) as private_update:
        await update_candidate_view(
            view_id,
            CandidateSavedViewUpdate(name="Stolen"),
            db=db,
            user=teammate,
        )
    assert private_update.value.status_code == 404


async def test_bulk_tag_and_stage_are_atomic_audited_and_no_op_safe(db):
    organization, user = await seed_tenant(db, "bulk")
    position = await add_position(db, organization, user, "Bulk Role")
    first = await add_candidate(db, organization, "bulk-first")
    second = await add_candidate(db, organization, "bulk-second")
    missing_application = await add_candidate(db, organization, "bulk-missing")
    first_score = await add_score(
        db, organization, first, position, score=81, stage="screening"
    )
    second_score = await add_score(
        db, organization, second, position, score=72, stage="new"
    )
    other_organization, _outsider_user = await seed_tenant(db, "bulk-other")
    outsider = await add_candidate(db, other_organization, "bulk-outsider")
    await db.commit()
    first_score_id = first_score.id
    second_score_id = second_score.id
    first_id = first.id
    second_id = second.id
    position_id = position.id
    organization_id = organization.id
    user_id = user.id

    added = await apply_candidate_bulk_action(
        CandidateBulkRequest(
            candidate_ids=[first.id, second.id], action="tag_add", tag="priority"
        ),
        db=db,
        user=user,
    )
    assert set(added.changed_candidate_ids) == {first.id, second.id}
    assert added.unchanged_candidate_ids == []

    repeated = await apply_candidate_bulk_action(
        CandidateBulkRequest(
            candidate_ids=[first.id, second.id], action="tag_add", tag="priority"
        ),
        db=db,
        user=user,
    )
    assert repeated.changed_candidate_ids == []
    assert set(repeated.unchanged_candidate_ids) == {first.id, second.id}

    moved = await apply_candidate_bulk_action(
        CandidateBulkRequest(
            candidate_ids=[first.id, second.id],
            action="stage",
            position_id=position.id,
            stage="screening",
        ),
        db=db,
        user=user,
    )
    assert moved.changed_candidate_ids == [second.id]
    assert moved.unchanged_candidate_ids == [first.id]

    activities = list(
        (
            await db.execute(
                select(CandidateActivity).where(
                    CandidateActivity.organization_id == organization.id
                )
            )
        ).scalars()
    )
    assert sum(event.event_type == "tag_added" for event in activities) == 2
    assert sum(event.event_type == "stage_changed" for event in activities) == 1
    assert all(
        "bulk_action_id" in event.payload
        for event in activities
        if event.event_type in {"tag_added", "stage_changed"}
    )

    with pytest.raises(HTTPException) as cross_tenant:
        await apply_candidate_bulk_action(
            CandidateBulkRequest(
                candidate_ids=[first.id, outsider.id],
                action="tag_add",
                tag="must-not-stick",
            ),
            db=db,
            user=user,
        )
    assert cross_tenant.value.status_code == 404
    assert not list(
        (
            await db.execute(
                select(CandidateTag).where(CandidateTag.label == "must-not-stick")
            )
        ).scalars()
    )

    with pytest.raises(HTTPException) as missing_score:
        await apply_candidate_bulk_action(
            CandidateBulkRequest(
                candidate_ids=[first.id, missing_application.id],
                action="stage",
                position_id=position.id,
                stage="offer",
            ),
            db=db,
            user=user,
        )
    assert missing_score.value.status_code == 404
    db.expire_all()
    persisted_first_score = await db.get(Score, first_score_id)
    persisted_second_score = await db.get(Score, second_score_id)
    assert persisted_first_score is not None
    assert persisted_second_score is not None
    assert persisted_first_score.stage == "screening"
    assert persisted_second_score.stage == "screening"
    user = await db.get(User, user_id)
    assert user is not None

    with pytest.raises(HTTPException) as invalid_transition:
        await apply_candidate_bulk_action(
            CandidateBulkRequest(
                candidate_ids=[first_id],
                action="stage",
                position_id=position_id,
                stage="offer",
            ),
            db=db,
            user=user,
        )
    assert invalid_transition.value.status_code == 409
    await db.refresh(persisted_first_score)
    assert persisted_first_score.stage == "screening"
    user = await db.get(User, user_id)
    assert user is not None

    rejected = await apply_candidate_bulk_action(
        CandidateBulkRequest(
            candidate_ids=[first_id, second_id],
            action="reject",
            position_id=position_id,
            disposition_code="not_qualified",
            reason="Required screening evidence was not demonstrated",
        ),
        db=db,
        user=user,
    )
    assert set(rejected.changed_candidate_ids) == {first_id, second_id}
    applications = list(
        (
            await db.execute(
                select(Application).where(
                    Application.organization_id == organization_id,
                    Application.position_id == position_id,
                )
            )
        ).scalars()
    )
    assert {application.stage for application in applications} == {"rejected"}
    rejection_events = list(
        (
            await db.execute(
                select(ApplicationStageEvent).where(
                    ApplicationStageEvent.application_id.in_(
                        [application.id for application in applications]
                    ),
                    ApplicationStageEvent.to_stage == "rejected",
                )
            )
        ).scalars()
    )
    assert len(rejection_events) == 2
    assert all(
        event.details["disposition_code"] == "not_qualified"
        and event.reason == "Required screening evidence was not demonstrated"
        for event in rejection_events
    )


async def test_csv_export_escapes_formula_cells_and_records_activity(db):
    organization, user = await seed_tenant(db, "csv")
    position = await add_position(db, organization, user, "=RUN()")
    candidate = await add_candidate(
        db,
        organization,
        "csv",
        source_ref='=HYPERLINK("https://evil.test")',
        raw_cv="SECRET RESUME CONTENT THAT MUST NOT BE EXPORTED",
        email="+cmd@example.test",
        phone="-992900000000",
    )
    await add_score(db, organization, candidate, position, score=84, stage="offer")
    db.add(
        CandidateTag(
            organization_id=organization.id,
            candidate_id=candidate.id,
            label="@danger",
            created_by=user.id,
        )
    )
    await db.commit()

    response = await export_candidates(
        CandidateExportRequest(
            candidate_ids=[candidate.id],
            position_id=position.id,
            include_contacts=True,
        ),
        db=db,
        user=user,
    )
    data = await response_bytes(response)
    assert data.startswith(b"\xef\xbb\xbf")
    decoded = data.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(decoded)))
    assert len(rows) == 1
    row = rows[0]
    assert row["reference"].startswith("'=")
    assert row["email"].startswith("'+")
    assert row["phone"].startswith("'-")
    assert row["tags"].startswith("'@")
    assert row["position"].startswith("'=")
    assert "SECRET RESUME CONTENT" not in decoded

    exported = list(
        (
            await db.execute(
                select(CandidateActivity).where(
                    CandidateActivity.candidate_id == candidate.id,
                    CandidateActivity.event_type == "candidate_exported",
                )
            )
        ).scalars()
    )
    assert len(exported) == 1
    assert exported[0].payload["format"] == "csv"


async def test_duplicate_groups_are_transitive_and_tenant_scoped(db):
    organization, user = await seed_tenant(db, "duplicates")
    first = await add_candidate(
        db,
        organization,
        "duplicate-first",
        raw_cv="First unique resume",
        email="shared@example.test",
    )
    second = await add_candidate(
        db,
        organization,
        "duplicate-second",
        raw_cv="Shared exact resume body",
        email="shared@example.test",
    )
    third = await add_candidate(
        db,
        organization,
        "duplicate-third",
        raw_cv="  shared   exact RESUME body  ",
    )
    other_organization, _other_user = await seed_tenant(db, "duplicates-other")
    outsider = await add_candidate(
        db,
        other_organization,
        "duplicate-outsider",
        raw_cv="Shared exact resume body",
        email="shared@example.test",
    )
    await db.commit()

    page = await list_candidate_duplicates(db=db, user=user)
    assert page.candidate_count == 3
    assert len(page.items) == 1
    group = page.items[0]
    assert {member.id for member in group.candidates} == {
        first.id,
        second.id,
        third.id,
    }
    assert group.candidate_count == 3
    assert group.has_more_candidates is False
    assert set(group.match_types) == {"email", "cv"}
    assert outsider.id not in {member.id for member in group.candidates}

    search_page = await run_search(db, user)
    duplicate_counts = {item.id: item.duplicate_count for item in search_page.items}
    assert duplicate_counts[first.id] == 2
    assert duplicate_counts[second.id] == 2
    assert duplicate_counts[third.id] == 2


async def test_duplicate_group_members_are_stably_bounded(db):
    organization, user = await seed_tenant(db, "duplicates-bounded")
    cluster_size = 3_000
    candidates = []
    for index in range(cluster_size):
        email_pair = index if index % 2 == 0 else index - 1
        phone_pair = 0 if index == 0 else index if index % 2 else index - 1
        candidates.append(
            Candidate(
                organization_id=organization.id,
                source="manual",
                source_ref=f"bounded-{index:04d}",
                raw_cv=f"Unique bounded duplicate resume {index:04d}",
                email_normalized=f"bounded-{email_pair:04d}@example.test",
                phone_normalized=f"+992900{phone_pair:07d}",
            )
        )
    db.add_all(candidates)
    await db.flush()

    page = await list_candidate_duplicates(limit=1, db=db, user=user)

    assert page.candidate_count == cluster_size
    assert len(page.items) == 1
    group = page.items[0]
    assert group.candidate_count == cluster_size
    assert group.has_more_candidates is True
    assert len(group.candidates) == CANDIDATE_DUPLICATE_GROUP_MEMBER_LIMIT
    expected_ids = [
        candidate.id
        for candidate in sorted(
            candidates,
            key=lambda candidate: (candidate.created_at, candidate.id),
            reverse=True,
        )[:CANDIDATE_DUPLICATE_GROUP_MEMBER_LIMIT]
    ]
    assert [member.id for member in group.candidates] == expected_ids
    assert len(page.model_dump_json().encode()) < 100_000


async def test_search_duplicate_counts_include_hidden_alias_identities(db):
    organization, user = await seed_tenant(db, "duplicate-alias-search")
    canonical = await add_candidate(
        db,
        organization,
        "alias-canonical",
        raw_cv="Canonical resume with a distinct fingerprint",
    )
    alias = await add_candidate(
        db,
        organization,
        "alias-history",
        raw_cv="Historical resume with another distinct fingerprint",
        email="alias-shared@example.test",
    )
    alias.merged_into_candidate_id = canonical.id
    alias.merged_at = datetime.now(UTC)
    alias.merged_by_user_id = user.id
    peer = await add_candidate(
        db,
        organization,
        "alias-peer",
        raw_cv="Peer resume with its own distinct fingerprint",
        email="alias-shared@example.test",
    )
    await db.commit()

    page = await run_search(db, user)
    duplicate_counts = {item.id: item.duplicate_count for item in page.items}
    assert alias.id not in duplicate_counts
    assert duplicate_counts[canonical.id] == 1
    assert duplicate_counts[peer.id] == 1

    alias_match = await run_search(db, user, q="Historical")
    assert [item.id for item in alias_match.items] == [canonical.id]


async def test_merge_rehomes_children_keeps_tombstone_history_and_score_conflicts(
    db, tmp_path, monkeypatch
):
    organization, user = await seed_tenant(db, "merge")
    organization_id = organization.id
    primary = await add_candidate(
        db,
        organization,
        "primary",
        raw_cv="Canonical candidate profile",
        email="merged@example.test",
    )
    duplicate = await add_candidate(
        db,
        organization,
        "duplicate",
        raw_cv="Duplicate candidate history",
        email="merged@example.test",
        github="merged-source-login",
    )
    duplicate.parsed_cv = {"private": "parsed evidence"}
    from app.config import get_settings

    storage_root = tmp_path / "storage"
    monkeypatch.setattr(get_settings(), "storage_dir", str(storage_root))
    stored_cv = storage_root / "cvs" / "duplicate.pdf"
    stored_cv.parent.mkdir(parents=True)
    stored_cv.write_bytes(b"private merged source CV")
    duplicate.pdf_path = str(stored_cv)
    duplicate.github_data = {"private": "github snapshot"}
    conflict_position = await add_position(db, organization, user, "Shared Role")
    unique_position = await add_position(db, organization, user, "Alias-only Role")
    primary_score = await add_score(
        db, organization, primary, conflict_position, score=88, stage="screening"
    )
    alias_conflict = await add_score(
        db, organization, duplicate, conflict_position, score=99, stage="offer"
    )
    alias_unique = await add_score(
        db, organization, duplicate, unique_position, score=74, stage="interview"
    )
    alias_unique_link = await ensure_application_link(
        db,
        organization_id=organization.id,
        candidate_id=duplicate.id,
        position_id=unique_position.id,
        source=duplicate.source,
        owner_user_id=alias_unique.owner_user_id,
        actor_user_id=user.id,
        actor_kind="human",
        reason="candidate_merge_test_setup",
    )

    note = CandidateNote(
        organization_id=organization.id,
        candidate_id=duplicate.id,
        author_user_id=user.id,
        content="Historical note",
    )
    primary_tag = CandidateTag(
        organization_id=organization.id,
        candidate_id=primary.id,
        label="shared",
        created_by=user.id,
    )
    alias_shared_tag = CandidateTag(
        organization_id=organization.id,
        candidate_id=duplicate.id,
        label="shared",
        created_by=user.id,
    )
    alias_unique_tag = CandidateTag(
        organization_id=organization.id,
        candidate_id=duplicate.id,
        label="alias-only",
        created_by=user.id,
    )
    interview = Interview(
        organization_id=organization.id,
        application_id=alias_unique_link.application.id,
        candidate_id=duplicate.id,
        position_id=unique_position.id,
        created_by=user.id,
        status="draft",
    )
    recording = InterviewRecording(
        organization_id=organization.id,
        candidate_id=duplicate.id,
        position_id=unique_position.id,
        application_id=alias_unique_link.application.id,
        created_by=user.id,
        title="Historical recording",
        status="pending",
    )
    task = TaskSubmission(
        organization_id=organization.id,
        candidate_id=duplicate.id,
        position_id=unique_position.id,
        application_id=alias_unique_link.application.id,
        created_by=user.id,
        title="Historical task",
        status="sent",
    )
    chat = ChatSession(
        organization_id=organization.id,
        user_id=user.id,
        scope="candidate",
        candidate_id=duplicate.id,
        title="Historical chat",
    )
    notification = Notification(
        organization_id=organization.id,
        user_id=user.id,
        type="candidate_applied",
        payload={"candidate_id": str(duplicate.id)},
    )
    db.add_all(
        [
            note,
            primary_tag,
            alias_shared_tag,
            alias_unique_tag,
            interview,
            recording,
            task,
            chat,
            notification,
        ]
    )
    await db.flush()
    historical = append_candidate_activity(
        db,
        organization_id=organization.id,
        candidate_id=duplicate.id,
        actor_user_id=user.id,
        event_type="note_added",
        subject_type="note",
        subject_id=note.id,
    )
    await db.commit()
    primary_id = primary.id
    duplicate_id = duplicate.id
    user_id = user.id
    note_id = note.id
    interview_id = interview.id
    recording_id = recording.id
    task_id = task.id
    chat_id = chat.id
    notification_id = notification.id
    primary_score_id = primary_score.id
    alias_conflict_id = alias_conflict.id
    alias_unique_id = alias_unique.id
    historical_id = historical.id

    preview = await preview_candidate_merge(
        CandidateMergePreviewRequest(candidate_ids=[primary_id, duplicate_id]),
        db=db,
        user=user,
    )
    assert preview.shared_tags == ["shared"]
    assert len(preview.score_conflicts) == 1
    assert preview.score_conflicts[0].position_id == conflict_position.id
    assert set(preview.score_conflicts[0].candidate_ids) == {
        primary_id,
        duplicate_id,
    }

    with pytest.raises(HTTPException) as missing_winner:
        await merge_candidate_duplicates(
            primary_id,
            CandidateMergeRequest(duplicate_ids=[duplicate_id]),
            db=db,
            user=user,
        )
    assert missing_winner.value.status_code == 409

    result = await merge_candidate_duplicates(
        primary_id,
        CandidateMergeRequest(
            duplicate_ids=[duplicate_id],
            score_winners=[
                CandidateMergeScoreWinner(
                    position_id=conflict_position.id,
                    winner_candidate_id=primary_id,
                )
            ],
        ),
        db=db,
        user=user,
    )
    assert result.primary_candidate_id == primary_id
    assert result.merged_candidate_ids == [duplicate_id]

    db.expire_all()
    user = await db.get(User, user_id)
    assert user is not None
    merged_primary = await db.get(Candidate, primary_id)
    tombstone = await db.get(Candidate, duplicate_id)
    assert merged_primary is not None and tombstone is not None
    assert merged_primary.pdf_path == str(stored_cv)
    assert tombstone.pdf_path is None
    assert tombstone.merged_into_candidate_id == primary_id
    assert tombstone.merged_at is not None
    assert tombstone.merged_by_user_id == user_id

    assert (await db.get(CandidateNote, note_id)).candidate_id == primary_id
    assert (await db.get(Interview, interview_id)).candidate_id == primary_id
    assert (await db.get(InterviewRecording, recording_id)).candidate_id == primary_id
    assert (await db.get(TaskSubmission, task_id)).candidate_id == primary_id
    assert (await db.get(ChatSession, chat_id)).candidate_id == primary_id
    assert (await db.get(Notification, notification_id)).payload["candidate_id"] == str(
        primary_id
    )

    tags = list(
        (
            await db.execute(
                select(CandidateTag).where(CandidateTag.candidate_id == primary_id)
            )
        ).scalars()
    )
    assert {tag.label for tag in tags} == {"shared", "alias-only"}

    assert (await db.get(Score, primary_score_id)).candidate_id == primary_id
    assert (await db.get(Score, alias_unique_id)).candidate_id == primary_id
    assert (await db.get(Score, alias_conflict_id)).candidate_id == duplicate_id

    source_records = await list_merged_candidate_sources(primary_id, db=db, user=user)
    assert [source.is_primary for source in source_records.items] == [True, False]
    alias_source = next(source for source in source_records.items if not source.is_primary)
    assert alias_source.has_cv is True
    assert alias_source.has_contact is True
    assert alias_source.github_login == "merged-source-login"
    serialized_source = alias_source.model_dump(mode="json")
    assert {
        "raw_cv",
        "parsed_cv",
        "pdf_path",
        "phone_normalized",
        "email_normalized",
        "github_data",
        "github_fetched_at",
    }.isdisjoint(serialized_source)
    assert [score.id for score in alias_source.scores] == [alias_conflict_id]
    assert alias_source.scores[0].selected is False

    evidence = await get_merged_candidate_source_evidence(
        primary_id,
        duplicate_id,
        db=db,
        user=user,
    )
    assert evidence.raw_cv == "Duplicate candidate history"
    assert evidence.parsed_cv == {"private": "parsed evidence"}
    assert evidence.email_normalized == "merged@example.test"
    assert evidence.github_data == {"private": "github snapshot"}
    assert evidence.has_original_file is True
    download = await download_merged_candidate_source_cv(
        primary_id,
        duplicate_id,
        db=db,
        user=user,
    )
    assert b"".join([part async for part in download.body_iterator]) == (
        b"private merged source CV"
    )

    original_activity = await db.get(CandidateActivity, historical_id)
    assert original_activity is not None
    assert original_activity.candidate_id == duplicate_id
    timeline = await list_candidate_activities(
        primary_id,
        limit=100,
        cursor=None,
        category="all",
        db=db,
        user=user,
    )
    assert {item.event_type for item in timeline.items} >= {
        "note_added",
        "candidate_merged",
    }

    search_page = await run_search(db, user)
    assert duplicate_id not in {item.id for item in search_page.items}
    assert primary_id in {item.id for item in search_page.items}

    organization = await db.get(Organization, organization_id)
    assert organization is not None
    unrelated = await add_candidate(db, organization, "unrelated-source-family")
    await db.commit()
    unrelated_id = unrelated.id
    with pytest.raises(HTTPException) as wrong_candidate_family:
        await get_merged_candidate_source_evidence(
            primary_id,
            unrelated_id,
            db=db,
            user=user,
        )
    assert wrong_candidate_family.value.status_code == 404

    other_organization, _other_user = await seed_tenant(db, "merge-other")
    outsider = await add_candidate(db, other_organization, "merge-outsider")
    await db.commit()
    outsider_id = outsider.id
    with pytest.raises(HTTPException) as cross_tenant:
        await merge_candidate_duplicates(
            primary_id,
            CandidateMergeRequest(duplicate_ids=[outsider_id]),
            db=db,
            user=user,
        )
    assert cross_tenant.value.status_code == 404
    with pytest.raises(HTTPException) as hidden_evidence:
        await get_merged_candidate_source_evidence(
            primary_id,
            outsider_id,
            db=db,
            user=user,
        )
    assert hidden_evidence.value.status_code == 404
    db.expire_all()
    persisted_outsider = await db.get(Candidate, outsider_id)
    assert persisted_outsider is not None
    assert persisted_outsider.merged_at is None


async def test_merged_source_legacy_cv_survives_registered_cutover_provenance(
    db, tmp_path, monkeypatch
):
    organization, user = await seed_tenant(db, "merge-legacy-cutover")
    primary = await add_candidate(db, organization, "cutover-primary")
    alias = await add_candidate(db, organization, "cutover-alias")

    from app.config import get_settings

    storage_root = tmp_path / "cutover-storage"
    storage_root.mkdir(parents=True)
    monkeypatch.setattr(get_settings(), "storage_dir", str(storage_root))
    monkeypatch.setattr(
        get_settings(),
        "legacy_private_storage_dir",
        str(storage_root),
    )
    content = b"legacy source promoted after candidate merge"
    legacy_file_id = uuid4()
    legacy_key = f"cvs/{legacy_file_id}.pdf"
    legacy_reference = f"storage/{legacy_key}"
    legacy_source = storage_root / legacy_key
    legacy_source.parent.mkdir(parents=True)
    legacy_source.write_bytes(content)
    alias.pdf_path = legacy_reference
    await db.commit()
    organization_id = organization.id
    primary_id = primary.id
    alias_id = alias.id
    user_id = user.id

    await merge_candidate_duplicates(
        primary_id,
        CandidateMergeRequest(duplicate_ids=[alias_id]),
        db=db,
        user=user,
    )

    db.expire_all()
    merged_primary = await db.get(Candidate, primary_id)
    merged_alias = await db.get(Candidate, alias_id)
    assert merged_primary is not None and merged_alias is not None
    assert merged_primary.pdf_path == legacy_reference
    assert merged_alias.pdf_path is None

    private_object_id = uuid4()
    object_key = (
        f"private/{organization_id}/candidates/{private_object_id}.pdf"
    )
    canonical_source = storage_root / object_key
    canonical_source.parent.mkdir(parents=True)
    canonical_source.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    private_object = PrivateObject(
        id=private_object_id,
        organization_id=organization_id,
        category="candidates",
        object_key=object_key,
        original_filename=f"{legacy_file_id}.pdf",
        media_type="application/pdf",
        size_bytes=len(content),
        sha256=digest,
        state="ready",
        scanner_engine="test",
        scanner_verdict="clean",
        resource_type="candidate",
        resource_id=primary_id,
        ready_at=datetime.now(UTC),
    )
    db.add(private_object)
    await db.flush()
    cutover_at = datetime.now(UTC)
    deletion = LegacyPrivateObjectDeletion(
        organization_id=organization_id,
        category="candidates",
        normalized_key=legacy_key,
        resource_type="candidate",
        resource_id=primary_id,
        size_bytes=len(content),
        sha256_snapshot=digest,
        reason="replacement",
        requested_at=cutover_at,
        not_before=cutover_at,
        attempt_count=1,
        next_attempt_at=cutover_at,
        completed_at=cutover_at,
    )
    db.add(deletion)
    merged_primary.pdf_path = object_key
    merged_primary.private_object_id = private_object_id
    await db.commit()
    legacy_source.unlink()

    user = await db.get(User, user_id)
    assert user is not None
    evidence = await get_merged_candidate_source_evidence(
        primary_id,
        alias_id,
        db=db,
        user=user,
    )
    assert evidence.has_original_file is True
    download = await download_merged_candidate_source_cv(
        primary_id,
        alias_id,
        db=db,
        user=user,
    )
    assert b"".join([part async for part in download.body_iterator]) == content

    deletion.sha256_snapshot = "f" * 64
    await db.commit()
    user = await db.get(User, user_id)
    assert user is not None
    with pytest.raises(HTTPException) as mismatched_provenance:
        await get_merged_candidate_source_evidence(
            primary_id,
            alias_id,
            db=db,
            user=user,
        )
    assert mismatched_provenance.value.status_code == 404


async def test_merged_source_registered_cv_uses_moved_owner_and_snapshot_integrity(
    db, tmp_path, monkeypatch
):
    organization, user = await seed_tenant(db, "merge-registered-evidence")
    primary = await add_candidate(db, organization, "registered-primary")
    alias = await add_candidate(db, organization, "registered-alias")
    organization_id = organization.id

    from app.config import get_settings

    storage_root = tmp_path / "registered-objects"
    monkeypatch.setattr(get_settings(), "storage_dir", str(storage_root))
    content = b"registered merged source CV"
    private_object_id = uuid4()
    object_key = f"private/{organization.id}/candidates/{private_object_id}.pdf"
    stored_cv = storage_root / object_key
    stored_cv.parent.mkdir(parents=True)
    stored_cv.write_bytes(content)
    private_object = PrivateObject(
        id=private_object_id,
        organization_id=organization.id,
        category="candidates",
        object_key=object_key,
        original_filename="registered-alias.pdf",
        media_type="application/pdf",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        state="ready",
        scanner_engine="test",
        scanner_verdict="clean",
        resource_type="candidate",
        resource_id=alias.id,
        ready_at=datetime.now(UTC),
    )
    db.add(private_object)
    await db.flush()
    alias.pdf_path = object_key
    alias.private_object_id = private_object.id
    await db.commit()
    primary_id = primary.id
    alias_id = alias.id
    user_id = user.id

    result = await merge_candidate_duplicates(
        primary_id,
        CandidateMergeRequest(duplicate_ids=[alias_id]),
        db=db,
        user=user,
    )

    db.expire_all()
    user = await db.get(User, user_id)
    merged_primary = await db.get(Candidate, primary_id)
    merged_alias = await db.get(Candidate, alias_id)
    moved_object = await db.get(PrivateObject, private_object_id)
    assert user is not None
    assert merged_primary is not None and merged_alias is not None
    assert moved_object is not None
    assert merged_primary.private_object_id == private_object_id
    assert merged_primary.pdf_path == object_key
    assert merged_alias.private_object_id is None
    assert merged_alias.pdf_path is None
    assert moved_object.resource_id == primary_id

    evidence = await get_merged_candidate_source_evidence(
        primary_id,
        alias_id,
        db=db,
        user=user,
    )
    assert evidence.has_original_file is True
    download = await download_merged_candidate_source_cv(
        primary_id,
        alias_id,
        db=db,
        user=user,
    )
    assert b"".join([part async for part in download.body_iterator]) == content
    assert download.headers["cache-control"] == "private, no-store, max-age=0"
    assert download.headers["x-content-type-options"] == "nosniff"
    audit = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.organization_id == organization_id,
                AuditLog.action == "download_merged_candidate_cv",
                AuditLog.resource_id == alias_id,
            )
        )
    ).scalar_one()
    assert audit.metadata_json["canonical_candidate_id"] == str(primary_id)
    assert audit.metadata_json["sensitive_read"] is True

    batch = await db.get(CandidateMergeBatch, result.batch_id)
    assert batch is not None
    corrupted_ciphertext = batch.snapshot_ciphertext[:-1] + bytes(
        [batch.snapshot_ciphertext[-1] ^ 0x01]
    )
    batch.snapshot_ciphertext = corrupted_ciphertext
    with db.no_autoflush:
        with pytest.raises(HTTPException) as corrupted_evidence:
            await get_merged_candidate_source_evidence(
                primary_id,
                alias_id,
                db=db,
                user=user,
            )
    assert corrupted_evidence.value.status_code == 404
    db.expire(batch, ["snapshot_ciphertext"])


async def test_merged_source_without_pre_merge_file_reports_none(db):
    organization, user = await seed_tenant(db, "merge-no-original-file")
    primary = await add_candidate(db, organization, "no-file-primary")
    alias = await add_candidate(db, organization, "no-file-alias")
    await db.commit()
    primary_id = primary.id
    alias_id = alias.id

    await merge_candidate_duplicates(
        primary_id,
        CandidateMergeRequest(duplicate_ids=[alias_id]),
        db=db,
        user=user,
    )

    evidence = await get_merged_candidate_source_evidence(
        primary_id,
        alias_id,
        db=db,
        user=user,
    )
    assert evidence.has_original_file is False
    with pytest.raises(HTTPException) as missing_download:
        await download_merged_candidate_source_cv(
            primary_id,
            alias_id,
            db=db,
            user=user,
        )
    assert missing_download.value.status_code == 404


async def test_merge_can_select_alias_score_and_search_preserved_source(db):
    organization, user = await seed_tenant(db, "merge-winner")
    primary = await add_candidate(
        db,
        organization,
        "merge-winner-primary",
        raw_cv="Canonical profile without the historical specialty.",
    )
    alias = await add_candidate(
        db,
        organization,
        "merge-winner-alias",
        raw_cv="Historical profile with uniquearchitectkeyword expertise.",
    )
    position = await add_position(db, organization, user, "Architecture")
    primary_score = await add_score(
        db, organization, primary, position, score=61, stage="screening"
    )
    alias_score = await add_score(
        db, organization, alias, position, score=96, stage="offer"
    )
    await db.commit()
    primary_id = primary.id
    alias_id = alias.id
    user_id = user.id
    primary_score_id = primary_score.id
    alias_score_id = alias_score.id

    await merge_candidate_duplicates(
        primary_id,
        CandidateMergeRequest(
            duplicate_ids=[alias_id],
            score_winners=[
                CandidateMergeScoreWinner(
                    position_id=position.id,
                    winner_candidate_id=alias_id,
                )
            ],
        ),
        db=db,
        user=user,
    )

    db.expire_all()
    user = await db.get(User, user_id)
    assert user is not None
    assert (await db.get(Score, alias_score_id)).candidate_id == primary_id
    assert (await db.get(Score, primary_score_id)).candidate_id == alias_id
    page = await run_search(db, user, q="uniquearchitectkeyword")
    assert [item.id for item in page.items] == [primary_id]

    sources = await list_merged_candidate_sources(primary_id, db=db, user=user)
    live = next(
        score for source in sources.items for score in source.scores if score.selected
    )
    retained = next(
        score for source in sources.items for score in source.scores if not score.selected
    )
    assert live.id == alias_score_id
    assert retained.id == primary_score_id

    canonical = await db.get(Candidate, primary_id)
    assert canonical is not None
    await db.delete(canonical)
    await db.commit()
    db.expire_all()
    assert await db.get(Candidate, alias_id) is None
