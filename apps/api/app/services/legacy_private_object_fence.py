"""Serialize the legacy object cutover with legacy-reference writers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


# Stable signed bigint derived from
# sha256("hr-agent:legacy-private-object-cutover:v1")[:8]. Both sides of the
# fence must contend on this exact value.
LEGACY_PRIVATE_OBJECT_FENCE_KEY = 1481345922282259566


async def acquire_legacy_private_object_write_fence(db: AsyncSession) -> None:
    """Block until the current transaction exclusively owns the write fence."""

    await db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": LEGACY_PRIVATE_OBJECT_FENCE_KEY},
        execution_options={"skip_tenant_scope": True},
    )


@asynccontextmanager
async def hold_legacy_private_object_cutover_fence(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Hold a shared transaction lock on one pinned connection for a cutover."""

    db = session_factory()
    try:
        # Transaction-scoped locks are released by commit/rollback before the
        # physical connection can return to SQLAlchemy's pool. This remains
        # safe even when task cancellation interrupts the cutover.
        async with db.begin():
            await db.execute(
                text("SELECT pg_advisory_xact_lock_shared(:lock_key)"),
                {"lock_key": LEGACY_PRIVATE_OBJECT_FENCE_KEY},
                execution_options={"skip_tenant_scope": True},
            )
            yield db
    finally:
        await db.close()


__all__ = [
    "LEGACY_PRIVATE_OBJECT_FENCE_KEY",
    "acquire_legacy_private_object_write_fence",
    "hold_legacy_private_object_cutover_fence",
]
