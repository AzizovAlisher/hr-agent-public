"""Application-cycle integrity helpers for structured hiring artifacts."""

from datetime import datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application import Application
from app.services.applications import ensure_application_link


def application_is_active(application: Application) -> bool:
    return application.closed_at is None and application.archived_at is None


async def resolve_existing_active_application(
    db: AsyncSession,
    *,
    organization_id: UUID,
    candidate_id: UUID,
    position_id: UUID,
    application_id: UUID | None,
    lock: bool = False,
) -> Application:
    """Resolve an already-existing active cycle without creating side effects.

    Preview, chat, communication, and scheduling surfaces use this narrower
    contract: merely viewing or composing an artifact must never attach a
    candidate to a position. Reapplication history is safe because the caller
    either names the exact cycle or the one-active-cycle invariant identifies
    it unambiguously.
    """

    stmt = select(Application).where(
        Application.organization_id == organization_id,
        Application.candidate_id == candidate_id,
        Application.position_id == position_id,
        Application.closed_at.is_(None),
        Application.archived_at.is_(None),
    )
    if application_id is not None:
        stmt = stmt.where(Application.id == application_id)
    if lock:
        stmt = stmt.with_for_update()
    application = (await db.execute(stmt)).scalar_one_or_none()
    if application is None:
        if application_id is not None:
            mismatch = (
                await db.execute(
                    select(Application.id).where(
                        Application.id == application_id,
                        Application.organization_id == organization_id,
                    )
                )
            ).scalar_one_or_none()
            if mismatch is not None:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Application does not match this active candidate and position",
                )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "An active application is required",
        )
    return application


async def resolve_application_for_write(
    db: AsyncSession,
    *,
    organization_id: UUID,
    candidate_id: UUID,
    position_id: UUID,
    application_id: UUID | None,
    source: str,
    actor_user_id: UUID | None,
    actor_kind: str,
    reason: str,
    occurred_at: datetime | None = None,
) -> Application:
    """Resolve the exact active application cycle for a new child artifact.

    Existing clients may omit ``application_id``. That compatibility path is
    accepted only when one active cycle exists, or when the legacy Score pair
    has never had an Application and can be promoted atomically. Closed-cycle
    inference is intentionally rejected because a reapplication makes it
    impossible to know which hiring record the caller intended.
    """

    if application_id is not None:
        application = (
            await db.execute(
                select(Application)
                .where(
                    Application.id == application_id,
                    Application.organization_id == organization_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if application is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Application not found")
        if (
            application.candidate_id != candidate_id
            or application.position_id != position_id
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Application does not match this candidate and position",
            )
        if not application_is_active(application):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "New hiring records require an active application",
            )
        return application

    applications = list(
        (
            await db.execute(
                select(Application)
                .where(
                    Application.organization_id == organization_id,
                    Application.candidate_id == candidate_id,
                    Application.position_id == position_id,
                )
                .order_by(Application.cycle_number.desc(), Application.id.desc())
                .with_for_update()
            )
        ).scalars()
    )
    active = [item for item in applications if application_is_active(item)]
    if len(active) == 1:
        return active[0]
    if len(active) > 1:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Application cycle is ambiguous; provide application_id",
        )
    if applications:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "No active application exists; provide an active application_id",
        )

    linked = await ensure_application_link(
        db,
        organization_id=organization_id,
        candidate_id=candidate_id,
        position_id=position_id,
        source=source,
        owner_user_id=None,
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
        reason=reason,
        occurred_at=occurred_at,
    )
    if not application_is_active(linked.application):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "New hiring records require an active application",
        )
    return linked.application


async def resolve_linked_application(
    db: AsyncSession,
    *,
    organization_id: UUID,
    application_id: UUID | None,
    candidate_id: UUID,
    position_id: UUID | None,
    lock: bool = False,
) -> Application:
    """Resolve a persisted artifact link, with a narrow legacy fallback."""

    if application_id is not None:
        stmt = select(Application).where(
            Application.id == application_id,
            Application.organization_id == organization_id,
            Application.candidate_id == candidate_id,
        )
        if position_id is not None:
            stmt = stmt.where(Application.position_id == position_id)
        if lock:
            stmt = stmt.with_for_update()
        application = (await db.execute(stmt)).scalar_one_or_none()
        if application is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Application not found")
        return application

    if position_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Legacy hiring record has no unambiguous application link",
        )
    stmt = select(Application).where(
        Application.organization_id == organization_id,
        Application.candidate_id == candidate_id,
        Application.position_id == position_id,
    )
    if lock:
        stmt = stmt.with_for_update()
    matches = list((await db.execute(stmt)).scalars())
    if len(matches) != 1:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Legacy hiring record has no unambiguous application link",
        )
    return matches[0]
