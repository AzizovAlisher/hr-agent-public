"""Organization (current-org) endpoints — branding (logo + accent color),
and the lightweight self-fetch the web layer uses to seed the settings panel.

Auth: admin-only for mutations; any authed user can GET /me.
"""

import re
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import current_user
from app.db import get_session
from app.models.organization import Organization
from app.models.user import User
from app.services.enterprise import require_permission
from app.services.uploads import (
    cleanup_org_logo_after_db_failure,
    remove_org_logo,
    save_org_logo,
)

router = APIRouter(
    prefix="/organizations",
    tags=["organizations"],
    dependencies=[Depends(current_user)],
)

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


async def _require_admin(db: AsyncSession, user: User) -> None:
    await require_permission(db, user, "enterprise.manage")


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str
    logo_path: str | None
    accent_color: str | None


class OrganizationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    accent_color: str | None = Field(default=None, max_length=16)


async def _load_org(user: User, db: AsyncSession) -> Organization:
    org = (
        await db.execute(
            select(Organization).where(Organization.id == user.organization_id)
        )
    ).scalar_one_or_none()
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found.")
    return org


@router.get("/me", response_model=OrganizationOut)
async def get_my_org(
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> OrganizationOut:
    org = await _load_org(user, db)
    return OrganizationOut.model_validate(org)


@router.patch("/me", response_model=OrganizationOut)
async def update_my_org(
    body: OrganizationUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> OrganizationOut:
    await _require_admin(db, user)
    org = await _load_org(user, db)

    fields = body.model_dump(exclude_unset=True)
    if "accent_color" in fields:
        color = fields["accent_color"]
        if color is not None and color.strip() and not HEX_COLOR.match(color.strip()):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "accent_color must be a 7-char hex (e.g. #2b6cb0) or null.",
            )
        org.accent_color = color.strip() if color else None
    if "name" in fields and fields["name"]:
        org.name = fields["name"].strip()

    await db.commit()
    await db.refresh(org)
    return OrganizationOut.model_validate(org)


@router.post("/me/logo", response_model=OrganizationOut)
async def upload_my_org_logo(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> OrganizationOut:
    await _require_admin(db, user)
    org = await _load_org(user, db)

    organization_id = org.id
    previous = org.logo_path
    path = await save_org_logo(org_id=organization_id, upload=file)
    org.logo_path = path
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        await cleanup_org_logo_after_db_failure(
            org_id=organization_id,
            logo_path=path,
        )
        raise
    if previous and previous != path:
        try:
            await remove_org_logo(org_id=organization_id, logo_path=previous)
        except HTTPException:
            # The newly committed logo remains authoritative. A public-object
            # lifecycle sweep cleans a stale predecessor after provider errors.
            pass
    await db.refresh(org)
    return OrganizationOut.model_validate(org)


@router.delete("/me/logo", response_model=OrganizationOut)
async def delete_my_org_logo(
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> OrganizationOut:
    await _require_admin(db, user)
    org = await _load_org(user, db)
    if org.logo_path:
        organization_id = org.id
        previous = org.logo_path
        org.logo_path = None
        await db.commit()
        try:
            await remove_org_logo(org_id=organization_id, logo_path=previous)
        except HTTPException:
            # The database is authoritative; a public-object lifecycle sweep
            # removes an orphan if the provider was temporarily unavailable.
            pass
        await db.refresh(org)
    return OrganizationOut.model_validate(org)
