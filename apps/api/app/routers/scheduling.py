"""Authenticated recruiter and public candidate scheduling endpoints."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import current_user
from app.config import get_settings
from app.db import get_session
from app.models.integration import IntegrationConnection
from app.models.scheduling import (
    AvailabilityBlock,
    InterviewAppointment,
    InterviewAppointmentInterviewer,
    SchedulingLink,
)
from app.models.user import User
from app.schemas.pagination import CursorPage
from app.schemas.scheduling import (
    AppointmentManageTokenOut,
    AppointmentOut,
    AvailabilityBlockCreate,
    AvailabilityBlockOut,
    AvailabilityOut,
    AvailabilityUpdate,
    CalendarProviderOut,
    PublicAppointmentDetails,
    PublicBookingCreate,
    PublicBookingOut,
    PublicCancelRequest,
    PublicRescheduleRequest,
    PublicSchedulingDetails,
    SchedulingLinkCreate,
    SchedulingLinkIssueOut,
    SchedulingLinkOut,
    SchedulingSlotPage,
)
from app.services.candidate_merge import resolve_canonical_candidate
from app.services.pagination import (
    collection_cursor_scope,
    cursor_datetime,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)
from app.services.scheduling import (
    SchedulingError,
    appointment_out,
    available_slots,
    book_appointment,
    cancel_appointment,
    create_availability_block,
    create_scheduling_link,
    delete_availability_block,
    get_appointment_by_manage_token,
    get_link_by_token,
    get_user_availability,
    public_appointment_details,
    public_appointment_out,
    public_link_details,
    replace_user_availability,
    reschedule_appointment,
    revoke_appointment_manage_token,
    revoke_scheduling_link,
    rotate_appointment_manage_token,
    rotate_scheduling_link,
    scheduling_link_out,
    timezone_for,
)
from app.services.scheduling_providers import provider_statuses

router = APIRouter(tags=["scheduling"])
settings = get_settings()
_PUBLIC_CAPABILITY_HEADERS = {
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
}


def _protect_public_response(response: Response | None) -> None:
    if response is not None:
        response.headers.update(_PUBLIC_CAPABILITY_HEADERS)


def _paginate_slots(
    items: list,
    *,
    namespace: str,
    scope: str,
    limit: int,
    cursor: str | None,
) -> tuple[list, str | None]:
    def slot_id(item) -> UUID:
        return uuid5(
            NAMESPACE_URL,
            f"{scope}:{item.starts_at.astimezone(UTC).isoformat()}:{item.ends_at.astimezone(UTC).isoformat()}",
        )

    ordered = sorted(items, key=lambda item: (item.starts_at, slot_id(item)))
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"starts_at", "id"},
            detail="Invalid scheduling slots cursor",
        )
        cursor_time = cursor_datetime(
            payload, "starts_at", detail="Invalid scheduling slots cursor"
        )
        cursor_id = cursor_uuid(payload, detail="Invalid scheduling slots cursor")
        ordered = [
            item
            for item in ordered
            if (item.starts_at, slot_id(item)) > (cursor_time, cursor_id)
        ]
    has_more = len(ordered) > limit
    page = ordered[:limit]
    next_cursor = (
        encode_collection_cursor(
            namespace,
            scope=scope,
            boundary={
                "starts_at": page[-1].starts_at.astimezone(UTC).isoformat(),
                "id": str(slot_id(page[-1])),
            },
        )
        if has_more and page
        else None
    )
    return page, next_cursor


@router.get(
    "/calendar/appointments/me",
    response_model=CursorPage[AppointmentOut],
    dependencies=[Depends(current_user)],
)
async def list_my_appointments(
    from_datetime: datetime | None = Query(default=None),
    to_datetime: datetime | None = Query(default=None),
    include_cancelled: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[
        str | None, Query(min_length=32, max_length=2_048)
    ] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[AppointmentOut]:
    """Appointments where the caller is organizer or interviewer.

    With no explicit range this returns the current business-calendar day,
    which lets My Work show real bookings without candidate-by-candidate fanout.
    """

    if from_datetime is None and to_datetime is None:
        timezone = ZoneInfo(settings.business_timezone)
        current_day = datetime.now(timezone).date()
        from_datetime = datetime.combine(
            current_day, time.min, tzinfo=timezone
        ).astimezone(UTC)
        to_datetime = datetime.combine(
            current_day + timedelta(days=1), time.min, tzinfo=timezone
        ).astimezone(UTC)
    if from_datetime is None or to_datetime is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "from_datetime and to_datetime must be supplied together",
        )
    if from_datetime.tzinfo is None or to_datetime.tzinfo is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Appointment range must include a timezone",
        )
    if from_datetime >= to_datetime or to_datetime - from_datetime > timedelta(days=93):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Invalid appointment range",
        )
    interviewer = exists(
        select(InterviewAppointmentInterviewer.id).where(
            InterviewAppointmentInterviewer.organization_id == user.organization_id,
            InterviewAppointmentInterviewer.appointment_id == InterviewAppointment.id,
            InterviewAppointmentInterviewer.user_id == user.id,
        )
    )
    statement = select(InterviewAppointment).where(
        InterviewAppointment.organization_id == user.organization_id,
        InterviewAppointment.starts_at >= from_datetime,
        InterviewAppointment.starts_at < to_datetime,
        or_(InterviewAppointment.organizer_user_id == user.id, interviewer),
    )
    if not include_cancelled:
        statement = statement.where(InterviewAppointment.status != "cancelled")
    namespace = "scheduling.my-appointments"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={
            "user_id": str(user.id),
            "from": from_datetime.astimezone(UTC).isoformat(),
            "to": to_datetime.astimezone(UTC).isoformat(),
            "include_cancelled": include_cancelled,
        },
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"starts_at", "id"},
            detail="Invalid appointments cursor",
        )
        cursor_time = cursor_datetime(
            payload, "starts_at", detail="Invalid appointments cursor"
        )
        cursor_id = cursor_uuid(payload, detail="Invalid appointments cursor")
        statement = statement.where(
            or_(
                InterviewAppointment.starts_at > cursor_time,
                and_(
                    InterviewAppointment.starts_at == cursor_time,
                    InterviewAppointment.id > cursor_id,
                ),
            )
        )
    appointments = list(
        (
            await db.execute(
                statement.order_by(
                    InterviewAppointment.starts_at, InterviewAppointment.id
                ).limit(limit + 1)
            )
        ).scalars()
    )
    has_more = len(appointments) > limit
    appointments = appointments[:limit]
    return CursorPage(
        items=[await appointment_out(db, item) for item in appointments],
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "starts_at": appointments[-1].starts_at.astimezone(UTC).isoformat(),
                    "id": str(appointments[-1].id),
                },
            )
            if has_more and appointments
            else None
        ),
    )


def _http_error(exc: SchedulingError, *, public: bool = False) -> HTTPException:
    if exc.code in {
        "candidate_not_found",
        "position_not_found",
        "interview_not_found",
        "interviewer_not_found",
        "organizer_not_found",
        "link_not_found",
        "appointment_not_found",
    }:
        code = status.HTTP_404_NOT_FOUND
    elif exc.code in {"link_expired", "link_revoked"}:
        code = status.HTTP_410_GONE
    elif exc.code in {
        "link_booked",
        "slot_unavailable",
        "idempotency_conflict",
        "version_conflict",
        "appointment_not_scheduled",
        "reschedule_unavailable",
    }:
        code = status.HTTP_409_CONFLICT
    else:
        code = status.HTTP_422_UNPROCESSABLE_CONTENT
    return HTTPException(
        code,
        {"code": exc.code, "message": str(exc)},
        headers=_PUBLIC_CAPABILITY_HEADERS if public else None,
    )


async def _candidate_or_404(db: AsyncSession, candidate_id: UUID, user: User):
    candidate = await resolve_canonical_candidate(
        db, candidate_id, user.organization_id
    )
    if candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    return candidate


@router.get(
    "/calendar/providers",
    response_model=CursorPage[CalendarProviderOut],
)
async def list_calendar_providers(
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[CalendarProviderOut]:
    connections = list(
        (
            await db.execute(
                select(IntegrationConnection)
                .where(
                    IntegrationConnection.organization_id == user.organization_id,
                    IntegrationConnection.provider.in_(
                        ("google_workspace", "microsoft_365")
                    ),
                    IntegrationConnection.revoked_at.is_(None),
                )
                .order_by(
                    IntegrationConnection.updated_at,
                    IntegrationConnection.id,
                )
            )
        ).scalars()
    )
    return CursorPage(
        items=[
            CalendarProviderOut(
                provider=item.provider,
                connection_id=item.connection_id,
                name=item.name,
                status=item.status,
                capability=item.capability,
                available=item.available,
                reason=item.reason,
            )
            for item in provider_statuses(connections)
        ],
        next_cursor=None,
    )


@router.get(
    "/calendar/availability/me",
    response_model=AvailabilityOut,
    dependencies=[Depends(current_user)],
)
async def get_my_availability(
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> AvailabilityOut:
    return await get_user_availability(
        db,
        organization_id=user.organization_id,
        user_id=user.id,
    )


@router.put(
    "/calendar/availability/me",
    response_model=AvailabilityOut,
    dependencies=[Depends(current_user)],
)
async def update_my_availability(
    body: AvailabilityUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> AvailabilityOut:
    try:
        result = await replace_user_availability(
            db,
            organization_id=user.organization_id,
            user_id=user.id,
            timezone=body.timezone,
            intervals=body.intervals,
        )
    except SchedulingError as exc:
        raise _http_error(exc) from exc
    await db.commit()
    return result


@router.get(
    "/calendar/availability/blocks",
    response_model=CursorPage[AvailabilityBlockOut],
    dependencies=[Depends(current_user)],
)
async def list_my_availability_blocks(
    starts_before: datetime | None = None,
    ends_after: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[
        str | None, Query(min_length=32, max_length=2_048)
    ] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[AvailabilityBlockOut]:
    if any(
        value is not None and (value.tzinfo is None or value.utcoffset() is None)
        for value in (starts_before, ends_after)
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Block range timestamps must include a timezone",
        )
    stmt = select(AvailabilityBlock).where(
        AvailabilityBlock.organization_id == user.organization_id,
        AvailabilityBlock.user_id == user.id,
    )
    if starts_before is not None:
        stmt = stmt.where(AvailabilityBlock.starts_at < starts_before)
    if ends_after is not None:
        stmt = stmt.where(AvailabilityBlock.ends_at > ends_after)
    namespace = "scheduling.availability-blocks"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={
            "user_id": str(user.id),
            "starts_before": starts_before.isoformat() if starts_before else None,
            "ends_after": ends_after.isoformat() if ends_after else None,
        },
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"starts_at", "id"},
            detail="Invalid availability blocks cursor",
        )
        cursor_time = cursor_datetime(
            payload, "starts_at", detail="Invalid availability blocks cursor"
        )
        cursor_id = cursor_uuid(payload, detail="Invalid availability blocks cursor")
        stmt = stmt.where(
            or_(
                AvailabilityBlock.starts_at > cursor_time,
                and_(
                    AvailabilityBlock.starts_at == cursor_time,
                    AvailabilityBlock.id > cursor_id,
                ),
            )
        )
    blocks = list(
        (
            await db.execute(
                stmt.order_by(AvailabilityBlock.starts_at, AvailabilityBlock.id).limit(
                    limit + 1
                )
            )
        ).scalars()
    )
    has_more = len(blocks) > limit
    blocks = blocks[:limit]
    return CursorPage(
        items=[AvailabilityBlockOut.model_validate(block) for block in blocks],
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "starts_at": blocks[-1].starts_at.astimezone(UTC).isoformat(),
                    "id": str(blocks[-1].id),
                },
            )
            if has_more and blocks
            else None
        ),
    )


@router.post(
    "/calendar/availability/blocks",
    response_model=AvailabilityBlockOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(current_user)],
)
async def create_my_availability_block(
    body: AvailabilityBlockCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> AvailabilityBlockOut:
    try:
        block = await create_availability_block(
            db,
            organization_id=user.organization_id,
            user_id=user.id,
            actor_user_id=user.id,
            starts_at=body.starts_at,
            ends_at=body.ends_at,
        )
    except SchedulingError as exc:
        raise _http_error(exc) from exc
    await db.commit()
    return AvailabilityBlockOut.model_validate(block)


@router.delete(
    "/calendar/availability/blocks/{block_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(current_user)],
)
async def delete_my_availability_block(
    block_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> None:
    removed = await delete_availability_block(
        db,
        organization_id=user.organization_id,
        user_id=user.id,
        block_id=block_id,
    )
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Availability block not found")
    await db.commit()


@router.post(
    "/candidates/{candidate_id}/scheduling-links",
    response_model=SchedulingLinkIssueOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(current_user)],
)
async def create_candidate_scheduling_link(
    candidate_id: UUID,
    body: SchedulingLinkCreate,
    response: Response,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> SchedulingLinkIssueOut:
    if user.role == "interviewer" and body.interview_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found")
    _protect_public_response(response)
    try:
        issue = await create_scheduling_link(
            db,
            organization_id=user.organization_id,
            candidate_id=candidate_id,
            actor_user_id=user.id,
            body=body,
        )
        payload = SchedulingLinkIssueOut(
            **(
                await scheduling_link_out(
                    db,
                    issue.link,
                    public_url=issue.public_url,
                )
            ).model_dump()
        )
        await db.commit()
        return payload
    except SchedulingError as exc:
        await db.rollback()
        raise _http_error(exc) from exc


@router.get(
    "/candidates/{candidate_id}/scheduling-links",
    response_model=CursorPage[SchedulingLinkOut],
    dependencies=[Depends(current_user)],
)
async def list_candidate_scheduling_links(
    candidate_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[
        str | None, Query(min_length=32, max_length=2_048)
    ] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[SchedulingLinkOut]:
    candidate = await _candidate_or_404(db, candidate_id, user)
    namespace = "scheduling.candidate-links"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"candidate_id": str(candidate.id)},
    )
    stmt = select(SchedulingLink).where(
        SchedulingLink.organization_id == user.organization_id,
        SchedulingLink.candidate_id == candidate.id,
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"created_at", "id"},
            detail="Invalid scheduling links cursor",
        )
        cursor_time = cursor_datetime(
            payload, "created_at", detail="Invalid scheduling links cursor"
        )
        cursor_id = cursor_uuid(payload, detail="Invalid scheduling links cursor")
        stmt = stmt.where(
            or_(
                SchedulingLink.created_at < cursor_time,
                and_(SchedulingLink.created_at == cursor_time, SchedulingLink.id < cursor_id),
            )
        )
    links = list(
        (
            await db.execute(
                stmt.order_by(SchedulingLink.created_at.desc(), SchedulingLink.id.desc()).limit(
                    limit + 1
                )
            )
        ).scalars()
    )
    has_more = len(links) > limit
    links = links[:limit]
    return CursorPage(
        items=[await scheduling_link_out(db, link) for link in links],
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "created_at": links[-1].created_at.astimezone(UTC).isoformat(),
                    "id": str(links[-1].id),
                },
            )
            if has_more and links
            else None
        ),
    )


@router.post(
    "/scheduling-links/{link_id}/rotate",
    response_model=SchedulingLinkIssueOut,
    dependencies=[Depends(current_user)],
)
async def rotate_link(
    link_id: UUID,
    response: Response,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> SchedulingLinkIssueOut:
    _protect_public_response(response)
    try:
        issue = await rotate_scheduling_link(
            db,
            organization_id=user.organization_id,
            link_id=link_id,
            actor_user_id=user.id,
        )
        if issue is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "Scheduling link not found",
            )
        payload = SchedulingLinkIssueOut(
            **(
                await scheduling_link_out(
                    db,
                    issue.link,
                    public_url=issue.public_url,
                )
            ).model_dump()
        )
        await db.commit()
        return payload
    except SchedulingError as exc:
        await db.rollback()
        raise _http_error(exc) from exc


@router.delete(
    "/scheduling-links/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(current_user)],
)
async def revoke_link(
    link_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> None:
    link = await revoke_scheduling_link(
        db,
        organization_id=user.organization_id,
        link_id=link_id,
        actor_user_id=user.id,
    )
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scheduling link not found")
    await db.commit()


@router.get(
    "/candidates/{candidate_id}/appointments",
    response_model=CursorPage[AppointmentOut],
    dependencies=[Depends(current_user)],
)
async def list_candidate_appointments(
    candidate_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[
        str | None, Query(min_length=32, max_length=2_048)
    ] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[AppointmentOut]:
    candidate = await _candidate_or_404(db, candidate_id, user)
    namespace = "scheduling.candidate-appointments"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"candidate_id": str(candidate.id)},
    )
    stmt = select(InterviewAppointment).where(
        InterviewAppointment.organization_id == user.organization_id,
        InterviewAppointment.candidate_id == candidate.id,
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"starts_at", "id"},
            detail="Invalid candidate appointments cursor",
        )
        cursor_time = cursor_datetime(
            payload, "starts_at", detail="Invalid candidate appointments cursor"
        )
        cursor_id = cursor_uuid(payload, detail="Invalid candidate appointments cursor")
        stmt = stmt.where(
            or_(
                InterviewAppointment.starts_at < cursor_time,
                and_(
                    InterviewAppointment.starts_at == cursor_time,
                    InterviewAppointment.id < cursor_id,
                ),
            )
        )
    appointments = list(
        (
            await db.execute(
                stmt.order_by(
                    InterviewAppointment.starts_at.desc(),
                    InterviewAppointment.id.desc(),
                ).limit(limit + 1)
            )
        ).scalars()
    )
    has_more = len(appointments) > limit
    appointments = appointments[:limit]
    return CursorPage(
        items=[await appointment_out(db, item) for item in appointments],
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "starts_at": appointments[-1].starts_at.astimezone(UTC).isoformat(),
                    "id": str(appointments[-1].id),
                },
            )
            if has_more and appointments
            else None
        ),
    )


@router.post(
    "/calendar/appointments/{appointment_id}/manage-token/rotate",
    response_model=AppointmentManageTokenOut,
    dependencies=[Depends(current_user)],
)
async def rotate_candidate_appointment_manage_token(
    appointment_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> AppointmentManageTokenOut:
    try:
        appointment, manage_url = await rotate_appointment_manage_token(
            db,
            organization_id=user.organization_id,
            appointment_id=appointment_id,
            actor_user_id=user.id,
        )
        await db.commit()
    except SchedulingError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    return AppointmentManageTokenOut(
        manage_url=manage_url,
        expires_at=appointment.manage_token_expires_at,
    )


@router.delete(
    "/calendar/appointments/{appointment_id}/manage-token",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(current_user)],
)
async def revoke_candidate_appointment_manage_token(
    appointment_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> None:
    try:
        await revoke_appointment_manage_token(
            db,
            organization_id=user.organization_id,
            appointment_id=appointment_id,
            actor_user_id=user.id,
        )
        await db.commit()
    except SchedulingError as exc:
        await db.rollback()
        raise _http_error(exc) from exc


@router.get(
    "/public/scheduling/{token}",
    response_model=PublicSchedulingDetails,
)
async def get_public_scheduling_link(
    token: str,
    response: Response = None,
    db: AsyncSession = Depends(get_session),
) -> PublicSchedulingDetails:
    _protect_public_response(response)
    try:
        link = await get_link_by_token(db, token)
        return await public_link_details(db, link)
    except SchedulingError as exc:
        raise _http_error(exc, public=True) from exc


@router.get(
    "/public/scheduling/{token}/slots",
    response_model=SchedulingSlotPage,
)
async def list_public_scheduling_slots(
    token: str,
    response: Response = None,
    timezone: str = Query(default=settings.business_timezone),
    from_date: date | None = None,
    days: int = Query(default=14, ge=1, le=31),
    limit: int = Query(default=100, ge=1, le=100),
    cursor: str | None = None,
    db: AsyncSession = Depends(get_session),
) -> SchedulingSlotPage:
    _protect_public_response(response)
    try:
        zone = timezone_for(timezone)
        first_date = from_date or datetime.now(zone).date()
        link = await get_link_by_token(db, token)
        items = await available_slots(
            db,
            link=link,
            timezone=timezone,
            from_date=first_date,
            days=days,
        )
        namespace = "public-scheduling.slots"
        scope = collection_cursor_scope(
            namespace,
            organization_id=link.organization_id,
            filters={
                "link_id": str(link.id),
                "timezone": timezone,
                "from_date": first_date.isoformat(),
                "days": days,
            },
        )
        items, next_cursor = _paginate_slots(
            items,
            namespace=namespace,
            scope=scope,
            limit=limit,
            cursor=cursor,
        )
        return SchedulingSlotPage(
            timezone=timezone,
            from_date=first_date,
            through_date=first_date.fromordinal(first_date.toordinal() + days - 1),
            items=items,
            next_cursor=next_cursor,
        )
    except SchedulingError as exc:
        raise _http_error(exc, public=True) from exc


@router.post(
    "/public/scheduling/{token}/book",
    response_model=PublicBookingOut,
    status_code=status.HTTP_201_CREATED,
)
async def book_public_scheduling_slot(
    token: str,
    body: PublicBookingCreate,
    response: Response = None,
    db: AsyncSession = Depends(get_session),
) -> PublicBookingOut:
    _protect_public_response(response)
    try:
        appointment, manage_url = await book_appointment(
            db,
            token=token,
            starts_at=body.starts_at,
            timezone=body.timezone,
            idempotency_key=body.idempotency_key,
        )
        await db.commit()
        return PublicBookingOut(
            appointment=await public_appointment_out(db, appointment),
            manage_url=manage_url,
        )
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "code": "booking_conflict",
                "message": "Booking was already created or the slot became unavailable",
            },
            headers=_PUBLIC_CAPABILITY_HEADERS,
        ) from exc
    except SchedulingError as exc:
        await db.rollback()
        raise _http_error(exc, public=True) from exc


@router.get(
    "/public/scheduling/manage/{manage_token}",
    response_model=PublicAppointmentDetails,
)
async def get_public_appointment(
    manage_token: str,
    response: Response = None,
    db: AsyncSession = Depends(get_session),
) -> PublicAppointmentDetails:
    _protect_public_response(response)
    try:
        appointment = await get_appointment_by_manage_token(db, manage_token)
        return await public_appointment_details(db, appointment)
    except SchedulingError as exc:
        raise _http_error(exc, public=True) from exc


@router.get(
    "/public/scheduling/manage/{manage_token}/slots",
    response_model=SchedulingSlotPage,
)
async def list_public_reschedule_slots(
    manage_token: str,
    response: Response = None,
    timezone: str | None = None,
    from_date: date | None = None,
    days: int = Query(default=14, ge=1, le=31),
    limit: int = Query(default=100, ge=1, le=100),
    cursor: str | None = None,
    db: AsyncSession = Depends(get_session),
) -> SchedulingSlotPage:
    _protect_public_response(response)
    try:
        appointment = await get_appointment_by_manage_token(db, manage_token)
        if appointment.status != "scheduled":
            raise SchedulingError(
                "appointment_not_scheduled",
                "Only a scheduled appointment can be rescheduled",
            )
        if appointment.scheduling_link_id is None:
            raise SchedulingError(
                "reschedule_unavailable",
                "This appointment cannot be self-rescheduled",
            )
        link = (
            await db.execute(
                select(SchedulingLink).where(
                    SchedulingLink.id == appointment.scheduling_link_id,
                    SchedulingLink.organization_id == appointment.organization_id,
                )
            )
        ).scalar_one_or_none()
        if link is None:
            raise SchedulingError(
                "reschedule_unavailable",
                "This appointment cannot be self-rescheduled",
            )
        zone_name = timezone or appointment.candidate_timezone
        zone = timezone_for(zone_name)
        first_date = from_date or datetime.now(zone).date()
        items = await available_slots(
            db,
            link=link,
            timezone=zone_name,
            from_date=first_date,
            days=days,
            exclude_appointment_id=appointment.id,
        )
        namespace = "public-scheduling.reschedule-slots"
        scope = collection_cursor_scope(
            namespace,
            organization_id=appointment.organization_id,
            filters={
                "appointment_id": str(appointment.id),
                "link_id": str(link.id),
                "timezone": zone_name,
                "from_date": first_date.isoformat(),
                "days": days,
            },
        )
        items, next_cursor = _paginate_slots(
            items,
            namespace=namespace,
            scope=scope,
            limit=limit,
            cursor=cursor,
        )
        return SchedulingSlotPage(
            timezone=zone_name,
            from_date=first_date,
            through_date=first_date.fromordinal(first_date.toordinal() + days - 1),
            items=items,
            next_cursor=next_cursor,
        )
    except SchedulingError as exc:
        raise _http_error(exc, public=True) from exc


@router.post(
    "/public/scheduling/manage/{manage_token}/reschedule",
    response_model=PublicAppointmentDetails,
)
async def reschedule_public_appointment(
    manage_token: str,
    body: PublicRescheduleRequest,
    response: Response = None,
    db: AsyncSession = Depends(get_session),
) -> PublicAppointmentDetails:
    _protect_public_response(response)
    try:
        appointment = await reschedule_appointment(
            db,
            manage_token=manage_token,
            starts_at=body.starts_at,
            timezone=body.timezone,
            expected_version=body.expected_version,
        )
        await db.commit()
        return await public_appointment_details(db, appointment)
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "code": "reschedule_conflict",
                "message": "The slot became unavailable",
            },
            headers=_PUBLIC_CAPABILITY_HEADERS,
        ) from exc
    except SchedulingError as exc:
        await db.rollback()
        raise _http_error(exc, public=True) from exc


@router.post(
    "/public/scheduling/manage/{manage_token}/cancel",
    response_model=PublicAppointmentDetails,
)
async def cancel_public_appointment(
    manage_token: str,
    body: PublicCancelRequest,
    response: Response = None,
    db: AsyncSession = Depends(get_session),
) -> PublicAppointmentDetails:
    _protect_public_response(response)
    try:
        appointment = await cancel_appointment(
            db,
            manage_token=manage_token,
            expected_version=body.expected_version,
        )
        await db.commit()
        return await public_appointment_details(db, appointment)
    except SchedulingError as exc:
        await db.rollback()
        raise _http_error(exc, public=True) from exc
