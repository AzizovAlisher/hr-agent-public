"""Talent pools, explainable rediscovery, and nurture sequences."""

import json
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from app.auth.authorization import FieldClass
from app.auth.dependencies import current_user
from app.db import get_session
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.communication import CommunicationTemplate
from app.models.position import Position
from app.models.score import Score
from app.models.talent import (
    TalentNurtureEnrollment,
    TalentNurtureSequence,
    TalentNurtureStep,
    TalentPool,
    TalentPoolMember,
)
from app.models.user import User
from app.schemas.communications import CandidateCommunicationCompose
from app.schemas.talent import (
    TalentCandidateOut,
    TalentCandidatePage,
    TalentNurtureLaunchOut,
    TalentNurtureLaunchRequest,
    TalentNurtureSequencePage,
    TalentNurtureSequenceCreate,
    TalentNurtureSequenceOut,
    TalentNurtureStepOut,
    TalentPoolCreate,
    TalentPoolMembersRequest,
    TalentPoolMutationOut,
    TalentPoolOut,
    TalentPoolPage,
    TalentPoolUpdate,
)
from app.services.activities import append_candidate_activity
from app.services.candidate_merge import resolve_canonical_candidate
from app.services.communications import (
    CommunicationPreparationError,
    create_outbound_message,
    prepare_candidate_communication,
)
from app.services.enterprise import can_access_field, require_field_access
from app.services.pagination import (
    cursor_scope_fingerprint,
    decode_signed_cursor,
    encode_signed_cursor,
)


router = APIRouter(tags=["talent-crm"])

_POOL_CURSOR_NAMESPACE = "talent.pools"
_POOL_MEMBER_CURSOR_NAMESPACE = "talent.pool-members"
_SEARCH_CURSOR_NAMESPACE = "talent.search"
_MATCH_CURSOR_NAMESPACE = "talent.position-matches"
_NURTURE_CURSOR_NAMESPACE = "talent.nurture-sequences"


def _talent_cursor_scope(
    namespace: str, organization_id: UUID, **claims: str | None
) -> str:
    return cursor_scope_fingerprint(
        namespace,
        {"organization_id": str(organization_id), **claims},
    )


def _encode_time_cursor(
    namespace: str,
    *,
    created_at: datetime,
    row_id: UUID,
    scope: str,
) -> str:
    return encode_signed_cursor(
        namespace,
        {
            "created_at": created_at.astimezone(UTC).isoformat(),
            "id": str(row_id),
            "scope": scope,
        },
    )


def _decode_time_cursor(
    cursor: str, *, namespace: str, scope: str
) -> tuple[datetime, UUID]:
    try:
        payload = decode_signed_cursor(cursor, namespace=namespace)
        if set(payload) != {"created_at", "id", "scope"} or payload["scope"] != scope:
            raise ValueError("cursor does not match this talent query")
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("cursor timestamp must include a timezone")
        return created_at.astimezone(UTC), UUID(str(payload["id"]))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Invalid talent cursor",
        ) from exc


def _encode_rank_cursor(
    namespace: str,
    *,
    relevance: float,
    created_at: datetime,
    candidate_id: UUID,
    scope: str,
) -> str:
    return encode_signed_cursor(
        namespace,
        {
            "relevance": repr(float(relevance)),
            "created_at": created_at.astimezone(UTC).isoformat(),
            "id": str(candidate_id),
            "scope": scope,
        },
    )


def _decode_rank_cursor(
    cursor: str, *, namespace: str, scope: str
) -> tuple[float, datetime, UUID]:
    try:
        payload = decode_signed_cursor(cursor, namespace=namespace)
        if (
            set(payload) != {"relevance", "created_at", "id", "scope"}
            or payload["scope"] != scope
        ):
            raise ValueError("cursor does not match this talent query")
        relevance = float(str(payload["relevance"]))
        if not 0 <= relevance <= 1_000_000:
            raise ValueError("invalid relevance boundary")
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("cursor timestamp must include a timezone")
        return relevance, created_at.astimezone(UTC), UUID(str(payload["id"]))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Invalid talent cursor",
        ) from exc


async def _pool(
    db: AsyncSession,
    pool_id: UUID,
    organization_id: UUID,
    *,
    for_update: bool = False,
) -> TalentPool:
    stmt = select(TalentPool).where(
        TalentPool.id == pool_id,
        TalentPool.organization_id == organization_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    pool = (await db.execute(stmt)).scalar_one_or_none()
    if pool is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Talent pool not found")
    return pool


async def _pool_page(
    db: AsyncSession,
    organization_id: UUID,
    *,
    limit: int,
    cursor: str | None,
) -> TalentPoolPage:
    scope = _talent_cursor_scope(_POOL_CURSOR_NAMESPACE, organization_id)
    stmt = (
        select(TalentPool, func.count(TalentPoolMember.id))
        .outerjoin(TalentPoolMember, TalentPoolMember.pool_id == TalentPool.id)
        .where(TalentPool.organization_id == organization_id)
        .group_by(TalentPool.id)
    )
    if cursor is not None:
        cursor_time, cursor_id = _decode_time_cursor(
            cursor, namespace=_POOL_CURSOR_NAMESPACE, scope=scope
        )
        stmt = stmt.where(
            or_(
                TalentPool.created_at < cursor_time,
                and_(TalentPool.created_at == cursor_time, TalentPool.id < cursor_id),
            )
        )
    rows = (
        await db.execute(
            stmt.order_by(TalentPool.created_at.desc(), TalentPool.id.desc()).limit(
                limit + 1
            )
        )
    ).all()
    visible = rows[:limit]
    items = [
        TalentPoolOut(
            id=pool.id,
            name=pool.name,
            description=pool.description,
            color=pool.color,
            member_count=count,
            created_at=pool.created_at,
            updated_at=pool.updated_at,
        )
        for pool, count in visible
    ]
    return TalentPoolPage(
        items=items,
        next_cursor=(
            _encode_time_cursor(
                _POOL_CURSOR_NAMESPACE,
                created_at=visible[-1][0].created_at,
                row_id=visible[-1][0].id,
                scope=scope,
            )
            if len(rows) > limit and visible
            else None
        ),
    )


@router.get("/talent/pools", response_model=TalentPoolPage)
async def list_talent_pools(
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> TalentPoolPage:
    return await _pool_page(
        db,
        user.organization_id,
        limit=limit,
        cursor=cursor,
    )


@router.post(
    "/talent/pools", response_model=TalentPoolOut, status_code=status.HTTP_201_CREATED
)
async def create_talent_pool(
    body: TalentPoolCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> TalentPoolOut:
    pool = TalentPool(
        organization_id=user.organization_id,
        created_by_user_id=user.id,
        **body.model_dump(),
    )
    db.add(pool)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A talent pool with this name already exists"
        ) from exc
    await db.refresh(pool)
    return TalentPoolOut(
        id=pool.id,
        name=pool.name,
        description=pool.description,
        color=pool.color,
        member_count=0,
        created_at=pool.created_at,
        updated_at=pool.updated_at,
    )


@router.patch("/talent/pools/{pool_id}", response_model=TalentPoolOut)
async def update_talent_pool(
    pool_id: UUID,
    body: TalentPoolUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> TalentPoolOut:
    pool = await _pool(db, pool_id, user.organization_id, for_update=True)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(pool, field, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A talent pool with this name already exists"
        ) from exc
    count = (
        await db.execute(
            select(func.count(TalentPoolMember.id)).where(
                TalentPoolMember.organization_id == user.organization_id,
                TalentPoolMember.pool_id == pool.id,
            )
        )
    ).scalar_one()
    await db.refresh(pool)
    return TalentPoolOut(
        id=pool.id,
        name=pool.name,
        description=pool.description,
        color=pool.color,
        member_count=count,
        created_at=pool.created_at,
        updated_at=pool.updated_at,
    )


@router.delete("/talent/pools/{pool_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_talent_pool(
    pool_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> None:
    pool = await _pool(db, pool_id, user.organization_id, for_update=True)
    await db.delete(pool)
    await db.commit()


@router.post(
    "/talent/pools/{pool_id}/members", response_model=TalentPoolMutationOut
)
async def add_talent_pool_members(
    pool_id: UUID,
    body: TalentPoolMembersRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> TalentPoolMutationOut:
    pool = await _pool(db, pool_id, user.organization_id, for_update=True)
    existing_ids = set(
        (
            await db.execute(
                select(TalentPoolMember.candidate_id).where(
                    TalentPoolMember.pool_id == pool.id,
                    TalentPoolMember.organization_id == user.organization_id,
                )
            )
        ).scalars()
    )
    canonical_candidates: dict[UUID, Candidate] = {}
    for candidate_id in body.candidate_ids:
        candidate = await resolve_canonical_candidate(
            db, candidate_id, user.organization_id
        )
        if candidate is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
        canonical_candidates[candidate.id] = candidate
    added = 0
    for candidate in canonical_candidates.values():
        if candidate.id in existing_ids:
            continue
        member = TalentPoolMember(
            organization_id=user.organization_id,
            pool_id=pool.id,
            candidate_id=candidate.id,
            added_by_user_id=user.id,
        )
        db.add(member)
        await db.flush()
        append_candidate_activity(
            db,
            organization_id=user.organization_id,
            candidate_id=candidate.id,
            actor_user_id=user.id,
            event_type="talent_pool_added",
            subject_type="talent_pool_member",
            subject_id=member.id,
            payload={"pool_name": pool.name},
        )
        added += 1
    await db.commit()
    return TalentPoolMutationOut(
        added=added, unchanged=len(canonical_candidates) - added
    )


@router.delete(
    "/talent/pools/{pool_id}/members/{candidate_id}",
    response_model=TalentPoolMutationOut,
)
async def remove_talent_pool_member(
    pool_id: UUID,
    candidate_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> TalentPoolMutationOut:
    pool = await _pool(db, pool_id, user.organization_id, for_update=True)
    candidate = await resolve_canonical_candidate(
        db, candidate_id, user.organization_id
    )
    if candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    member = (
        await db.execute(
            select(TalentPoolMember).where(
                TalentPoolMember.organization_id == user.organization_id,
                TalentPoolMember.pool_id == pool.id,
                TalentPoolMember.candidate_id == candidate.id,
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if member is None:
        await db.commit()
        return TalentPoolMutationOut(unchanged=1)
    member_id = member.id
    await db.delete(member)
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=candidate.id,
        actor_user_id=user.id,
        event_type="talent_pool_removed",
        subject_type="talent_pool_member",
        subject_id=member_id,
        payload={"pool_name": pool.name},
    )
    await db.commit()
    return TalentPoolMutationOut(removed=1)


async def _best_scores(
    db: AsyncSession, organization_id: UUID, candidate_ids: list[UUID]
) -> dict[UUID, tuple[int | None, UUID, str]]:
    if not candidate_ids:
        return {}
    rows = (
        await db.execute(
            select(Score.candidate_id, Score.overall_score, Position.id, Position.title)
            .join(Position, Position.id == Score.position_id)
            .where(
                Score.organization_id == organization_id,
                Score.candidate_id.in_(candidate_ids),
                Position.organization_id == organization_id,
            )
            .order_by(
                Score.candidate_id,
                Score.overall_score.desc().nullslast(),
                Score.id,
            )
        )
    ).all()
    best: dict[UUID, tuple[int | None, UUID, str]] = {}
    for candidate_id, overall_score, position_id, title in rows:
        best.setdefault(candidate_id, (overall_score, position_id, title))
    return best


async def _visible_best_scores(
    db: AsyncSession,
    user: User,
    candidate_ids: list[UUID],
) -> dict[UUID, tuple[int | None, UUID, str]]:
    """Load score-derived talent decoration only when field policy permits it."""

    if not await can_access_field(db, user, FieldClass.ASSESSMENT_RESULT):
        return {}
    return await _best_scores(db, user.organization_id, candidate_ids)


def _candidate_out(
    candidate: Candidate,
    best: dict[UUID, tuple[int | None, UUID, str]],
    *,
    contact_visible: bool,
    relevance: float | None = None,
    match_reasons: list[str] | None = None,
    already_in_pipeline: bool = False,
    pool_member_id: UUID | None = None,
) -> TalentCandidateOut:
    score = best.get(candidate.id)
    return TalentCandidateOut(
        id=candidate.id,
        source=candidate.source,
        source_ref=candidate.source_ref if contact_visible else None,
        created_at=candidate.created_at,
        best_score=score[0] if score else None,
        best_position_id=score[1] if score else None,
        best_position_title=score[2] if score else None,
        relevance=relevance,
        match_reasons=match_reasons or [],
        already_in_pipeline=already_in_pipeline,
        pool_member_id=pool_member_id,
    )


@router.get(
    "/talent/pools/{pool_id}/members", response_model=TalentCandidatePage
)
async def list_talent_pool_members(
    pool_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> TalentCandidatePage:
    pool = await _pool(db, pool_id, user.organization_id)
    scope = _talent_cursor_scope(
        _POOL_MEMBER_CURSOR_NAMESPACE,
        user.organization_id,
        pool_id=str(pool.id),
    )
    total = (
        await db.execute(
            select(func.count(TalentPoolMember.id))
            .join(Candidate, Candidate.id == TalentPoolMember.candidate_id)
            .where(
                TalentPoolMember.organization_id == user.organization_id,
                TalentPoolMember.pool_id == pool.id,
                Candidate.organization_id == user.organization_id,
                Candidate.merged_at.is_(None),
            )
        )
    ).scalar_one()
    stmt = (
        select(TalentPoolMember, Candidate)
        .join(Candidate, Candidate.id == TalentPoolMember.candidate_id)
        .where(
            TalentPoolMember.organization_id == user.organization_id,
            TalentPoolMember.pool_id == pool.id,
            Candidate.organization_id == user.organization_id,
            Candidate.merged_at.is_(None),
        )
    )
    if cursor is not None:
        cursor_time, cursor_id = _decode_time_cursor(
            cursor,
            namespace=_POOL_MEMBER_CURSOR_NAMESPACE,
            scope=scope,
        )
        stmt = stmt.where(
            or_(
                TalentPoolMember.created_at < cursor_time,
                and_(
                    TalentPoolMember.created_at == cursor_time,
                    TalentPoolMember.id < cursor_id,
                ),
            )
        )
    rows = (
        await db.execute(
            stmt
            .order_by(TalentPoolMember.created_at.desc(), TalentPoolMember.id.desc())
            .limit(limit + 1)
        )
    ).all()
    visible = rows[:limit]
    best = await _visible_best_scores(
        db,
        user,
        [candidate.id for _member, candidate in visible],
    )
    contact_visible = await can_access_field(db, user, FieldClass.CONTACT)
    return TalentCandidatePage(
        items=[
            _candidate_out(
                candidate,
                best,
                contact_visible=contact_visible,
                pool_member_id=member.id,
            )
            for member, candidate in visible
        ],
        total=total,
        next_cursor=(
            _encode_time_cursor(
                _POOL_MEMBER_CURSOR_NAMESPACE,
                created_at=visible[-1][0].created_at,
                row_id=visible[-1][0].id,
                scope=scope,
            )
            if len(rows) > limit and visible
            else None
        ),
    )


_STOPWORDS = {
    "and",
    "the",
    "with",
    "for",
    "from",
    "or",
    "not",
    "или",
    "для",
    "как",
    "это",
    "что",
    "при",
    "опыт",
    "работы",
    "knowledge",
    "experience",
    "years",
}


def _normalized_term(value: str) -> str:
    return re.sub(r"[^\w]+", "", value.lower(), flags=re.UNICODE)


def _text_terms(value: str, *, limit: int = 12) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[\w+#.-]{2,}", value.lower(), flags=re.UNICODE):
        cleaned = token.strip(".-")
        normalized = _normalized_term(cleaned)
        if (
            not cleaned
            or not normalized
            or cleaned in _STOPWORDS
            or normalized in seen
            or cleaned.isdigit()
        ):
            continue
        seen.add(normalized)
        terms.append(cleaned)
        if len(terms) >= limit:
            break
    return terms


def _explain_matches(
    candidates: list[Candidate], terms: list[str], *, limit: int = 6
) -> list[str]:
    searchable = " ".join(
        value
        for candidate in candidates
        for value in (
            candidate.source_ref or "",
            candidate.raw_cv or "",
            json.dumps(candidate.parsed_cv or {}, ensure_ascii=False),
        )
    )
    searchable_terms = {
        _normalized_term(token)
        for token in re.findall(r"[\w+#.-]{1,}", searchable, flags=re.UNICODE)
    }
    reasons: list[str] = []
    seen: set[str] = set()
    for term in terms:
        normalized = _normalized_term(term)
        if normalized and normalized in searchable_terms and normalized not in seen:
            seen.add(normalized)
            reasons.append(term)
            if len(reasons) >= limit:
                break
    return reasons


def _family_search(query, organization_id: UUID):
    search_alias = aliased(Candidate)
    alias_match = exists(
        select(search_alias.id).where(
            search_alias.organization_id == organization_id,
            search_alias.merged_into_candidate_id == Candidate.id,
            search_alias.search_document.op("@@")(query),
        )
    )
    alias_rank = (
        select(func.max(func.ts_rank_cd(search_alias.search_document, query)))
        .where(
            search_alias.organization_id == organization_id,
            search_alias.merged_into_candidate_id == Candidate.id,
            search_alias.search_document.op("@@")(query),
        )
        .correlate(Candidate)
        .scalar_subquery()
    )
    relevance = func.greatest(
        func.ts_rank_cd(Candidate.search_document, query),
        func.coalesce(alias_rank, 0.0),
    )
    return or_(Candidate.search_document.op("@@")(query), alias_match), relevance


async def _candidate_aliases(
    db: AsyncSession, organization_id: UUID, candidate_ids: list[UUID]
) -> dict[UUID, list[Candidate]]:
    aliases: dict[UUID, list[Candidate]] = defaultdict(list)
    if not candidate_ids:
        return aliases
    rows = list(
        (
            await db.execute(
                select(Candidate).where(
                    Candidate.organization_id == organization_id,
                    Candidate.merged_into_candidate_id.in_(candidate_ids),
                )
            )
        ).scalars()
    )
    for alias in rows:
        if alias.merged_into_candidate_id is not None:
            aliases[alias.merged_into_candidate_id].append(alias)
    return aliases


@router.get("/talent/search", response_model=TalentCandidatePage)
async def search_talent(
    q: str = Query(min_length=2, max_length=200),
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    pool_id: UUID | None = None,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> TalentCandidatePage:
    cleaned_query = " ".join(q.split())
    if len(cleaned_query) < 2:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Search query must contain at least two characters",
        )
    # The indexed document and relevance/order values combine contact/source
    # references with CV text. Redaction after querying would retain inference.
    await require_field_access(db, user, FieldClass.CONTACT)
    await require_field_access(db, user, FieldClass.CANDIDATE_DOCUMENT)
    query = func.websearch_to_tsquery("simple", cleaned_query)
    family_match, relevance = _family_search(query, user.organization_id)
    stmt = select(Candidate, relevance.label("relevance")).where(
        Candidate.organization_id == user.organization_id,
        Candidate.merged_at.is_(None),
        family_match,
    )
    if pool_id is not None:
        await _pool(db, pool_id, user.organization_id)
        stmt = stmt.join(
            TalentPoolMember,
            (TalentPoolMember.candidate_id == Candidate.id)
            & (TalentPoolMember.pool_id == pool_id),
        )
    scope = _talent_cursor_scope(
        _SEARCH_CURSOR_NAMESPACE,
        user.organization_id,
        query=cleaned_query,
        pool_id=str(pool_id) if pool_id is not None else None,
    )
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    if cursor is not None:
        cursor_rank, cursor_time, cursor_id = _decode_rank_cursor(
            cursor,
            namespace=_SEARCH_CURSOR_NAMESPACE,
            scope=scope,
        )
        stmt = stmt.where(
            or_(
                relevance < cursor_rank,
                and_(
                    relevance == cursor_rank,
                    Candidate.created_at < cursor_time,
                ),
                and_(
                    relevance == cursor_rank,
                    Candidate.created_at == cursor_time,
                    Candidate.id < cursor_id,
                ),
            )
        )
    rows = (
        await db.execute(
            stmt.order_by(
                relevance.desc(), Candidate.created_at.desc(), Candidate.id.desc()
            ).limit(limit + 1)
        )
    ).all()
    visible = rows[:limit]
    explanation_terms = _text_terms(cleaned_query)
    aliases = await _candidate_aliases(
        db, user.organization_id, [candidate.id for candidate, _rank in visible]
    )
    best = await _visible_best_scores(
        db,
        user,
        [candidate.id for candidate, _rank in visible],
    )
    return TalentCandidatePage(
        items=[
            _candidate_out(
                candidate,
                best,
                contact_visible=True,
                relevance=round(float(rank), 4),
                match_reasons=_explain_matches(
                    [candidate, *aliases.get(candidate.id, [])], explanation_terms
                ),
            )
            for candidate, rank in visible
        ],
        total=total,
        next_cursor=(
            _encode_rank_cursor(
                _SEARCH_CURSOR_NAMESPACE,
                relevance=float(visible[-1][1]),
                created_at=visible[-1][0].created_at,
                candidate_id=visible[-1][0].id,
                scope=scope,
            )
            if len(rows) > limit and visible
            else None
        ),
    )


def _requirement_terms(value: object) -> list[str]:
    fragments: list[str] = []
    if isinstance(value, str):
        fragments.append(value)
    elif isinstance(value, list):
        for child in value:
            fragments.extend(_requirement_terms(child))
    elif isinstance(value, dict):
        for key, child in value.items():
            if key.lower() not in {"company", "salary_range", "location"}:
                fragments.extend(_requirement_terms(child))
    tokens: list[str] = []
    for fragment in fragments:
        for token in re.findall(r"[\w+#.-]{2,}", fragment.lower(), flags=re.UNICODE):
            cleaned = token.strip(".-")
            if cleaned and cleaned not in _STOPWORDS and not cleaned.isdigit():
                tokens.append(cleaned)
    counts: dict[str, int] = defaultdict(int)
    for token in tokens:
        counts[token] += 1
    return [
        token
        for token, _count in sorted(
            counts.items(), key=lambda item: (-item[1], item[0])
        )[:24]
    ]


@router.get(
    "/positions/{position_id}/talent-matches", response_model=TalentCandidatePage
)
async def rediscover_talent_for_position(
    position_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> TalentCandidatePage:
    position = (
        await db.execute(
            select(Position).where(
                Position.id == position_id,
                Position.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if position is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found")
    await require_field_access(db, user, FieldClass.CONTACT)
    await require_field_access(db, user, FieldClass.CANDIDATE_DOCUMENT)
    terms = _requirement_terms(
        [position.title, position.description or "", position.requirements or {}]
    )
    if not terms:
        return TalentCandidatePage(items=[], total=0)
    # `to_tsquery` has its own operators. Strip punctuation from extracted
    # display terms before constructing the OR expression so values such as
    # C++ or .NET can never become query syntax.
    lexemes = [re.sub(r"[^\w]+", "", term, flags=re.UNICODE) for term in terms]
    lexemes = [term for term in lexemes if term]
    if not lexemes:
        return TalentCandidatePage(items=[], total=0)
    ts_query = func.to_tsquery("simple", " | ".join(lexemes))
    family_match, relevance = _family_search(ts_query, user.organization_id)
    match_stmt = select(Candidate, relevance.label("relevance")).where(
        Candidate.organization_id == user.organization_id,
        Candidate.merged_at.is_(None),
        family_match,
    )
    scope = _talent_cursor_scope(
        _MATCH_CURSOR_NAMESPACE,
        user.organization_id,
        position_id=str(position.id),
        search_expression=" | ".join(lexemes),
    )
    total = (
        await db.execute(select(func.count()).select_from(match_stmt.subquery()))
    ).scalar_one()
    if cursor is not None:
        cursor_rank, cursor_time, cursor_id = _decode_rank_cursor(
            cursor,
            namespace=_MATCH_CURSOR_NAMESPACE,
            scope=scope,
        )
        match_stmt = match_stmt.where(
            or_(
                relevance < cursor_rank,
                and_(
                    relevance == cursor_rank,
                    Candidate.created_at < cursor_time,
                ),
                and_(
                    relevance == cursor_rank,
                    Candidate.created_at == cursor_time,
                    Candidate.id < cursor_id,
                ),
            )
        )
    rows = (
        await db.execute(
            match_stmt.order_by(
                relevance.desc(), Candidate.created_at.desc(), Candidate.id.desc()
            )
            .limit(limit + 1)
        )
    ).all()
    visible = rows[:limit]
    candidate_ids = [candidate.id for candidate, _rank in visible]
    aliases = await _candidate_aliases(db, user.organization_id, candidate_ids)
    best = await _visible_best_scores(db, user, candidate_ids)
    in_pipeline = set(
        (
            await db.execute(
                select(Application.candidate_id).where(
                    Application.organization_id == user.organization_id,
                    Application.position_id == position.id,
                    Application.candidate_id.in_(candidate_ids),
                    Application.closed_at.is_(None),
                    Application.archived_at.is_(None),
                )
            )
        ).scalars()
    )
    outputs: list[TalentCandidateOut] = []
    for candidate, rank in visible:
        outputs.append(
            _candidate_out(
                candidate,
                best,
                contact_visible=True,
                relevance=round(float(rank), 4),
                match_reasons=_explain_matches(
                    [candidate, *aliases.get(candidate.id, [])], terms
                ),
                already_in_pipeline=candidate.id in in_pipeline,
            )
        )
    return TalentCandidatePage(
        items=outputs,
        total=total,
        next_cursor=(
            _encode_rank_cursor(
                _MATCH_CURSOR_NAMESPACE,
                relevance=float(visible[-1][1]),
                created_at=visible[-1][0].created_at,
                candidate_id=visible[-1][0].id,
                scope=scope,
            )
            if len(rows) > limit and visible
            else None
        ),
    )


def _sequence_out(sequence: TalentNurtureSequence) -> TalentNurtureSequenceOut:
    return TalentNurtureSequenceOut(
        id=sequence.id,
        name=sequence.name,
        pool_id=sequence.pool_id,
        is_active=sequence.is_active,
        steps=[
            TalentNurtureStepOut(
                id=step.id,
                position=step.position,
                channel=step.channel,
                template_id=step.template_id,
                template_name=step.template_name_snapshot,
                delay_days=step.delay_days,
            )
            for step in sequence.steps
        ],
        created_at=sequence.created_at,
        updated_at=sequence.updated_at,
    )


@router.get(
    "/talent/nurture-sequences", response_model=TalentNurtureSequencePage
)
async def list_nurture_sequences(
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> TalentNurtureSequencePage:
    scope = _talent_cursor_scope(_NURTURE_CURSOR_NAMESPACE, user.organization_id)
    stmt = (
        select(TalentNurtureSequence)
        .options(selectinload(TalentNurtureSequence.steps))
        .where(TalentNurtureSequence.organization_id == user.organization_id)
    )
    if cursor is not None:
        cursor_time, cursor_id = _decode_time_cursor(
            cursor,
            namespace=_NURTURE_CURSOR_NAMESPACE,
            scope=scope,
        )
        stmt = stmt.where(
            or_(
                TalentNurtureSequence.created_at < cursor_time,
                and_(
                    TalentNurtureSequence.created_at == cursor_time,
                    TalentNurtureSequence.id < cursor_id,
                ),
            )
        )
    sequences = list(
        (
            await db.execute(
                stmt.order_by(
                    TalentNurtureSequence.created_at.desc(),
                    TalentNurtureSequence.id.desc(),
                ).limit(limit + 1)
            )
        ).scalars()
    )
    visible = sequences[:limit]
    return TalentNurtureSequencePage(
        items=[_sequence_out(sequence) for sequence in visible],
        next_cursor=(
            _encode_time_cursor(
                _NURTURE_CURSOR_NAMESPACE,
                created_at=visible[-1].created_at,
                row_id=visible[-1].id,
                scope=scope,
            )
            if len(sequences) > limit and visible
            else None
        ),
    )


@router.post(
    "/talent/nurture-sequences",
    response_model=TalentNurtureSequenceOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_nurture_sequence(
    body: TalentNurtureSequenceCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> TalentNurtureSequenceOut:
    if body.pool_id is not None:
        await _pool(db, body.pool_id, user.organization_id)
    template_ids = [step.template_id for step in body.steps]
    templates = list(
        (
            await db.execute(
                select(CommunicationTemplate).where(
                    CommunicationTemplate.organization_id == user.organization_id,
                    CommunicationTemplate.id.in_(template_ids),
                    CommunicationTemplate.is_active.is_(True),
                )
            )
        ).scalars()
    )
    by_id = {template.id: template for template in templates}
    if len(by_id) != len(set(template_ids)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Communication template not found")
    for step_body in body.steps:
        if by_id[step_body.template_id].channel != step_body.channel:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Template channel does not match nurture step",
            )
    sequence = TalentNurtureSequence(
        organization_id=user.organization_id,
        pool_id=body.pool_id,
        created_by_user_id=user.id,
        name=body.name,
    )
    try:
        db.add(sequence)
        await db.flush()
        for index, step_body in enumerate(body.steps):
            template = by_id[step_body.template_id]
            db.add(
                TalentNurtureStep(
                    organization_id=user.organization_id,
                    sequence_id=sequence.id,
                    template_id=template.id,
                    position=index,
                    delay_days=step_body.delay_days,
                    channel=step_body.channel,
                    template_name_snapshot=template.name,
                )
            )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A nurture sequence with this name already exists"
        ) from exc
    refreshed = (
        await db.execute(
            select(TalentNurtureSequence)
            .options(selectinload(TalentNurtureSequence.steps))
            .where(TalentNurtureSequence.id == sequence.id)
        )
    ).scalar_one()
    return _sequence_out(refreshed)


@router.post(
    "/talent/nurture-sequences/{sequence_id}/launch",
    response_model=TalentNurtureLaunchOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def launch_nurture_sequence(
    sequence_id: UUID,
    body: TalentNurtureLaunchRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> TalentNurtureLaunchOut:
    await require_field_access(db, user, FieldClass.CONTACT)
    sequence = (
        await db.execute(
            select(TalentNurtureSequence)
            .options(selectinload(TalentNurtureSequence.steps))
            .where(
                TalentNurtureSequence.id == sequence_id,
                TalentNurtureSequence.organization_id == user.organization_id,
                TalentNurtureSequence.is_active.is_(True),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if sequence is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Nurture sequence not found")
    if body.position_id is not None:
        position = (
            await db.execute(
                select(Position).where(
                    Position.id == body.position_id,
                    Position.organization_id == user.organization_id,
                )
            )
        ).scalar_one_or_none()
        if position is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found")
    raw_candidate_ids = body.candidate_ids
    if raw_candidate_ids is None:
        if sequence.pool_id is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Provide candidates or attach the sequence to a pool",
            )
        raw_candidate_ids = list(
            (
                await db.execute(
                    select(TalentPoolMember.candidate_id)
                    .where(
                        TalentPoolMember.organization_id == user.organization_id,
                        TalentPoolMember.pool_id == sequence.pool_id,
                        TalentPoolMember.status == "active",
                    )
                    .limit(101)
                )
            ).scalars()
        )
    if len(raw_candidate_ids) > 100:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Nurture launch is capped at 100 candidates",
        )
    canonical_candidates: dict[UUID, Candidate] = {}
    for raw_candidate_id in raw_candidate_ids:
        candidate = await resolve_canonical_candidate(
            db, raw_candidate_id, user.organization_id
        )
        if candidate is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
        canonical_candidates[candidate.id] = candidate
    candidate_ids = list(canonical_candidates)
    existing_candidate_ids = set(
        (
            await db.execute(
                select(TalentNurtureEnrollment.candidate_id).where(
                    TalentNurtureEnrollment.organization_id == user.organization_id,
                    TalentNurtureEnrollment.sequence_id == sequence.id,
                    TalentNurtureEnrollment.candidate_id.in_(candidate_ids),
                )
            )
        ).scalars()
    )
    templates = list(
        (
            await db.execute(
                select(CommunicationTemplate).where(
                    CommunicationTemplate.organization_id == user.organization_id,
                    CommunicationTemplate.id.in_(
                        [step.template_id for step in sequence.steps if step.template_id]
                    ),
                    CommunicationTemplate.is_active.is_(True),
                )
            )
        ).scalars()
    )
    available_templates = {template.id for template in templates}
    now = datetime.now(UTC)
    enrolled = 0
    skipped = 0
    already = 0
    messages_scheduled = 0
    for candidate in canonical_candidates.values():
        if candidate.id in existing_candidate_ids:
            already += 1
            continue
        prepared_steps = []
        for step in sequence.steps:
            if step.template_id is None or step.template_id not in available_templates:
                continue
            compose = CandidateCommunicationCompose(
                channel=step.channel,
                template_id=step.template_id,
                position_id=body.position_id,
            )
            try:
                prepared = await prepare_candidate_communication(
                    db,
                    organization_id=user.organization_id,
                    actor=user,
                    candidate=candidate,
                    request=compose,
                )
            except CommunicationPreparationError:
                continue
            prepared_steps.append((step, prepared))
        if not prepared_steps:
            skipped += 1
            continue
        enrollment = TalentNurtureEnrollment(
            organization_id=user.organization_id,
            sequence_id=sequence.id,
            candidate_id=candidate.id,
            started_by_user_id=user.id,
            sequence_name_snapshot=sequence.name,
            status=("active" if len(prepared_steps) == len(sequence.steps) else "partial"),
            scheduled_count=len(prepared_steps),
            started_at=now,
        )
        db.add(enrollment)
        await db.flush()
        for step, prepared in prepared_steps:
            scheduled_for = now + timedelta(days=step.delay_days)
            _message, created = await create_outbound_message(
                db,
                prepared=prepared,
                actor_user_id=user.id,
                idempotency_key=f"nurture:{sequence.id}:{candidate.id}:{step.id}",
                scheduled_for=scheduled_for,
            )
            if created:
                messages_scheduled += 1
        append_candidate_activity(
            db,
            organization_id=user.organization_id,
            candidate_id=candidate.id,
            actor_user_id=user.id,
            position_id=body.position_id,
            application_id=(
                prepared_steps[0][1].application.id
                if prepared_steps[0][1].application is not None
                else None
            ),
            event_type="nurture_started",
            subject_type="talent_nurture_enrollment",
            subject_id=enrollment.id,
            payload={
                "sequence_name": sequence.name,
                "step_count": len(prepared_steps),
            },
        )
        enrolled += 1
    await db.commit()
    return TalentNurtureLaunchOut(
        enrolled=enrolled,
        already_enrolled=already,
        skipped=skipped,
        messages_scheduled=messages_scheduled,
    )
