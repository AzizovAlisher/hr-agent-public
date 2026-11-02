"""PostgreSQL coverage for enterprise governance and recovery controls."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.auth.jwt import encode_access_token
from app.config import get_settings
from app.llm.gateway import BudgetedLLMClient, LLMConfigurationError
from app.main import app
from app.models.candidate import Candidate
from app.models.enterprise import AuditLog, EnterprisePolicy
from app.models.operations import OrganizationEntitlement
from app.models.organization import Organization
from app.models.position import Position
from app.models.user import User
from app.routers.candidates import delete_candidate
from app.routers.enterprise import (
    create_api_key,
    get_policy,
    list_api_keys,
    list_trash,
    preview_retention,
    put_model_configuration,
    put_permission_override,
    put_sso,
    restore_from_trash,
    revoke_api_key,
    run_retention,
    update_policy,
)
from app.schemas.enterprise import (
    ApiKeyCreate,
    EnterprisePolicyUpdate,
    ModelConfigurationIn,
    PermissionOverrideIn,
    RetentionRunRequest,
    SsoConfigurationIn,
)
from app.services.candidate_merge import resolve_canonical_candidate
from app.services.enterprise import (
    MANAGED_PROCESSING_NOTICE_VERSION,
    authenticate_api_key,
    organization_llm_capability,
    organization_llm_client,
    require_permission,
)

pytestmark = pytest.mark.asyncio


async def seed(db, label: str, role: str = "admin"):
    org = Organization(
        name=f"Enterprise {label}", slug=f"enterprise-{label}-{uuid4().hex[:8]}"
    )
    db.add(org)
    await db.flush()
    user = User(
        organization_id=org.id,
        email=f"enterprise-{label}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role=role,
        locale="en",
    )
    db.add(user)
    await db.commit()
    return org, user


async def test_candidate_soft_delete_is_hidden_and_admin_can_restore(db):
    org, admin = await seed(db, "trash")
    candidate = Candidate(
        organization_id=org.id,
        source="manual",
        source_ref="Recoverable candidate",
        raw_cv="Private CV contents remain recoverable until purge.",
    )
    db.add(candidate)
    await db.commit()
    candidate_id = candidate.id

    await delete_candidate(candidate_id, db=db, user=admin)
    assert await resolve_canonical_candidate(db, candidate_id, org.id) is None
    deleted = (
        await db.execute(
            select(Candidate)
            .where(Candidate.id == candidate_id)
            .execution_options(include_deleted=True)
        )
    ).scalar_one()
    assert deleted.deleted_at is not None
    assert deleted.deleted_by_user_id == admin.id

    trash = await list_trash(resource_type="all", limit=100, db=db, user=admin)
    assert [(item.resource_type, item.id) for item in trash.items] == [
        ("candidate", candidate_id)
    ]
    await restore_from_trash("candidate", candidate_id, db=db, user=admin)
    restored = await resolve_canonical_candidate(db, candidate_id, org.id)
    assert restored is not None and restored.deleted_at is None


async def test_permission_override_and_policy_are_enforced(db):
    org, admin = await seed(db, "permissions")
    _other_org, outsider = await seed(db, "permissions-other", role="recruiter")
    recruiter = User(
        organization_id=org.id,
        email=f"recruiter-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    db.add(recruiter)
    await db.commit()

    await require_permission(db, recruiter, "candidates.export")
    override = await put_permission_override(
        PermissionOverrideIn(
            role="recruiter", permission="candidates.export", allowed=False
        ),
        db=db,
        user=admin,
    )
    assert override.allowed is False
    with pytest.raises(HTTPException) as denied:
        await require_permission(db, recruiter, "candidates.export")
    assert denied.value.status_code == 403
    # Overrides are organization scoped; another tenant retains defaults.
    await require_permission(db, outsider, "candidates.export")

    policy = await update_policy(
        EnterprisePolicyUpdate(
            candidate_export_enabled=False,
            field_visibility={"compensation": ["admin"]},
        ),
        db=db,
        user=admin,
    )
    assert policy.candidate_export_enabled is False
    assert policy.field_visibility == {"compensation": ["admin"]}


async def test_candidate_retention_defaults_and_contract_bounds(db):
    _org, admin = await seed(db, "retention-contract")

    policy = await get_policy(db=db, user=admin)
    assert policy.candidate_retention_days == 365

    with pytest.raises(ValidationError):
        EnterprisePolicyUpdate(candidate_retention_days=731)

    upper_bound = await update_policy(
        EnterprisePolicyUpdate(candidate_retention_days=730),
        db=db,
        user=admin,
    )
    assert upper_bound.candidate_retention_days == 730


async def test_api_key_secret_is_returned_once_scoped_and_revocable(db):
    org, admin = await seed(db, "api-key")
    candidate = Candidate(
        organization_id=org.id,
        source="referral",
        source_ref="API visible metadata",
    )
    db.add(candidate)
    await db.commit()

    created = await create_api_key(
        ApiKeyCreate(name="Reporting", scopes=["candidates:read"]),
        db=db,
        user=admin,
    )
    assert created.secret.startswith("hr_live_")
    listed = await list_api_keys(db=db, user=admin)
    assert listed.items[0].key_prefix == created.secret[:16]
    assert not hasattr(listed.items[0], "secret")

    authenticated = await authenticate_api_key(db, created.secret, "candidates:read")
    assert authenticated.organization_id == org.id
    with pytest.raises(HTTPException) as under_scoped:
        await authenticate_api_key(db, created.secret, "positions:read")
    assert under_scoped.value.status_code == 401

    await revoke_api_key(created.id, db=db, user=admin)
    with pytest.raises(HTTPException) as revoked:
        await authenticate_api_key(db, created.secret, "candidates:read")
    assert revoked.value.status_code == 401


async def test_candidate_api_key_contact_scope_redacts_public_applicant_canaries(db):
    org, admin = await seed(db, "api-contact-projection")
    candidate = Candidate(
        organization_id=org.id,
        source="public",
        source_ref="public · API Contact Canary +992900001122",
        parsed_cv={
            "applicant_name": "API Contact Canary",
            "applicant_email": "canary@example.test",
            "applicant_phone": "+992900001122",
        },
        email_normalized="canary@example.test",
        phone_normalized="+992900001122",
    )
    db.add(candidate)
    await db.commit()

    read_only = await create_api_key(
        ApiKeyCreate(name="Candidate metadata", scopes=["candidates:read"]),
        db=db,
        user=admin,
    )
    contact_reader = await create_api_key(
        ApiKeyCreate(
            name="Candidate contacts",
            scopes=["candidates:read", "candidates:contact"],
        ),
        db=db,
        user=admin,
    )
    with pytest.raises(ValidationError, match="requires candidates:read"):
        ApiKeyCreate(name="Contact only", scopes=["candidates:contact"])

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        redacted = await client.get(
            "/enterprise/api/v1/candidates",
            headers={"X-API-Key": read_only.secret},
        )
        revealed = await client.get(
            "/enterprise/api/v1/candidates",
            headers={"X-API-Key": contact_reader.secret},
        )

    assert redacted.status_code == 200
    redacted_text = redacted.text
    assert "API Contact Canary" not in redacted_text
    assert "+992900001122" not in redacted_text
    assert "canary@example.test" not in redacted_text
    redacted_item = redacted.json()["items"][0]
    assert set(redacted_item) == {"id", "source", "created_at"}

    assert revealed.status_code == 200
    revealed_item = revealed.json()["items"][0]
    assert revealed_item["source_ref"] == ("public · API Contact Canary +992900001122")
    assert revealed_item["name"] == "API Contact Canary"
    assert revealed_item["email"] == "canary@example.test"
    assert revealed_item["phone"] == "+992900001122"

    redacted_audit = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.request_id == UUID(redacted.headers["X-Request-ID"])
            )
        )
    ).scalar_one()
    contact_audit = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.request_id == UUID(revealed.headers["X-Request-ID"])
            )
        )
    ).scalar_one()
    assert "field_classes" not in redacted_audit.metadata_json
    assert contact_audit.metadata_json["field_classes"] == ["candidate_contacts"]
    assert contact_audit.metadata_json["required_scopes"] == [
        "candidates:read",
        "candidates:contact",
    ]
    assert contact_audit.metadata_json["sensitive_read"] is True
    assert "API Contact Canary" not in str(contact_audit.metadata_json)


async def test_retention_preview_and_confirmed_purge(db):
    org, admin = await seed(db, "retention")
    old = datetime.now(UTC) - timedelta(days=120)
    deleted_candidate = Candidate(
        organization_id=org.id,
        source="manual",
        source_ref="expired deleted candidate",
        deleted_at=old,
        deleted_by_user_id=admin.id,
    )
    old_active = Candidate(
        organization_id=org.id,
        source="manual",
        source_ref="old active candidate",
        created_at=datetime.now(UTC) - timedelta(days=400),
    )
    deleted_position = Position(
        organization_id=org.id,
        title="Expired position",
        created_by=admin.id,
        deleted_at=old,
        deleted_by_user_id=admin.id,
    )
    db.add_all([deleted_candidate, old_active, deleted_position])
    db.add(
        EnterprisePolicy(
            organization_id=org.id,
            candidate_retention_days=365,
            deleted_retention_days=30,
            audit_retention_days=90,
        )
    )
    await db.commit()

    preview = await preview_retention(db=db, user=admin)
    assert preview.deleted_candidates == 1
    assert preview.deleted_positions == 1
    assert preview.old_active_candidates == 1
    result = await run_retention(
        RetentionRunRequest(confirmation="PURGE", include_active_candidates=False),
        db=db,
        user=admin,
    )
    assert result.deleted_candidates == 1
    assert (
        await db.execute(
            select(Candidate)
            .where(Candidate.id == deleted_candidate.id)
            .execution_options(include_deleted=True)
        )
    ).scalar_one_or_none() is None
    assert await resolve_canonical_candidate(db, old_active.id, org.id) is not None


async def test_provider_configs_never_accept_secrets_or_enable_unverified_sso(
    db, monkeypatch
):
    monkeypatch.setenv(
        "LLM_PRIVATE_BASE_URL_ALLOWLIST", "http://model.internal:11434/v1"
    )
    get_settings.cache_clear()
    _org, admin = await seed(db, "providers")
    with pytest.raises(HTTPException) as unavailable_sso:
        await put_sso(
            SsoConfigurationIn(
                provider="oidc",
                issuer_url="https://identity.example.test",
                client_id="hr-agent",
                allowed_domains=["example.test"],
                is_enabled=True,
                enforce_for_members=True,
            ),
            db=db,
            user=admin,
        )
    assert unavailable_sso.value.status_code == 409
    assert unavailable_sso.value.detail["code"] == "sso_configuration_unavailable"

    with pytest.raises(HTTPException) as disabled_configuration:
        await put_sso(
            SsoConfigurationIn(
                provider="oidc",
                issuer_url="https://identity.example.test",
                client_id="hr-agent",
                allowed_domains=["EXAMPLE.TEST"],
                is_enabled=False,
            ),
            db=db,
            user=admin,
        )
    assert disabled_configuration.value.status_code == 409
    assert (
        disabled_configuration.value.detail["code"] == "sso_configuration_unavailable"
    )

    config = await put_model_configuration(
        ModelConfigurationIn(
            provider="ollama",
            base_url="http://model.internal:11434/v1",
            model="qwen3",
            data_residency="on_prem",
        ),
        db=db,
        user=admin,
    )
    assert config.status == "ready"
    assert config.data_residency == "on_prem"


async def test_managed_provider_requires_recorded_tenant_acceptance_and_revokes(
    db, monkeypatch
):
    with pytest.raises(ValidationError, match="explicit tenant acceptance"):
        ModelConfigurationIn(provider="openrouter")

    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-managed-key-never-sent")
    get_settings.cache_clear()
    org, admin = await seed(db, "managed-opt-in")

    class FakeManagedClient:
        def __init__(self, *, default_model=None):
            self.default_model = default_model

    monkeypatch.setattr("app.llm.openrouter.OpenRouterClient", FakeManagedClient)
    accepted = await put_model_configuration(
        ModelConfigurationIn(
            provider="openrouter",
            model="managed/model",
            managed_processing_accepted=True,
        ),
        db=db,
        user=admin,
    )
    assert accepted.status == "ready"
    assert accepted.managed_processing_accepted_at is not None
    assert accepted.managed_processing_accepted_by_user_id == admin.id
    assert (
        accepted.managed_processing_notice_version == MANAGED_PROCESSING_NOTICE_VERSION
    )
    assert accepted.managed_processing_revoked_at is None

    routed = await organization_llm_client(db, org.id)
    assert isinstance(routed, BudgetedLLMClient)
    assert routed._managed is True
    assert routed._provider == "openrouter"

    revoked = await put_model_configuration(
        ModelConfigurationIn(provider="inherit"),
        db=db,
        user=admin,
    )
    assert revoked.status == "disabled"
    assert revoked.managed_processing_revoked_at is not None
    with pytest.raises(
        LLMConfigurationError,
        match="Tenant acceptance is required",
    ):
        await organization_llm_client(db, org.id)
    get_settings.cache_clear()


async def test_ai_capability_fails_closed_until_managed_route_is_ready(db, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    get_settings.cache_clear()
    org, admin = await seed(db, "managed-capability")

    unavailable = await organization_llm_capability(db, org.id)
    assert unavailable == {
        "available": False,
        "route": None,
        "reason": "ai_managed_processing_opt_in_required",
    }

    monkeypatch.setenv("OPENROUTER_API_KEY", "managed-capability-test-key")
    get_settings.cache_clear()
    await put_model_configuration(
        ModelConfigurationIn(
            provider="openrouter",
            model="managed/model",
            managed_processing_accepted=True,
        ),
        db=db,
        user=admin,
    )
    assert await organization_llm_capability(db, org.id) == {
        "available": True,
        "route": "managed",
        "reason": None,
    }
    get_settings.cache_clear()


async def test_ai_capability_checks_private_allowlist_without_provider_io(
    db, monkeypatch
):
    base_url = "http://model.internal:11434/v1"
    monkeypatch.setenv("LLM_PROVIDER", "vllm")
    monkeypatch.setenv("LLM_PRIVATE_BASE_URL_ALLOWLIST", base_url)
    get_settings.cache_clear()
    org, admin = await seed(db, "private-capability")
    await put_model_configuration(
        ModelConfigurationIn(
            provider="ollama",
            base_url=base_url,
            model="qwen3",
            data_residency="on_prem",
        ),
        db=db,
        user=admin,
    )

    async def provider_io_must_not_run(*_args, **_kwargs):
        raise AssertionError("capability check performed provider or DNS I/O")

    monkeypatch.setattr(
        "app.services.enterprise.resolve_model_endpoint", provider_io_must_not_run
    )
    assert await organization_llm_capability(db, org.id) == {
        "available": True,
        "route": "private",
        "reason": None,
    }
    get_settings.cache_clear()


async def test_ai_capability_respects_zero_contract_budget(db, monkeypatch):
    base_url = "http://model.internal:11434/v1"
    monkeypatch.setenv("LLM_PROVIDER", "vllm")
    monkeypatch.setenv("LLM_PRIVATE_BASE_URL_ALLOWLIST", base_url)
    get_settings.cache_clear()
    org, admin = await seed(db, "budget-capability")
    await put_model_configuration(
        ModelConfigurationIn(
            provider="ollama",
            base_url=base_url,
            model="qwen3",
            data_residency="on_prem",
        ),
        db=db,
        user=admin,
    )
    db.add(
        OrganizationEntitlement(
            organization_id=org.id,
            metric="ai_tokens",
            limit_value=0,
            period="contract",
        )
    )
    await db.commit()

    assert await organization_llm_capability(db, org.id) == {
        "available": False,
        "route": None,
        "reason": "ai_budget_exceeded",
    }
    get_settings.cache_clear()


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:8000/v1",
        "http://169.254.169.254/latest",
        "http://[::1]:8000/v1",
        "https://10.0.0.1/v1",
        "https://public-model.example/v1",
        "https://user:secret@example.com/v1",
    ],
)
async def test_model_configuration_rejects_ssrf_targets(db, monkeypatch, base_url):
    monkeypatch.setenv("LLM_PRIVATE_BASE_URL_ALLOWLIST", "")
    get_settings.cache_clear()
    _org, admin = await seed(db, f"ssrf-{uuid4().hex[:6]}")
    with pytest.raises(HTTPException) as rejected:
        await put_model_configuration(
            ModelConfigurationIn(
                provider="openai_compatible",
                base_url=base_url,
                model="unsafe",
                data_residency="private_cloud",
            ),
            db=db,
            user=admin,
        )
    assert rejected.value.status_code == 422


async def test_authenticated_mutation_produces_body_free_audit_record(db):
    org, admin = await seed(db, "audit")
    token = encode_access_token(admin.id, admin.role, admin.locale)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            "/organizations/me",
            headers={"Authorization": f"Bearer {token}", "User-Agent": "audit-test"},
            json={"name": "Renamed enterprise tenant"},
        )
    assert response.status_code == 200
    assert response.headers.get("x-request-id")
    audit = (
        await db.execute(select(AuditLog).where(AuditLog.organization_id == org.id))
    ).scalar_one()
    assert audit.action == "update_my_org"
    assert audit.path_template == "/organizations/me"
    assert audit.actor_email_snapshot == admin.email
    assert audit.ip_hash and audit.user_agent_hash
    assert audit.metadata_json == {"query_keys": []}
    serialized = str(audit.metadata_json)
    assert "Renamed enterprise tenant" not in serialized


async def test_mutation_audit_intent_never_persists_raw_capability_on_crash(
    db, monkeypatch
):
    from app.routers import auth as auth_router_module

    org, admin = await seed(db, "audit-capability-crash")
    bearer = encode_access_token(admin.id, admin.role, admin.locale)
    raw_capability = f"private-capability-canary-{uuid4().hex}"

    async def explode_before_route_completion(*_args, **_kwargs):
        raise RuntimeError("simulated route crash")

    monkeypatch.setattr(
        auth_router_module,
        "resolve_user_access_token",
        explode_before_route_completion,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/auth/password-reset/{raw_capability}",
            headers={"Authorization": f"Bearer {bearer}"},
            json={"password": "ReplacementPassword!2026"},
        )

    assert response.status_code == 500
    intent = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.organization_id == org.id,
                AuditLog.status_code == 102,
            )
        )
    ).scalar_one()
    assert intent.path_template == "/auth/password-reset/{token}"
    assert intent.action == "post:/auth/password-reset/{token}"
    assert intent.metadata_json["audit_state"] == "started"
    serialized = " ".join(
        (
            intent.action,
            intent.path_template,
            str(intent.metadata_json),
            str(intent.resource_type),
            str(intent.resource_id),
        )
    )
    assert raw_capability not in serialized


async def test_post_export_receives_central_body_free_sensitive_read_audit(db):
    org, admin = await seed(db, "post-sensitive-export")
    candidate = Candidate(
        organization_id=org.id,
        source="manual",
        source_ref="candidate-export-private-canary",
        raw_cv="candidate-export-cv-canary",
    )
    db.add(candidate)
    await db.commit()
    bearer = encode_access_token(admin.id, admin.role, admin.locale)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/candidate-export",
            headers={"Authorization": f"Bearer {bearer}"},
            json={"candidate_ids": [str(candidate.id)], "include_contacts": False},
        )

    assert response.status_code == 200
    audit = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.organization_id == org.id,
                AuditLog.request_id == UUID(response.headers["X-Request-ID"]),
            )
        )
    ).scalar_one()
    assert audit.action == "export_candidates"
    assert audit.path_template == "/candidate-export"
    assert audit.method == "POST"
    assert audit.status_code == 200
    assert audit.metadata_json == {
        "sensitive_read": True,
        "field_classes": [
            "assessment_results",
            "candidate_contacts",
            "exports",
        ],
        "query_keys": [],
    }
    assert "candidate-export-private-canary" not in str(audit.metadata_json)
    assert "candidate-export-cv-canary" not in str(audit.metadata_json)


async def test_authenticated_mutation_fails_closed_before_write_when_audit_is_down(
    db, monkeypatch
):
    from app.middleware import audit as audit_middleware

    org, admin = await seed(db, "audit-outage")
    organization_id = org.id
    original_name = org.name
    token = encode_access_token(admin.id, admin.role, admin.locale)

    @asynccontextmanager
    async def unavailable_audit_session():
        raise RuntimeError("simulated audit database outage")
        yield  # pragma: no cover

    monkeypatch.setattr(
        audit_middleware,
        "audit_session_scope",
        unavailable_audit_session,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.patch(
            "/organizations/me",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "Must never be persisted"},
        )

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "audit_unavailable"
    db.expire_all()
    stored = await db.get(Organization, organization_id)
    assert stored is not None
    assert stored.name == original_name


async def test_candidate_sensitive_read_is_centrally_audited_without_payload(db):
    org, admin = await seed(db, "candidate-sensitive-read")
    candidate = Candidate(
        organization_id=org.id,
        source="manual",
        source_ref="Sensitive Candidate Name",
        raw_cv="Sensitive CV canary that must never enter audit metadata.",
    )
    db.add(candidate)
    await db.commit()
    token = encode_access_token(admin.id, admin.role, admin.locale)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/candidates/{candidate.id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    audit = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.organization_id == org.id,
                AuditLog.action == "sensitive_read.get_candidate",
            )
        )
    ).scalar_one()
    assert audit.resource_type == "candidate"
    assert audit.resource_id == candidate.id
    assert audit.metadata_json["sensitive_read"] is True
    assert audit.metadata_json["field_classes"] == [
        "candidate_contacts",
        "candidate_cv",
    ]
    assert "Sensitive CV canary" not in str(audit.metadata_json)
    assert "Sensitive Candidate Name" not in str(audit.metadata_json)


async def test_core_data_export_is_honest_private_and_sensitive_read_audited(db):
    org, admin = await seed(db, "core-export")
    db.add(
        Candidate(
            organization_id=org.id,
            source="manual",
            source_ref="Exported candidate",
            raw_cv="Private candidate content included by explicit backup permission.",
        )
    )
    await db.commit()
    token = encode_access_token(admin.id, admin.role, admin.locale)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/enterprise/backup",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["x-hr-export-restorable"] == "false"
    assert "core-data-export" in response.headers["content-disposition"]
    payload = response.json()
    assert payload["format"] == "hr-agent-core-data-export-v1"
    assert payload["purpose"] == "data_portability"
    assert payload["restorable"] is False
    assert "private_object_bytes" in payload["excluded"]
    assert payload["candidates"][0]["source_ref"] == "Exported candidate"

    audit = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.organization_id == org.id,
                AuditLog.action == "export_core_data",
            )
        )
    ).scalar_one()
    assert audit.metadata_json == {
        "sensitive_read": True,
        "export_kind": "core_data_portability",
        "restorable": False,
        "user_count": 1,
        "position_count": 0,
        "candidate_count": 1,
        "legacy_score_count": 0,
    }
