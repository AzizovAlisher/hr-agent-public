import base64
import json
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from urllib.parse import parse_qs
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.schemas.integrations import (
    AssessmentProviderErasureRequest,
    AssessmentProviderInvitationCreate,
    WebhookEnvelope,
)
from app.services.integrations.assessments import (
    create_provider_invitation,
    erase_provider_candidate_data,
    get_provider_attempt,
    get_provider_result,
    normalize_assessment_callback,
)
from app.services.integrations.http import (
    ProviderHttpClient,
    ProviderRequestError,
    validate_operator_approved_url,
)
from app.services.integrations.providers import (
    CalendarEventRequest,
    EmailRequest,
    GenericAssessmentAdapter,
    GoogleWorkspaceAdapter,
    HeadHunterAdapter,
    Microsoft365Adapter,
    TelegramAdapter,
    VacancyPublishRequest,
)

pytestmark = pytest.mark.asyncio


async def _no_sleep(_delay: float) -> None:
    return None


async def test_google_mail_calendar_and_refresh_contracts_use_official_endpoints():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/messages/send"):
            raw = json.loads(request.content)["raw"]
            decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
            email = message_from_bytes(decoded)
            assert email["To"] == "candidate@example.test"
            assert email["Subject"] == "Interview"
            return httpx.Response(200, json={"id": "gmail-message-1"})
        if request.method == "POST" and request.url.path.endswith("/events"):
            payload = json.loads(request.content)
            assert payload["start"]["timeZone"] == "Asia/Dushanbe"
            assert payload["conferenceData"]["createRequest"]["requestId"]
            return httpx.Response(
                200,
                json={
                    "id": "google-event-1",
                    "htmlLink": "https://calendar.google.test/event-1",
                    "conferenceData": {
                        "entryPoints": [
                            {
                                "entryPointType": "video",
                                "uri": "https://meet.google.test/abc",
                            }
                        ]
                    },
                },
            )
        if request.url == httpx.URL("https://oauth2.googleapis.com/token"):
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["refresh_token"]
            return httpx.Response(
                200,
                json={"access_token": "fresh-access", "expires_in": 3600},
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GoogleWorkspaceAdapter(ProviderHttpClient(client, sleep=_no_sleep))
        credentials = {
            "client_id": "client",
            "client_secret": "secret",
            "access_token": "access",
            "refresh_token": "refresh",
        }
        message_id = await adapter.send_email(
            credentials,
            EmailRequest(
                to="candidate@example.test",
                subject="Interview",
                body_text="Please select a time.",
            ),
        )
        assert message_id == "gmail-message-1"
        start = datetime.now(UTC) + timedelta(days=1)
        event = await adapter.create_event(
            credentials,
            {"calendar_id": "primary"},
            CalendarEventRequest(
                title="Structured interview",
                starts_at=start,
                ends_at=start + timedelta(hours=1),
                timezone="Asia/Dushanbe",
                attendees=("candidate@example.test",),
                video_conference=True,
                idempotency_key="calendar-event-1",
            ),
        )
        assert event.event_id == "google-event-1"
        assert event.meeting_url == "https://meet.google.test/abc"
        refreshed = await adapter.refresh(credentials)
        assert refreshed.access_token == "fresh-access"
        assert refreshed.refresh_token == "refresh"
    assert requests[0].url.host == "gmail.googleapis.com"
    assert requests[1].url.host == "www.googleapis.com"


async def test_google_health_revoke_and_calendar_mutations_use_official_endpoints():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url == httpx.URL(
            "https://www.googleapis.com/oauth2/v3/userinfo"
        ):
            return httpx.Response(200, json={"sub": "google-account-1"})
        if request.method == "GET" and request.url.path.endswith("/events"):
            assert request.url.params["timeMin"] == "2026-10-25T01:30:00+00:00"
            assert request.url.params["timeMax"] == "2026-10-25T02:30:00+00:00"
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "google-event-1",
                            "status": "confirmed",
                            "transparency": "opaque",
                        }
                    ]
                },
            )
        if request.url == httpx.URL("https://oauth2.googleapis.com/revoke"):
            assert parse_qs(request.content.decode()) == {"token": ["refresh"]}
            return httpx.Response(200)
        if request.method == "PATCH" and request.url.path.endswith(
            "/events/google-event-1"
        ):
            payload = json.loads(request.content)
            assert payload["summary"] == "Updated interview"
            assert payload["start"]["timeZone"] == "Asia/Dushanbe"
            return httpx.Response(200, json={"id": "google-event-1"})
        if request.method == "DELETE" and request.url.path.endswith(
            "/events/google-event-1"
        ):
            return httpx.Response(204)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GoogleWorkspaceAdapter(ProviderHttpClient(client, sleep=_no_sleep))
        credentials = {
            "access_token": "access",
            "refresh_token": "refresh",
        }
        assert await adapter.health(credentials, {}) == {
            "provider_account_id": "google-account-1"
        }
        berlin = ZoneInfo("Europe/Berlin")
        busy_start = datetime(2026, 10, 25, 2, 30, tzinfo=berlin, fold=1)
        busy_end = datetime(2026, 10, 25, 3, 30, tzinfo=berlin)
        assert await adapter.has_calendar_conflict(
            credentials,
            {"calendar_id": "primary"},
            starts_at=busy_start,
            ends_at=busy_end,
        )
        assert not await adapter.has_calendar_conflict(
            credentials,
            {"calendar_id": "primary"},
            starts_at=busy_start,
            ends_at=busy_end,
            exclude_event_id="google-event-1",
        )
        starts_at = datetime.now(UTC) + timedelta(days=1)
        updated = await adapter.update_event(
            credentials,
            {"calendar_id": "primary"},
            "google-event-1",
            CalendarEventRequest(
                title="Updated interview",
                starts_at=starts_at,
                ends_at=starts_at + timedelta(hours=1),
                timezone="Asia/Dushanbe",
            ),
        )
        assert updated.event_id == "google-event-1"
        await adapter.delete_event(
            credentials, {"calendar_id": "primary"}, "google-event-1"
        )
        await adapter.revoke(credentials)

    assert [request.method for request in requests] == [
        "GET",
        "GET",
        "GET",
        "PATCH",
        "DELETE",
        "POST",
    ]


async def test_microsoft_mail_calendar_and_oauth_contracts_use_graph_and_transaction_id():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/sendMail"):
            payload = json.loads(request.content)
            assert payload["message"]["toRecipients"][0]["emailAddress"]["address"] == (
                "candidate@example.test"
            )
            return httpx.Response(202)
        if request.method == "POST" and request.url.path.endswith("/me/events"):
            payload = json.loads(request.content)
            assert len(payload["transactionId"]) == 64
            assert payload["onlineMeetingProvider"] == "teamsForBusiness"
            return httpx.Response(
                201,
                json={
                    "id": "graph-event-1",
                    "webLink": "https://outlook.office.test/event-1",
                    "onlineMeeting": {"joinUrl": "https://teams.microsoft.test/join/1"},
                },
            )
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "graph-access",
                    "refresh_token": "graph-refresh",
                    "expires_in": 3600,
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = Microsoft365Adapter(ProviderHttpClient(client, sleep=_no_sleep))
        credentials = {
            "client_id": "client",
            "client_secret": "secret",
            "access_token": "access",
            "refresh_token": "refresh",
        }
        assert (
            await adapter.send_email(
                credentials,
                EmailRequest(
                    to="candidate@example.test",
                    subject="Offer",
                    body_text="Your offer is ready.",
                ),
            )
            is None
        )
        start = datetime.now(UTC) + timedelta(days=2)
        event = await adapter.create_event(
            credentials,
            {"tenant": "organizations"},
            CalendarEventRequest(
                title="Hiring manager interview",
                starts_at=start,
                ends_at=start + timedelta(minutes=45),
                timezone="UTC",
                video_conference=True,
                idempotency_key="graph-event-1",
            ),
        )
        assert event.event_id == "graph-event-1"
        assert event.meeting_url == "https://teams.microsoft.test/join/1"
        tokens = await adapter.exchange_code(
            credentials,
            {"tenant": "organizations"},
            code="code",
            code_verifier="verifier",
            redirect_uri="https://hr.example.test/oauth/callback",
        )
        assert tokens.refresh_token == "graph-refresh"
    assert requests[0].url.host == "graph.microsoft.com"
    assert requests[-1].url.host == "login.microsoftonline.com"


async def test_microsoft_refresh_health_calendar_mutations_and_local_revoke_contract():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/oauth2/v2.0/token"):
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["refresh_token"]
            return httpx.Response(
                200,
                json={"access_token": "fresh-graph", "expires_in": 3600},
            )
        if request.method == "GET" and request.url.path == "/v1.0/me":
            assert request.url.params["$select"] == "id"
            return httpx.Response(200, json={"id": "graph-account-1"})
        if request.method == "GET" and request.url.path.endswith(
            "/me/calendarView"
        ):
            assert request.headers["prefer"] == 'outlook.timezone="UTC"'
            assert request.url.params["startDateTime"] == (
                "2026-10-25T01:30:00+00:00"
            )
            assert request.url.params["endDateTime"] == "2026-10-25T02:30:00+00:00"
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "graph-event-1",
                            "isCancelled": False,
                            "showAs": "busy",
                        }
                    ]
                },
            )
        if request.method == "PATCH" and request.url.path.endswith(
            "/me/events/graph-event-1"
        ):
            assert json.loads(request.content)["subject"] == "Updated interview"
            return httpx.Response(200, json={"id": "graph-event-1"})
        if request.method == "DELETE" and request.url.path.endswith(
            "/me/events/graph-event-1"
        ):
            return httpx.Response(204)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = Microsoft365Adapter(ProviderHttpClient(client, sleep=_no_sleep))
        credentials = {
            "client_id": "client",
            "client_secret": "secret",
            "access_token": "access",
            "refresh_token": "refresh",
        }
        authorization_url = adapter.authorization_url(
            credentials,
            {"tenant": "organizations"},
            redirect_uri="https://hr.example.test/oauth/callback",
            state="state",
            code_challenge="challenge",
        )
        requested_scopes = httpx.URL(authorization_url).params["scope"].split()
        assert "User.RevokeSessions.All" not in requested_scopes
        refreshed = await adapter.refresh(
            credentials, {"tenant": "organizations"}
        )
        assert refreshed.access_token == "fresh-graph"
        assert refreshed.refresh_token == "refresh"
        assert await adapter.health(credentials, {}) == {
            "provider_account_id": "graph-account-1"
        }
        berlin = ZoneInfo("Europe/Berlin")
        busy_start = datetime(2026, 10, 25, 2, 30, tzinfo=berlin, fold=1)
        busy_end = datetime(2026, 10, 25, 3, 30, tzinfo=berlin)
        assert await adapter.has_calendar_conflict(
            credentials,
            {},
            starts_at=busy_start,
            ends_at=busy_end,
        )
        assert not await adapter.has_calendar_conflict(
            credentials,
            {},
            starts_at=busy_start,
            ends_at=busy_end,
            exclude_event_id="graph-event-1",
        )
        starts_at = datetime.now(UTC) + timedelta(days=1)
        updated = await adapter.update_event(
            credentials,
            {},
            "graph-event-1",
            CalendarEventRequest(
                title="Updated interview",
                starts_at=starts_at,
                ends_at=starts_at + timedelta(hours=1),
                timezone="UTC",
            ),
        )
        assert updated.event_id == "graph-event-1"
        await adapter.delete_event(credentials, {}, "graph-event-1")
        before_revoke = len(requests)
        await adapter.revoke(credentials)
        assert len(requests) == before_revoke

    assert all("revokeSignInSessions" not in request.url.path for request in requests)


async def test_hh_adapter_identifies_operator_publishes_and_reads_negotiation_collections():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["user-agent"] == "HR Pilot (ops@example.test)"
        if request.method == "POST" and request.url.path == "/vacancies":
            assert request.headers["x-idempotency-key"] == "vacancy-publish-1"
            return httpx.Response(
                201,
                json={"id": "hh-vacancy-1", "alternate_url": "https://hh.ru/vacancy/1"},
            )
        if request.method == "GET" and request.url.path == "/negotiations":
            assert request.url.params["vacancy_id"] == "hh-vacancy-1"
            return httpx.Response(
                200,
                json={
                    "collections": [
                        {
                            "id": "response",
                            "url": "https://api.hh.ru/negotiations/response",
                        }
                    ]
                },
            )
        if request.method == "GET" and request.url.path == "/negotiations/response":
            assert dict(request.url.params) == {
                "vacancy_id": "hh-vacancy-1",
                "page": "1",
                "per_page": "50",
                "order_by": "created_at",
                "order": "desc",
            }
            return httpx.Response(
                200,
                json={
                    "items": [{"id": "negotiation-1"}],
                    "page": 1,
                    "pages": 2,
                },
            )
        if (
            request.method == "GET"
            and request.url.path == "/negotiations/response/negotiation-1"
        ):
            return httpx.Response(
                200,
                json={"id": "negotiation-1", "resume": {"id": "resume-1"}},
            )
        if request.method == "GET" and request.url.path == "/resumes/resume-1":
            assert request.url.params["with_creds"] == "true"
            return httpx.Response(
                200,
                json={
                    "id": "resume-1",
                    "first_name": "Ada",
                    "last_name": "Engineer",
                    "title": "Backend engineer",
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = HeadHunterAdapter(
            ProviderHttpClient(client, sleep=_no_sleep),
            user_agent="HR Pilot (ops@example.test)",
        )
        credentials = {"access_token": "hh-access"}
        published = await adapter.publish_vacancy(
            credentials,
            VacancyPublishRequest(
                payload={"name": "Backend engineer"},
                idempotency_key="vacancy-publish-1",
            ),
        )
        assert published.external_id == "hh-vacancy-1"
        collections = await adapter.list_negotiation_collections(
            credentials, vacancy_id=published.external_id
        )
        assert collections["collections"][0]["id"] == "response"
        negotiations = await adapter.list_negotiations(
            credentials,
            vacancy_id=published.external_id,
            collection_id="response",
            page=1,
            per_page=50,
        )
        assert negotiations["items"][0]["id"] == "negotiation-1"
        negotiation = await adapter.get_negotiation(
            credentials,
            collection_id="response",
            negotiation_id="negotiation-1",
        )
        resume = await adapter.get_resume(
            credentials, resume_id=negotiation["resume"]["id"]
        )
        assert resume["first_name"] == "Ada"
    assert all(item.url.host == "api.hh.ru" for item in requests)


async def test_hh_negotiation_reads_retry_rate_limits_only_to_the_bounded_http_limit():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert request.method == "GET"
        assert request.url.path == "/negotiations/response"
        return httpx.Response(429, json={"errors": [{"type": "rate_limit"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = HeadHunterAdapter(
            ProviderHttpClient(client, max_attempts=3, sleep=_no_sleep),
            user_agent="HR Pilot (ops@example.test)",
        )
        with pytest.raises(ProviderRequestError) as rate_limited:
            await adapter.list_negotiations(
                {"access_token": "hh-access"},
                vacancy_id="hh-vacancy-1",
                collection_id="response",
                page=0,
                per_page=100,
            )
    assert attempts == 3
    assert rate_limited.value.code == "provider_rate_limited"
    assert rate_limited.value.retryable is True


async def test_hh_oauth_health_revoke_and_vacancy_rate_limit_contracts():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["user-agent"] == "HR Pilot (ops@example.test)"
        if request.method == "POST" and request.url.path == "/oauth/token":
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                assert form["code_verifier"] == ["verifier"]
                return httpx.Response(
                    200,
                    json={
                        "access_token": "hh-access",
                        "refresh_token": "hh-refresh",
                        "expires_in": 3600,
                    },
                )
            assert form["grant_type"] == ["refresh_token"]
            return httpx.Response(
                200,
                json={"access_token": "hh-fresh", "expires_in": 3600},
            )
        if request.method == "GET" and request.url.path == "/me":
            return httpx.Response(200, json={"id": "hh-account-1"})
        if request.method == "DELETE" and request.url.path == "/oauth/token":
            return httpx.Response(204)
        if request.method == "POST" and request.url.path == "/vacancies":
            return httpx.Response(429, json={"errors": [{"type": "rate_limit"}]})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = HeadHunterAdapter(
            ProviderHttpClient(client, max_attempts=3, sleep=_no_sleep),
            user_agent="HR Pilot (ops@example.test)",
        )
        credentials = {
            "client_id": "client",
            "client_secret": "secret",
            "access_token": "hh-access",
            "refresh_token": "hh-refresh",
        }
        authorization_url = adapter.authorization_url(
            credentials,
            redirect_uri="https://hr.example.test/oauth/callback",
            state="state",
            code_challenge="challenge",
        )
        assert httpx.URL(authorization_url).params["code_challenge_method"] == "S256"
        exchanged = await adapter.exchange_code(
            credentials,
            code="code",
            code_verifier="verifier",
            redirect_uri="https://hr.example.test/oauth/callback",
        )
        assert exchanged.refresh_token == "hh-refresh"
        refreshed = await adapter.refresh(credentials)
        assert refreshed.access_token == "hh-fresh"
        assert refreshed.refresh_token == "hh-refresh"
        assert await adapter.health(credentials, {}) == {
            "provider_account_id": "hh-account-1"
        }
        await adapter.revoke(credentials)
        with pytest.raises(ProviderRequestError) as rate_limited:
            await adapter.publish_vacancy(
                credentials,
                VacancyPublishRequest(
                    payload={"name": "Backend engineer"},
                    idempotency_key="hh-rate-limit",
                ),
            )

    vacancy_requests = [request for request in requests if request.url.path == "/vacancies"]
    assert len(vacancy_requests) == 1
    assert rate_limited.value.code == "provider_rate_limited"
    assert rate_limited.value.retryable is True


async def test_telegram_rate_limit_is_durable_without_ambiguous_auto_replay():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert request.url.path.endswith("/sendMessage")
        return httpx.Response(429, json={"ok": False, "error_code": 429})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = TelegramAdapter(
            ProviderHttpClient(client, max_attempts=3, sleep=_no_sleep)
        )
        with pytest.raises(ProviderRequestError) as rate_limited:
            await adapter.send_message(
                {"bot_token": "123456:test-token"},
                chat_id="@regional_jobs",
                text="Backend engineer",
            )

    assert attempts == 1
    assert rate_limited.value.code == "provider_rate_limited"
    assert rate_limited.value.retryable is True


async def test_generic_assessment_contract_normalizes_invitation_attempt_and_result():
    requests: list[httpx.Request] = []
    expires = datetime.now(UTC) + timedelta(days=7)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer assessment-test-key"
        if request.method == "POST" and request.url.path == "/v1/invitations":
            assert request.headers["idempotency-key"] == "assessment-invite-1"
            return httpx.Response(
                201,
                json={
                    "id": "provider-invite-1",
                    "status": "invited",
                    "launch_url": "https://assessment.vendor.test/launch/1",
                    "expires_at": expires.isoformat(),
                },
            )
        if request.method == "POST" and request.url.path == "/v1/privacy/erasures":
            assert request.headers["idempotency-key"] == "assessment-erasure-1"
            assert json.loads(request.content) == {
                "subject_ref": "00000000-0000-0000-0000-000000000001",
                "provider_invitation_id": "provider-invite-1",
                "provider_attempt_id": "attempt-1",
            }
            return httpx.Response(
                200,
                json={"status": "erased", "receipt_id": "private-receipt-1"},
            )
        if request.url.path == "/v1/attempts/attempt-1":
            return httpx.Response(
                200,
                json={
                    "id": "attempt-1",
                    "status": "submitted",
                    "started_at": datetime.now(UTC).isoformat(),
                    "submitted_at": datetime.now(UTC).isoformat(),
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            )
        if request.url.path == "/v1/attempts/attempt-1/result":
            return httpx.Response(
                200,
                json={
                    "attempt_id": "attempt-1",
                    "status": "graded",
                    "score": 84,
                    "recommendation": "review",
                    "evidence": [{"criterion": "correctness", "score": 8}],
                    "completed_at": datetime.now(UTC).isoformat(),
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

        async def approved(value: str) -> str:
            assert value == "https://assessment.vendor.test"
            return value

        adapter = GenericAssessmentAdapter(
            ProviderHttpClient(client, sleep=_no_sleep),
            validate_endpoint=approved,
        )
        credentials = {"api_key": "assessment-test-key"}
        configuration = {"base_url": "https://assessment.vendor.test"}
        invitation = await create_provider_invitation(
            adapter,
            credentials=credentials,
            configuration=configuration,
            request=AssessmentProviderInvitationCreate(
                application_id=uuid4(),
                external_template_ref="template-1",
                expires_at=expires,
                idempotency_key="assessment-invite-1",
            ),
            candidate_ref="candidate-1",
            callback_url="https://hr.example.test/integrations/webhooks/callback",
        )
        assert invitation.provider_invitation_id == "provider-invite-1"
        attempt = await get_provider_attempt(
            adapter,
            credentials=credentials,
            configuration=configuration,
            attempt_id="attempt-1",
        )
        result = await get_provider_result(
            adapter,
            credentials=credentials,
            configuration=configuration,
            attempt_id="attempt-1",
        )
        receipt_digest = await erase_provider_candidate_data(
            adapter,
            credentials=credentials,
            configuration=configuration,
            request=AssessmentProviderErasureRequest(
                subject_ref="00000000-0000-0000-0000-000000000001",
                provider_invitation_id="provider-invite-1",
                provider_attempt_id="attempt-1",
            ),
            idempotency_key="assessment-erasure-1",
        )
        assert attempt.status == "submitted"
        assert (result.score, result.recommendation) == (84.0, "review")
        assert len(receipt_digest) == 64
        assert "private-receipt-1" not in receipt_digest
    assert len(requests) == 4


async def test_generic_assessment_erasure_requires_completed_provider_receipt():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/privacy/erasures"
        return httpx.Response(
            200,
            json={"status": "accepted", "receipt_id": "not-yet-complete"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

        async def approved(value: str) -> str:
            return value

        adapter = GenericAssessmentAdapter(
            ProviderHttpClient(client, sleep=_no_sleep),
            validate_endpoint=approved,
        )
        with pytest.raises(ProviderRequestError) as unconfirmed:
            await erase_provider_candidate_data(
                adapter,
                credentials={"api_key": "assessment-test-key"},
                configuration={"base_url": "https://assessment.vendor.test"},
                request=AssessmentProviderErasureRequest(
                    subject_ref="00000000-0000-0000-0000-000000000001"
                ),
                idempotency_key="assessment-erasure-unconfirmed",
            )
    assert unconfirmed.value.code == "assessment_erasure_unconfirmed"
    assert unconfirmed.value.retryable is True


async def test_generic_assessment_signed_callback_contract_is_type_safe():
    completed_at = datetime.now(UTC)
    normalized = normalize_assessment_callback(
        WebhookEnvelope(
            id="assessment-result-1",
            type="assessment.result.ready",
            created_at=completed_at,
            data={
                "provider_invitation_id": "provider-invite-1",
                "provider_attempt_id": "attempt-1",
                "status": "graded",
                "score": 84,
                "recommendation": "review",
                "evidence": [{"criterion": "correctness", "score": 8}],
                "completed_at": completed_at.isoformat(),
            },
        )
    )
    assert normalized.provider_attempt_id == "attempt-1"
    assert normalized.score == 84

    with pytest.raises(ProviderRequestError) as unsupported:
        normalize_assessment_callback(
            WebhookEnvelope(
                id="assessment-other-1",
                type="assessment.unknown",
                created_at=completed_at,
                data={},
            )
        )
    assert unsupported.value.code == "assessment_callback_type_unsupported"


async def test_provider_response_body_is_bounded_before_parsing():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"x" * 1_025)
        )
    ) as client:
        with pytest.raises(ProviderRequestError) as oversized:
            await ProviderHttpClient(
                client,
                max_response_bytes=1_024,
                sleep=_no_sleep,
            ).request("GET", "https://provider.example.test/health")
    assert oversized.value.code == "provider_response_too_large"


async def test_google_calendar_conflict_replays_by_stable_event_id():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(409)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"id": request.url.path.rsplit("/", 1)[-1]},
            )
        raise AssertionError(request.method)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GoogleWorkspaceAdapter(ProviderHttpClient(client, sleep=_no_sleep))
        starts_at = datetime.now(UTC) + timedelta(days=1)
        result = await adapter.create_event(
            {"access_token": "access"},
            {"calendar_id": "primary"},
            CalendarEventRequest(
                title="Replay-safe interview",
                starts_at=starts_at,
                ends_at=starts_at + timedelta(hours=1),
                timezone="UTC",
                idempotency_key="stable-calendar-create",
            ),
        )
    assert result.event_id == requests[1].url.path.rsplit("/", 1)[-1]
    assert [request.method for request in requests] == ["POST", "GET"]


async def test_http_retries_only_safe_calls_and_endpoint_policy_blocks_private_dns():
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        http = ProviderHttpClient(client, max_attempts=3, sleep=_no_sleep)
        with pytest.raises(ProviderRequestError) as safe:
            await http.request(
                "GET", "https://provider.example.test/health", retry_safe=True
            )
        assert safe.value.code == "provider_unavailable"
        assert attempts == 3
        attempts = 0
        with pytest.raises(ProviderRequestError) as terminal:
            await http.request("POST", "https://provider.example.test/effect")
        assert attempts == 1
        assert terminal.value.retryable is False
        attempts = 0
        with pytest.raises(ProviderRequestError) as durable:
            await http.request(
                "POST",
                "https://provider.example.test/effect",
                retryable_responses=True,
            )
        assert attempts == 1
        assert durable.value.code == "provider_unavailable"
        assert durable.value.retryable is True

    def network_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("ambiguous failure", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(network_failure)
    ) as client:
        with pytest.raises(ProviderRequestError) as ambiguous:
            await ProviderHttpClient(client, sleep=_no_sleep).request(
                "POST",
                "https://provider.example.test/effect",
                retryable_responses=True,
            )
    assert ambiguous.value.code == "provider_network_error"
    assert ambiguous.value.retryable is False

    def private_dns(*_args):
        return [(None, None, None, None, ("127.0.0.1", 443))]

    with pytest.raises(ProviderRequestError) as blocked:
        await validate_operator_approved_url(
            "https://provider.example.test/callback",
            allowed_hosts={"provider.example.test"},
            resolve=private_dns,
        )
    assert blocked.value.code == "endpoint_not_allowed"
