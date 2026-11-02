"""Race-safe contract entitlement and append-only usage helpers."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.operations import OrganizationEntitlement, UsageLedgerEntry
from app.services.pagination import (
    collection_cursor_scope,
    cursor_datetime,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)

ENTITLEMENT_METRICS = (
    "seats",
    "active_jobs",
    "candidates",
    "candidate_import_rows",
    "assessment_starts",
    "ai_cost_microusd",
    "ai_tokens",
    "storage_bytes",
    "integration_runs",
)

# Current-state metrics are derived from the domain tables on every read.  The
# ledger remains an immutable audit trail, but it is deliberately not the
# source of truth for resources that can be created, disabled, merged, closed,
# restored, or erased.  This makes existing tenant data visible immediately
# without a risky one-off backfill.
CURRENT_STATE_METRICS = frozenset(
    {"seats", "active_jobs", "candidates", "storage_bytes"}
)
USAGE_EVENT_KINDS = frozenset({"reserve", "usage", "settle", "refund", "adjustment"})

# These are technical safety bounds, not commercial pricing tiers. Keeping
# them in one server contract prevents UI/API drift.
TECHNICAL_LIMITS = {
    "candidate_document_bytes": 15 * 1024 * 1024,
    "sourcing_document_bytes": 10 * 1024 * 1024,
    "synchronous_import_rows": 500,
    "synchronous_recipients": 100,
}


def effective_synchronous_recipient_limit(configured_limit: int) -> int:
    """Return the authoritative synchronous communication batch limit.

    ``TECHNICAL_LIMITS`` is the absolute safety ceiling. Operators may choose a
    more conservative deployment limit, but configuration can never increase
    the public contract beyond that ceiling or make the feature unusable with
    a zero/negative value.
    """

    return max(
        1,
        min(int(configured_limit), TECHNICAL_LIMITS["synchronous_recipients"]),
    )


def effective_technical_limits(
    *, communications_max_bulk_candidates: int
) -> dict[str, int]:
    """Copy the public limits contract with deployment-specific clamps."""

    limits = dict(TECHNICAL_LIMITS)
    limits["synchronous_recipients"] = effective_synchronous_recipient_limit(
        communications_max_bulk_candidates
    )
    return limits


class EntitlementExceeded(ValueError):
    def __init__(self, *, metric: str, limit: int, used: int, requested: int):
        self.metric = metric
        self.limit = limit
        self.used = used
        self.requested = requested
        super().__init__(f"Entitlement {metric} would exceed its contract limit")


class UsageIdempotencyConflict(ValueError):
    pass


@dataclass(frozen=True)
class EntitlementSnapshot:
    metric: str
    limit: int | None
    used: int
    period: str
    effective_from: datetime | None
    effective_until: datetime | None

    @property
    def basis(self) -> str:
        return (
            "current_state"
            if self.metric in CURRENT_STATE_METRICS
            else "periodic_cumulative"
        )

    @property
    def remaining(self) -> int | None:
        if self.limit is None:
            return None
        return max(0, self.limit - self.used)


def _period_start(period: str, now: datetime) -> datetime | None:
    if period == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return None


async def _active_entitlement(
    db: AsyncSession, organization_id: UUID, metric: str, now: datetime
) -> OrganizationEntitlement | None:
    if metric not in ENTITLEMENT_METRICS:
        raise ValueError(f"Unknown entitlement metric: {metric}")
    return (
        await db.execute(
            select(OrganizationEntitlement).where(
                OrganizationEntitlement.organization_id == organization_id,
                OrganizationEntitlement.metric == metric,
                or_(
                    OrganizationEntitlement.effective_from.is_(None),
                    OrganizationEntitlement.effective_from <= now,
                ),
                or_(
                    OrganizationEntitlement.effective_until.is_(None),
                    OrganizationEntitlement.effective_until > now,
                ),
            )
        )
    ).scalar_one_or_none()


async def _used(
    db: AsyncSession,
    organization_id: UUID,
    metric: str,
    *,
    period: str,
    now: datetime,
) -> int:
    if metric in CURRENT_STATE_METRICS:
        return await _current_state_used(db, organization_id, metric)

    conditions = [
        UsageLedgerEntry.organization_id == organization_id,
        UsageLedgerEntry.metric == metric,
    ]
    period_start = _period_start(period, now)
    if period_start is not None:
        conditions.append(UsageLedgerEntry.occurred_at >= period_start)
    value = (
        await db.execute(
            select(func.coalesce(func.sum(UsageLedgerEntry.amount), 0)).where(
                *conditions
            )
        )
    ).scalar_one()
    return max(0, int(value))


async def _current_state_used(
    db: AsyncSession,
    organization_id: UUID,
    metric: str,
) -> int:
    """Read a contract's current usage from its authoritative live table."""

    # Imports stay local to avoid coupling model initialization to this service
    # (the storage registry imports ``record_usage`` for promotion/deletion).
    if metric == "seats":
        from app.models.user import User

        statement = select(func.count(User.id)).where(
            User.organization_id == organization_id,
            User.is_active.is_(True),
        )
    elif metric == "active_jobs":
        from app.models.position import Position

        statement = select(func.count(Position.id)).where(
            Position.organization_id == organization_id,
            Position.status == "open",
            Position.deleted_at.is_(None),
        )
    elif metric == "candidates":
        from app.models.candidate import Candidate

        statement = select(func.count(Candidate.id)).where(
            Candidate.organization_id == organization_id,
            Candidate.merged_at.is_(None),
            Candidate.deleted_at.is_(None),
        )
    elif metric == "storage_bytes":
        from app.models.private_object import PrivateObject

        # ``delete_pending`` objects still occupy the private object store and
        # remain billable until the durable deletion worker settles them.
        statement = select(func.coalesce(func.sum(PrivateObject.size_bytes), 0)).where(
            PrivateObject.organization_id == organization_id,
            PrivateObject.state.in_(("ready", "delete_pending")),
        )
    else:  # pragma: no cover - callers validate against CURRENT_STATE_METRICS
        raise ValueError(f"Unknown current-state metric: {metric}")
    return max(0, int((await db.execute(statement)).scalar_one()))


async def entitlement_snapshot(
    db: AsyncSession, organization_id: UUID, metric: str, *, now: datetime | None = None
) -> EntitlementSnapshot:
    now = now or datetime.now(UTC)
    row = await _active_entitlement(db, organization_id, metric, now)
    # A day/month window has no sensible meaning for a current live count.
    # Fail to the documented contract basis even if operator metadata contains
    # a stale period value from an older build.
    period = (
        "contract"
        if metric in CURRENT_STATE_METRICS
        else (row.period if row else "contract")
    )
    used = await _used(db, organization_id, metric, period=period, now=now)
    return EntitlementSnapshot(
        metric=metric,
        limit=row.limit_value if row else None,
        used=used,
        period=period,
        effective_from=row.effective_from if row else None,
        effective_until=row.effective_until if row else None,
    )


async def all_entitlement_snapshots(
    db: AsyncSession, organization_id: UUID
) -> list[EntitlementSnapshot]:
    now = datetime.now(UTC)
    return [
        await entitlement_snapshot(db, organization_id, metric, now=now)
        for metric in ENTITLEMENT_METRICS
    ]


async def record_usage(
    db: AsyncSession,
    *,
    organization_id: UUID,
    metric: str,
    amount: int,
    kind: str,
    idempotency_key: str,
    resource_type: str | None = None,
    resource_id: UUID | None = None,
    metadata: dict | None = None,
    enforce_limit: bool = True,
) -> UsageLedgerEntry:
    """Atomically append a usage delta and optionally enforce the hard cap.

    A PostgreSQL transaction advisory lock serializes concurrent reservations
    for the same organization/metric. The caller owns commit/rollback so the
    ledger entry is atomic with the domain mutation it meters.
    """

    if metric not in ENTITLEMENT_METRICS:
        raise ValueError(f"Unknown entitlement metric: {metric}")
    if kind not in USAGE_EVENT_KINDS:
        raise ValueError(f"Unknown usage event kind: {kind}")
    if amount == 0:
        raise ValueError("Usage event amount must not be zero")
    if not idempotency_key or len(idempotency_key) > 160:
        raise ValueError("Invalid usage idempotency key")
    existing = (
        await db.execute(
            select(UsageLedgerEntry).where(
                UsageLedgerEntry.organization_id == organization_id,
                UsageLedgerEntry.metric == metric,
                UsageLedgerEntry.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.amount != amount
            or existing.kind != kind
            or existing.resource_type != resource_type
            or existing.resource_id != resource_id
            or existing.metadata_json != (metadata or {})
        ):
            raise UsageIdempotencyConflict(
                "Idempotency key is already bound to another usage event"
            )
        return existing

    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"entitlement:{organization_id}:{metric}"},
    )
    # Check again after acquiring the lock: another transaction may have
    # committed the same idempotency key while this transaction was waiting.
    existing = (
        await db.execute(
            select(UsageLedgerEntry).where(
                UsageLedgerEntry.organization_id == organization_id,
                UsageLedgerEntry.metric == metric,
                UsageLedgerEntry.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.amount != amount
            or existing.kind != kind
            or existing.resource_type != resource_type
            or existing.resource_id != resource_id
            or existing.metadata_json != (metadata or {})
        ):
            raise UsageIdempotencyConflict(
                "Idempotency key is already bound to another usage event"
            )
        return existing

    snapshot = await entitlement_snapshot(db, organization_id, metric)
    if (
        enforce_limit
        and amount > 0
        and snapshot.limit is not None
        and snapshot.used + amount > snapshot.limit
    ):
        raise EntitlementExceeded(
            metric=metric,
            limit=snapshot.limit,
            used=snapshot.used,
            requested=amount,
        )
    row = UsageLedgerEntry(
        organization_id=organization_id,
        metric=metric,
        amount=amount,
        kind=kind,
        idempotency_key=idempotency_key,
        resource_type=resource_type,
        resource_id=resource_id,
        metadata_json=metadata or {},
        occurred_at=datetime.now(UTC),
    )
    db.add(row)
    await db.flush()
    return row


async def ensure_entitlement_capacity(
    db: AsyncSession,
    *,
    organization_id: UUID,
    metric: str,
    requested: int = 1,
) -> EntitlementSnapshot:
    """Serialize and check capacity without recording or consuming a unit.

    This is used for inactive invitations: an invitation does not count as a
    seat, but creating one while the tenant is already full would be
    misleading.  Acceptance performs the authoritative metered mutation.
    """

    if metric not in ENTITLEMENT_METRICS:
        raise ValueError(f"Unknown entitlement metric: {metric}")
    if requested <= 0:
        raise ValueError("requested must be positive")
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"entitlement:{organization_id}:{metric}"},
    )
    snapshot = await entitlement_snapshot(db, organization_id, metric)
    if snapshot.limit is not None and snapshot.used + requested > snapshot.limit:
        raise EntitlementExceeded(
            metric=metric,
            limit=snapshot.limit,
            used=snapshot.used,
            requested=requested,
        )
    return snapshot


def encode_usage_cursor(
    occurred_at: datetime,
    entry_id: UUID,
    *,
    scope: str,
) -> str:
    return encode_collection_cursor(
        "enterprise.usage-ledger",
        scope=scope,
        boundary={"occurred_at": occurred_at.isoformat(), "id": str(entry_id)},
    )


def decode_usage_cursor(cursor: str, *, scope: str) -> tuple[datetime, UUID]:
    payload = decode_collection_cursor(
        cursor,
        namespace="enterprise.usage-ledger",
        scope=scope,
        boundary_keys={"occurred_at", "id"},
        detail="Invalid usage cursor",
    )
    return (
        cursor_datetime(payload, "occurred_at", detail="Invalid usage cursor"),
        cursor_uuid(payload, detail="Invalid usage cursor"),
    )


async def list_usage(
    db: AsyncSession,
    *,
    organization_id: UUID,
    metric: str | None,
    limit: int,
    cursor: str | None,
) -> tuple[list[UsageLedgerEntry], str | None]:
    conditions = [UsageLedgerEntry.organization_id == organization_id]
    if metric is not None:
        if metric not in ENTITLEMENT_METRICS:
            raise ValueError("Unknown entitlement metric")
        conditions.append(UsageLedgerEntry.metric == metric)
    cursor_scope = collection_cursor_scope(
        "enterprise.usage-ledger",
        organization_id=organization_id,
        filters={"metric": metric},
    )
    if cursor:
        cursor_time, cursor_id = decode_usage_cursor(cursor, scope=cursor_scope)
        conditions.append(
            or_(
                UsageLedgerEntry.occurred_at < cursor_time,
                and_(
                    UsageLedgerEntry.occurred_at == cursor_time,
                    UsageLedgerEntry.id < cursor_id,
                ),
            )
        )
    rows = list(
        (
            await db.execute(
                select(UsageLedgerEntry)
                .where(*conditions)
                .order_by(
                    UsageLedgerEntry.occurred_at.desc(), UsageLedgerEntry.id.desc()
                )
                .limit(limit + 1)
            )
        ).scalars()
    )
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit and page:
        next_cursor = encode_usage_cursor(
            page[-1].occurred_at,
            page[-1].id,
            scope=cursor_scope,
        )
    return page, next_cursor
