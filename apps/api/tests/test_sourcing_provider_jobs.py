"""Regional provider binding, durable publication, and hh.ru intake regressions."""

import json
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException, Response
from sqlalchemy import func, select

from app.auth.jwt import encode_access_token
from app.main import app
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.enterprise import PermissionOverride
from app.models.integration import (
    IntegrationConnection,
    IntegrationCursor,
    IntegrationJob,
)
from app.models.organization import Organization
from app.models.position import Position
from app.models.sourcing import (
    SourceCandidateReceipt,
    SourceIntegration,
    SourcePublication,
)
from app.models.user import User
from app.routers.integrations import (
    ProviderRuntime,
    cancel_integration_job,
    replay_integration_job,
)
from app.routers.sourcing import (
    create_source_integration,
    enqueue_hh_candidate_import,
    list_source_integrations,
    publish_source_publication,
    rotate_source_webhook_secret,
    update_source_integration,
)
from app.schemas.sourcing import (
    SourceHeadHunterImportRequest,
    SourceIntegrationCreate,
    SourceIntegrationUpdate,
    SourcePublicationPreviewRequest,
)
from app.services.integrations.crypto import IntegrationCipher
from app.services.integrations.http import ProviderRequestError
from app.services.integrations.jobs import enqueue_job, load_job_payload
from app.services.integrations.lifecycle import create_connection
from app.services.integrations.providers import VacancyPublishResult
from app.services.integrations.runtime import execute_provider_job

pytestmark = pytest.mark.asyncio
TEST_CIPHER = IntegrationCipher(b"r" * 32, key_id="regional-provider-test")


def _auth(user: User) -> dict[str, str]:
    token = encode_access_token(
        user.id,
        user.role,
        user.locale,
        token_version=user.token_version,
    )
    return {"Authorization": f"Bearer {token}"}


async def _seed(db, suffix: str):
    organization = Organization(
        name=f"Regional provider {suffix}",
        slug=f"regional-provider-{suffix}-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    admin = User(
        organization_id=organization.id,
        email=f"regional-{suffix}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    first = Position(
        organization_id=organization.id,
        title=f"Platform Engineer {suffix}",
        description="INTERNAL-COMPENSATION-AND-CUSTOMER-SECRET",
        public_description="Build reliable public services for regional customers.",
        sensitivity="restricted",
        status="open",
        public_apply_enabled=True,
    )
    second = Position(
        organization_id=organization.id,
        title=f"Backend Engineer {suffix}",
        description="SECOND-INTERNAL-SECRET",
        public_description="Own dependable APIs and data pipelines.",
        sensitivity="restricted",
        status="open",
        public_apply_enabled=True,
    )
    db.add_all([admin, first, second])
    await db.flush()
    first.created_by = admin.id
    second.created_by = admin.id
    await db.commit()
    return organization, admin, first, second


async def _connection(
    db,
    organization: Organization,
    actor: User,
    provider: str,
    name: str,
    configuration: dict | None = None,
) -> IntegrationConnection:
    connection, _secret = await create_connection(
        db,
        organization_id=organization.id,
        actor_user_id=actor.id,
        provider=provider,
        name=name,
        configuration=configuration or {},
        cipher=TEST_CIPHER,
    )
    connection.status = "connected"
    await db.commit()
    return connection


async def _source(
    db,
    actor: User,
    *,
    provider: str,
    name: str,
    connection: IntegrationConnection,
    configuration: dict | None = None,
) -> SourceIntegration:
    created = await create_source_integration(
        SourceIntegrationCreate(
            provider=provider,
            name=name,
            connection_id=connection.id,
            configuration=configuration or {},
        ),
        db=db,
        user=actor,
    )
    stored = await db.get(SourceIntegration, created.integration.id)
    assert stored is not None
    return stored


def _runtime(*, adapters: dict | None = None) -> ProviderRuntime:
    return ProviderRuntime(
        cipher=TEST_CIPHER,
        http=None,  # type: ignore[arg-type] - provider I/O is supplied by fakes.
        adapters=adapters or {},
        redirect_origins=set(),
        outbound_hosts=set(),
    )


async def _run_job(db, job: IntegrationJob, connection: IntegrationConnection, adapter):
    job.status = "running"
    job.attempt_count += 1
    await db.flush()
    await execute_provider_job(
        db,
        job=job,
        cipher=TEST_CIPHER,
        adapters={connection.provider: adapter},
        prepared_credentials={"access_token": "test-only"},
    )
    await db.commit()
    await db.refresh(job)


async def test_source_binding_is_tenant_provider_and_connection_state_authoritative(db):
    organization, admin, _position, _second = await _seed(db, "binding")
    other_organization, other_admin, _other_position, _ = await _seed(
        db, "binding-other"
    )
    telegram = await _connection(
        db, organization, admin, "telegram", "Wrong provider", {"chat_id": "@jobs"}
    )
    other_hh = await _connection(
        db, other_organization, other_admin, "hh_ru", "Other tenant hh.ru"
    )

    with pytest.raises(HTTPException) as mismatch:
        await create_source_integration(
            SourceIntegrationCreate(
                provider="hh", name="Mismatched", connection_id=telegram.id
            ),
            db=db,
            user=admin,
        )
    assert mismatch.value.status_code == 422
    assert mismatch.value.detail["code"] == "connection_provider_mismatch"

    with pytest.raises(HTTPException) as cross_tenant:
        await create_source_integration(
            SourceIntegrationCreate(
                provider="hh", name="Cross tenant", connection_id=other_hh.id
            ),
            db=db,
            user=admin,
        )
    assert cross_tenant.value.status_code == 404

    hh = await _connection(db, organization, admin, "hh_ru", "Canonical hh.ru")
    source = await _source(
        db,
        admin,
        provider="hh",
        name="Bound hh.ru",
        connection=hh,
        configuration={"area_id": "2759"},
    )
    page = await list_source_integrations(limit=50, cursor=None, db=db, user=admin)
    visible = next(item for item in page.items if item.id == source.id)
    assert visible.connection_id == hh.id
    assert visible.connection_provider == "hh_ru"
    assert visible.connection_status == "connected"
    assert visible.status == "ready"
    assert visible.capabilities.candidate_import == "available"

    hh.status = "revoked"
    await db.commit()
    page = await list_source_integrations(limit=50, cursor=None, db=db, user=admin)
    revoked = next(item for item in page.items if item.id == source.id)
    assert revoked.connection_status == "revoked"
    assert revoked.status == "error"
    assert revoked.capabilities.candidate_import == "credentials_not_configured"

    with pytest.raises(HTTPException) as revoked_binding:
        await create_source_integration(
            SourceIntegrationCreate(
                provider="hh", name="Revoked binding", connection_id=hh.id
            ),
            db=db,
            user=admin,
        )
    assert revoked_binding.value.status_code == 409
    assert revoked_binding.value.detail["code"] == "connection_not_ready"

    with pytest.raises(HTTPException) as cross_tenant_update:
        await update_source_integration(
            source.id,
            SourceIntegrationUpdate(connection_id=other_hh.id),
            db=db,
            user=admin,
        )
    assert cross_tenant_update.value.status_code == 404
    await db.refresh(source)
    assert source.connection_id == hh.id


async def test_source_webhook_secrets_are_one_time_no_store_and_permission_gated(db):
    organization, admin, _position, _second = await _seed(db, "secret")
    connection = await _connection(
        db, organization, admin, "generic_webhook", "Canonical signed webhook"
    )
    created_response = Response()
    created = await create_source_integration(
        SourceIntegrationCreate(
            provider="generic_webhook",
            name="Inbound sourcing webhook",
            connection_id=connection.id,
        ),
        db=db,
        user=admin,
        response=created_response,
    )
    assert created.webhook_secret
    assert created_response.headers["cache-control"] == "private, no-store"
    stored = await db.get(SourceIntegration, created.integration.id)
    assert stored is not None
    assert created.webhook_secret not in stored.webhook_secret_hash

    rotated_response = Response()
    rotated = await rotate_source_webhook_secret(
        stored.id,
        db=db,
        user=admin,
        response=rotated_response,
    )
    assert rotated.webhook_secret != created.webhook_secret
    assert rotated_response.headers["cache-control"] == "private, no-store"


class _TelegramSuccess:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_message(self, _credentials, **kwargs):
        self.messages.append(kwargs)
        return "telegram-message-42"


class _TelegramRateLimited:
    async def send_message(self, _credentials, **_kwargs):
        raise ProviderRequestError("provider_rate_limited", retryable=True)


async def test_telegram_publication_is_encrypted_idempotent_reconciled_and_replayable(
    db,
):
    organization, admin, position, _second = await _seed(db, "telegram-live")
    connection = await _connection(
        db,
        organization,
        admin,
        "telegram",
        "Telegram publication",
        {"chat_id": "@regional_jobs"},
    )
    source = await _source(
        db,
        admin,
        provider="telegram",
        name="Telegram source",
        connection=connection,
    )
    body = SourcePublicationPreviewRequest(
        position_id=position.id,
        locale="en",
        idempotency_key="telegram-publication-1",
    )
    queued = await publish_source_publication(
        source.id, body, db=db, user=admin, runtime=_runtime()
    )
    assert queued.status == "queued"
    assert queued.job_status == "queued"
    assert "Build reliable public services" in queued.post_text
    assert "INTERNAL-COMPENSATION" not in queued.post_text
    job = await db.get(IntegrationJob, queued.job_id)
    assert job is not None
    reference_text = json.dumps(job.reference_payload, sort_keys=True)
    assert "Build reliable" not in reference_text
    assert "INTERNAL-COMPENSATION" not in reference_text
    assert b"Build reliable" not in (job.payload_ciphertext or b"")
    payload = load_job_payload(job, cipher=TEST_CIPHER)
    assert payload == {"text": queued.post_text}

    adapter = _TelegramSuccess()
    await _run_job(db, job, connection, adapter)
    publication = await db.get(SourcePublication, queued.id)
    assert publication is not None
    assert job.status == "succeeded"
    assert job.result_summary == {
        "provider_accepted": True,
        "external_ref": "telegram-message-42",
    }
    assert publication.status == "published"
    assert publication.external_id == "telegram-message-42"
    assert adapter.messages[0]["text"] == queued.post_text

    replayed_request = await publish_source_publication(
        source.id, body, db=db, user=admin, runtime=_runtime()
    )
    assert replayed_request.id == queued.id
    assert replayed_request.job_id == job.id
    assert replayed_request.job_status == "succeeded"

    outage_body = body.model_copy(
        update={"idempotency_key": "telegram-publication-outage"}
    )
    outage = await publish_source_publication(
        source.id, outage_body, db=db, user=admin, runtime=_runtime()
    )
    outage_job = await db.get(IntegrationJob, outage.job_id)
    outage_publication = await db.get(SourcePublication, outage.id)
    assert outage_job is not None and outage_publication is not None
    await _run_job(db, outage_job, connection, _TelegramRateLimited())
    assert outage_job.status == "retrying"
    assert outage_job.failure_code == "provider_rate_limited"
    assert outage_publication.status == "queued"

    outage_job.status = "running"
    outage_job.attempt_count = outage_job.max_attempts
    await execute_provider_job(
        db,
        job=outage_job,
        cipher=TEST_CIPHER,
        adapters={"telegram": _TelegramRateLimited()},
        prepared_credentials={"bot_token": "test-only"},
    )
    await db.commit()
    assert outage_job.status == "dead_letter"
    assert outage_publication.status == "failed"

    replayed = await replay_integration_job(outage_job.id, db=db, user=admin)
    assert replayed.status == "queued"
    assert outage_publication.status == "queued"
    cancelled = await cancel_integration_job(outage_job.id, db=db, user=admin)
    assert cancelled.status == "cancelled"
    assert outage_publication.status == "failed"
    assert outage_publication.failure_code == "job_cancelled"


class _HeadHunterPublisher:
    def __init__(self) -> None:
        self.requests = []

    async def publish_vacancy(self, _credentials, request):
        self.requests.append(request)
        return VacancyPublishResult(
            external_id="hh-vacancy-9001",
            url="https://hh.ru/vacancy/9001",
        )


async def test_hh_publication_uses_public_projection_and_somon_is_link_only(db):
    organization, admin, position, _second = await _seed(db, "regional-publish")
    hh_connection = await _connection(db, organization, admin, "hh_ru", "hh.ru OAuth")
    hh_source = await _source(
        db,
        admin,
        provider="hh",
        name="hh.ru publication",
        connection=hh_connection,
        configuration={"area_id": "2759", "schedule_id": "remote"},
    )
    body = SourcePublicationPreviewRequest(
        position_id=position.id,
        locale="ru",
        idempotency_key="hh-publication-1",
    )
    queued = await publish_source_publication(
        hh_source.id, body, db=db, user=admin, runtime=_runtime()
    )
    job = await db.get(IntegrationJob, queued.job_id)
    assert job is not None
    payload = load_job_payload(job, cipher=TEST_CIPHER)
    encoded = json.dumps(payload, ensure_ascii=False)
    assert payload["area"] == {"id": "2759"}
    assert payload["schedule"] == {"id": "remote"}
    assert "Build reliable public services" in encoded
    assert "INTERNAL-COMPENSATION" not in encoded
    assert "INTERNAL-COMPENSATION" not in json.dumps(job.reference_payload)

    publisher = _HeadHunterPublisher()
    await _run_job(db, job, hh_connection, publisher)
    publication = await db.get(SourcePublication, queued.id)
    assert publication is not None
    assert publication.status == "published"
    assert publication.external_id == "hh-vacancy-9001"
    assert publisher.requests[0].payload == payload

    somon_connection = await _connection(
        db, organization, admin, "somon", "Somon tracked links"
    )
    somon_source = await _source(
        db,
        admin,
        provider="somon",
        name="Somon tracked source",
        connection=somon_connection,
    )
    tracked = await publish_source_publication(
        somon_source.id,
        body.model_copy(update={"idempotency_key": "somon-tracked-1"}),
        db=db,
        user=admin,
        runtime=_runtime(adapters={"somon": object()}),
    )
    assert tracked.status == "tracked"
    assert tracked.job_id is None
    assert tracked.external_id and tracked.external_id.startswith("tracked-")
    somon_jobs = await db.scalar(
        select(func.count(IntegrationJob.id)).where(
            IntegrationJob.connection_id == somon_connection.id
        )
    )
    assert somon_jobs == 0


class _HeadHunterIntake:
    def __init__(self) -> None:
        self.pages: list[tuple[str, int, int]] = []

    async def list_negotiation_collections(self, _credentials, *, vacancy_id):
        return {"collections": [{"id": "response", "url": "provider-only"}]}

    async def list_negotiations(
        self,
        _credentials,
        *,
        vacancy_id,
        collection_id,
        page,
        per_page,
    ):
        self.pages.append((vacancy_id, page, per_page))
        if vacancy_id == "vacancy-one":
            items = (
                [{"id": "negotiation-one"}]
                if page == 0
                else [{"id": "negotiation-two"}]
            )
            return {"items": items, "page": page, "pages": 2}
        if vacancy_id == "vacancy-two":
            return {"items": [{"id": "negotiation-three"}], "page": page, "pages": 1}
        return {"items": [], "page": page, "pages": 0}

    async def get_negotiation(self, _credentials, *, collection_id, negotiation_id):
        return {
            "id": negotiation_id,
            "resume": {"id": f"resume-{negotiation_id}"},
        }

    async def get_resume(self, _credentials, *, resume_id):
        negotiation_id = resume_id.removeprefix("resume-")
        if negotiation_id == "negotiation-one":
            first_name, email, number = "Ada", "shared@example.com", "123456"
        elif negotiation_id == "negotiation-three":
            first_name, email, number = "Ada", "shared@example.com", "123456"
        else:
            first_name, email, number = "Grace", "grace@example.com", "654321"
        return {
            "id": resume_id,
            "first_name": first_name,
            "last_name": "Engineer",
            "title": "Backend engineer",
            "skills": "Python, PostgreSQL, distributed systems",
            "contact": [
                {"type": {"id": "email"}, "value": email},
                {
                    "type": {"id": "cell"},
                    "value": {"country": "992", "city": "900", "number": number},
                },
            ],
            "experience": [
                {
                    "company": "Example",
                    "position": "Engineer",
                    "description": "Built reliable APIs",
                }
            ],
        }


async def _enqueue_hh_sync(
    db,
    admin: User,
    source: SourceIntegration,
    position: Position,
    *,
    vacancy_id: str,
    key: str,
) -> IntegrationJob:
    output = await enqueue_hh_candidate_import(
        source.id,
        SourceHeadHunterImportRequest(
            position_id=position.id,
            vacancy_id=vacancy_id,
            collection_ids=["response"],
            max_pages=10,
            max_records=500,
            idempotency_key=key,
        ),
        db=db,
        user=admin,
        runtime=_runtime(),
    )
    job = await db.get(IntegrationJob, output.id)
    assert job is not None
    return job


async def test_hh_intake_paginates_deduplicates_and_links_one_person_to_two_positions(
    db,
):
    organization, admin, first, second = await _seed(db, "hh-intake")
    connection = await _connection(db, organization, admin, "hh_ru", "hh.ru intake")
    source = await _source(
        db,
        admin,
        provider="hh",
        name="hh.ru negotiations",
        connection=connection,
        configuration={"area_id": "2759"},
    )
    adapter = _HeadHunterIntake()

    first_job = await _enqueue_hh_sync(
        db,
        admin,
        source,
        first,
        vacancy_id="vacancy-one",
        key="hh-sync-first",
    )
    assert "vacancy-one" not in json.dumps(first_job.reference_payload)
    assert b"vacancy-one" not in (first_job.payload_ciphertext or b"")
    await _run_job(db, first_job, connection, adapter)
    assert first_job.status == "succeeded"
    assert first_job.result_summary == {
        "provider_accepted": True,
        "collection_count": 1,
        "page_count": 2,
        "scanned_count": 2,
        "imported_count": 2,
        "duplicate_count": 0,
        "rejected_count": 0,
    }
    assert [(vacancy, page) for vacancy, page, _ in adapter.pages[:2]] == [
        ("vacancy-one", 0),
        ("vacancy-one", 1),
    ]
    assert await db.scalar(select(func.count(Candidate.id))) == 2
    assert await db.scalar(select(func.count(Application.id))) == 2

    duplicate_job = await _enqueue_hh_sync(
        db,
        admin,
        source,
        first,
        vacancy_id="vacancy-one",
        key="hh-sync-duplicate",
    )
    await _run_job(db, duplicate_job, connection, adapter)
    assert duplicate_job.result_summary["imported_count"] == 0
    assert duplicate_job.result_summary["duplicate_count"] == 2
    assert await db.scalar(select(func.count(Candidate.id))) == 2
    assert await db.scalar(select(func.count(Application.id))) == 2

    second_position_job = await _enqueue_hh_sync(
        db,
        admin,
        source,
        second,
        vacancy_id="vacancy-two",
        key="hh-sync-second-position",
    )
    await _run_job(db, second_position_job, connection, adapter)
    assert await db.scalar(select(func.count(Candidate.id))) == 2
    assert await db.scalar(select(func.count(Application.id))) == 3
    assert await db.scalar(select(func.count(SourceCandidateReceipt.id))) == 3
    shared_candidate = await db.scalar(
        select(Candidate).where(
            Candidate.organization_id == organization.id,
            Candidate.email_normalized == "shared@example.com",
        )
    )
    assert shared_candidate is not None
    shared_positions = set(
        (
            await db.execute(
                select(Application.position_id).where(
                    Application.organization_id == organization.id,
                    Application.candidate_id == shared_candidate.id,
                )
            )
        ).scalars()
    )
    assert shared_positions == {first.id, second.id}
    assert await db.scalar(select(func.count(IntegrationCursor.id))) == 0

    for job in (first_job, duplicate_job, second_position_job):
        safe = json.dumps(
            {"references": job.reference_payload, "result": job.result_summary},
            sort_keys=True,
        )
        assert "shared@example.com" not in safe
        assert "Built reliable APIs" not in safe
        assert "Ada Engineer" not in safe


async def test_integration_admin_routes_enforce_read_manage_credentials_and_tenant(db):
    organization, admin, _position, _second = await _seed(db, "route-auth")
    other_organization, other_admin, _other_position, _ = await _seed(
        db, "route-auth-other"
    )
    restricted = User(
        organization_id=organization.id,
        email=f"route-restricted-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="restricted",
        locale="en",
    )
    db.add(restricted)
    await db.commit()
    connection = await _connection(
        db, organization, admin, "csv_xlsx", "Authorized local import"
    )
    other_connection = await _connection(
        db, other_organization, other_admin, "csv_xlsx", "Other tenant import"
    )
    job, _created = await enqueue_job(
        db,
        connection=connection,
        actor_user_id=admin.id,
        operation="health",
        idempotency_key="route-auth-health",
        references={},
    )
    other_job, _created = await enqueue_job(
        db,
        connection=other_connection,
        actor_user_id=other_admin.id,
        operation="health",
        idempotency_key="route-auth-other-health",
        references={},
    )
    await db.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.get("/integrations", headers=_auth(restricted))
    assert denied.status_code == 403

    db.add(
        PermissionOverride(
            organization_id=organization.id,
            role="restricted",
            permission="integrations.read",
            allowed=True,
        )
    )
    await db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        listed = await client.get("/integrations", headers=_auth(restricted))
        detail = await client.get(
            f"/integrations/{connection.id}", headers=_auth(restricted)
        )
        jobs = await client.get("/integrations/jobs", headers=_auth(restricted))
        job_detail = await client.get(
            f"/integrations/jobs/{job.id}", headers=_auth(restricted)
        )
        deliveries = await client.get(
            "/integrations/deliveries", headers=_auth(restricted)
        )
        cross_connection = await client.get(
            f"/integrations/{other_connection.id}", headers=_auth(restricted)
        )
        cross_job = await client.get(
            f"/integrations/jobs/{other_job.id}", headers=_auth(restricted)
        )
        denied_patch = await client.patch(
            f"/integrations/{connection.id}",
            headers=_auth(restricted),
            json={"name": "Must not change"},
        )
        denied_health = await client.post(
            f"/integrations/{connection.id}/health", headers=_auth(restricted)
        )
        denied_credentials = await client.put(
            f"/integrations/{connection.id}/credentials",
            headers=_auth(restricted),
            json={"credentials": {"signing_secret": "must-not-write"}},
        )
        denied_cancel = await client.post(
            f"/integrations/jobs/{job.id}/cancel", headers=_auth(restricted)
        )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [str(connection.id)]
    assert detail.status_code == 200
    assert jobs.status_code == 200
    assert job_detail.status_code == 200
    assert deliveries.status_code == 200
    assert cross_connection.status_code == 404
    assert cross_job.status_code == 404
    assert {
        denied_patch.status_code,
        denied_health.status_code,
        denied_credentials.status_code,
        denied_cancel.status_code,
    } == {403}

    db.add(
        PermissionOverride(
            organization_id=organization.id,
            role="restricted",
            permission="integrations.manage",
            allowed=True,
        )
    )
    await db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        patched = await client.patch(
            f"/integrations/{connection.id}",
            headers=_auth(restricted),
            json={"name": "Delegated local import"},
        )
        healthy = await client.post(
            f"/integrations/{connection.id}/health", headers=_auth(restricted)
        )
        still_denied_credentials = await client.put(
            f"/integrations/{connection.id}/credentials",
            headers=_auth(restricted),
            json={"credentials": {"signing_secret": "still-forbidden"}},
        )
        cancelled = await client.post(
            f"/integrations/jobs/{job.id}/cancel", headers=_auth(restricted)
        )
    assert patched.status_code == 200
    assert healthy.status_code == 200
    assert still_denied_credentials.status_code == 403
    assert cancelled.status_code == 200
