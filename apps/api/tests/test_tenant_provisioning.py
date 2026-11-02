from __future__ import annotations

import asyncio
import stat
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.db import session_scope
from app.models.enterprise import AuditLog, EnterprisePolicy
from app.models.operations import OrganizationEntitlement
from app.models.organization import Organization
from app.models.user import User, UserAccessToken
from app.scripts import seed_admin
from app.scripts.provision_tenant import (
    ProvisioningError,
    TenantContract,
    _write_secret_once,
    provision_tenant,
)
from app.services.entitlements import ENTITLEMENT_METRICS


def _contract(*, slug: str = "pilot-alpha") -> TenantContract:
    return TenantContract.model_validate(
        {
            "organization": {
                "name": "Pilot Alpha",
                "slug": slug,
                "candidate_retention_days": 365,
            },
            "initial_admin": {
                "email": "Admin@Pilot-Alpha.Example",
                "locale": "ru",
            },
            "entitlements": {
                metric: {
                    "limit": 25 if metric == "seats" else None,
                    "period": "contract",
                }
                for metric in ENTITLEMENT_METRICS
            },
        }
    )


@pytest.mark.asyncio
async def test_operator_provisioning_is_complete_audited_and_invitation_only(db):
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    result = await provision_tenant(db, _contract(), now=now)
    await db.commit()

    organization = (
        await db.execute(
            select(Organization).where(Organization.id == result.organization_id)
        )
    ).scalar_one()
    admin = (
        await db.execute(select(User).where(User.id == result.admin_user_id))
    ).scalar_one()
    policy = (
        await db.execute(
            select(EnterprisePolicy).where(
                EnterprisePolicy.organization_id == organization.id
            )
        )
    ).scalar_one()
    entitlements = list(
        (
            await db.execute(
                select(OrganizationEntitlement).where(
                    OrganizationEntitlement.organization_id == organization.id
                )
            )
        ).scalars()
    )
    invitation = (
        await db.execute(
            select(UserAccessToken).where(
                UserAccessToken.organization_id == organization.id
            )
        )
    ).scalar_one()
    audit = (
        await db.execute(
            select(AuditLog).where(AuditLog.organization_id == organization.id)
        )
    ).scalar_one()

    assert organization.slug == "pilot-alpha"
    assert admin.email == "admin@pilot-alpha.example"
    assert admin.role == "admin"
    assert admin.locale == "ru"
    assert admin.is_active is False
    assert policy.candidate_retention_days == 365
    assert {row.metric for row in entitlements} == set(ENTITLEMENT_METRICS)
    assert next(row for row in entitlements if row.metric == "seats").limit_value == 25
    assert invitation.purpose == "invitation"
    raw_token = result.invitation_url.rsplit("/", 1)[-1]
    assert raw_token.startswith(invitation.token_prefix)
    assert raw_token != invitation.token_digest
    assert result.invitation_token_prefix == invitation.token_prefix
    assert audit.action == "operator.tenant_provision"
    assert audit.metadata_json["invitation_token_prefix"] == invitation.token_prefix
    assert raw_token not in str(audit.metadata_json)


@pytest.mark.asyncio
async def test_operator_provisioning_rejects_duplicate_contract_without_mutation(db):
    contract = _contract()
    await provision_tenant(db, contract)
    await db.commit()

    with pytest.raises(ProvisioningError, match="already provisioned"):
        await provision_tenant(db, contract)
    await db.rollback()

    assert (
        await db.execute(select(func.count()).select_from(Organization))
    ).scalar_one() == 1
    assert (await db.execute(select(func.count()).select_from(User))).scalar_one() == 1


@pytest.mark.asyncio
async def test_concurrent_cross_slug_provisioning_serializes_global_admin_email(db):
    await db.rollback()

    async def attempt(contract: TenantContract):
        try:
            async with session_scope() as session:
                return await provision_tenant(session, contract)
        except ProvisioningError as exc:
            return exc

    results = await asyncio.gather(
        attempt(_contract(slug="pilot-concurrent-a")),
        attempt(_contract(slug="pilot-concurrent-b")),
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, ProvisioningError) for result in results) == 1
    assert (
        await db.execute(select(func.count()).select_from(Organization))
    ).scalar_one() == 1
    assert (await db.execute(select(func.count()).select_from(User))).scalar_one() == 1


def test_contract_requires_every_entitlement_metric():
    payload = _contract().model_dump(mode="json")
    payload["entitlements"].pop("seats")
    with pytest.raises(ValueError, match="missing: seats"):
        TenantContract.model_validate(payload)


def test_contract_rejects_negative_entitlement_limit():
    payload = _contract().model_dump(mode="json")
    payload["entitlements"]["seats"]["limit"] = -1
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        TenantContract.model_validate(payload)


def test_invitation_secret_output_is_mode_0600_and_never_overwritten(tmp_path):
    target = tmp_path / "pilot-invitation.txt"
    written = _write_secret_once(target.resolve(), "https://example.test/invite/secret")

    assert written.read_text(encoding="utf-8").strip().endswith("/secret")
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    with pytest.raises(ProvisioningError, match="already exists"):
        _write_secret_once(target.resolve(), "replacement")
    assert written.read_text(encoding="utf-8").strip().endswith("/secret")


def test_incomplete_invitation_secret_write_removes_only_its_own_file(
    tmp_path, monkeypatch
):
    target = (tmp_path / "interrupted-invitation.txt").resolve()

    def fail_chmod(_path, _mode):
        raise OSError("simulated metadata failure")

    monkeypatch.setattr("app.scripts.provision_tenant.os.chmod", fail_chmod)
    with pytest.raises(OSError, match="simulated metadata failure"):
        _write_secret_once(target, "https://example.test/invite/secret")
    assert not target.exists()


@pytest.mark.asyncio
async def test_legacy_active_admin_seed_rejects_implicit_defaults(monkeypatch):
    # Exercise the only environment in which this legacy helper is available;
    # release suites themselves run with APP_ENVIRONMENT=test.
    monkeypatch.setenv("APP_ENVIRONMENT", "development")
    for name in (
        "SEED_ADMIN_CONFIRM",
        "SEED_ADMIN_EMAIL",
        "SEED_ADMIN_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)

    seed_admin.get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="confirmation is required"):
            await seed_admin.main()
    finally:
        seed_admin.get_settings.cache_clear()
