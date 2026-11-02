"""Candidate discovery, saved views, duplicate merge, bulk actions, and export."""

import csv
import hashlib
import io
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy import (
    String,
    and_,
    column,
    delete,
    distinct,
    exists,
    func,
    literal_column,
    or_,
    select,
    union_all,
    values,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.auth.authorization import FieldClass
from app.auth.dependencies import current_user, user_with_permissions
from app.db import get_session
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.candidate_merge import CandidateMergeBatch
from app.models.candidate_note import CandidateTag
from app.models.candidate_saved_view import CandidateSavedView
from app.models.enterprise import AuditLog
from app.models.position import Position
from app.models.score import Score
from app.models.user import User
from app.schemas.applications import ApplicationStageTransition
from app.schemas.candidate_search import (
    CANDIDATE_DUPLICATE_GROUP_MEMBER_LIMIT,
    CandidateBulkOut,
    CandidateBulkRequest,
    CandidateDuplicateGroup,
    CandidateDuplicateMember,
    CandidateDuplicatePage,
    CandidateExportRequest,
    CandidateMergeConflict,
    CandidateMergeConflictOption,
    CandidateMergedSourceEvidence,
    CandidateMergedSourceScore,
    CandidateMergedSourceSummary,
    CandidateMergeHistoryItem,
    CandidateMergeHistoryPage,
    CandidateMergeOut,
    CandidateMergePreviewOut,
    CandidateMergePreviewRequest,
    CandidateMergeRequest,
    CandidateMergeReverseOut,
    CandidateMergeReverseRequest,
    CandidateSavedViewCreate,
    CandidateSavedViewOut,
    CandidateSavedViewUpdate,
    CandidateSearchApplication,
    CandidateSearchFilters,
    CandidateSearchItem,
    CandidateSearchPage,
    CandidateStage,
)
from app.schemas.pagination import CursorPage
from app.services.activities import append_candidate_activity
from app.services.applications import (
    apply_application_stage_transition,
)
from app.services.candidate_merge import (
    merge_candidates,
    resolve_canonical_candidate,
    resolve_merged_source_original_document,
    reverse_candidate_merge,
)
from app.services.enterprise import (
    can_access_field,
    get_or_create_policy,
    require_field_access,
)
from app.services.pagination import (
    collection_cursor_scope,
    cursor_datetime,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)
from app.services.uploads import (
    private_blob_response,
    resolve_resource_private_blob_reference,
    safe_download_filename,
    validated_private_media_type,
)

router = APIRouter(tags=["candidate-search"], dependencies=[Depends(current_user)])
candidate_reader = user_with_permissions("candidates.read")
candidate_writer = user_with_permissions("candidates.write")
candidate_exporter = user_with_permissions("candidates.export")


def _require_organization_candidate_mutation(user: User) -> None:
    """Bulk identity rewrites cannot be safely reduced to assigned pairs."""

    if user.role not in {"admin", "recruiter"}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Permission denied")


async def _merged_source_record(
    db: AsyncSession,
    *,
    candidate_id: UUID,
    source_candidate_id: UUID,
    organization_id: UUID,
) -> Candidate:
    """Resolve a source only within the requested canonical candidate family."""

    canonical = await resolve_canonical_candidate(db, candidate_id, organization_id)
    if canonical is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found.")
    source = (
        await db.execute(
            select(Candidate).where(
                Candidate.id == source_candidate_id,
                Candidate.organization_id == organization_id,
                or_(
                    Candidate.id == canonical.id,
                    Candidate.merged_into_candidate_id == canonical.id,
                ),
            )
        )
    ).scalar_one_or_none()
    if source is None:
        # Do not reveal whether the source exists in a different tenant or
        # belongs to another canonical candidate.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Merged source not found.")
    return source


def _encode_cursor(
    created_at: datetime,
    candidate_id: UUID,
    *,
    scope: str,
) -> str:
    return encode_collection_cursor(
        "candidate-search.results",
        scope=scope,
        boundary={
            "created_at": created_at.isoformat(),
            "id": str(candidate_id),
        },
    )


def _decode_cursor(
    cursor: str, *, scope: str
) -> tuple[datetime, UUID]:
    payload = decode_collection_cursor(
        cursor,
        namespace="candidate-search.results",
        scope=scope,
        boundary_keys={"created_at", "id"},
        detail="Invalid candidate-search cursor",
    )
    return (
        cursor_datetime(payload, "created_at", detail="Invalid candidate-search cursor"),
        cursor_uuid(payload, detail="Invalid candidate-search cursor"),
    )


def _candidate_conditions(
    filters: CandidateSearchFilters,
    organization_id: UUID,
    *,
    assessment_results_visible: bool,
) -> list:
    conditions = [
        Candidate.organization_id == organization_id,
        Candidate.merged_at.is_(None),
    ]
    if filters.sources:
        source_alias = aliased(Candidate)
        conditions.append(
            or_(
                func.lower(Candidate.source).in_(filters.sources),
                exists(
                    select(source_alias.id).where(
                        source_alias.organization_id == organization_id,
                        source_alias.merged_into_candidate_id == Candidate.id,
                        func.lower(source_alias.source).in_(filters.sources),
                    )
                ),
            )
        )
    if filters.favorites_only:
        conditions.append(Candidate.is_favorite.is_(True))
    if filters.new_only:
        conditions.append(Candidate.viewed_at.is_(None))
    if filters.q:
        query = func.websearch_to_tsquery(
            literal_column("'simple'::regconfig"), filters.q
        )
        direct_match = aliased(Candidate)
        search_alias = aliased(Candidate)
        matching_candidate_ids = union_all(
            select(direct_match.id).where(
                direct_match.organization_id == organization_id,
                direct_match.search_document.op("@@")(query),
            ),
            select(search_alias.merged_into_candidate_id).where(
                search_alias.organization_id == organization_id,
                search_alias.merged_into_candidate_id.is_not(None),
                search_alias.search_document.op("@@")(query),
            )
        ).cte("candidate_search_full_text_matches")
        matching_candidate_ids = matching_candidate_ids.prefix_with(
            "MATERIALIZED", dialect="postgresql"
        )
        # Keeping the full-text predicates in independent UNION branches lets
        # PostgreSQL use the GIN index for both canonical rows and aliases. An
        # OR-correlated EXISTS forces a tenant-wide candidate scan at 10k rows.
        conditions.append(
            Candidate.id.in_(select(matching_candidate_ids.c.id))
        )
    if filters.tags:
        tag_count = (
            select(func.count(distinct(CandidateTag.label)))
            .where(
                CandidateTag.organization_id == organization_id,
                CandidateTag.candidate_id == Candidate.id,
                CandidateTag.label.in_(filters.tags),
            )
            .correlate(Candidate)
            .scalar_subquery()
        )
        conditions.append(tag_count == len(filters.tags))

    application_filtering = any(
        (
            filters.position_id is not None,
            bool(filters.stages),
            filters.score_min is not None,
            filters.score_max is not None,
        )
    )
    if application_filtering:
        application = aliased(Application)
        score = aliased(Score)
        application_conditions = [
            application.organization_id == organization_id,
            application.candidate_id == Candidate.id,
            application.archived_at.is_(None),
        ]
        if filters.position_id is not None:
            application_conditions.append(
                application.position_id == filters.position_id
            )
        if filters.stages:
            application_conditions.append(application.stage.in_(filters.stages))
        application_query = select(application.id)
        if assessment_results_visible and any(
            (
                filters.score_min is not None,
                filters.score_max is not None,
                filters.unscored_only,
            )
        ):
            application_query = application_query.outerjoin(
                score,
                and_(
                    score.id == application.legacy_score_id,
                    score.organization_id == application.organization_id,
                ),
            )
        if filters.score_min is not None:
            application_conditions.append(score.overall_score >= filters.score_min)
        if filters.score_max is not None:
            application_conditions.append(score.overall_score <= filters.score_max)
        if filters.unscored_only:
            application_conditions.append(score.overall_score.is_(None))
        conditions.append(
            exists(application_query.where(*application_conditions))
        )
    elif filters.unscored_only:
        # The caller is field-authorized before this helper is invoked.  This
        # count/filter semantic would otherwise reveal which candidates have
        # assessment results even if every response value were nulled.
        scored = aliased(Score)
        conditions.append(
            ~exists(
                select(scored.id).where(
                    scored.organization_id == organization_id,
                    scored.candidate_id == Candidate.id,
                    scored.overall_score.is_not(None),
                )
            )
        )
    return conditions


@dataclass
class _DuplicateProfile:
    id: UUID
    source: str
    source_ref: str | None
    created_at: datetime
    keys: set[tuple[str, str]] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _DuplicateIdentityRow:
    """The narrow candidate projection needed for duplicate discovery."""

    id: UUID
    merged_into_candidate_id: UUID | None
    merged_at: datetime | None
    source: str
    source_ref: str | None
    phone_normalized: str | None
    email_normalized: str | None
    github_login: str | None
    duplicate_cv_fingerprint: str | None
    created_at: datetime


_ALL_DUPLICATE_IDENTITY_KINDS = frozenset(
    {"phone", "email", "source_ref", "github", "cv"}
)


def _visible_duplicate_identity_kinds(
    *, contact_visible: bool, document_visible: bool
) -> frozenset[str]:
    """Return only identity evidence the caller may use for inference.

    Duplicate counts, groups, match types, and cursor boundaries are all response
    side channels.  Building those values from a denied identity class would leak
    the hidden value even if the value itself were later nulled in serialization.
    """

    kinds: set[str] = set()
    if contact_visible:
        kinds.update({"phone", "email", "source_ref"})
    if document_visible:
        kinds.add("cv")
    if contact_visible and document_visible:
        kinds.add("github")
    return frozenset(kinds)


def _duplicate_identity_columns() -> tuple:
    return (
        Candidate.id,
        Candidate.merged_into_candidate_id,
        Candidate.merged_at,
        Candidate.source,
        Candidate.source_ref,
        Candidate.phone_normalized,
        Candidate.email_normalized,
        Candidate.github_login,
        Candidate.duplicate_cv_fingerprint,
        Candidate.created_at,
    )


def _identity_row(candidate: Candidate | object) -> _DuplicateIdentityRow:
    return _DuplicateIdentityRow(
        id=candidate.id,
        merged_into_candidate_id=candidate.merged_into_candidate_id,
        merged_at=candidate.merged_at,
        source=candidate.source,
        source_ref=candidate.source_ref,
        phone_normalized=candidate.phone_normalized,
        email_normalized=candidate.email_normalized,
        github_login=candidate.github_login,
        duplicate_cv_fingerprint=candidate.duplicate_cv_fingerprint,
        created_at=candidate.created_at,
    )


def _identity_keys(
    candidate: _DuplicateIdentityRow,
    allowed_kinds: frozenset[str] = _ALL_DUPLICATE_IDENTITY_KINDS,
) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    if "phone" in allowed_kinds and candidate.phone_normalized:
        keys.add(("phone", candidate.phone_normalized))
    if "email" in allowed_kinds and candidate.email_normalized:
        keys.add(("email", candidate.email_normalized))
    if "source_ref" in allowed_kinds and candidate.source_ref:
        keys.add(
            (
                "source_ref",
                f"{candidate.source.lower()}:{candidate.source_ref.strip().lower()}",
            )
        )
    if "github" in allowed_kinds and candidate.github_login:
        keys.add(("github", candidate.github_login.strip().lower()))
    if "cv" in allowed_kinds and candidate.duplicate_cv_fingerprint:
        keys.add(("cv", candidate.duplicate_cv_fingerprint))
    return keys


async def _load_identity_rows_by_ids(
    db: AsyncSession,
    organization_id: UUID,
    candidate_ids: set[UUID],
) -> list[_DuplicateIdentityRow]:
    if not candidate_ids:
        return []
    rows = (
        await db.execute(
            select(*_duplicate_identity_columns()).where(
                Candidate.organization_id == organization_id,
                Candidate.id.in_(candidate_ids),
            )
        )
    ).all()
    return [_identity_row(row) for row in rows]


async def _duplicate_counts_for_candidates(
    db: AsyncSession,
    candidates: list[Candidate],
    organization_id: UUID,
    *,
    allowed_identity_kinds: frozenset[str] = _ALL_DUPLICATE_IDENTITY_KINDS,
) -> dict[UUID, int]:
    """Return exact duplicate-component counts without loading the whole tenant.

    Candidate search only needs counts for the current page. Duplicate identities
    can form transitive components and merged aliases contribute their original
    identities, so this performs an indexed graph expansion from those page roots.
    The dedicated duplicate-management endpoint intentionally keeps using the full
    tenant scan because it must enumerate every component.
    """

    if not candidates:
        return {}

    rows_by_id = {candidate.id: _identity_row(candidate) for candidate in candidates}
    seed_ids = set(rows_by_id)
    root_ids = {
        candidate.id
        for candidate in rows_by_id.values()
        if candidate.merged_into_candidate_id is None and candidate.merged_at is None
    }
    family_root_by_id = {root_id: root_id for root_id in root_ids}
    expanded_parent_ids: set[UUID] = set()
    profiles: dict[UUID, _DuplicateProfile] = {}
    queried_keys: set[tuple[str, str]] = set()

    async def load_root_families(new_root_ids: set[UUID]) -> None:
        missing_roots = new_root_ids.difference(rows_by_id)
        if missing_roots:
            for row in await _load_identity_rows_by_ids(
                db, organization_id, missing_roots
            ):
                rows_by_id[row.id] = row

        frontier: set[UUID] = set()
        for root_id in new_root_ids:
            root = rows_by_id.get(root_id)
            if (
                root is None
                or root.merged_into_candidate_id is not None
                or root.merged_at is not None
            ):
                continue
            family_root_by_id[root_id] = root_id
            profile = profiles.setdefault(
                root_id,
                _DuplicateProfile(
                    id=root.id,
                    source=root.source,
                    source_ref=root.source_ref,
                    created_at=root.created_at,
                ),
            )
            profile.keys.update(_identity_keys(root, allowed_identity_kinds))
            if root_id not in expanded_parent_ids:
                frontier.add(root_id)

        while frontier:
            parent_roots = {
                parent_id: family_root_by_id[parent_id] for parent_id in frontier
            }
            child_rows = (
                await db.execute(
                    select(*_duplicate_identity_columns()).where(
                        Candidate.organization_id == organization_id,
                        Candidate.merged_into_candidate_id.in_(frontier),
                    )
                )
            ).all()
            expanded_parent_ids.update(frontier)
            next_frontier: set[UUID] = set()
            for raw_row in child_rows:
                row = _identity_row(raw_row)
                rows_by_id[row.id] = row
                parent_id = row.merged_into_candidate_id
                if parent_id is None:
                    continue
                root_id = parent_roots[parent_id]
                family_root_by_id[row.id] = root_id
                profiles[root_id].keys.update(
                    _identity_keys(row, allowed_identity_kinds)
                )
                if row.id not in expanded_parent_ids:
                    next_frontier.add(row.id)
            frontier = next_frontier

    async def load_missing_ancestors(rows: list[_DuplicateIdentityRow]) -> None:
        pending = {
            row.merged_into_candidate_id
            for row in rows
            if row.merged_into_candidate_id is not None
            and row.merged_into_candidate_id not in rows_by_id
        }
        while pending:
            loaded = await _load_identity_rows_by_ids(db, organization_id, pending)
            if not loaded:
                break
            pending = set()
            for row in loaded:
                rows_by_id[row.id] = row
                if (
                    row.merged_into_candidate_id is not None
                    and row.merged_into_candidate_id not in rows_by_id
                ):
                    pending.add(row.merged_into_candidate_id)

    def canonical_root_id(candidate_id: UUID) -> UUID | None:
        seen: set[UUID] = set()
        while candidate_id not in seen:
            seen.add(candidate_id)
            row = rows_by_id.get(candidate_id)
            if row is None:
                return None
            if row.merged_into_candidate_id is None:
                return candidate_id if row.merged_at is None else None
            candidate_id = row.merged_into_candidate_id
        return None

    await load_root_families(root_ids)
    while True:
        pending_keys = set().union(
            *(profile.keys for profile in profiles.values())
        ).difference(queried_keys)
        if not pending_keys:
            break
        queried_keys.update(pending_keys)

        phone_values = [value for kind, value in pending_keys if kind == "phone"]
        email_values = [value for kind, value in pending_keys if kind == "email"]
        github_values = [value for kind, value in pending_keys if kind == "github"]
        cv_values = [value for kind, value in pending_keys if kind == "cv"]
        source_pairs = [
            tuple(value.split(":", 1))
            for kind, value in pending_keys
            if kind == "source_ref"
        ]
        match_conditions = []
        if phone_values:
            match_conditions.append(Candidate.phone_normalized.in_(phone_values))
        if email_values:
            match_conditions.append(Candidate.email_normalized.in_(email_values))
        if github_values:
            match_conditions.append(
                func.lower(func.btrim(Candidate.github_login)).in_(github_values)
            )
        if cv_values:
            match_conditions.append(
                Candidate.duplicate_cv_fingerprint.in_(cv_values)
            )
        if not match_conditions and not source_pairs:
            break

        matching_by_id: dict[UUID, _DuplicateIdentityRow] = {}
        if match_conditions:
            non_source_rows = (
                await db.execute(
                    select(*_duplicate_identity_columns()).where(
                        Candidate.organization_id == organization_id,
                        or_(*match_conditions),
                    )
                )
            ).all()
            matching_by_id.update(
                (row.id, _identity_row(row)) for row in non_source_rows
            )
        if source_pairs:
            source_keys = (
                values(
                    column("source", String),
                    column("source_ref", String),
                    name="candidate_source_keys",
                )
                .data(source_pairs)
                .cte()
            )
            source_rows = (
                await db.execute(
                    select(*_duplicate_identity_columns())
                    .join(
                        source_keys,
                        and_(
                            func.lower(Candidate.source) == source_keys.c.source,
                            func.lower(func.btrim(Candidate.source_ref))
                            == source_keys.c.source_ref,
                        ),
                    )
                    .where(Candidate.organization_id == organization_id)
                )
            ).all()
            matching_by_id.update(
                (row.id, _identity_row(row)) for row in source_rows
            )
        matching_rows = list(matching_by_id.values())
        for row in matching_rows:
            rows_by_id[row.id] = row
        await load_missing_ancestors(matching_rows)

        discovered_roots = {
            root_id
            for row in matching_rows
            if (root_id := canonical_root_id(row.id)) is not None
        }
        new_root_ids = discovered_roots.difference(root_ids)
        if not new_root_ids:
            break
        root_ids.update(new_root_ids)
        await load_root_families(new_root_ids)

    _components, counts = _duplicate_components(list(profiles.values()))
    return {candidate_id: counts.get(candidate_id, 0) for candidate_id in seed_ids}


async def _load_duplicate_profiles(
    db: AsyncSession,
    organization_id: UUID,
    *,
    allowed_identity_kinds: frozenset[str] = _ALL_DUPLICATE_IDENTITY_KINDS,
) -> list[_DuplicateProfile]:
    """Load indexed identities only and fold hidden source aliases into roots."""

    rows = (
        await db.execute(
            select(
                Candidate.id,
                Candidate.merged_into_candidate_id,
                Candidate.merged_at,
                Candidate.source,
                Candidate.source_ref,
                Candidate.phone_normalized,
                Candidate.email_normalized,
                Candidate.github_login,
                Candidate.duplicate_cv_fingerprint,
                Candidate.created_at,
            ).where(Candidate.organization_id == organization_id)
        )
    ).all()
    parent_by_id = {row.id: row.merged_into_candidate_id for row in rows}

    def canonical_id(candidate_id: UUID) -> UUID | None:
        seen: set[UUID] = set()
        while candidate_id not in seen:
            seen.add(candidate_id)
            parent_id = parent_by_id.get(candidate_id)
            if parent_id is None:
                return candidate_id
            candidate_id = parent_id
        return None

    profiles = {
        row.id: _DuplicateProfile(
            id=row.id,
            source=row.source,
            source_ref=row.source_ref,
            created_at=row.created_at,
        )
        for row in rows
        if row.merged_into_candidate_id is None and row.merged_at is None
    }
    for row in rows:
        profile = profiles.get(canonical_id(row.id))
        if profile is None:
            continue
        if "phone" in allowed_identity_kinds and row.phone_normalized:
            profile.keys.add(("phone", row.phone_normalized))
        if "email" in allowed_identity_kinds and row.email_normalized:
            profile.keys.add(("email", row.email_normalized))
        if "source_ref" in allowed_identity_kinds and row.source_ref:
            profile.keys.add(
                (
                    "source_ref",
                    f"{row.source.lower()}:{row.source_ref.strip().lower()}",
                )
            )
        if "github" in allowed_identity_kinds and row.github_login:
            profile.keys.add(("github", row.github_login.strip().lower()))
        if "cv" in allowed_identity_kinds and row.duplicate_cv_fingerprint:
            profile.keys.add(("cv", row.duplicate_cv_fingerprint))
    return list(profiles.values())


def _duplicate_components(
    candidates: list[_DuplicateProfile],
) -> tuple[list[tuple[list[_DuplicateProfile], set[str]]], dict[UUID, int]]:
    by_id = {candidate.id: candidate for candidate in candidates}
    parent = {candidate.id: candidate.id for candidate in candidates}
    keys_by_id = {candidate.id: candidate.keys for candidate in candidates}
    key_owners: dict[tuple[str, str], list[UUID]] = defaultdict(list)
    for candidate_id, keys in keys_by_id.items():
        for key in keys:
            key_owners[key].append(candidate_id)

    def find(candidate_id: UUID) -> UUID:
        while parent[candidate_id] != candidate_id:
            parent[candidate_id] = parent[parent[candidate_id]]
            candidate_id = parent[candidate_id]
        return candidate_id

    def union(left: UUID, right: UUID) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for owners in key_owners.values():
        for candidate_id in owners[1:]:
            union(owners[0], candidate_id)

    groups: dict[UUID, list[UUID]] = defaultdict(list)
    for candidate_id in parent:
        groups[find(candidate_id)].append(candidate_id)

    components: list[tuple[list[_DuplicateProfile], set[str]]] = []
    counts: dict[UUID, int] = {candidate_id: 0 for candidate_id in parent}
    for ids in groups.values():
        if len(ids) < 2:
            continue
        shared_types: set[str] = set()
        id_set = set(ids)
        for (match_type, _value), owners in key_owners.items():
            if len(id_set.intersection(owners)) > 1:
                shared_types.add(match_type)
        members = sorted(
            (by_id[candidate_id] for candidate_id in ids),
            key=lambda candidate: (candidate.created_at, candidate.id),
            reverse=True,
        )
        components.append((members, shared_types))
        for candidate_id in ids:
            counts[candidate_id] = len(ids) - 1
    components.sort(key=lambda item: item[0][0].created_at, reverse=True)
    return components, counts


async def _enrich_candidates(
    db: AsyncSession,
    candidates: list[Candidate],
    organization_id: UUID,
    *,
    assessment_results_visible: bool,
    contact_visible: bool,
    document_visible: bool,
) -> list[CandidateSearchItem]:
    if not candidates:
        return []
    ids = [candidate.id for candidate in candidates]
    application_stmt = (
        select(Application, Position)
        .join(Position, Position.id == Application.position_id)
        .where(
            Application.organization_id == organization_id,
            Application.candidate_id.in_(ids),
            Application.archived_at.is_(None),
            Position.organization_id == organization_id,
        )
    )
    if assessment_results_visible:
        application_stmt = application_stmt.add_columns(Score).outerjoin(
            Score,
            and_(
                Score.id == Application.legacy_score_id,
                Score.organization_id == Application.organization_id,
            ),
        )
        application_stmt = application_stmt.order_by(
            Application.candidate_id,
            Score.overall_score.desc().nulls_last(),
            Application.id,
        )
    else:
        application_stmt = application_stmt.order_by(
            Application.candidate_id,
            Application.applied_at.desc(),
            Application.id,
        )
    application_rows = (await db.execute(application_stmt)).all()
    applications: dict[UUID, list[CandidateSearchApplication]] = defaultdict(list)
    for row in application_rows:
        application, position = row[0], row[1]
        score = row[2] if assessment_results_visible else None
        applications[application.candidate_id].append(
            CandidateSearchApplication(
                score_id=score.id if score is not None else None,
                position_id=position.id,
                position_title=position.title,
                stage=application.stage,
                overall_score=score.overall_score if score is not None else None,
            )
        )
    tag_rows = (
        await db.execute(
            select(CandidateTag.candidate_id, CandidateTag.label)
            .where(
                CandidateTag.organization_id == organization_id,
                CandidateTag.candidate_id.in_(ids),
            )
            .order_by(CandidateTag.label)
        )
    ).all()
    tags: dict[UUID, list[str]] = defaultdict(list)
    for candidate_id, label in tag_rows:
        tags[candidate_id].append(label)

    duplicate_counts = await _duplicate_counts_for_candidates(
        db,
        candidates,
        organization_id,
        allowed_identity_kinds=_visible_duplicate_identity_kinds(
            contact_visible=contact_visible,
            document_visible=document_visible,
        ),
    )

    items: list[CandidateSearchItem] = []
    for candidate in candidates:
        candidate_apps = applications.get(candidate.id, [])
        scored_apps = [
            application
            for application in candidate_apps
            if application.score_id is not None
        ]
        best = scored_apps[0] if assessment_results_visible and scored_apps else None
        items.append(
            CandidateSearchItem(
                id=candidate.id,
                source=candidate.source,
                source_ref=candidate.source_ref if contact_visible else None,
                is_favorite=candidate.is_favorite,
                viewed_at=candidate.viewed_at,
                created_at=candidate.created_at,
                best_score=best.overall_score if best else None,
                best_position_id=best.position_id if best else None,
                best_position_title=best.position_title if best else None,
                score_count=len(scored_apps) if assessment_results_visible else 0,
                stages=sorted({application.stage for application in candidate_apps}),
                tags=tags.get(candidate.id, []),
                applications=candidate_apps,
                duplicate_count=duplicate_counts.get(candidate.id, 0),
            )
        )
    return items


@router.get("/candidate-search", response_model=CandidateSearchPage)
async def search_candidates(
    q: str | None = Query(default=None, max_length=200),
    score_min: int | None = Query(default=None, ge=0, le=100),
    score_max: int | None = Query(default=None, ge=0, le=100),
    source: list[str] = Query(default=[]),
    stage: list[CandidateStage] = Query(default=[]),
    position_id: UUID | None = None,
    tag: list[str] = Query(default=[]),
    favorites_only: bool = False,
    new_only: bool = False,
    unscored_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_reader),
) -> CandidateSearchPage:
    try:
        filters = CandidateSearchFilters(
            q=q,
            score_min=score_min,
            score_max=score_max,
            sources=source,
            stages=stage,
            position_id=position_id,
            tags=tag,
            favorites_only=favorites_only,
            new_only=new_only,
            unscored_only=unscored_only,
        )
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            json.loads(exc.json(include_url=False)),
        ) from exc
    assessment_results_visible = await can_access_field(
        db, user, FieldClass.ASSESSMENT_RESULT
    )
    contact_visible = await can_access_field(db, user, FieldClass.CONTACT)
    document_visible = await can_access_field(
        db, user, FieldClass.CANDIDATE_DOCUMENT
    )
    if filters.q:
        # Candidate.search_document combines contact/source references and CV
        # text. A match/count result would reveal either denied field class.
        if not contact_visible:
            await require_field_access(db, user, FieldClass.CONTACT)
        if not document_visible:
            await require_field_access(db, user, FieldClass.CANDIDATE_DOCUMENT)
    if not assessment_results_visible and any(
        (
            filters.score_min is not None,
            filters.score_max is not None,
            filters.unscored_only,
        )
    ):
        # Filtering or counting by a hidden score leaks the result even when
        # the serialized fields themselves are null.
        await require_field_access(db, user, FieldClass.ASSESSMENT_RESULT)
    cursor_scope = collection_cursor_scope(
        "candidate-search.results",
        organization_id=user.organization_id,
        filters={
            key: json.dumps(value, sort_keys=True, separators=(",", ":"))
            if isinstance(value, (list, dict))
            else str(value)
            if value is not None
            else None
            for key, value in filters.model_dump(mode="json").items()
        },
    )
    conditions = _candidate_conditions(
        filters,
        user.organization_id,
        assessment_results_visible=assessment_results_visible,
    )
    stmt = select(Candidate).where(*conditions)
    if cursor:
        cursor_time, cursor_id = _decode_cursor(cursor, scope=cursor_scope)
        stmt = stmt.where(
            or_(
                Candidate.created_at < cursor_time,
                and_(Candidate.created_at == cursor_time, Candidate.id < cursor_id),
            )
        )
    candidates = list(
        (
            await db.execute(
                stmt.order_by(Candidate.created_at.desc(), Candidate.id.desc()).limit(
                    limit + 1
                )
            )
        ).scalars()
    )
    has_more = len(candidates) > limit
    page_candidates = candidates[:limit]
    total = (
        await db.execute(select(func.count(Candidate.id)).where(*conditions))
    ).scalar_one()
    available_sources = list(
        (
            await db.execute(
                select(distinct(Candidate.source))
                .where(
                    Candidate.organization_id == user.organization_id,
                )
                .order_by(Candidate.source)
            )
        ).scalars()
    )
    next_cursor = None
    if has_more and page_candidates:
        last = page_candidates[-1]
        next_cursor = _encode_cursor(
            last.created_at,
            last.id,
            scope=cursor_scope,
        )
    return CandidateSearchPage(
        items=await _enrich_candidates(
            db,
            page_candidates,
            user.organization_id,
            assessment_results_visible=assessment_results_visible,
            contact_visible=contact_visible,
            document_visible=document_visible,
        ),
        total=total,
        available_sources=available_sources,
        next_cursor=next_cursor,
    )


@router.get("/candidate-duplicates", response_model=CandidateDuplicatePage)
async def list_candidate_duplicates(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_reader),
) -> CandidateDuplicatePage:
    assessment_results_visible = await can_access_field(
        db, user, FieldClass.ASSESSMENT_RESULT
    )
    contact_visible = await can_access_field(db, user, FieldClass.CONTACT)
    document_visible = await can_access_field(
        db, user, FieldClass.CANDIDATE_DOCUMENT
    )
    allowed_identity_kinds = _visible_duplicate_identity_kinds(
        contact_visible=contact_visible,
        document_visible=document_visible,
    )
    candidates = await _load_duplicate_profiles(
        db,
        user.organization_id,
        allowed_identity_kinds=allowed_identity_kinds,
    )
    components, _counts = _duplicate_components(candidates)
    group_components: list[tuple[str, list[_DuplicateProfile], set[str]]] = []
    for members, match_types in components:
        stable = ":".join(sorted(str(candidate.id) for candidate in members))
        group_components.append(
            (hashlib.sha256(stable.encode()).hexdigest()[:20], members, match_types)
        )
    group_components.sort(key=lambda item: item[0])
    candidate_count = sum(len(members) for _id, members, _types in group_components)
    namespace = "candidate-search.duplicates"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={
            "merged": False,
            "identity_kinds": ",".join(sorted(allowed_identity_kinds)),
        },
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"id"},
            detail="Invalid candidate duplicates cursor",
        )
        cursor_id = payload["id"]
        if not isinstance(cursor_id, str):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {
                    "code": "cursor_invalid",
                    "detail": "Invalid candidate duplicates cursor",
                },
            )
        group_components = [
            component for component in group_components if component[0] > cursor_id
        ]
    has_more = len(group_components) > limit
    page_components = group_components[:limit]
    duplicate_ids = [
        candidate.id
        for _id, members, _types in page_components
        for candidate in members[:CANDIDATE_DUPLICATE_GROUP_MEMBER_LIMIT]
    ]
    best_by_candidate: dict[UUID, tuple[int | None, str | None]] = {}
    if assessment_results_visible and duplicate_ids:
        rows = (
            await db.execute(
                select(Score.candidate_id, Score.overall_score, Position.title)
                .join(Position, Position.id == Score.position_id)
                .where(
                    Score.organization_id == user.organization_id,
                    Score.candidate_id.in_(duplicate_ids),
                )
                .order_by(
                    Score.candidate_id,
                    Score.overall_score.desc().nulls_last(),
                    Score.id,
                )
            )
        ).all()
        for candidate_id, overall_score, title in rows:
            best_by_candidate.setdefault(candidate_id, (overall_score, title))

    groups: list[CandidateDuplicateGroup] = []
    for group_id, members, match_types in page_components:
        visible_members = members[:CANDIDATE_DUPLICATE_GROUP_MEMBER_LIMIT]
        groups.append(
            CandidateDuplicateGroup(
                id=group_id,
                match_types=sorted(match_types),
                candidates=[
                    CandidateDuplicateMember(
                        id=candidate.id,
                        source=candidate.source,
                        source_ref=candidate.source_ref if contact_visible else None,
                        created_at=candidate.created_at,
                        best_score=best_by_candidate.get(candidate.id, (None, None))[0],
                        best_position_title=best_by_candidate.get(
                            candidate.id, (None, None)
                        )[1],
                    )
                    for candidate in visible_members
                ],
                candidate_count=len(members),
                has_more_candidates=len(members) > len(visible_members),
            )
        )
    return CandidateDuplicatePage(
        items=groups,
        candidate_count=candidate_count,
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={"id": groups[-1].id},
            )
            if has_more and groups
            else None
        ),
    )


@router.post("/candidate-merges/preview", response_model=CandidateMergePreviewOut)
async def preview_candidate_merge(
    body: CandidateMergePreviewRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_reader),
) -> CandidateMergePreviewOut:
    # Score winner conflicts are the purpose of this preview. Redacting them
    # would make the subsequent merge decision unsafe, so the operation is
    # field-gated rather than returning an incomplete preview.
    await require_field_access(db, user, FieldClass.ASSESSMENT_RESULT)
    contact_visible = await can_access_field(db, user, FieldClass.CONTACT)
    candidates = list(
        (
            await db.execute(
                select(Candidate).where(
                    Candidate.id.in_(body.candidate_ids),
                    Candidate.organization_id == user.organization_id,
                    Candidate.merged_at.is_(None),
                )
            )
        ).scalars()
    )
    if len(candidates) != len(body.candidate_ids):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found.")
    score_rows = (
        await db.execute(
            select(Score, Position)
            .join(Position, Position.id == Score.position_id)
            .where(
                Score.organization_id == user.organization_id,
                Score.candidate_id.in_(body.candidate_ids),
            )
        )
    ).all()
    scores_by_position: dict[UUID, list[Score]] = defaultdict(list)
    titles: dict[UUID, str] = {}
    for score, position in score_rows:
        scores_by_position[position.id].append(score)
        titles[position.id] = position.title
    conflicts = [
        CandidateMergeConflict(
            position_id=position_id,
            position_title=titles[position_id],
            candidate_ids=sorted(score.candidate_id for score in scores),
            options=[
                CandidateMergeConflictOption(
                    candidate_id=score.candidate_id,
                    overall_score=score.overall_score,
                    stage=score.stage,
                    owner_user_id=score.owner_user_id,
                    next_action_due_date=score.next_action_due_date,
                    scored_at=score.scored_at,
                )
                for score in sorted(
                    scores,
                    key=lambda score: (
                        score.overall_score is not None,
                        score.overall_score or -1,
                        score.scored_at,
                        score.id,
                    ),
                    reverse=True,
                )
            ],
        )
        for position_id, scores in scores_by_position.items()
        if len(scores) > 1
    ]
    tag_rows = (
        await db.execute(
            select(CandidateTag.label, func.count(distinct(CandidateTag.candidate_id)))
            .where(
                CandidateTag.organization_id == user.organization_id,
                CandidateTag.candidate_id.in_(body.candidate_ids),
            )
            .group_by(CandidateTag.label)
        )
    ).all()
    best_by_candidate: dict[UUID, tuple[int | None, str | None]] = {}
    for score, position in sorted(
        score_rows,
        key=lambda row: (
            row[0].candidate_id,
            row[0].overall_score is not None,
            row[0].overall_score or -1,
        ),
        reverse=True,
    ):
        best_by_candidate.setdefault(
            score.candidate_id, (score.overall_score, position.title)
        )
    return CandidateMergePreviewOut(
        candidates=[
            CandidateDuplicateMember(
                id=candidate.id,
                source=candidate.source,
                source_ref=candidate.source_ref if contact_visible else None,
                created_at=candidate.created_at,
                best_score=best_by_candidate.get(candidate.id, (None, None))[0],
                best_position_title=best_by_candidate.get(candidate.id, (None, None))[
                    1
                ],
            )
            for candidate in sorted(
                candidates, key=lambda candidate: candidate.created_at, reverse=True
            )
        ],
        score_conflicts=conflicts,
        shared_tags=sorted(label for label, count in tag_rows if count > 1),
    )


@router.post(
    "/candidates/{primary_candidate_id}/merge", response_model=CandidateMergeOut
)
async def merge_candidate_duplicates(
    primary_candidate_id: UUID,
    body: CandidateMergeRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_writer),
) -> CandidateMergeOut:
    _require_organization_candidate_mutation(user)
    outcome = await merge_candidates(
        db,
        organization_id=user.organization_id,
        actor_user_id=user.id,
        primary_candidate_id=primary_candidate_id,
        duplicate_candidate_ids=body.duplicate_ids,
        score_winners={
            winner.position_id: winner.winner_candidate_id
            for winner in body.score_winners
        },
    )
    return CandidateMergeOut(
        batch_id=outcome.batch_id,
        primary_candidate_id=primary_candidate_id,
        merged_candidate_ids=list(outcome.merged_candidate_ids),
    )


def _encode_merge_history_cursor(
    batch: CandidateMergeBatch, *, scope: str
) -> str:
    return encode_collection_cursor(
        "candidate-search.merge-history",
        scope=scope,
        boundary={
            "created_at": batch.created_at.astimezone(UTC).isoformat(),
            "id": str(batch.id),
        },
    )


def _decode_merge_history_cursor(value: str, *, scope: str) -> tuple[datetime, UUID]:
    payload = decode_collection_cursor(
        value,
        namespace="candidate-search.merge-history",
        scope=scope,
        boundary_keys={"created_at", "id"},
        detail="Invalid merge history cursor",
    )
    return (
        cursor_datetime(payload, "created_at", detail="Invalid merge history cursor"),
        cursor_uuid(payload, detail="Invalid merge history cursor"),
    )


@router.get(
    "/candidates/{candidate_id}/merge-history",
    response_model=CandidateMergeHistoryPage,
)
async def list_candidate_merge_history(
    candidate_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query()] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_writer),
) -> CandidateMergeHistoryPage:
    canonical = await resolve_canonical_candidate(
        db, candidate_id, user.organization_id
    )
    if canonical is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found.")
    cursor_scope = collection_cursor_scope(
        "candidate-search.merge-history",
        organization_id=user.organization_id,
        filters={"candidate_id": str(canonical.id)},
    )
    conditions = [
        CandidateMergeBatch.organization_id == user.organization_id,
        CandidateMergeBatch.primary_candidate_id == canonical.id,
    ]
    if cursor:
        created_at, cursor_id = _decode_merge_history_cursor(
            cursor, scope=cursor_scope
        )
        conditions.append(
            or_(
                CandidateMergeBatch.created_at < created_at,
                and_(
                    CandidateMergeBatch.created_at == created_at,
                    CandidateMergeBatch.id < cursor_id,
                ),
            )
        )
    rows = list(
        (
            await db.execute(
                select(CandidateMergeBatch)
                .where(*conditions)
                .order_by(
                    CandidateMergeBatch.created_at.desc(),
                    CandidateMergeBatch.id.desc(),
                )
                .limit(limit + 1)
            )
        ).scalars()
    )
    has_more = len(rows) > limit
    visible = rows[:limit]
    return CandidateMergeHistoryPage(
        items=[
            CandidateMergeHistoryItem(
                id=batch.id,
                primary_candidate_id=batch.primary_candidate_id,
                member_count=batch.member_count,
                status=batch.status,
                version=batch.version,
                can_reverse=batch.status == "merged"
                and (batch.created_by_user_id == user.id or user.role == "admin"),
                created_at=batch.created_at,
                reversed_at=batch.reversed_at,
            )
            for batch in visible
        ],
        next_cursor=(
            _encode_merge_history_cursor(visible[-1], scope=cursor_scope)
            if has_more and visible
            else None
        ),
    )


@router.post(
    "/candidate-merges/{batch_id}/reverse",
    response_model=CandidateMergeReverseOut,
)
async def reverse_candidate_merge_batch(
    batch_id: UUID,
    body: CandidateMergeReverseRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_writer),
) -> CandidateMergeReverseOut:
    _require_organization_candidate_mutation(user)
    outcome = await reverse_candidate_merge(
        db,
        organization_id=user.organization_id,
        actor_user_id=user.id,
        actor_role=user.role,
        batch_id=batch_id,
        expected_version=body.expected_version,
    )
    return CandidateMergeReverseOut(
        batch_id=outcome.batch_id,
        primary_candidate_id=outcome.primary_candidate_id,
        restored_candidate_ids=list(outcome.restored_candidate_ids),
        version=outcome.version,
        reversed_at=outcome.reversed_at,
    )


@router.get(
    "/candidates/{candidate_id}/merged-sources",
    response_model=CursorPage[CandidateMergedSourceSummary],
)
async def list_merged_candidate_sources(
    candidate_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_reader),
) -> CursorPage[CandidateMergedSourceSummary]:
    """Return safe summaries while keeping merged evidence on the server."""

    canonical = await resolve_canonical_candidate(
        db, candidate_id, user.organization_id
    )
    if canonical is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found.")
    contact_visible = await can_access_field(db, user, FieldClass.CONTACT)
    document_visible = await can_access_field(
        db, user, FieldClass.CANDIDATE_DOCUMENT
    )
    source_records = list(
        (
            await db.execute(
                select(Candidate).where(
                    Candidate.organization_id == user.organization_id,
                    or_(
                        Candidate.id == canonical.id,
                        Candidate.merged_into_candidate_id == canonical.id,
                    ),
                )
            )
        ).scalars()
    )
    source_records.sort(
        key=lambda source: (
            source.id != canonical.id,
            -(source.created_at.timestamp()),
            str(source.id),
        )
    )
    namespace = "candidate-search.merged-sources"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={
            "candidate_id": str(canonical.id),
            "contact_visible": contact_visible,
            "document_visible": document_visible,
        },
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"group", "created_at", "id"},
            detail="Invalid merged sources cursor",
        )
        try:
            cursor_group = int(payload["group"])
            if cursor_group not in {0, 1}:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"code": "cursor_invalid", "detail": "Invalid merged sources cursor"},
            ) from exc
        cursor_time = cursor_datetime(
            payload, "created_at", detail="Invalid merged sources cursor"
        )
        cursor_id = cursor_uuid(payload, detail="Invalid merged sources cursor")
        boundary = (cursor_group, -cursor_time.timestamp(), str(cursor_id))
        source_records = [
            source
            for source in source_records
            if (
                int(source.id != canonical.id),
                -source.created_at.timestamp(),
                str(source.id),
            )
            > boundary
        ]
    has_more = len(source_records) > limit
    source_records = source_records[:limit]
    source_ids = [source.id for source in source_records]
    assessment_results_visible = await can_access_field(
        db, user, FieldClass.ASSESSMENT_RESULT
    )
    score_rows = []
    if assessment_results_visible and source_ids:
        score_rows = (
            await db.execute(
                select(Score, Position)
                .join(Position, Position.id == Score.position_id)
                .where(
                    Score.organization_id == user.organization_id,
                    Score.candidate_id.in_(source_ids),
                    Position.organization_id == user.organization_id,
                )
                .order_by(Score.scored_at.desc(), Score.id.desc())
            )
        ).all()
    scores_by_candidate: dict[UUID, list[CandidateMergedSourceScore]] = defaultdict(
        list
    )
    for score, position in score_rows:
        scores_by_candidate[score.candidate_id].append(
            CandidateMergedSourceScore(
                id=score.id,
                position_id=position.id,
                position_title=position.title,
                overall_score=score.overall_score,
                stage=score.stage,
                selected=score.candidate_id == canonical.id,
            )
        )
    items = [
        CandidateMergedSourceSummary(
            id=source.id,
            is_primary=source.id == canonical.id,
            source=source.source,
            source_ref=source.source_ref if contact_visible else None,
            has_cv=(
                bool(source.raw_cv or source.parsed_cv or source.pdf_path)
                if document_visible
                else False
            ),
            has_contact=(
                bool(source.phone_normalized or source.email_normalized)
                if contact_visible
                else False
            ),
            github_login=(
                source.github_login
                if contact_visible and document_visible
                else None
            ),
            created_at=source.created_at,
            merged_at=source.merged_at,
            scores=scores_by_candidate[source.id],
        )
        for source in source_records
    ]
    last = source_records[-1] if source_records else None
    return CursorPage(
        items=items,
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "group": int(last.id != canonical.id),
                    "created_at": last.created_at.astimezone(UTC).isoformat(),
                    "id": str(last.id),
                },
            )
            if has_more and last is not None
            else None
        ),
    )


@router.get(
    "/candidates/{candidate_id}/merged-sources/{source_candidate_id}/evidence",
    response_model=CandidateMergedSourceEvidence,
)
async def get_merged_candidate_source_evidence(
    candidate_id: UUID,
    source_candidate_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_reader),
) -> CandidateMergedSourceEvidence:
    """Return source evidence after an explicit authenticated request."""

    source = await _merged_source_record(
        db,
        candidate_id=candidate_id,
        source_candidate_id=source_candidate_id,
        organization_id=user.organization_id,
    )
    document_owner = await resolve_merged_source_original_document(
        db,
        organization_id=user.organization_id,
        canonical_candidate_id=candidate_id,
        source_candidate_id=source.id,
    )
    return CandidateMergedSourceEvidence(
        id=source.id,
        source=source.source,
        source_ref=source.source_ref,
        raw_cv=source.raw_cv,
        parsed_cv=source.parsed_cv,
        phone_normalized=source.phone_normalized,
        email_normalized=source.email_normalized,
        github_login=source.github_login,
        github_data=source.github_data,
        github_fetched_at=source.github_fetched_at,
        has_original_file=document_owner is not None,
    )


@router.get("/candidates/{candidate_id}/merged-sources/{source_candidate_id}/cv")
async def download_merged_candidate_source_cv(
    candidate_id: UUID,
    source_candidate_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_reader),
) -> StreamingResponse:
    """Download an original merged-source file without exposing its path."""

    source = await _merged_source_record(
        db,
        candidate_id=candidate_id,
        source_candidate_id=source_candidate_id,
        organization_id=user.organization_id,
    )
    await require_field_access(db, user, FieldClass.CANDIDATE_DOCUMENT)
    await require_field_access(db, user, FieldClass.CONTACT)
    document_owner = await resolve_merged_source_original_document(
        db,
        organization_id=user.organization_id,
        canonical_candidate_id=candidate_id,
        source_candidate_id=source.id,
    )
    if document_owner is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No original file for this merged source.",
        )
    key = await resolve_resource_private_blob_reference(
        db,
        organization_id=user.organization_id,
        category="candidates",
        resource_type="candidate",
        resource_id=document_owner.id,
        private_object_id=document_owner.private_object_id,
        legacy_reference=document_owner.pdf_path,
    )
    if key is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Stored CV file is unavailable.",
        )
    extension = Path(key).suffix.lower() or ".bin"
    media_type = validated_private_media_type(
        key,
        allowed_suffixes={".pdf", ".docx"},
    )
    response = await private_blob_response(
        key,
        filename=safe_download_filename(
            source.source_ref,
            fallback=str(source.id),
            suffix=extension,
        ),
        media_type=media_type,
    )
    db.add(
        AuditLog(
            organization_id=user.organization_id,
            actor_user_id=user.id,
            actor_email_snapshot=user.email,
            action="download_merged_candidate_cv",
            resource_type="candidate",
            resource_id=source.id,
            path_template=(
                "/candidates/{candidate_id}/merged-sources/{source_candidate_id}/cv"
            ),
            method="GET",
            status_code=200,
            request_id=uuid4(),
            metadata_json={
                "canonical_candidate_id": str(candidate_id),
                "sensitive_read": True,
            },
            occurred_at=datetime.now(UTC),
        )
    )
    await db.commit()
    return response


@router.get("/candidate-views", response_model=CursorPage[CandidateSavedViewOut])
async def list_candidate_views(
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_reader),
) -> CursorPage[CandidateSavedViewOut]:
    namespace = "candidate-search.saved-views"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"user_id": str(user.id)},
    )
    normalized_name = func.lower(CandidateSavedView.name)
    stmt = select(CandidateSavedView).where(
        CandidateSavedView.organization_id == user.organization_id,
        CandidateSavedView.user_id == user.id,
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"name", "id"},
            detail="Invalid candidate views cursor",
        )
        cursor_name = payload["name"]
        if not isinstance(cursor_name, str):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"code": "cursor_invalid", "detail": "Invalid candidate views cursor"},
            )
        cursor_id = cursor_uuid(payload, detail="Invalid candidate views cursor")
        stmt = stmt.where(
            or_(
                normalized_name > cursor_name,
                and_(normalized_name == cursor_name, CandidateSavedView.id > cursor_id),
            )
        )
    views = list(
        (
            await db.execute(
                stmt.order_by(normalized_name.asc(), CandidateSavedView.id.asc()).limit(
                    limit + 1
                )
            )
        ).scalars()
    )
    has_more = len(views) > limit
    views = views[:limit]
    return CursorPage(
        items=[CandidateSavedViewOut.model_validate(view) for view in views],
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={"name": views[-1].name.lower(), "id": str(views[-1].id)},
            )
            if has_more and views
            else None
        ),
    )


@router.post(
    "/candidate-views",
    response_model=CandidateSavedViewOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_candidate_view(
    body: CandidateSavedViewCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_reader),
) -> CandidateSavedViewOut:
    view = CandidateSavedView(
        organization_id=user.organization_id,
        user_id=user.id,
        name=body.name.strip(),
        filters=body.filters.model_dump(mode="json"),
    )
    db.add(view)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A saved view with this name already exists.",
        ) from exc
    await db.refresh(view)
    return CandidateSavedViewOut.model_validate(view)


@router.patch("/candidate-views/{view_id}", response_model=CandidateSavedViewOut)
async def update_candidate_view(
    view_id: UUID,
    body: CandidateSavedViewUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_reader),
) -> CandidateSavedViewOut:
    view = (
        await db.execute(
            select(CandidateSavedView).where(
                CandidateSavedView.id == view_id,
                CandidateSavedView.organization_id == user.organization_id,
                CandidateSavedView.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if view is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Saved view not found.")
    if "name" in body.model_fields_set and body.name is not None:
        view.name = body.name.strip()
    if "filters" in body.model_fields_set and body.filters is not None:
        view.filters = body.filters.model_dump(mode="json")
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A saved view with this name already exists.",
        ) from exc
    await db.refresh(view)
    return CandidateSavedViewOut.model_validate(view)


@router.delete("/candidate-views/{view_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_candidate_view(
    view_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_reader),
) -> None:
    result = await db.execute(
        delete(CandidateSavedView).where(
            CandidateSavedView.id == view_id,
            CandidateSavedView.organization_id == user.organization_id,
            CandidateSavedView.user_id == user.id,
        )
    )
    if result.rowcount == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Saved view not found.")
    await db.commit()


@router.post("/candidate-bulk", response_model=CandidateBulkOut)
async def apply_candidate_bulk_action(
    body: CandidateBulkRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_writer),
) -> CandidateBulkOut:
    candidates = list(
        (
            await db.execute(
                select(Candidate)
                .where(
                    Candidate.id.in_(body.candidate_ids),
                    Candidate.organization_id == user.organization_id,
                    Candidate.merged_at.is_(None),
                )
                .order_by(Candidate.id)
                .with_for_update()
            )
        ).scalars()
    )
    if len(candidates) != len(body.candidate_ids):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found.")
    changed: list[UUID] = []
    unchanged: list[UUID] = []
    batch_id = uuid4()

    if body.action in {"stage", "reject"}:
        position = (
            await db.execute(
                select(Position)
                .where(
                    Position.id == body.position_id,
                    Position.organization_id == user.organization_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if position is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found.")
        applications = list(
            (
                await db.execute(
                    select(Application)
                    .where(
                        Application.organization_id == user.organization_id,
                        Application.position_id == body.position_id,
                        Application.candidate_id.in_(body.candidate_ids),
                        Application.closed_at.is_(None),
                        Application.archived_at.is_(None),
                    )
                    .order_by(Application.id)
                    .with_for_update()
                )
            ).scalars()
        )
        if len(applications) != len(body.candidate_ids):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "Candidate is not in the selected position.",
            )
        target_stage = "rejected" if body.action == "reject" else body.stage
        assert target_stage is not None
        try:
            for application in applications:
                transition = ApplicationStageTransition(
                    stage=target_stage,
                    expected_version=application.updated_at,
                    disposition_code=body.disposition_code,
                    reason=body.reason or "Bulk stage change",
                    details={
                        "bulk_action_id": str(batch_id),
                        "position_title": position.title,
                    },
                )
                _application, did_change = await apply_application_stage_transition(
                    db,
                    organization_id=user.organization_id,
                    application_id=application.id,
                    body=transition,
                    actor_user_id=user.id,
                    actor_kind="human",
                )
                (changed if did_change else unchanged).append(
                    application.candidate_id
                )
        except Exception:
            await db.rollback()
            raise
    else:
        assert body.tag is not None
        existing_tags = list(
            (
                await db.execute(
                    select(CandidateTag)
                    .where(
                        CandidateTag.organization_id == user.organization_id,
                        CandidateTag.candidate_id.in_(body.candidate_ids),
                        CandidateTag.label == body.tag,
                    )
                    .order_by(CandidateTag.id)
                    .with_for_update()
                )
            ).scalars()
        )
        existing_by_candidate = {tag.candidate_id: tag for tag in existing_tags}
        for candidate in candidates:
            existing = existing_by_candidate.get(candidate.id)
            if body.action == "tag_add":
                if existing is not None:
                    unchanged.append(candidate.id)
                    continue
                tag_id = uuid4()
                db.add(
                    CandidateTag(
                        id=tag_id,
                        organization_id=user.organization_id,
                        candidate_id=candidate.id,
                        label=body.tag,
                        created_by=user.id,
                    )
                )
                event_type = "tag_added"
            else:
                if existing is None:
                    unchanged.append(candidate.id)
                    continue
                tag_id = existing.id
                await db.delete(existing)
                event_type = "tag_removed"
            changed.append(candidate.id)
            append_candidate_activity(
                db,
                organization_id=user.organization_id,
                candidate_id=candidate.id,
                actor_user_id=user.id,
                event_type=event_type,
                subject_type="tag",
                subject_id=tag_id,
                payload={"label": body.tag, "bulk_action_id": str(batch_id)},
            )
    await db.commit()
    return CandidateBulkOut(
        changed_candidate_ids=changed,
        unchanged_candidate_ids=unchanged,
    )


def _safe_csv_cell(value: object | None) -> str:
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@")):
        return f"'{text}"
    return text


@router.post("/candidate-export")
async def export_candidates(
    body: CandidateExportRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_exporter),
) -> StreamingResponse:
    policy = await get_or_create_policy(db, user.organization_id)
    if not policy.candidate_export_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Candidate exports are disabled")
    contact_visible = await can_access_field(db, user, FieldClass.CONTACT)
    include_contacts = body.include_contacts and contact_visible
    if body.include_contacts and not include_contacts:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Candidate contact export is not permitted",
        )
    candidates = list(
        (
            await db.execute(
                select(Candidate).where(
                    Candidate.id.in_(body.candidate_ids),
                    Candidate.organization_id == user.organization_id,
                    Candidate.merged_at.is_(None),
                )
            )
        ).scalars()
    )
    if len(candidates) != len(body.candidate_ids):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found.")
    position: Position | None = None
    scores_by_candidate: dict[UUID, Score] = {}
    if body.position_id is not None:
        # A position-scoped export contains stage and score columns.  Returning
        # blank cells would still disclose score presence through which rows
        # match the requested relationship, so fail closed for this mode.
        await require_field_access(db, user, FieldClass.ASSESSMENT_RESULT)
        position = (
            await db.execute(
                select(Position).where(
                    Position.id == body.position_id,
                    Position.organization_id == user.organization_id,
                )
            )
        ).scalar_one_or_none()
        if position is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found.")
        scores_by_candidate = {
            score.candidate_id: score
            for score in (
                await db.execute(
                    select(Score).where(
                        Score.organization_id == user.organization_id,
                        Score.position_id == position.id,
                        Score.candidate_id.in_(body.candidate_ids),
                    )
                )
            ).scalars()
        }
    tag_rows = (
        await db.execute(
            select(CandidateTag.candidate_id, CandidateTag.label)
            .where(
                CandidateTag.organization_id == user.organization_id,
                CandidateTag.candidate_id.in_(body.candidate_ids),
            )
            .order_by(CandidateTag.label)
        )
    ).all()
    tags: dict[UUID, list[str]] = defaultdict(list)
    for candidate_id, label in tag_rows:
        tags[candidate_id].append(label)

    ordered = {candidate.id: candidate for candidate in candidates}
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(
        [
            "candidate_id",
            "reference",
            "source",
            "email",
            "phone",
            "created_at",
            "tags",
            "position",
            "stage",
            "score",
        ]
    )
    for candidate_id in body.candidate_ids:
        candidate = ordered[candidate_id]
        score = scores_by_candidate.get(candidate_id)
        writer.writerow(
            [
                _safe_csv_cell(candidate.id),
                _safe_csv_cell(candidate.source_ref if contact_visible else None),
                _safe_csv_cell(candidate.source),
                _safe_csv_cell(
                    candidate.email_normalized if include_contacts else None
                ),
                _safe_csv_cell(
                    candidate.phone_normalized if include_contacts else None
                ),
                _safe_csv_cell(candidate.created_at.isoformat()),
                _safe_csv_cell(", ".join(tags.get(candidate.id, []))),
                _safe_csv_cell(position.title if position else None),
                _safe_csv_cell(score.stage if score else None),
                _safe_csv_cell(score.overall_score if score else None),
            ]
        )
    batch_id = uuid4()
    for candidate in candidates:
        append_candidate_activity(
            db,
            organization_id=user.organization_id,
            candidate_id=candidate.id,
            actor_user_id=user.id,
            event_type="candidate_exported",
            subject_type="candidate",
            subject_id=candidate.id,
            payload={"format": "csv", "bulk_action_id": str(batch_id)},
        )
    await db.commit()
    data = ("\ufeff" + stream.getvalue()).encode("utf-8")
    return StreamingResponse(
        iter([data]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="candidates.csv"'},
    )
