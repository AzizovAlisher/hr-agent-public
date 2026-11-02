"""Public, versioned careers API for external company websites.

The API returns a safe projection of positions and accepts candidate
applications without exposing recruiter-only data. Publication is controlled
by the existing position workflow: a position must be open, have public
applications enabled, and not belong to an archived folder. Closing or
unpublishing it removes it from the feed and disables intake immediately.
"""

import hashlib
import html
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import (
    bootstrap_public_organization,
    forced_tenant_rls,
    get_session,
    set_tenant_context,
)
from app.models.folder import Folder
from app.models.organization import Organization
from app.models.position import Position
from app.models.sourcing import ReferralLink
from app.schemas.careers import (
    PublicCareerApplicationAccepted,
    PublicCareerPosition,
    PublicCareerPositionPage,
    PublicCareersOrganization,
)
from app.services.pagination import (
    collection_cursor_scope,
    cursor_datetime,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)
from app.services.public_applications import submit_public_application
from app.services.public_positions import (
    public_position_filters,
    safe_public_requirements,
)
from app.services.uploads import public_logo_url

router = APIRouter(prefix="/public/v1/careers", tags=["public-careers"])
settings = get_settings()

_CACHE_CONTROL = "public, max-age=60, stale-while-revalidate=300"
_WIDGET_PATH = (
    Path(__file__).resolve().parent.parent / "static" / "careers-widget.v1.js"
)


def encode_career_cursor(
    updated_at: datetime, position_id: UUID, *, scope: str
) -> str:
    return encode_collection_cursor(
        "public-careers.positions",
        scope=scope,
        boundary={"updated_at": updated_at.isoformat(), "id": str(position_id)},
    )


def decode_career_cursor(cursor: str, *, scope: str) -> tuple[datetime, UUID]:
    payload = decode_collection_cursor(
        cursor,
        namespace="public-careers.positions",
        scope=scope,
        boundary_keys={"updated_at", "id"},
        detail="Invalid careers cursor",
    )
    return (
        cursor_datetime(payload, "updated_at", detail="Invalid careers cursor"),
        cursor_uuid(payload, detail="Invalid careers cursor"),
    )


def _text_requirement(requirements: dict[str, Any], key: str) -> str | None:
    value = requirements.get(key)
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _organization_out(
    organization: Organization, request: Request
) -> PublicCareersOrganization:
    logo_url = None
    if organization.logo_path:
        # ``logo_path`` is stored as ``logos/<opaque-name>`` for compatibility
        # with public pages. The static route itself is rooted at that dedicated
        # directory so no sibling private storage can be requested.
        logo_url = public_logo_url(organization.logo_path) or str(
            request.url_for(
                "branding-storage",
                path=Path(organization.logo_path).name,
            )
        )
    return PublicCareersOrganization(
        slug=organization.slug,
        name=organization.name,
        logo_url=logo_url,
        accent_color=organization.accent_color,
    )


def _position_out(
    position: Position,
    organization: Organization,
    request: Request,
) -> PublicCareerPosition:
    raw_requirements = (
        position.requirements if isinstance(position.requirements, dict) else {}
    )
    apply_query = urlencode({"utm_source": "careers_api"})
    apply_url = f"{settings.web_origin.rstrip('/')}/apply/{position.id}?{apply_query}"
    api_url = str(
        request.url_for(
            "get_public_career_position",
            organization_slug=organization.slug,
            position_id=position.id,
        )
    )
    return PublicCareerPosition(
        id=position.id,
        title=position.title,
        description=position.description,
        full_description=position.full_description,
        requirements=safe_public_requirements(raw_requirements) or None,
        company=_text_requirement(raw_requirements, "company") or organization.name,
        location=_text_requirement(raw_requirements, "location"),
        work_format=_text_requirement(raw_requirements, "work_format"),
        employment_type=_text_requirement(raw_requirements, "employment_type"),
        salary_range=_text_requirement(raw_requirements, "salary_range"),
        seniority=_text_requirement(raw_requirements, "seniority"),
        apply_url=apply_url,
        api_url=api_url,
        created_at=position.created_at,
        updated_at=position.updated_at,
    )


def _etag_for_page(
    organization: Organization,
    positions: list[Position],
    *,
    total: int,
    query_signature: str,
) -> str:
    material = "|".join(
        [
            str(organization.id),
            organization.updated_at.isoformat(),
            str(total),
            query_signature,
            *(
                f"{position.id}:{position.updated_at.isoformat()}"
                for position in positions
            ),
        ]
    )
    digest = hashlib.sha256(material.encode()).hexdigest()[:32]
    return f'W/"{digest}"'


def _set_cache_headers(
    response: Response, *, etag: str, total: int | None = None
) -> None:
    response.headers["Cache-Control"] = _CACHE_CONTROL
    response.headers["ETag"] = etag
    if total is not None:
        response.headers["X-Total-Count"] = str(total)


async def _organization_by_slug(slug: str, db: AsyncSession) -> Organization:
    if forced_tenant_rls():
        organization_id = await bootstrap_public_organization(db, slug)
        if organization_id is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Careers site not found.")
        await set_tenant_context(
            db,
            organization_id=organization_id,
            actor_id=None,
            identity_kind="public",
        )
    organization = (
        await db.execute(select(Organization).where(Organization.slug == slug))
    ).scalar_one_or_none()
    if organization is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Careers site not found.")
    return organization


async def _published_position_by_id(
    organization_id: UUID,
    position_id: UUID,
    db: AsyncSession,
) -> Position:
    position = (
        await db.execute(
            select(Position)
            .outerjoin(Folder, Folder.id == Position.folder_id)
            .where(
                Position.id == position_id,
                *public_position_filters(organization_id),
            )
        )
    ).scalar_one_or_none()
    if position is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found.")
    return position


@router.get(
    "/widget.js",
    response_class=FileResponse,
    summary="Download the embeddable careers widget",
)
async def get_careers_widget() -> FileResponse:
    """Serve the dependency-free widget used on external company websites."""

    return FileResponse(
        _WIDGET_PATH,
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "public, max-age=3600, stale-while-revalidate=86400"},
    )


@router.get(
    "/{organization_slug}/preview",
    response_class=HTMLResponse,
    summary="Preview the embeddable careers experience",
)
async def preview_public_careers(
    organization_slug: str,
    request: Request,
    locale: str = Query(default="ru", pattern="^(ru|en)$"),
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the exact widget experience in a production-like host page."""

    organization = await _organization_by_slug(organization_slug, db)
    safe_locale = html.escape(locale, quote=True)
    safe_slug = html.escape(organization.slug, quote=True)
    safe_name = html.escape(organization.name, quote=True)
    widget_url = html.escape(
        str(request.url_for("get_careers_widget")),
        quote=True,
    )
    document = f"""<!doctype html>
<html lang="{safe_locale}">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="icon" href="data:,">
    <title>{safe_name} · Careers</title>
    <style>
      * {{ box-sizing: border-box; }}
      body {{ margin: 0; background: #f7f8fa; color: #17181b; }}
      main {{ width: min(1180px, calc(100% - 32px)); margin: 0 auto; padding: 56px 0; }}
      @media (max-width: 640px) {{ main {{ width: 100%; padding: 0; }} }}
    </style>
  </head>
  <body>
    <main>
      <div data-recruiter-careers
        data-organization="{safe_slug}"
        data-locale="{safe_locale}"
        data-source="company_website"></div>
    </main>
    <script async src="{widget_url}"></script>
  </body>
</html>"""
    return HTMLResponse(
        document,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
                "form-action 'none'; frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _apply_optional_filters(
    filters: list[Any],
    *,
    q: str | None,
    company: str | None,
    location: str | None,
    employment_type: str | None,
) -> None:
    if q and (term := q.strip()):
        pattern = f"%{term}%"
        filters.append(
            or_(
                Position.title.ilike(pattern),
                Position.description.ilike(pattern),
                Position.full_description.ilike(pattern),
            )
        )
    for key, value in (
        ("company", company),
        ("location", location),
        ("employment_type", employment_type),
    ):
        if value and (normalized := value.strip().lower()):
            filters.append(func.lower(Position.requirements[key].astext) == normalized)


@router.get(
    "/{organization_slug}/positions",
    response_model=PublicCareerPositionPage,
    responses={304: {"description": "Not modified"}},
)
async def list_public_career_positions(
    organization_slug: str,
    request: Request,
    response: Response,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    q: str | None = Query(default=None, max_length=100),
    company: str | None = Query(default=None, max_length=255),
    location: str | None = Query(default=None, max_length=255),
    employment_type: str | None = Query(default=None, max_length=64),
    db: AsyncSession = Depends(get_session),
) -> PublicCareerPositionPage | Response:
    """List website-published positions, newest changes first."""

    organization = await _organization_by_slug(organization_slug, db)
    filters = public_position_filters(organization.id)
    _apply_optional_filters(
        filters,
        q=q,
        company=company,
        location=location,
        employment_type=employment_type,
    )
    cursor_scope = collection_cursor_scope(
        "public-careers.positions",
        organization_id=organization.id,
        filters={
            "q": q.strip().lower() if q else None,
            "company": company.strip().lower() if company else None,
            "location": location.strip().lower() if location else None,
            "employment_type": employment_type.strip().lower()
            if employment_type
            else None,
        },
    )

    total = (
        await db.execute(
            select(func.count(Position.id))
            .select_from(Position)
            .outerjoin(Folder, Folder.id == Position.folder_id)
            .where(*filters)
        )
    ).scalar_one()

    page_filters = list(filters)
    if cursor:
        cursor_time, cursor_id = decode_career_cursor(cursor, scope=cursor_scope)
        page_filters.append(
            or_(
                Position.updated_at < cursor_time,
                and_(Position.updated_at == cursor_time, Position.id < cursor_id),
            )
        )

    positions = list(
        (
            await db.execute(
                select(Position)
                .outerjoin(Folder, Folder.id == Position.folder_id)
                .where(*page_filters)
                .order_by(Position.updated_at.desc(), Position.id.desc())
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    has_more = len(positions) > limit
    page_positions = positions[:limit]
    next_cursor = None
    if has_more and page_positions:
        last = page_positions[-1]
        next_cursor = encode_career_cursor(
            last.updated_at, last.id, scope=cursor_scope
        )

    query_signature = json.dumps(
        {
            "limit": limit,
            "cursor": cursor,
            "q": q,
            "company": company,
            "location": location,
            "employment_type": employment_type,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    etag = _etag_for_page(
        organization,
        page_positions,
        total=total,
        query_signature=query_signature,
    )
    if request.headers.get("if-none-match") == etag:
        not_modified = Response(status_code=status.HTTP_304_NOT_MODIFIED)
        _set_cache_headers(not_modified, etag=etag, total=total)
        return not_modified

    _set_cache_headers(response, etag=etag, total=total)
    return PublicCareerPositionPage(
        organization=_organization_out(organization, request),
        items=[
            _position_out(position, organization, request)
            for position in page_positions
        ],
        total=total,
        next_cursor=next_cursor,
    )


@router.get(
    "/{organization_slug}/positions/{position_id}",
    response_model=PublicCareerPosition,
)
async def get_public_career_position(
    organization_slug: str,
    position_id: UUID,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> PublicCareerPosition | Response:
    """Return one published position without recruiter-only fields."""

    organization = await _organization_by_slug(organization_slug, db)
    position = await _published_position_by_id(
        organization.id,
        position_id,
        db,
    )

    etag = _etag_for_page(
        organization,
        [position],
        total=1,
        query_signature=str(position.id),
    )
    if request.headers.get("if-none-match") == etag:
        not_modified = Response(status_code=status.HTTP_304_NOT_MODIFIED)
        _set_cache_headers(not_modified, etag=etag)
        return not_modified

    _set_cache_headers(response, etag=etag)
    return _position_out(position, organization, request)


@router.post(
    "/{organization_slug}/positions/{position_id}/apply",
    response_model=PublicCareerApplicationAccepted,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"description": "Missing or invalid resume"},
        404: {"description": "Published position not found"},
        413: {"description": "Candidate document exceeds the 15 MiB pilot limit"},
        415: {"description": "Unsupported resume format"},
    },
)
async def apply_to_public_career_position(
    organization_slug: str,
    position_id: UUID,
    name: str = Form(..., min_length=1, max_length=200),
    phone: str = Form(..., min_length=4, max_length=64),
    email: Annotated[str | None, Form(max_length=320)] = None,
    source: Annotated[str | None, Form(max_length=64)] = "company_website",
    referral_token: Annotated[str | None, Form(max_length=128)] = None,
    file: Annotated[UploadFile | None, File()] = None,
    raw_text: Annotated[str | None, Form(max_length=200_000)] = None,
    privacy_notice_version: Annotated[str | None, Form(max_length=32)] = None,
    privacy_acknowledged: Annotated[bool, Form()] = False,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", max_length=160),
    ] = None,
    db: AsyncSession = Depends(get_session),
) -> PublicCareerApplicationAccepted:
    """Accept a website application through the versioned careers contract."""

    organization = await _organization_by_slug(organization_slug, db)
    position = await _published_position_by_id(
        organization.id,
        position_id,
        db,
    )
    referral_link = None
    if isinstance(referral_token, str) and referral_token:
        now = datetime.now(UTC)
        referral_link = (
            await db.execute(
                select(ReferralLink)
                .where(
                    ReferralLink.token_digest
                    == hashlib.sha256(referral_token.encode()).hexdigest(),
                    ReferralLink.organization_id == organization.id,
                    ReferralLink.position_id == position.id,
                    ReferralLink.is_active.is_(True),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if referral_link is None or (
            referral_link.expires_at is not None and referral_link.expires_at <= now
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Referral link not found")
        source = referral_link.source
    await submit_public_application(
        position=position,
        name=name,
        phone=phone,
        email=email,
        source=source,
        file=file,
        raw_text=raw_text,
        referral_link=referral_link,
        idempotency_key=idempotency_key,
        privacy_notice_version=privacy_notice_version,
        privacy_acknowledged=privacy_acknowledged,
        db=db,
    )
    return PublicCareerApplicationAccepted(
        accepted=True,
        message="Application received. We'll be in touch.",
    )
