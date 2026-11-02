import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models.integration import (
    IntegrationCursor,
    IntegrationOAuthState,
    IntegrationWebhookDeliveryAttempt,
    IntegrationWebhookEvent,
)
from app.models.operations import OrganizationEntitlement, UsageLedgerEntry
from app.models.organization import Organization
from app.models.user import User
from app.observability.metrics import worker_telemetry_snapshot
from app.schemas.integrations import WebhookEnvelope
from app.services.entitlements import EntitlementExceeded
from app.services.integrations.crypto import (
    IntegrationCipher,
    IntegrationCryptoError,
)
from app.services.integrations.cursors import (
    IntegrationCursorError,
    delete_cursor,
    load_cursor,
    store_cursor,
)
from app.services.integrations.http import ProviderHttpClient, ProviderRequestError
from app.services.integrations.jobs import (
    IntegrationJobError,
    claim_jobs,
    enqueue_job,
    load_job_payload,
)
from app.services.integrations.lifecycle import (
    IntegrationLifecycleError,
    check_health,
    complete_oauth,
    create_connection,
    ensure_fresh_credentials,
    get_connection,
    load_credentials,
    revoke_connection,
    set_connection_credentials,
    start_oauth,
)
from app.services.integrations.providers import (
    GoogleWorkspaceAdapter,
    OAuthTokenSet,
    TelegramAdapter,
)
from app.services.integrations.runtime import execute_provider_job
from app.services.integrations.webhooks import (
    WebhookError,
    accept_webhook_event,
    canonical_envelope_bytes,
    deliver_webhook,
    queue_webhook_delivery,
    request_webhook_delivery_attempt,
    sign_webhook,
)
from app.services.integrations.worker import process_integration_jobs_once

pytestmark = pytest.mark.asyncio


TEST_CIPHER = IntegrationCipher(b"i" * 32, key_id="integration-test-key")


async def seed_admin(db, suffix: str):
    organization = Organization(
        name=f"Integration org {suffix}",
        slug=f"integration-{suffix}-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    admin = User(
        organization_id=organization.id,
        email=f"integration-{suffix}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    db.add(admin)
    await db.commit()
    return organization, admin


async def test_credentials_are_purpose_bound_and_tenant_connection_lookup_isolated(db):
    organization, admin = await seed_admin(db, "crypto")
    other, _other_admin = await seed_admin(db, "crypto-other")
    connection, _ = await create_connection(
        db,
        organization_id=organization.id,
        actor_user_id=admin.id,
        provider="telegram",
        name="Recruiting bot",
        configuration={"chat_id": "@jobs"},
        cipher=TEST_CIPHER,
    )
    await set_connection_credentials(
        db,
        connection=connection,
        credentials={"bot_token": "123456:fake-test-token"},
        cipher=TEST_CIPHER,
    )
    await db.commit()

    assert connection.credential_ciphertext is not None
    assert b"fake-test-token" not in connection.credential_ciphertext
    assert load_credentials(connection, cipher=TEST_CIPHER) == {
        "bot_token": "123456:fake-test-token"
    }
    with pytest.raises(IntegrationCryptoError):
        IntegrationCipher(b"x" * 32, key_id="wrong").decrypt_json(
            connection.credential_ciphertext,
            connection.credential_nonce,
            aad=b"wrong-purpose",
            key_id=connection.credential_key_id,
        )
    with pytest.raises(IntegrationLifecycleError) as hidden:
        await get_connection(
            db,
            connection_id=connection.id,
            organization_id=other.id,
        )
    assert hidden.value.code == "connection_not_found"


async def test_provider_cursors_are_encrypted_named_and_connection_bound(db):
    organization, admin = await seed_admin(db, "cursor")
    connection, _ = await create_connection(
        db,
        organization_id=organization.id,
        actor_user_id=admin.id,
        provider="csv_xlsx",
        name="Import cursor",
        configuration={},
        cipher=TEST_CIPHER,
    )
    marker = "provider-page-token-private"
    stored = await store_cursor(
        db,
        connection=connection,
        cursor_name="candidate_import.next_page",
        value={"next": marker, "offset": 25},
        cipher=TEST_CIPHER,
    )
    assert marker.encode() not in stored.value_ciphertext
    assert await load_cursor(
        db,
        connection=connection,
        cursor_name="candidate_import.next_page",
        cipher=TEST_CIPHER,
    ) == {"next": marker, "offset": 25}

    await store_cursor(
        db,
        connection=connection,
        cursor_name="candidate_import.next_page",
        value={"next": "replacement"},
        cipher=TEST_CIPHER,
    )
    persisted = await db.scalar(select(IntegrationCursor))
    assert persisted is stored
    assert await load_cursor(
        db,
        connection=connection,
        cursor_name="candidate_import.next_page",
        cipher=TEST_CIPHER,
    ) == {"next": "replacement"}
    with pytest.raises(IntegrationCursorError) as invalid:
        await store_cursor(
            db,
            connection=connection,
            cursor_name="../unsafe",
            value="cursor",
            cipher=TEST_CIPHER,
        )
    assert invalid.value.code == "cursor_name_invalid"
    assert (
        await delete_cursor(
            db,
            connection=connection,
            cursor_name="candidate_import.next_page",
        )
        is True
    )
    assert (
        await load_cursor(
            db,
            connection=connection,
            cursor_name="candidate_import.next_page",
            cipher=TEST_CIPHER,
        )
        is None
    )


async def test_integration_jobs_are_idempotently_metered_and_hard_capped(db):
    organization, admin = await seed_admin(db, "job-entitlement")
    connection, _ = await create_connection(
        db,
        organization_id=organization.id,
        actor_user_id=admin.id,
        provider="csv_xlsx",
        name="Metered imports",
        configuration={},
        cipher=TEST_CIPHER,
    )
    db.add(
        OrganizationEntitlement(
            organization_id=organization.id,
            metric="integration_runs",
            limit_value=1,
            period="contract",
        )
    )
    await db.commit()

    first, created = await enqueue_job(
        db,
        connection=connection,
        actor_user_id=admin.id,
        operation="candidate.import",
        idempotency_key="import-1",
        references={"source": "fixture"},
    )
    duplicate, duplicate_created = await enqueue_job(
        db,
        connection=connection,
        actor_user_id=admin.id,
        operation="candidate.import",
        idempotency_key="import-1",
        references={"source": "fixture"},
    )
    assert created is True and duplicate_created is False
    assert duplicate.id == first.id
    assert len(
        list(
            (
                await db.execute(
                    select(UsageLedgerEntry).where(
                        UsageLedgerEntry.organization_id == organization.id,
                        UsageLedgerEntry.metric == "integration_runs",
                    )
                )
            ).scalars()
        )
    ) == 1

    with pytest.raises(EntitlementExceeded):
        await enqueue_job(
            db,
            connection=connection,
            actor_user_id=admin.id,
            operation="candidate.import",
            idempotency_key="import-2",
            references={"source": "fixture-2"},
        )
    assert await db.scalar(
        select(func.count(UsageLedgerEntry.id)).where(
            UsageLedgerEntry.organization_id == organization.id,
            UsageLedgerEntry.metric == "integration_runs",
        )
    ) == 1
async def test_google_oauth_pkce_is_one_time_and_tokens_stay_encrypted(db):
    organization, admin = await seed_admin(db, "oauth")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url == httpx.URL("https://oauth2.googleapis.com/token"):
            assert b"code_verifier=" in request.content
            return httpx.Response(
                200,
                json={
                    "access_token": "google-access-test",
                    "refresh_token": "google-refresh-test",
                    "expires_in": 3600,
                    "scope": "openid email",
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GoogleWorkspaceAdapter(ProviderHttpClient(client))
        connection, _ = await create_connection(
            db,
            organization_id=organization.id,
            actor_user_id=admin.id,
            provider="google_workspace",
            name="Google Workspace",
            configuration={"calendar_id": "primary"},
            cipher=TEST_CIPHER,
        )
        await set_connection_credentials(
            db,
            connection=connection,
            credentials={
                "client_id": "google-client",
                "client_secret": "google-secret",
            },
            cipher=TEST_CIPHER,
        )
        authorization_url, _expires = await start_oauth(
            db,
            connection=connection,
            redirect_uri="https://hr.example.test/integrations/oauth/callback",
            allowed_redirect_origins={"https://hr.example.test"},
            cipher=TEST_CIPHER,
            adapter=adapter,
        )
        await db.commit()
        query = parse_qs(urlsplit(authorization_url).query)
        assert query["code_challenge_method"] == ["S256"]
        assert "code_challenge" in query
        raw_state = query["state"][0]
        state_row = (await db.execute(select(IntegrationOAuthState))).scalar_one()
        assert raw_state not in state_row.state_digest
        assert b"google-secret" not in connection.credential_ciphertext

        connected = await complete_oauth(
            db,
            raw_state=raw_state,
            code="one-time-code",
            cipher=TEST_CIPHER,
            adapters={"google_workspace": adapter},
        )
        await db.commit()
        assert connected.status == "connected"
        assert connected.credential_expires_at is not None
        encrypted = connected.credential_ciphertext
        assert b"google-access-test" not in encrypted
        assert load_credentials(connected, cipher=TEST_CIPHER)["refresh_token"] == (
            "google-refresh-test"
        )
        with pytest.raises(IntegrationLifecycleError) as replay:
            await complete_oauth(
                db,
                raw_state=raw_state,
                code="replay-code",
                cipher=TEST_CIPHER,
                adapters={"google_workspace": adapter},
            )
        assert replay.value.code == "oauth_state_consumed"
    assert len(seen) == 1


async def test_oauth_refresh_rotation_and_revoke_are_monotonic_and_fail_safe(db):
    organization, admin = await seed_admin(db, "oauth-lifecycle")
    connection, _ = await create_connection(
        db,
        organization_id=organization.id,
        actor_user_id=admin.id,
        provider="google_workspace",
        name="Lifecycle Google Workspace",
        configuration={"calendar_id": "primary"},
        cipher=TEST_CIPHER,
    )
    await set_connection_credentials(
        db,
        connection=connection,
        credentials={
            "client_id": "lifecycle-client",
            "client_secret": "lifecycle-secret",
            "access_token": "stale-access",
            "refresh_token": "rotating-refresh",
        },
        cipher=TEST_CIPHER,
    )
    now = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
    connection.status = "connected"
    connection.credential_expires_at = now + timedelta(seconds=30)

    class LifecycleAdapter:
        def __init__(self) -> None:
            self.refreshed: list[dict] = []
            self.revoked: list[dict] = []

        async def refresh(self, credentials: dict) -> OAuthTokenSet:
            self.refreshed.append(dict(credentials))
            return OAuthTokenSet(
                access_token="fresh-access",
                refresh_token="fresh-refresh",
                expires_at=now + timedelta(hours=1),
                scopes=("openid", "email"),
                provider_account_id="provider-account-42",
            )

        async def revoke(self, credentials: dict) -> None:
            self.revoked.append(dict(credentials))

    adapter = LifecycleAdapter()
    refreshed = await ensure_fresh_credentials(
        db,
        connection=connection,
        cipher=TEST_CIPHER,
        adapter=adapter,
        now=now,
    )
    assert refreshed["access_token"] == "fresh-access"
    assert refreshed["refresh_token"] == "fresh-refresh"
    assert adapter.refreshed[0]["access_token"] == "stale-access"
    assert connection.status == "connected"
    assert connection.granted_scopes == ["openid", "email"]
    assert connection.provider_account_id == "provider-account-42"
    assert b"fresh-access" not in connection.credential_ciphertext

    await revoke_connection(
        db,
        connection=connection,
        cipher=TEST_CIPHER,
        adapters={"google_workspace": adapter},
        now=now + timedelta(minutes=1),
    )
    assert adapter.revoked[0]["access_token"] == "fresh-access"
    assert connection.status == "revoked"
    assert connection.revoked_at == now + timedelta(minutes=1)
    assert connection.credential_ciphertext is None
    assert connection.credential_nonce is None
    assert connection.credential_key_id is None
    assert connection.granted_scopes == []

    unavailable, _ = await create_connection(
        db,
        organization_id=organization.id,
        actor_user_id=admin.id,
        provider="google_workspace",
        name="Unavailable revoke",
        configuration={},
        cipher=TEST_CIPHER,
    )
    await set_connection_credentials(
        db,
        connection=unavailable,
        credentials={
            "client_id": "unavailable-client",
            "client_secret": "unavailable-secret",
            "access_token": "unavailable-access",
            "refresh_token": "unavailable-refresh",
        },
        cipher=TEST_CIPHER,
    )

    class UnavailableAdapter:
        async def revoke(self, _credentials: dict) -> None:
            raise ProviderRequestError("provider_unavailable", retryable=True)

    await revoke_connection(
        db,
        connection=unavailable,
        cipher=TEST_CIPHER,
        adapters={"google_workspace": UnavailableAdapter()},
        now=now,
    )
    assert unavailable.status == "revoked"
    assert unavailable.last_error_code == "provider_unavailable"
    assert unavailable.credential_ciphertext is None


async def test_signed_webhook_replay_conflict_and_durable_delivery(db):
    organization, admin = await seed_admin(db, "webhook")
    connection, secret = await create_connection(
        db,
        organization_id=organization.id,
        actor_user_id=admin.id,
        provider="generic_webhook",
        name="Signed partner",
        configuration={"target_url": "https://partner.example.test/events"},
        cipher=TEST_CIPHER,
    )
    await db.commit()
    assert secret is not None
    now = datetime.now(UTC)
    envelope = WebhookEnvelope(
        id="candidate.created:test-1",
        type="candidate.created",
        created_at=now,
        data={"candidate_id": "candidate-ref-1"},
    )
    body = canonical_envelope_bytes(envelope)
    timestamp = str(int(now.timestamp()))
    signature = sign_webhook(secret, int(timestamp), body)
    with pytest.raises(WebhookError) as invalid_signature:
        await accept_webhook_event(
            db,
            connection=connection,
            cipher=TEST_CIPHER,
            raw_body=body,
            timestamp=timestamp,
            signature="v1=" + ("0" * 64),
            now=now,
        )
    assert invalid_signature.value.code == "webhook_signature_invalid"
    assert (
        await db.scalar(select(func.count(IntegrationWebhookEvent.id)))
        == 0
    )
    event, replay = await accept_webhook_event(
        db,
        connection=connection,
        cipher=TEST_CIPHER,
        raw_body=body,
        timestamp=timestamp,
        signature=signature,
        now=now,
    )
    assert replay is False
    assert b"candidate-ref-1" not in event.body_ciphertext
    repeated, replay = await accept_webhook_event(
        db,
        connection=connection,
        cipher=TEST_CIPHER,
        raw_body=body,
        timestamp=timestamp,
        signature=signature,
        now=now,
    )
    assert repeated.id == event.id and replay is True

    conflicting = envelope.model_copy(update={"data": {"candidate_id": "different"}})
    conflicting_body = canonical_envelope_bytes(conflicting)
    with pytest.raises(WebhookError) as conflict:
        await accept_webhook_event(
            db,
            connection=connection,
            cipher=TEST_CIPHER,
            raw_body=conflicting_body,
            timestamp=timestamp,
            signature=sign_webhook(secret, int(timestamp), conflicting_body),
            now=now,
        )
    assert conflict.value.code == "webhook_event_conflict"
    with pytest.raises(WebhookError) as expired:
        await accept_webhook_event(
            db,
            connection=connection,
            cipher=TEST_CIPHER,
            raw_body=body,
            timestamp=str(int((now - timedelta(minutes=10)).timestamp())),
            signature=sign_webhook(
                secret, int((now - timedelta(minutes=10)).timestamp()), body
            ),
            now=now,
        )
    assert expired.value.code == "webhook_timestamp_expired"

    delivery, created = await queue_webhook_delivery(
        db,
        connection=connection,
        cipher=TEST_CIPHER,
        event_type="application.stage.changed",
        data={"application_id": "application-ref-1", "stage": "interview"},
        idempotency_key="stage-change-1",
        now=now,
    )
    same, created_again = await queue_webhook_delivery(
        db,
        connection=connection,
        cipher=TEST_CIPHER,
        event_type="application.stage.changed",
        data={"application_id": "application-ref-1", "stage": "interview"},
        idempotency_key="stage-change-1",
        now=now + timedelta(seconds=1),
    )
    assert created is True and created_again is False and same.id == delivery.id
    assert b"application-ref-1" not in delivery.body_ciphertext
    received_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received_headers.update(request.headers)
        parsed = json.loads(request.content)
        assert parsed["id"] == delivery.event_id
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        http = ProviderHttpClient(client, sleep=_no_sleep)

        async def approved(value: str) -> str:
            assert value == "https://partner.example.test/events"
            return value

        await deliver_webhook(
            db,
            delivery=delivery,
            connection=connection,
            cipher=TEST_CIPHER,
            http=http,
            validate_endpoint=approved,
            now=now,
        )
    assert delivery.status == "delivered"
    assert received_headers["x-hr-webhook-id"] == delivery.event_id
    assert received_headers["x-hr-webhook-signature"].startswith("v1=")
    attempts = list(
        (
            await db.execute(
                select(IntegrationWebhookDeliveryAttempt).where(
                    IntegrationWebhookDeliveryAttempt.delivery_id == delivery.id
                )
            )
        ).scalars()
    )
    assert [
        (item.attempt_number, item.status, item.response_status) for item in attempts
    ] == [(1, "delivered", 204)]

    retry_delivery, _ = await queue_webhook_delivery(
        db,
        connection=connection,
        cipher=TEST_CIPHER,
        event_type="application.updated",
        data={"application_id": "application-ref-2"},
        idempotency_key="retry-delivery-1",
        now=now,
    )

    def unavailable(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        await deliver_webhook(
            db,
            delivery=retry_delivery,
            connection=connection,
            cipher=TEST_CIPHER,
            http=ProviderHttpClient(client, sleep=_no_sleep),
            validate_endpoint=approved,
            now=now,
        )
    assert retry_delivery.status == "retrying"
    assert retry_delivery.attempt_count == 1
    assert retry_delivery.next_attempt_at == now + timedelta(seconds=10)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(204))
    ) as client:
        await deliver_webhook(
            db,
            delivery=retry_delivery,
            connection=connection,
            cipher=TEST_CIPHER,
            http=ProviderHttpClient(client, sleep=_no_sleep),
            validate_endpoint=approved,
            now=now + timedelta(seconds=11),
        )
    retry_attempts = list(
        (
            await db.execute(
                select(IntegrationWebhookDeliveryAttempt)
                .where(
                    IntegrationWebhookDeliveryAttempt.delivery_id == retry_delivery.id
                )
                .order_by(IntegrationWebhookDeliveryAttempt.attempt_number)
            )
        ).scalars()
    )
    assert [(item.attempt_number, item.status) for item in retry_attempts] == [
        (1, "retrying"),
        (2, "delivered"),
    ]
    assert (
        await db.scalar(
            select(IntegrationWebhookEvent).where(
                IntegrationWebhookEvent.event_id == envelope.id
            )
        )
    ) is not None

    dead_delivery, _ = await queue_webhook_delivery(
        db,
        connection=connection,
        cipher=TEST_CIPHER,
        event_type="application.deleted",
        data={"application_id": "application-ref-3"},
        idempotency_key="dead-delivery-1",
        now=now,
    )
    dead_delivery.max_attempts = 2
    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        dead_http = ProviderHttpClient(client, sleep=_no_sleep)
        await deliver_webhook(
            db,
            delivery=dead_delivery,
            connection=connection,
            cipher=TEST_CIPHER,
            http=dead_http,
            validate_endpoint=approved,
            now=now,
        )
        await deliver_webhook(
            db,
            delivery=dead_delivery,
            connection=connection,
            cipher=TEST_CIPHER,
            http=dead_http,
            validate_endpoint=approved,
            now=dead_delivery.next_attempt_at,
        )
    assert dead_delivery.status == "dead_letter"
    assert dead_delivery.attempt_count == 2
    assert dead_delivery.failure_code == "provider_unavailable"
    dead_attempts = list(
        (
            await db.execute(
                select(IntegrationWebhookDeliveryAttempt)
                .where(
                    IntegrationWebhookDeliveryAttempt.delivery_id
                    == dead_delivery.id
                )
                .order_by(IntegrationWebhookDeliveryAttempt.attempt_number)
            )
        ).scalars()
    )
    assert [attempt.status for attempt in dead_attempts] == [
        "retrying",
        "failed",
    ]

    replay_at = now + timedelta(hours=1)
    await request_webhook_delivery_attempt(
        db,
        delivery=dead_delivery,
        now=replay_at,
    )
    assert dead_delivery.status == "retrying"
    assert dead_delivery.attempt_count == 0
    assert dead_delivery.next_attempt_at == replay_at
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(204))
    ) as client:
        await deliver_webhook(
            db,
            delivery=dead_delivery,
            connection=connection,
            cipher=TEST_CIPHER,
            http=ProviderHttpClient(client, sleep=_no_sleep),
            validate_endpoint=approved,
            now=replay_at,
        )
    assert dead_delivery.status == "delivered"
    assert dead_delivery.attempt_count == 1
    replay_history = list(
        (
            await db.execute(
                select(IntegrationWebhookDeliveryAttempt.attempt_number)
                .where(
                    IntegrationWebhookDeliveryAttempt.delivery_id
                    == dead_delivery.id
                )
                .order_by(IntegrationWebhookDeliveryAttempt.attempt_number)
            )
        ).scalars()
    )
    assert replay_history == [1, 2, 3]


async def _no_sleep(_delay: float) -> None:
    return None


async def test_encrypted_jobs_are_idempotent_leased_and_execute_real_telegram_adapter(
    db,
):
    organization, admin = await seed_admin(db, "jobs")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 42}})
        if request.url.path.endswith("/sendMessage"):
            payload = json.loads(request.content)
            assert payload["text"] == "Private candidate message"
            return httpx.Response(
                200,
                json={"ok": True, "result": {"message_id": 1001}},
            )
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = TelegramAdapter(ProviderHttpClient(client, sleep=_no_sleep))
        connection, _ = await create_connection(
            db,
            organization_id=organization.id,
            actor_user_id=admin.id,
            provider="telegram",
            name="Telegram bot",
            configuration={"chat_id": "@jobs"},
            cipher=TEST_CIPHER,
        )
        await set_connection_credentials(
            db,
            connection=connection,
            credentials={"bot_token": "123456:runtime-fake-token"},
            cipher=TEST_CIPHER,
        )
        health = await check_health(
            db,
            connection=connection,
            cipher=TEST_CIPHER,
            adapters={"telegram": adapter},
        )
        assert health.status == "connected"
        job, created = await enqueue_job(
            db,
            connection=connection,
            actor_user_id=admin.id,
            operation="telegram.send",
            idempotency_key="telegram-message-1",
            references={},
            sensitive_payload={"text": "Private candidate message"},
            cipher=TEST_CIPHER,
        )
        replayed, created_again = await enqueue_job(
            db,
            connection=connection,
            actor_user_id=admin.id,
            operation="telegram.send",
            idempotency_key="telegram-message-1",
            references={},
            sensitive_payload={"text": "Private candidate message"},
            cipher=TEST_CIPHER,
        )
        assert created is True and created_again is False and replayed.id == job.id
        assert b"Private candidate message" not in job.payload_ciphertext
        assert load_job_payload(job, cipher=TEST_CIPHER)["text"] == (
            "Private candidate message"
        )
        with pytest.raises(IntegrationJobError) as conflict:
            await enqueue_job(
                db,
                connection=connection,
                actor_user_id=admin.id,
                operation="telegram.send",
                idempotency_key="telegram-message-1",
                references={},
                sensitive_payload={"text": "Different message"},
                cipher=TEST_CIPHER,
            )
        assert conflict.value.code == "idempotency_conflict"

        claimed = await claim_jobs(db, worker_id="worker-test", limit=10)
        assert [item.id for item in claimed] == [job.id]
        await execute_provider_job(
            db,
            job=job,
            cipher=TEST_CIPHER,
            adapters={"telegram": adapter},
        )
        assert job.status == "succeeded"
        assert job.result_summary == {
            "provider_accepted": True,
            "external_ref": "1001",
        }
    assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == [
        "getMe",
        "sendMessage",
    ]


async def test_worker_dead_letters_malformed_job_without_poisoning_later_work(db):
    organization, admin = await seed_admin(db, "poison-job")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 2002}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = TelegramAdapter(ProviderHttpClient(client, sleep=_no_sleep))
        connection, _ = await create_connection(
            db,
            organization_id=organization.id,
            actor_user_id=admin.id,
            provider="telegram",
            name="Worker isolation bot",
            configuration={"chat_id": "@jobs"},
            cipher=TEST_CIPHER,
        )
        await set_connection_credentials(
            db,
            connection=connection,
            credentials={"bot_token": "123456:worker-isolation-token"},
            cipher=TEST_CIPHER,
        )
        connection.status = "connected"
        malformed, _ = await enqueue_job(
            db,
            connection=connection,
            actor_user_id=admin.id,
            operation="telegram.send",
            idempotency_key="malformed-message",
            references={},
            sensitive_payload={},
            cipher=TEST_CIPHER,
        )
        valid, _ = await enqueue_job(
            db,
            connection=connection,
            actor_user_id=admin.id,
            operation="telegram.send",
            idempotency_key="valid-message",
            references={},
            sensitive_payload={"text": "Continue after malformed work"},
            cipher=TEST_CIPHER,
        )
        await db.commit()

        result = await process_integration_jobs_once(
            SessionLocal,
            cipher=TEST_CIPHER,
            adapters={"telegram": adapter},
            worker_id="worker-isolation-test",
            limit=10,
        )

    await db.refresh(malformed)
    await db.refresh(valid)
    await db.refresh(connection)
    assert result == {"claimed": 2, "succeeded": 1, "failed": 1}
    assert (malformed.status, malformed.failure_code) == (
        "dead_letter",
        "job_payload_invalid",
    )
    assert valid.status == "succeeded"
    assert connection.status == "connected"
    assert len(requests) == 1
    telemetry = worker_telemetry_snapshot()
    assert any(
        item["provider"] == "telegram"
        and item["operation"] == "telegram_send"
        and item["outcome"] == "succeeded"
        for item in telemetry["provider_operations"]
    )
    serialized_telemetry = repr(telemetry)
    assert str(malformed.id) not in serialized_telemetry
    assert str(valid.id) not in serialized_telemetry
    assert "worker-isolation-token" not in serialized_telemetry
