"""Candidate merge coverage for registered and legacy CV ownership."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.db import clear_tenant_context, set_tenant_context
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.candidate_merge import CandidateMergeBatch
from app.models.organization import Organization
from app.models.position import Position
from app.models.private_object import PrivateObject
from app.models.requisition import Requisition
from app.models.user import User
from app.services.candidate_merge import (
    _capture_merge_graph,
    _state_digest,
    merge_candidates,
    reverse_candidate_merge,
)

pytestmark = pytest.mark.asyncio


async def _seed_candidates(db, label: str, *, count: int = 2):
    organization = Organization(
        name=f"Candidate merge objects {label}",
        slug=f"candidate-merge-objects-{label}-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    recruiter = User(
        organization_id=organization.id,
        email=f"candidate-merge-{label}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    candidates = [
        Candidate(
            organization_id=organization.id,
            source="manual" if index == 0 else "referral",
            source_ref=f"{label}-{index}",
        )
        for index in range(count)
    ]
    db.add_all([recruiter, *candidates])
    await db.commit()
    return organization, recruiter, candidates


async def _bind_registered_cv(
    db,
    *,
    organization: Organization,
    candidate: Candidate,
    label: str,
) -> PrivateObject:
    object_id = uuid4()
    object_key = f"private/{organization.id}/candidates/{object_id}.pdf"
    private_object = PrivateObject(
        id=object_id,
        organization_id=organization.id,
        category="candidates",
        object_key=object_key,
        original_filename=f"{label}.pdf",
        media_type="application/pdf",
        size_bytes=128,
        sha256="a" * 64,
        state="ready",
        scanner_engine="test",
        scanner_verdict="clean",
        resource_type="candidate",
        resource_id=candidate.id,
        ready_at=datetime.now(UTC),
    )
    db.add(private_object)
    await db.flush()
    candidate.pdf_path = object_key
    candidate.private_object_id = private_object.id
    await db.commit()
    return private_object


async def _merge(db, organization, recruiter, primary, aliases):
    return await merge_candidates(
        db,
        organization_id=organization.id,
        actor_user_id=recruiter.id,
        primary_candidate_id=primary.id,
        duplicate_candidate_ids=[alias.id for alias in aliases],
        score_winners={},
    )


async def test_merge_moves_registered_cv_and_registry_owner_to_empty_primary(db):
    organization, recruiter, (primary, alias) = await _seed_candidates(db, "registered")
    private_object = await _bind_registered_cv(
        db,
        organization=organization,
        candidate=alias,
        label="registered-alias",
    )
    primary_id = primary.id
    alias_id = alias.id
    private_object_id = private_object.id
    alias_path = alias.pdf_path

    await _merge(db, organization, recruiter, primary, [alias])

    db.expire_all()
    restored_primary = await db.get(Candidate, primary_id)
    restored_alias = await db.get(Candidate, alias_id)
    restored_object = await db.get(PrivateObject, private_object_id)
    assert restored_primary is not None and restored_alias is not None
    assert restored_object is not None
    assert restored_primary.pdf_path == alias_path
    assert restored_primary.private_object_id == private_object_id
    assert restored_alias.pdf_path is None
    assert restored_alias.private_object_id is None
    assert restored_object.resource_id == primary_id
    assert restored_object.organization_id == restored_primary.organization_id
    assert restored_object.category == "candidates"
    assert restored_object.resource_type == "candidate"


@pytest.mark.parametrize("document_kind", ["legacy", "registered"])
async def test_merge_rejects_turning_a_candidate_family_into_an_alias(
    db,
    document_kind,
):
    organization, recruiter, (first_primary, next_primary, oldest_source) = (
        await _seed_candidates(db, f"chained-{document_kind}", count=3)
    )
    organization_id = organization.id
    first_primary_id = first_primary.id
    next_primary_id = next_primary.id
    oldest_source_id = oldest_source.id
    if document_kind == "legacy":
        oldest_source.pdf_path = f"storage/cvs/{uuid4()}.pdf"
        await db.commit()
    else:
        await _bind_registered_cv(
            db,
            organization=organization,
            candidate=oldest_source,
            label="chained-registered",
        )

    first_outcome = await _merge(
        db,
        organization,
        recruiter,
        first_primary,
        [oldest_source],
    )
    with pytest.raises(HTTPException) as chained:
        await _merge(
            db,
            organization,
            recruiter,
            next_primary,
            [first_primary],
        )

    assert chained.value.status_code == 409
    assert chained.value.detail == {
        "code": "candidate_merge_history_must_remain_canonical"
    }
    await db.rollback()
    db.expire_all()
    preserved_primary = await db.get(Candidate, first_primary_id)
    preserved_source = await db.get(Candidate, oldest_source_id)
    untouched_next = await db.get(Candidate, next_primary_id)
    assert preserved_primary is not None
    assert preserved_primary.merged_into_candidate_id is None
    assert preserved_source is not None
    assert preserved_source.merged_into_candidate_id == first_primary_id
    assert untouched_next is not None
    assert untouched_next.merged_into_candidate_id is None
    batches = list(
        (
            await db.execute(
                select(CandidateMergeBatch).where(
                    CandidateMergeBatch.organization_id == organization_id
                )
            )
        ).scalars()
    )
    assert [batch.id for batch in batches] == [first_outcome.batch_id]


async def test_assignment_scope_cannot_hide_descendant_from_chain_guard(db):
    organization, recruiter, (first_primary, next_primary, oldest_source) = (
        await _seed_candidates(db, "chained-assignment", count=3)
    )
    organization_id = organization.id
    first_primary_id = first_primary.id
    next_primary_id = next_primary.id
    oldest_source_id = oldest_source.id
    first_outcome = await _merge(
        db,
        organization,
        recruiter,
        first_primary,
        [oldest_source],
    )
    await db.commit()

    hiring_manager = User(
        organization_id=organization_id,
        email=f"merge-manager-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="hiring_manager",
        locale="en",
    )
    position = Position(
        organization_id=organization.id,
        title="Merge assignment guard",
        created_by=recruiter.id,
    )
    db.add_all([hiring_manager, position])
    await db.flush()
    db.add_all(
        [
            Requisition(
                organization_id=organization.id,
                created_by_user_id=recruiter.id,
                hiring_manager_user_id=hiring_manager.id,
                position_id=position.id,
                title=position.title,
                business_justification="Exercise canonical merge history",
            ),
            Application(
                organization_id=organization.id,
                candidate_id=first_primary.id,
                position_id=position.id,
                source="manual",
            ),
            Application(
                organization_id=organization.id,
                candidate_id=next_primary.id,
                position_id=position.id,
                source="manual",
            ),
        ]
    )
    await db.commit()
    await set_tenant_context(
        db,
        organization_id=organization.id,
        actor_id=hiring_manager.id,
        identity_kind="user",
        role="hiring_manager",
    )

    with pytest.raises(HTTPException) as chained:
        await merge_candidates(
            db,
            organization_id=organization.id,
            actor_user_id=hiring_manager.id,
            primary_candidate_id=next_primary.id,
            duplicate_candidate_ids=[first_primary.id],
            score_winners={},
        )

    assert chained.value.status_code == 409
    assert chained.value.detail == {
        "code": "candidate_merge_history_must_remain_canonical"
    }
    await db.rollback()
    clear_tenant_context(db)
    db.expire_all()
    preserved_primary = await db.get(Candidate, first_primary_id)
    preserved_source = await db.get(Candidate, oldest_source_id)
    untouched_next = await db.get(Candidate, next_primary_id)
    assert preserved_primary is not None
    assert preserved_primary.merged_into_candidate_id is None
    assert preserved_source is not None
    assert preserved_source.merged_into_candidate_id == first_primary_id
    assert untouched_next is not None
    assert untouched_next.merged_into_candidate_id is None
    batches = list(
        (
            await db.execute(
                select(CandidateMergeBatch).where(
                    CandidateMergeBatch.organization_id == organization_id
                )
            )
        ).scalars()
    )
    assert [batch.id for batch in batches] == [first_outcome.batch_id]


async def test_merge_moves_one_legacy_path_and_clears_identical_aliases(db):
    organization, recruiter, candidates = await _seed_candidates(
        db, "legacy-move", count=3
    )
    primary, first_alias, second_alias = candidates
    candidate_ids = [candidate.id for candidate in candidates]
    shared_path = f"storage/cvs/{uuid4()}.pdf"
    first_alias.pdf_path = shared_path
    second_alias.pdf_path = shared_path
    await db.commit()

    await _merge(db, organization, recruiter, primary, candidates[1:])

    db.expire_all()
    restored = [await db.get(Candidate, candidate_id) for candidate_id in candidate_ids]
    assert restored[0] is not None and restored[0].pdf_path == shared_path
    assert restored[1] is not None and restored[1].pdf_path is None
    assert restored[2] is not None and restored[2].pdf_path is None
    assert all(candidate.private_object_id is None for candidate in restored)


async def test_merge_deduplicates_legacy_aliases_beside_distinct_primary_cv(db):
    organization, recruiter, candidates = await _seed_candidates(
        db, "legacy-distinct", count=3
    )
    primary, first_alias, second_alias = candidates
    primary_id = primary.id
    first_alias_id = first_alias.id
    second_alias_id = second_alias.id
    primary_path = f"storage/cvs/{uuid4()}.pdf"
    alias_path = f"storage/cvs/{uuid4()}.pdf"
    primary.pdf_path = primary_path
    first_alias.pdf_path = alias_path
    second_alias.pdf_path = alias_path
    await db.commit()

    await _merge(db, organization, recruiter, primary, candidates[1:])

    db.expire_all()
    restored_primary = await db.get(Candidate, primary_id)
    restored_first = await db.get(Candidate, first_alias_id)
    restored_second = await db.get(Candidate, second_alias_id)
    assert restored_primary is not None and restored_primary.pdf_path == primary_path
    assert restored_first is not None and restored_first.pdf_path == alias_path
    assert restored_second is not None and restored_second.pdf_path is None


async def test_merge_retains_existing_distinct_registered_primary_document(db):
    organization, recruiter, (primary, alias) = await _seed_candidates(
        db, "existing-primary"
    )
    primary_object = await _bind_registered_cv(
        db,
        organization=organization,
        candidate=primary,
        label="primary",
    )
    alias_object = await _bind_registered_cv(
        db,
        organization=organization,
        candidate=alias,
        label="alias",
    )
    primary_id = primary.id
    alias_id = alias.id
    primary_object_id = primary_object.id
    alias_object_id = alias_object.id
    primary_path = primary.pdf_path
    alias_path = alias.pdf_path

    await _merge(db, organization, recruiter, primary, [alias])

    db.expire_all()
    restored_primary = await db.get(Candidate, primary_id)
    restored_alias = await db.get(Candidate, alias_id)
    restored_primary_object = await db.get(PrivateObject, primary_object_id)
    restored_alias_object = await db.get(PrivateObject, alias_object_id)
    assert restored_primary is not None and restored_alias is not None
    assert restored_primary.pdf_path == primary_path
    assert restored_primary.private_object_id == primary_object_id
    assert restored_alias.pdf_path == alias_path
    assert restored_alias.private_object_id == alias_object_id
    assert restored_primary_object is not None
    assert restored_primary_object.resource_id == primary_id
    assert restored_alias_object is not None
    assert restored_alias_object.resource_id == alias_id


async def test_reversal_restores_exact_registered_candidate_binding(db):
    organization, recruiter, (primary, alias) = await _seed_candidates(db, "reversal")
    private_object = await _bind_registered_cv(
        db,
        organization=organization,
        candidate=alias,
        label="reversible",
    )
    primary_id = primary.id
    alias_id = alias.id
    private_object_id = private_object.id
    alias_path = alias.pdf_path
    organization_id = organization.id
    before = await _capture_merge_graph(
        db,
        organization_id=organization_id,
        candidate_ids=[primary_id, alias_id],
        lock=False,
    )

    outcome = await _merge(db, organization, recruiter, primary, [alias])
    await reverse_candidate_merge(
        db,
        organization_id=organization_id,
        actor_user_id=recruiter.id,
        actor_role="recruiter",
        batch_id=outcome.batch_id,
        expected_version=1,
    )

    db.expire_all()
    restored_primary = await db.get(Candidate, primary_id)
    restored_alias = await db.get(Candidate, alias_id)
    restored_object = await db.get(PrivateObject, private_object_id)
    assert restored_primary is not None
    assert restored_primary.pdf_path is None
    assert restored_primary.private_object_id is None
    assert restored_alias is not None
    assert restored_alias.pdf_path == alias_path
    assert restored_alias.private_object_id == private_object_id
    assert restored_object is not None and restored_object.resource_id == alias_id
    restored_graph = await _capture_merge_graph(
        db,
        organization_id=organization_id,
        candidate_ids=[primary_id, alias_id],
        tracked=(before,),
        lock=False,
    )
    assert _state_digest(restored_graph) == _state_digest(before)


async def test_merge_rejects_cross_tenant_registry_resource_binding(db):
    organization, recruiter, (primary, alias) = await _seed_candidates(
        db, "invalid-binding"
    )
    private_object = await _bind_registered_cv(
        db,
        organization=organization,
        candidate=alias,
        label="invalid-binding",
    )
    private_object_id = private_object.id
    other_organization, _other_user, (other_candidate, _) = await _seed_candidates(
        db, "invalid-binding-other"
    )
    assert other_organization.id != organization.id
    private_object.resource_id = other_candidate.id
    await db.commit()

    with pytest.raises(HTTPException) as invalid:
        await _merge(db, organization, recruiter, primary, [alias])

    assert invalid.value.status_code == 409
    assert invalid.value.detail == {"code": "candidate_private_object_binding_invalid"}
    await db.refresh(primary)
    await db.refresh(alias)
    assert primary.merged_at is None and primary.private_object_id is None
    assert alias.merged_at is None and alias.private_object_id == private_object_id


async def test_merge_rejects_registered_cv_path_that_does_not_match_registry(db):
    organization, recruiter, (primary, alias) = await _seed_candidates(
        db,
        "invalid-object-key",
    )
    private_object = await _bind_registered_cv(
        db,
        organization=organization,
        candidate=alias,
        label="invalid-object-key",
    )
    alias.pdf_path = f"private/{organization.id}/candidates/{uuid4()}.pdf"
    await db.commit()

    with pytest.raises(HTTPException) as invalid:
        await _merge(db, organization, recruiter, primary, [alias])

    assert invalid.value.status_code == 409
    assert invalid.value.detail == {
        "code": "candidate_private_object_binding_invalid"
    }
    await db.refresh(alias)
    assert alias.private_object_id == private_object.id
    assert alias.pdf_path != private_object.object_key


async def test_merge_rejects_unregistered_registry_shaped_candidate_path(db):
    organization, recruiter, (primary, alias) = await _seed_candidates(
        db,
        "unregistered-private-path",
    )
    alias.pdf_path = f"private/{organization.id}/candidates/{uuid4()}.pdf"
    await db.commit()

    with pytest.raises(HTTPException) as invalid:
        await _merge(db, organization, recruiter, primary, [alias])

    assert invalid.value.status_code == 409
    assert invalid.value.detail == {
        "code": "candidate_private_object_binding_invalid"
    }
    await db.refresh(primary)
    await db.refresh(alias)
    assert primary.merged_at is None
    assert alias.merged_at is None


async def test_merge_repairs_transitional_unregistered_copy_of_alias_object(db):
    organization, recruiter, (primary, alias) = await _seed_candidates(
        db,
        "transitional-copy",
    )
    private_object = await _bind_registered_cv(
        db,
        organization=organization,
        candidate=alias,
        label="transitional-copy",
    )
    primary.pdf_path = private_object.object_key
    await db.commit()
    primary_id = primary.id
    alias_id = alias.id
    private_object_id = private_object.id
    object_key = private_object.object_key

    await _merge(db, organization, recruiter, primary, [alias])

    db.expire_all()
    restored_primary = await db.get(Candidate, primary_id)
    restored_alias = await db.get(Candidate, alias_id)
    restored_object = await db.get(PrivateObject, private_object_id)
    assert restored_primary is not None
    assert restored_primary.private_object_id == private_object_id
    assert restored_primary.pdf_path == object_key
    assert restored_alias is not None
    assert restored_alias.private_object_id is None
    assert restored_alias.pdf_path is None
    assert restored_object is not None
    assert restored_object.resource_id == primary_id


async def test_reversal_fails_closed_after_cross_tenant_registry_corruption(db):
    organization, recruiter, (primary, alias) = await _seed_candidates(
        db, "corrupt-reversal"
    )
    private_object = await _bind_registered_cv(
        db,
        organization=organization,
        candidate=alias,
        label="corrupt-reversal",
    )
    primary_id = primary.id
    alias_id = alias.id
    private_object_id = private_object.id
    outcome = await _merge(db, organization, recruiter, primary, [alias])
    other_organization, _other_user, (other_candidate, _) = await _seed_candidates(
        db, "corrupt-reversal-other"
    )
    assert other_organization.id != organization.id
    private_object = await db.get(PrivateObject, private_object_id)
    assert private_object is not None
    private_object.resource_id = other_candidate.id
    await db.commit()

    with pytest.raises(HTTPException) as diverged:
        await reverse_candidate_merge(
            db,
            organization_id=organization.id,
            actor_user_id=recruiter.id,
            actor_role="recruiter",
            batch_id=outcome.batch_id,
            expected_version=1,
        )

    assert diverged.value.status_code == 409
    assert diverged.value.detail == {"code": "candidate_merge_diverged"}
    db.expire_all()
    batch = await db.get(CandidateMergeBatch, outcome.batch_id)
    merged_primary = await db.get(Candidate, primary_id)
    merged_alias = await db.get(Candidate, alias_id)
    assert batch is not None and batch.status == "merged"
    assert merged_primary is not None
    assert merged_primary.private_object_id == private_object_id
    assert merged_alias is not None and merged_alias.private_object_id is None
