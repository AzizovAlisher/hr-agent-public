"""Fail-closed operator provisioning for a private-pilot tenant.

The contract file contains no password or credential material. The first admin
is inactive until they consume a hashed, expiring invitation capability. The
raw invitation URL is written exactly once to an operator-selected mode-0600
file and is never included in the audit log or command output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    ValidationError,
    field_validator,
)
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.passwords import hash_password
from app.db import session_scope
from app.models.enterprise import AuditLog, EnterprisePolicy
from app.models.operations import OrganizationEntitlement
from app.models.organization import Organization
from app.models.user import User
from app.services.entitlements import ENTITLEMENT_METRICS
from app.services.user_access import access_url, create_user_access_token


class ProvisioningError(RuntimeError):
    """Raised before a tenant contract can be safely applied."""


class OrganizationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$",
    )
    candidate_retention_days: int = Field(default=365, ge=30, le=730)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Organization name cannot be blank")
        return cleaned


class InitialAdminSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    locale: Literal["ru", "en"] = "en"


class EntitlementSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int | None = Field(ge=0, le=9_223_372_036_854_775_807)
    period: Literal["contract", "month", "day"]


class TenantContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization: OrganizationSpec
    initial_admin: InitialAdminSpec
    entitlements: dict[str, EntitlementSpec]

    @field_validator("entitlements")
    @classmethod
    def complete_entitlement_contract(
        cls, value: dict[str, EntitlementSpec]
    ) -> dict[str, EntitlementSpec]:
        expected = set(ENTITLEMENT_METRICS)
        actual = set(value)
        if actual != expected:
            missing = ", ".join(sorted(expected - actual)) or "none"
            unknown = ", ".join(sorted(actual - expected)) or "none"
            raise ValueError(
                "Entitlements must explicitly cover every metric "
                f"(missing: {missing}; unknown: {unknown})"
            )
        return value


@dataclass(frozen=True, slots=True)
class ProvisioningResult:
    organization_id: uuid.UUID
    admin_user_id: uuid.UUID
    admin_email: str
    invitation_token_prefix: str
    invitation_expires_at: datetime
    invitation_url: str = field(repr=False)


def load_contract(path: Path) -> TenantContract:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProvisioningError(
            "Unable to read a valid UTF-8 JSON contract"
        ) from exc
    try:
        return TenantContract.model_validate(payload)
    except ValidationError as exc:
        raise ProvisioningError(f"Invalid tenant contract: {exc}") from exc


async def _assert_operator_connection(db: AsyncSession) -> None:
    role = str((await db.execute(text("SELECT current_user"))).scalar_one())
    if role in {"hr_agent_api_login", "hr_agent_worker_login"}:
        raise ProvisioningError(
            "Tenant provisioning requires an operator database connection, "
            "not an application runtime role"
        )


async def _lock_and_assert_available(
    db: AsyncSession, contract: TenantContract
) -> None:
    slug = contract.organization.slug
    email = str(contract.initial_admin.email).lower()
    # Separate locks close the cross-pair race (same email with a different
    # slug, or the inverse). Sorting keeps concurrent provisioning deadlock-free.
    for lock_name in sorted(
        (f"tenant-provision:email:{email}", f"tenant-provision:slug:{slug}")
    ):
        await db.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(lock_name, 0)
                )
            )
        )
    existing_org = (
        await db.execute(select(Organization.id).where(Organization.slug == slug))
    ).scalar_one_or_none()
    existing_user = (
        await db.execute(select(User.id).where(func.lower(User.email) == email))
    ).scalar_one_or_none()
    if existing_org is not None or existing_user is not None:
        raise ProvisioningError(
            "Organization slug or initial-admin email is already provisioned"
        )


async def validate_tenant_available(
    db: AsyncSession, contract: TenantContract
) -> None:
    await _assert_operator_connection(db)
    await _lock_and_assert_available(db, contract)


async def provision_tenant(
    db: AsyncSession,
    contract: TenantContract,
    *,
    now: datetime | None = None,
) -> ProvisioningResult:
    await validate_tenant_available(db, contract)
    current = now or datetime.now(UTC)
    email = str(contract.initial_admin.email).lower()

    organization = Organization(
        name=contract.organization.name,
        slug=contract.organization.slug,
    )
    db.add(organization)
    await db.flush()

    policy = EnterprisePolicy(
        organization_id=organization.id,
        candidate_retention_days=contract.organization.candidate_retention_days,
    )
    db.add(policy)

    admin = User(
        organization_id=organization.id,
        email=email,
        password_hash=hash_password(secrets.token_urlsafe(48)),
        role="admin",
        locale=contract.initial_admin.locale,
        is_active=False,
    )
    db.add(admin)
    await db.flush()

    for metric in sorted(ENTITLEMENT_METRICS):
        entitlement = contract.entitlements[metric]
        db.add(
            OrganizationEntitlement(
                organization_id=organization.id,
                metric=metric,
                limit_value=entitlement.limit,
                period=entitlement.period,
            )
        )

    invitation, raw_token = await create_user_access_token(
        db,
        user=admin,
        purpose="invitation",
        created_by_user_id=None,
        lifetime=timedelta(days=7),
        now=current,
    )
    db.add(
        AuditLog(
            organization_id=organization.id,
            actor_user_id=None,
            actor_email_snapshot=None,
            action="operator.tenant_provision",
            resource_type="organization",
            resource_id=organization.id,
            path_template="operator://tenant-provision",
            method="CLI",
            status_code=201,
            request_id=uuid.uuid4(),
            metadata_json={
                "organization_slug": organization.slug,
                "initial_admin_user_id": str(admin.id),
                "invitation_token_prefix": invitation.token_prefix,
                "candidate_retention_days": policy.candidate_retention_days,
                "entitlement_metrics": sorted(ENTITLEMENT_METRICS),
            },
            occurred_at=current,
        )
    )
    await db.flush()
    return ProvisioningResult(
        organization_id=organization.id,
        admin_user_id=admin.id,
        admin_email=admin.email,
        invitation_token_prefix=invitation.token_prefix,
        invitation_expires_at=invitation.expires_at,
        invitation_url=access_url(purpose="invitation", raw_token=raw_token),
    )


def _write_secret_once(path: Path, value: str) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise ProvisioningError("Invitation output path must be absolute")
    try:
        parent = expanded.parent.resolve(strict=True)
    except OSError as exc:
        raise ProvisioningError("Invitation output directory does not exist") from exc
    target = parent / expanded.name
    if target.exists() or target.is_symlink():
        raise ProvisioningError("Invitation output already exists")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    created = False
    completed = False
    try:
        descriptor = os.open(target, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            descriptor = None
            output.write(value)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(target, 0o600)
        completed = True
    finally:
        if not completed:
            if descriptor is not None:
                os.close(descriptor)
            if created:
                target.unlink(missing_ok=True)
    return target


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Provision one private-pilot tenant from a reviewed contract"
    )
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--confirm-slug", required=True)
    parser.add_argument("--invitation-output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> dict[str, object]:
    contract = load_contract(args.contract)
    if args.confirm_slug != contract.organization.slug:
        raise ProvisioningError("--confirm-slug must exactly match the contract")
    if not args.dry_run and args.invitation_output is None:
        raise ProvisioningError("--invitation-output is required outside dry-run")

    if args.dry_run:
        async with session_scope() as db:
            await validate_tenant_available(db, contract)
        return {
            "status": "dry_run_pass",
            "organization_slug": contract.organization.slug,
            "admin_email": str(contract.initial_admin.email).lower(),
            "entitlement_metrics": sorted(contract.entitlements),
        }

    secret_path: Path | None = None
    committed = False
    try:
        async with session_scope() as db:
            result = await provision_tenant(db, contract)
            secret_path = _write_secret_once(
                args.invitation_output, result.invitation_url
            )
        committed = True
    finally:
        if not committed and secret_path is not None:
            secret_path.unlink(missing_ok=True)
    return {
        "status": "provisioned",
        "organization_id": str(result.organization_id),
        "organization_slug": contract.organization.slug,
        "admin_user_id": str(result.admin_user_id),
        "admin_email": result.admin_email,
        "invitation_token_prefix": result.invitation_token_prefix,
        "invitation_expires_at": result.invitation_expires_at.isoformat(),
        "invitation_output": str(secret_path),
        "entitlement_metrics": sorted(contract.entitlements),
    }


def main() -> None:
    args = _parser().parse_args()
    try:
        result = asyncio.run(_run(args))
    except (ProvisioningError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "detail": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from exc
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
