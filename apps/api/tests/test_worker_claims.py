"""Concurrency, reclaim, stale-ack, and tenant binding for worker leases."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from app.db import SessionLocal, tenant_session_scope
from app.models.notification import Notification
from app.models.organization import Organization
from app.models.user import User
from app.observability.metrics import worker_telemetry_snapshot
from app.services.worker_claims import (
    StaleWorkClaim,
    WorkClaim,
    WorkQueue,
    assert_active_claim,
    claim_work,
    complete_claim,
    process_one_claim,
    process_recurring_claim,
    release_claim,
)

pytestmark = pytest.mark.asyncio


async def _organization(db, label: str) -> Organization:
    marker = uuid4().hex[:10]
    organization = Organization(name=label, slug=f"{label.lower()}-{marker}")
    db.add(organization)
    await db.flush()
    return organization


async def _claim_maintenance(
    *,
    worker_id: str,
    now: datetime,
    limit: int = 1,
) -> list[WorkClaim]:
    async with SessionLocal() as session:
        claims = await claim_work(
            session,
            queue=WorkQueue.MAINTENANCE,
            worker_id=worker_id,
            limit=limit,
            mode="off",
            lease_seconds=15,
            now=now,
        )
        await session.commit()
        return claims


async def test_two_workers_claim_disjoint_work_and_bind_fresh_tenant_sessions(db):
    first = await _organization(db, "WorkerOne")
    second = await _organization(db, "WorkerTwo")
    await db.commit()
    now = datetime.now(UTC)

    left, right = await asyncio.gather(
        _claim_maintenance(worker_id="worker:left", now=now),
        _claim_maintenance(worker_id="worker:right", now=now),
    )
    claims = [*left, *right]
    assert len(claims) == 2
    assert {claim.work_id for claim in claims} == {first.id, second.id}
    assert len({claim.lease_token for claim in claims}) == 2

    observed: dict = {}

    async def inspect_context(session, claim: WorkClaim) -> None:
        observed[claim.work_id] = await session.scalar(
            text("SELECT current_setting('app.organization_id', true)")
        )

    await asyncio.gather(
        *(process_one_claim(claim, inspect_context) for claim in claims)
    )
    assert observed == {claim.work_id: str(claim.organization_id) for claim in claims}
    snapshot = worker_telemetry_snapshot()
    assert any(
        item["queue"] == "maintenance"
        and item["outcome"] == "succeeded"
        and item["count"] >= 2
        for item in snapshot["job_operations"]
    )
    serialized = repr(snapshot)
    assert all(str(claim.work_id) not in serialized for claim in claims)
    assert all(str(claim.organization_id) not in serialized for claim in claims)


async def test_release_and_expiry_reclaim_rotate_token(db):
    organization = await _organization(db, "Reclaim")
    await db.commit()
    now = datetime.now(UTC)
    first = (await _claim_maintenance(worker_id="worker:first", now=now))[0]

    async with tenant_session_scope(
        organization_id=organization.id,
        identity_kind="background",
        role="worker",
    ) as session:
        assert await release_claim(session, first) is True
    released_reclaim = (await _claim_maintenance(worker_id="worker:released", now=now))[
        0
    ]
    assert released_reclaim.lease_token != first.lease_token

    # Simulate a crash by leaving the second claim unacknowledged. It cannot be
    # stolen before expiry, then becomes reclaimable with a new token.
    assert (
        await _claim_maintenance(
            worker_id="worker:early",
            now=now + timedelta(seconds=14),
        )
        == []
    )
    expired_reclaim = (
        await _claim_maintenance(
            worker_id="worker:after-crash",
            now=now + timedelta(seconds=16),
        )
    )[0]
    assert expired_reclaim.work_id == organization.id
    assert expired_reclaim.lease_token != released_reclaim.lease_token


async def test_recurring_claim_cursor_prevents_bounded_batch_starvation(db):
    first = await _organization(db, "RecurringOne")
    second = await _organization(db, "RecurringTwo")
    third = await _organization(db, "RecurringThree")
    await db.commit()
    now = datetime.now(UTC)
    initial = await _claim_maintenance(
        worker_id="worker:recurring-initial",
        now=now,
        limit=2,
    )
    assert {claim.work_id for claim in initial} == {first.id, second.id}

    async def no_op(_session, _claim: WorkClaim) -> None:
        return None

    for claim in initial:
        await process_recurring_claim(
            claim,
            no_op,
            cooldown=timedelta(hours=1),
            now=now,
        )

    following = await _claim_maintenance(
        worker_id="worker:recurring-following",
        now=now + timedelta(seconds=1),
        limit=2,
    )
    assert [claim.work_id for claim in following] == [third.id]

    after_cooldown = await _claim_maintenance(
        worker_id="worker:recurring-next-cycle",
        now=now + timedelta(hours=1, seconds=1),
        limit=2,
    )
    assert {claim.work_id for claim in after_cooldown} == {first.id, second.id}


async def test_stale_ack_rolls_back_adjacent_tenant_mutation(db):
    organization = await _organization(db, "StaleAck")
    user = User(
        organization_id=organization.id,
        email=f"stale-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    db.add(user)
    await db.commit()
    now = datetime.now(UTC)
    old = (await _claim_maintenance(worker_id="worker:old", now=now))[0]
    current = (
        await _claim_maintenance(
            worker_id="worker:new",
            now=now + timedelta(seconds=16),
        )
    )[0]

    with pytest.raises(StaleWorkClaim):
        async with tenant_session_scope(
            organization_id=organization.id,
            identity_kind="background",
            role="worker",
        ) as session:
            session.add(
                Notification(
                    organization_id=organization.id,
                    user_id=user.id,
                    type="stale_worker",
                    payload={},
                )
            )
            await session.flush()
            await complete_claim(
                session,
                old,
                now=now + timedelta(seconds=16),
            )

    assert (
        int(
            (
                await db.execute(
                    select(func.count(Notification.id)).where(
                        Notification.organization_id == organization.id,
                        Notification.type == "stale_worker",
                    )
                )
            ).scalar_one()
        )
        == 0
    )
    async with tenant_session_scope(
        organization_id=organization.id,
        identity_kind="background",
        role="worker",
    ) as session:
        assert await release_claim(session, current) is True


async def test_claim_cannot_be_validated_from_another_tenant(db):
    first = await _organization(db, "TenantA")
    second = await _organization(db, "TenantB")
    await db.commit()
    claim = (
        await _claim_maintenance(
            worker_id="worker:tenant",
            now=datetime.now(UTC),
        )
    )[0]
    assert claim.organization_id in {first.id, second.id}
    wrong_organization = second.id if claim.organization_id == first.id else first.id
    forged = WorkClaim(
        queue=claim.queue,
        work_id=claim.work_id,
        organization_id=wrong_organization,
        lease_token=claim.lease_token,
        lease_expires_at=claim.lease_expires_at,
    )

    async with tenant_session_scope(
        organization_id=wrong_organization,
        identity_kind="background",
        role="worker",
    ) as session:
        with pytest.raises(StaleWorkClaim):
            await assert_active_claim(session, forged)
    async with tenant_session_scope(
        organization_id=claim.organization_id,
        identity_kind="background",
        role="worker",
    ) as session:
        assert await release_claim(session, claim) is True


async def test_forced_broker_claim_returns_identity_only_and_is_concurrency_safe(db):
    available = await db.scalar(
        text(
            "SELECT to_regprocedure("
            "'security.claim_maintenance_organizations(text,integer,timestamptz)')"
        )
    )
    if available is None:
        pytest.skip("Forced-RLS broker functions require the migrated schema")
    first = await _organization(db, "ForcedOne")
    second = await _organization(db, "ForcedTwo")
    await db.commit()
    now = datetime.now(UTC)

    async def forced(worker_id: str) -> list[WorkClaim]:
        async with SessionLocal() as session:
            claims = await claim_work(
                session,
                queue=WorkQueue.MAINTENANCE,
                worker_id=worker_id,
                limit=1,
                mode="forced",
                now=now,
            )
            await session.commit()
            return claims

    left, right = await asyncio.gather(forced("forced:left"), forced("forced:right"))
    claims = [*left, *right]
    assert len(claims) == 2
    assert {claim.work_id for claim in claims} == {first.id, second.id}
    assert set(WorkClaim.__dataclass_fields__) == {
        "queue",
        "work_id",
        "organization_id",
        "lease_token",
        "lease_expires_at",
    }
    for claim in claims:
        async with tenant_session_scope(
            organization_id=claim.organization_id,
            identity_kind="background",
            role="worker",
        ) as session:
            assert await release_claim(session, claim) is True
