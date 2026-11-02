"""Reproducible, event-derived hiring analytics and aggregate export."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import FieldClass
from app.auth.dependencies import current_user
from app.db import get_session
from app.models.enterprise import AuditLog
from app.models.user import User
from app.schemas.applications import (
    AnalyticsDrilldownDimension,
    ApplicationAnalyticsOut,
)
from app.services.application_analytics import (
    application_analytics,
    application_analytics_csv,
)
from app.services.enterprise import can_access_field, require_field_access

router = APIRouter(
    prefix="/analytics",
    tags=["analytics"],
    dependencies=[Depends(current_user)],
)


@router.get(
    "/pipeline",
    response_model=ApplicationAnalyticsOut,
    responses={
        200: {
            "content": {"application/json": {}, "text/csv": {}},
            "description": "Event-derived report or authorized UTF-8 CSV export",
        }
    },
)
async def pipeline_analytics(
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    timezone: str = Query(default="UTC", min_length=1, max_length=64),
    position_id: UUID | None = None,
    source: str | None = Query(default=None, min_length=1, max_length=32),
    owner_user_id: UUID | None = None,
    owner: str | None = Query(default=None, min_length=1, max_length=36),
    drilldown_dimension: AnalyticsDrilldownDimension | None = None,
    drilldown_key: str | None = Query(default=None, min_length=1, max_length=255),
    drilldown_cursor: str | None = Query(default=None, min_length=1, max_length=512),
    drilldown_limit: int = Query(default=50, ge=1, le=100),
    output_format: Literal["json", "csv"] = Query(default="json", alias="format"),
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ApplicationAnalyticsOut | Response:
    """Use one cohort for summary, funnel, timing, drilldowns, and export.

    CSV is a field-policy protected representation of the same report. A CSV
    request exports the full selected drilldown (or the full cohort when no
    drilldown was selected), not merely the visible cursor page.
    """

    if (start_at is not None and start_date is not None) or (
        end_at is not None and end_date is not None
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Use either timestamp or local-date analytics boundaries",
        )
    if (start_date is None) != (end_date is None):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Both analytics local dates are required",
        )
    if start_date is not None and end_date is not None:
        try:
            local_timezone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Unknown analytics timezone",
            ) from exc
        start_at = datetime.combine(start_date, time.min, local_timezone)
        end_at = datetime.combine(end_date + timedelta(days=1), time.min, local_timezone)

    if owner is not None and owner_user_id is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Use one analytics owner filter",
        )
    owner_unassigned = owner == "unassigned"
    if owner is not None and not owner_unassigned:
        try:
            owner_user_id = UUID(owner)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Invalid analytics owner",
            ) from exc

    export = output_format == "csv"
    if export:
        await require_field_access(
            db,
            user,
            FieldClass.EXPORT,
            required_permission="analytics.export",
        )
    quality_score_authorized = await can_access_field(
        db, user, FieldClass.ASSESSMENT_RESULT
    )
    ai_decisions_authorized = await can_access_field(
        db, user, FieldClass.AI_CONTEXT
    )
    report = await application_analytics(
        db,
        user.organization_id,
        start_at=start_at,
        end_at=end_at,
        timezone_name=timezone,
        position_id=position_id,
        source=source,
        owner_user_id=owner_user_id,
        owner_unassigned=owner_unassigned,
        include_organization_operations=user.role in {"admin", "recruiter"},
        include_quality_scores=quality_score_authorized,
        include_ai_decisions=ai_decisions_authorized,
        drilldown_dimension=(drilldown_dimension or "all") if export else drilldown_dimension,
        drilldown_key=drilldown_key,
        drilldown_cursor=None if export else drilldown_cursor,
        drilldown_limit=100_000 if export else drilldown_limit,
    )
    if not export:
        return report
    if report.drilldown is not None and report.drilldown.next_cursor is not None:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "Analytics export exceeds 100000 applications; narrow the cohort",
        )

    db.add(
        AuditLog(
            organization_id=user.organization_id,
            actor_user_id=user.id,
            actor_email_snapshot=user.email,
            action="export_application_analytics",
            resource_type="application_analytics",
            resource_id=None,
            path_template="/analytics/pipeline",
            method="GET",
            status_code=200,
            request_id=uuid4(),
            metadata_json={
                "sensitive_read": True,
                "start_at": report.start_at.isoformat(),
                "end_at": report.end_at.isoformat(),
                "timezone": report.timezone,
                "position_id": str(position_id) if position_id else None,
                "source": source,
                "owner": "unassigned"
                if owner_unassigned
                else (str(owner_user_id) if owner_user_id else None),
                "drilldown_dimension": report.drilldown.dimension
                if report.drilldown
                else None,
                "drilldown_key": report.drilldown.key if report.drilldown else None,
                "application_count": report.total_applications,
            },
            occurred_at=datetime.now(UTC),
        )
    )
    await db.commit()
    filename = (
        f"hiring-analytics-{report.start_at.date().isoformat()}-"
        f"{report.end_at.date().isoformat()}.csv"
    )
    return Response(
        content=application_analytics_csv(report).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store, max-age=0",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
        },
    )
