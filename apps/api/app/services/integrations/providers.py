"""Concrete request adapters for mandatory pilot providers.

Adapters contain protocol translation only. Connection state, encrypted token
storage, idempotent jobs, and retries are handled by the surrounding runtime.
"""

import base64
import hashlib
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any, Protocol
from urllib.parse import quote, urlencode

from app.services.integrations.http import (
    ProviderHttpClient,
    ProviderRequestError,
    validate_operator_approved_url,
)

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1"
GOOGLE_CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

MICROSOFT_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
MICROSOFT_LOGIN_BASE = "https://login.microsoftonline.com"

TELEGRAM_API_BASE = "https://api.telegram.org"
HH_API_BASE = "https://api.hh.ru"
HH_AUTHORIZE_URL = "https://hh.ru/oauth/authorize"
HH_TOKEN_URL = "https://hh.ru/oauth/token"
_HH_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,255}$")


class ProviderAdapter(Protocol):
    provider: str

    async def health(
        self, credentials: dict[str, Any], configuration: dict
    ) -> dict: ...


@dataclass(frozen=True)
class OAuthTokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scopes: tuple[str, ...] = ()
    provider_account_id: str | None = None


@dataclass(frozen=True)
class EmailRequest:
    to: str
    subject: str
    body_text: str
    from_email: str | None = None


@dataclass(frozen=True)
class CalendarEventRequest:
    title: str
    starts_at: datetime
    ends_at: datetime
    timezone: str
    attendees: tuple[str, ...] = ()
    description: str | None = None
    location: str | None = None
    video_conference: bool = False
    idempotency_key: str = field(default_factory=lambda: secrets.token_urlsafe(24))

    def __post_init__(self) -> None:
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError("Calendar timestamps must include a timezone")
        if self.starts_at >= self.ends_at:
            raise ValueError("Calendar event start must precede end")


@dataclass(frozen=True)
class CalendarEventResult:
    event_id: str
    web_url: str | None = None
    meeting_url: str | None = None


@dataclass(frozen=True)
class VacancyPublishRequest:
    payload: dict[str, Any]
    idempotency_key: str


@dataclass(frozen=True)
class VacancyPublishResult:
    external_id: str
    url: str | None = None


def _access_headers(credentials: dict[str, Any]) -> dict[str, str]:
    token = str(credentials.get("access_token") or "")
    if not token:
        raise ProviderRequestError("provider_credentials_missing")
    return {"Authorization": f"Bearer {token}"}


def _required(credentials: dict[str, Any], key: str) -> str:
    value = str(credentials.get(key) or "")
    if not value:
        raise ProviderRequestError("provider_credentials_missing")
    return value


def _token_set(payload: dict, *, fallback_refresh: str | None = None) -> OAuthTokenSet:
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ProviderRequestError("provider_response_invalid")
    refresh = payload.get("refresh_token")
    refresh_token = (
        refresh if isinstance(refresh, str) and refresh else fallback_refresh
    )
    expires_in = payload.get("expires_in")
    expires_at = None
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        # The one-minute guard avoids issuing work with an effectively expired token.
        expires_at = datetime.now(UTC) + timedelta(seconds=max(int(expires_in) - 60, 1))
    scope = payload.get("scope")
    scopes = tuple(scope.split()) if isinstance(scope, str) else ()
    return OAuthTokenSet(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        scopes=scopes,
    )


class GoogleWorkspaceAdapter:
    provider = "google_workspace"
    scopes = (
        "openid",
        "email",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.freebusy",
    )

    def __init__(self, http: ProviderHttpClient) -> None:
        self.http = http

    def authorization_url(
        self,
        credentials: dict[str, Any],
        *,
        redirect_uri: str,
        state: str,
        code_challenge: str,
    ) -> str:
        query = urlencode(
            {
                "client_id": _required(credentials, "client_id"),
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(self.scopes),
                "access_type": "offline",
                "include_granted_scopes": "true",
                "prompt": "consent",
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{GOOGLE_AUTHORIZE_URL}?{query}"

    async def exchange_code(
        self,
        credentials: dict[str, Any],
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> OAuthTokenSet:
        response = await self.http.request(
            "POST",
            GOOGLE_TOKEN_URL,
            data={
                "client_id": _required(credentials, "client_id"),
                "client_secret": _required(credentials, "client_secret"),
                "code": code,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )
        return _token_set(response.json if isinstance(response.json, dict) else {})

    async def refresh(self, credentials: dict[str, Any]) -> OAuthTokenSet:
        refresh = _required(credentials, "refresh_token")
        response = await self.http.request(
            "POST",
            GOOGLE_TOKEN_URL,
            data={
                "client_id": _required(credentials, "client_id"),
                "client_secret": _required(credentials, "client_secret"),
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
        )
        return _token_set(
            response.json if isinstance(response.json, dict) else {},
            fallback_refresh=refresh,
        )

    async def revoke(self, credentials: dict[str, Any]) -> None:
        token = credentials.get("refresh_token") or credentials.get("access_token")
        if token:
            await self.http.request(
                "POST", GOOGLE_REVOKE_URL, data={"token": str(token)}, expected={200}
            )

    async def health(self, credentials: dict[str, Any], _configuration: dict) -> dict:
        response = await self.http.request(
            "GET",
            GOOGLE_USERINFO_URL,
            headers=_access_headers(credentials),
            retry_safe=True,
        )
        payload = response.json if isinstance(response.json, dict) else {}
        return {"provider_account_id": str(payload.get("sub") or "") or None}

    async def send_email(
        self, credentials: dict[str, Any], request: EmailRequest
    ) -> str:
        message = EmailMessage()
        message["To"] = request.to
        message["Subject"] = request.subject
        if request.from_email:
            message["From"] = request.from_email
        message.set_content(request.body_text)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")
        response = await self.http.request(
            "POST",
            f"{GOOGLE_GMAIL_BASE}/users/me/messages/send",
            headers=_access_headers(credentials),
            json={"raw": raw},
            retryable_responses=True,
        )
        payload = response.json if isinstance(response.json, dict) else {}
        message_id = payload.get("id")
        if not isinstance(message_id, str) or not message_id:
            raise ProviderRequestError("provider_response_invalid")
        return message_id

    async def create_event(
        self,
        credentials: dict[str, Any],
        configuration: dict,
        request: CalendarEventRequest,
    ) -> CalendarEventResult:
        calendar_id = quote(str(configuration.get("calendar_id") or "primary"), safe="")
        body = self._event_body(request)
        event_id = hashlib.sha256(request.idempotency_key.encode()).hexdigest()[:32]
        body["id"] = event_id
        params = {"sendUpdates": "all"}
        if request.video_conference:
            params["conferenceDataVersion"] = "1"
            body["conferenceData"] = {
                "createRequest": {
                    "requestId": hashlib.sha256(
                        request.idempotency_key.encode()
                    ).hexdigest()
                }
            }
        try:
            response = await self.http.request(
                "POST",
                f"{GOOGLE_CALENDAR_BASE}/calendars/{calendar_id}/events",
                headers=_access_headers(credentials),
                params=params,
                json=body,
                retry_safe=True,
                automatic_retries=False,
            )
        except ProviderRequestError as exc:
            if exc.code != "provider_conflict":
                raise
            # The stable event ID makes an ambiguous create replay observable.
            response = await self.http.request(
                "GET",
                f"{GOOGLE_CALENDAR_BASE}/calendars/{calendar_id}/events/{event_id}",
                headers=_access_headers(credentials),
                retry_safe=True,
            )
        return self._event_result(response.json)

    async def update_event(
        self,
        credentials: dict[str, Any],
        configuration: dict,
        event_id: str,
        request: CalendarEventRequest,
    ) -> CalendarEventResult:
        calendar_id = quote(str(configuration.get("calendar_id") or "primary"), safe="")
        response = await self.http.request(
            "PATCH",
            f"{GOOGLE_CALENDAR_BASE}/calendars/{calendar_id}/events/{quote(event_id, safe='')}",
            headers=_access_headers(credentials),
            params={"sendUpdates": "all"},
            json=self._event_body(request),
            retry_safe=True,
        )
        return self._event_result(response.json)

    async def delete_event(
        self,
        credentials: dict[str, Any],
        configuration: dict,
        event_id: str,
    ) -> None:
        calendar_id = quote(str(configuration.get("calendar_id") or "primary"), safe="")
        await self.http.request(
            "DELETE",
            f"{GOOGLE_CALENDAR_BASE}/calendars/{calendar_id}/events/{quote(event_id, safe='')}",
            headers=_access_headers(credentials),
            params={"sendUpdates": "all"},
            expected={204, 410},
            retry_safe=True,
        )

    async def has_calendar_conflict(
        self,
        credentials: dict[str, Any],
        configuration: dict,
        *,
        starts_at: datetime,
        ends_at: datetime,
        exclude_event_id: str | None = None,
    ) -> bool:
        """Check the connected calendar's real busy events before mutation."""

        calendar_id = quote(str(configuration.get("calendar_id") or "primary"), safe="")
        response = await self.http.request(
            "GET",
            f"{GOOGLE_CALENDAR_BASE}/calendars/{calendar_id}/events",
            headers=_access_headers(credentials),
            params={
                "timeMin": starts_at.astimezone(UTC).isoformat(),
                "timeMax": ends_at.astimezone(UTC).isoformat(),
                "singleEvents": "true",
                "showDeleted": "false",
                "maxResults": "2500",
                "fields": "items(id,status,transparency)",
            },
            retry_safe=True,
        )
        payload = response.json if isinstance(response.json, dict) else {}
        items = payload.get("items", [])
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ProviderRequestError("provider_response_invalid")
        return any(
            item.get("id") != exclude_event_id
            and item.get("status") != "cancelled"
            and item.get("transparency") != "transparent"
            for item in items
        )

    @staticmethod
    def _event_body(request: CalendarEventRequest) -> dict:
        body: dict[str, Any] = {
            "summary": request.title,
            "start": {
                "dateTime": request.starts_at.isoformat(),
                "timeZone": request.timezone,
            },
            "end": {
                "dateTime": request.ends_at.isoformat(),
                "timeZone": request.timezone,
            },
            "attendees": [{"email": value} for value in request.attendees],
        }
        if request.description:
            body["description"] = request.description
        if request.location:
            body["location"] = request.location
        return body

    @staticmethod
    def _event_result(value: dict | list | None) -> CalendarEventResult:
        payload = value if isinstance(value, dict) else {}
        event_id = payload.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ProviderRequestError("provider_response_invalid")
        conference = payload.get("conferenceData")
        meeting_url = None
        if isinstance(conference, dict):
            for item in conference.get("entryPoints") or []:
                if isinstance(item, dict) and item.get("entryPointType") == "video":
                    meeting_url = item.get("uri")
                    break
        return CalendarEventResult(
            event_id=event_id,
            web_url=payload.get("htmlLink")
            if isinstance(payload.get("htmlLink"), str)
            else None,
            meeting_url=meeting_url if isinstance(meeting_url, str) else None,
        )


class Microsoft365Adapter:
    provider = "microsoft_365"
    scopes = (
        "openid",
        "offline_access",
        "User.Read",
        "Mail.Send",
        "Calendars.ReadWrite",
    )

    def __init__(self, http: ProviderHttpClient) -> None:
        self.http = http

    @staticmethod
    def _tenant(configuration: dict) -> str:
        return str(configuration.get("tenant") or "organizations")

    def authorization_url(
        self,
        credentials: dict[str, Any],
        configuration: dict,
        *,
        redirect_uri: str,
        state: str,
        code_challenge: str,
    ) -> str:
        query = urlencode(
            {
                "client_id": _required(credentials, "client_id"),
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "response_mode": "query",
                "scope": " ".join(self.scopes),
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{MICROSOFT_LOGIN_BASE}/{self._tenant(configuration)}/oauth2/v2.0/authorize?{query}"

    async def exchange_code(
        self,
        credentials: dict[str, Any],
        configuration: dict,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> OAuthTokenSet:
        response = await self.http.request(
            "POST",
            f"{MICROSOFT_LOGIN_BASE}/{self._tenant(configuration)}/oauth2/v2.0/token",
            data={
                "client_id": _required(credentials, "client_id"),
                "client_secret": _required(credentials, "client_secret"),
                "code": code,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "scope": " ".join(self.scopes),
            },
        )
        return _token_set(response.json if isinstance(response.json, dict) else {})

    async def refresh(
        self, credentials: dict[str, Any], configuration: dict
    ) -> OAuthTokenSet:
        refresh = _required(credentials, "refresh_token")
        response = await self.http.request(
            "POST",
            f"{MICROSOFT_LOGIN_BASE}/{self._tenant(configuration)}/oauth2/v2.0/token",
            data={
                "client_id": _required(credentials, "client_id"),
                "client_secret": _required(credentials, "client_secret"),
                "refresh_token": refresh,
                "grant_type": "refresh_token",
                "scope": " ".join(self.scopes),
            },
        )
        return _token_set(
            response.json if isinstance(response.json, dict) else {},
            fallback_refresh=refresh,
        )

    async def revoke(self, credentials: dict[str, Any]) -> None:
        # Microsoft exposes no app-scoped OAuth token revocation endpoint.
        # ``revokeSignInSessions`` would sign the person out across unrelated
        # applications, so connector revocation deliberately remains local:
        # lifecycle.revoke_connection deletes the encrypted tokens and marks
        # the connection revoked immediately.
        del credentials

    async def health(self, credentials: dict[str, Any], _configuration: dict) -> dict:
        response = await self.http.request(
            "GET",
            f"{MICROSOFT_GRAPH_BASE}/me?$select=id",
            headers=_access_headers(credentials),
            retry_safe=True,
        )
        payload = response.json if isinstance(response.json, dict) else {}
        return {"provider_account_id": str(payload.get("id") or "") or None}

    async def send_email(
        self, credentials: dict[str, Any], request: EmailRequest
    ) -> str | None:
        await self.http.request(
            "POST",
            f"{MICROSOFT_GRAPH_BASE}/me/sendMail",
            headers=_access_headers(credentials),
            json={
                "message": {
                    "subject": request.subject,
                    "body": {"contentType": "Text", "content": request.body_text},
                    "toRecipients": [{"emailAddress": {"address": request.to}}],
                },
                "saveToSentItems": True,
            },
            expected={202},
            retryable_responses=True,
        )
        # sendMail intentionally returns 202 with no provider message id.
        return None

    async def create_event(
        self,
        credentials: dict[str, Any],
        _configuration: dict,
        request: CalendarEventRequest,
    ) -> CalendarEventResult:
        response = await self.http.request(
            "POST",
            f"{MICROSOFT_GRAPH_BASE}/me/events",
            headers=_access_headers(credentials),
            json=self._event_body(request),
            expected={201},
            retry_safe=True,
            automatic_retries=False,
        )
        return self._event_result(response.json)

    async def update_event(
        self,
        credentials: dict[str, Any],
        _configuration: dict,
        event_id: str,
        request: CalendarEventRequest,
    ) -> CalendarEventResult:
        response = await self.http.request(
            "PATCH",
            f"{MICROSOFT_GRAPH_BASE}/me/events/{quote(event_id, safe='')}",
            headers=_access_headers(credentials),
            json=self._event_body(request),
            retry_safe=True,
        )
        return self._event_result(response.json)

    async def delete_event(
        self,
        credentials: dict[str, Any],
        _configuration: dict,
        event_id: str,
    ) -> None:
        await self.http.request(
            "DELETE",
            f"{MICROSOFT_GRAPH_BASE}/me/events/{quote(event_id, safe='')}",
            headers=_access_headers(credentials),
            expected={204, 404},
            retry_safe=True,
        )

    async def has_calendar_conflict(
        self,
        credentials: dict[str, Any],
        _configuration: dict,
        *,
        starts_at: datetime,
        ends_at: datetime,
        exclude_event_id: str | None = None,
    ) -> bool:
        response = await self.http.request(
            "GET",
            f"{MICROSOFT_GRAPH_BASE}/me/calendarView",
            headers={
                **_access_headers(credentials),
                "Prefer": 'outlook.timezone="UTC"',
            },
            params={
                "startDateTime": starts_at.astimezone(UTC).isoformat(),
                "endDateTime": ends_at.astimezone(UTC).isoformat(),
                "$select": "id,isCancelled,showAs",
                "$top": "1000",
            },
            retry_safe=True,
        )
        payload = response.json if isinstance(response.json, dict) else {}
        items = payload.get("value", [])
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ProviderRequestError("provider_response_invalid")
        return any(
            item.get("id") != exclude_event_id
            and item.get("isCancelled") is not True
            and item.get("showAs") != "free"
            for item in items
        )

    @staticmethod
    def _event_body(request: CalendarEventRequest) -> dict:
        body: dict[str, Any] = {
            "subject": request.title,
            "start": {
                "dateTime": request.starts_at.isoformat(),
                "timeZone": request.timezone,
            },
            "end": {
                "dateTime": request.ends_at.isoformat(),
                "timeZone": request.timezone,
            },
            "attendees": [
                {"emailAddress": {"address": value}, "type": "required"}
                for value in request.attendees
            ],
            # Graph uses transactionId to suppress duplicate creates.
            "transactionId": hashlib.sha256(
                request.idempotency_key.encode()
            ).hexdigest(),
            "isOnlineMeeting": request.video_conference,
            "onlineMeetingProvider": "teamsForBusiness"
            if request.video_conference
            else None,
        }
        if request.description:
            body["body"] = {"contentType": "Text", "content": request.description}
        if request.location:
            body["location"] = {"displayName": request.location}
        return {key: value for key, value in body.items() if value is not None}

    @staticmethod
    def _event_result(value: dict | list | None) -> CalendarEventResult:
        payload = value if isinstance(value, dict) else {}
        event_id = payload.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ProviderRequestError("provider_response_invalid")
        meeting = payload.get("onlineMeeting")
        meeting_url = meeting.get("joinUrl") if isinstance(meeting, dict) else None
        return CalendarEventResult(
            event_id=event_id,
            web_url=payload.get("webLink")
            if isinstance(payload.get("webLink"), str)
            else None,
            meeting_url=meeting_url if isinstance(meeting_url, str) else None,
        )


class TelegramAdapter:
    provider = "telegram"

    def __init__(self, http: ProviderHttpClient) -> None:
        self.http = http

    @staticmethod
    def _url(credentials: dict[str, Any], method: str) -> str:
        # The Bot API requires the token in the route. ProviderRequestError
        # deliberately never includes request URLs or response bodies.
        token = quote(_required(credentials, "bot_token"), safe=":")
        return f"{TELEGRAM_API_BASE}/bot{token}/{method}"

    async def health(self, credentials: dict[str, Any], _configuration: dict) -> dict:
        response = await self.http.request(
            "GET", self._url(credentials, "getMe"), retry_safe=True
        )
        payload = response.json if isinstance(response.json, dict) else {}
        result = payload.get("result") if payload.get("ok") is True else None
        if not isinstance(result, dict):
            raise ProviderRequestError("provider_response_invalid")
        return {"provider_account_id": str(result.get("id") or "") or None}

    async def send_message(
        self,
        credentials: dict[str, Any],
        *,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
    ) -> str:
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            body["parse_mode"] = parse_mode
        response = await self.http.request(
            "POST",
            self._url(credentials, "sendMessage"),
            json=body,
            retryable_responses=True,
        )
        payload = response.json if isinstance(response.json, dict) else {}
        result = payload.get("result") if payload.get("ok") is True else None
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if not isinstance(message_id, int):
            raise ProviderRequestError("provider_response_invalid")
        return str(message_id)


class HeadHunterAdapter:
    provider = "hh_ru"

    def __init__(self, http: ProviderHttpClient, *, user_agent: str) -> None:
        if not user_agent.strip():
            raise ValueError("hh.ru requires an identifying User-Agent")
        self.http = http
        self.user_agent = user_agent

    def _headers(self, credentials: dict[str, Any]) -> dict[str, str]:
        return {**_access_headers(credentials), "User-Agent": self.user_agent}

    def authorization_url(
        self,
        credentials: dict[str, Any],
        *,
        redirect_uri: str,
        state: str,
        code_challenge: str,
    ) -> str:
        return f"{HH_AUTHORIZE_URL}?{urlencode({'response_type': 'code', 'client_id': _required(credentials, 'client_id'), 'redirect_uri': redirect_uri, 'state': state, 'code_challenge': code_challenge, 'code_challenge_method': 'S256'})}"

    async def exchange_code(
        self,
        credentials: dict[str, Any],
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> OAuthTokenSet:
        response = await self.http.request(
            "POST",
            HH_TOKEN_URL,
            headers={"User-Agent": self.user_agent},
            data={
                "grant_type": "authorization_code",
                "client_id": _required(credentials, "client_id"),
                "client_secret": _required(credentials, "client_secret"),
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
            },
        )
        return _token_set(response.json if isinstance(response.json, dict) else {})

    async def refresh(self, credentials: dict[str, Any]) -> OAuthTokenSet:
        refresh = _required(credentials, "refresh_token")
        response = await self.http.request(
            "POST",
            HH_TOKEN_URL,
            headers={"User-Agent": self.user_agent},
            data={"grant_type": "refresh_token", "refresh_token": refresh},
        )
        return _token_set(
            response.json if isinstance(response.json, dict) else {},
            fallback_refresh=refresh,
        )

    async def revoke(self, credentials: dict[str, Any]) -> None:
        await self.http.request(
            "DELETE",
            HH_TOKEN_URL,
            headers=self._headers(credentials),
            expected={204},
            retry_safe=True,
        )

    async def health(self, credentials: dict[str, Any], _configuration: dict) -> dict:
        response = await self.http.request(
            "GET",
            f"{HH_API_BASE}/me",
            headers=self._headers(credentials),
            retry_safe=True,
        )
        payload = response.json if isinstance(response.json, dict) else {}
        return {"provider_account_id": str(payload.get("id") or "") or None}

    async def publish_vacancy(
        self, credentials: dict[str, Any], request: VacancyPublishRequest
    ) -> VacancyPublishResult:
        response = await self.http.request(
            "POST",
            f"{HH_API_BASE}/vacancies",
            headers={
                **self._headers(credentials),
                "X-Idempotency-Key": request.idempotency_key,
            },
            json=request.payload,
            expected={201},
            retryable_responses=True,
        )
        payload = response.json if isinstance(response.json, dict) else {}
        external_id = payload.get("id")
        if not isinstance(external_id, str) or not external_id:
            raise ProviderRequestError("provider_response_invalid")
        return VacancyPublishResult(
            external_id=external_id,
            url=payload.get("alternate_url")
            if isinstance(payload.get("alternate_url"), str)
            else None,
        )

    async def list_negotiation_collections(
        self,
        credentials: dict[str, Any],
        *,
        vacancy_id: str,
    ) -> dict:
        response = await self.http.request(
            "GET",
            f"{HH_API_BASE}/negotiations",
            headers=self._headers(credentials),
            params={"vacancy_id": vacancy_id},
            retry_safe=True,
        )
        if not isinstance(response.json, dict):
            raise ProviderRequestError("provider_response_invalid")
        return response.json

    @staticmethod
    def _identifier(value: str) -> str:
        if not _HH_IDENTIFIER.fullmatch(value):
            raise ProviderRequestError("provider_payload_invalid")
        return quote(value, safe="")

    async def list_negotiations(
        self,
        credentials: dict[str, Any],
        *,
        vacancy_id: str,
        collection_id: str,
        page: int,
        per_page: int,
    ) -> dict:
        if page < 0 or not 1 <= per_page <= 100:
            raise ProviderRequestError("provider_payload_invalid")
        response = await self.http.request(
            "GET",
            f"{HH_API_BASE}/negotiations/{self._identifier(collection_id)}",
            headers=self._headers(credentials),
            params={
                "vacancy_id": vacancy_id,
                "page": page,
                "per_page": per_page,
                "order_by": "created_at",
                "order": "desc",
            },
            retry_safe=True,
        )
        if not isinstance(response.json, dict):
            raise ProviderRequestError("provider_response_invalid")
        return response.json

    async def get_negotiation(
        self,
        credentials: dict[str, Any],
        *,
        collection_id: str,
        negotiation_id: str,
    ) -> dict:
        response = await self.http.request(
            "GET",
            (
                f"{HH_API_BASE}/negotiations/"
                f"{self._identifier(collection_id)}/"
                f"{self._identifier(negotiation_id)}"
            ),
            headers=self._headers(credentials),
            retry_safe=True,
        )
        if not isinstance(response.json, dict):
            raise ProviderRequestError("provider_response_invalid")
        return response.json

    async def get_resume(
        self,
        credentials: dict[str, Any],
        *,
        resume_id: str,
    ) -> dict:
        response = await self.http.request(
            "GET",
            f"{HH_API_BASE}/resumes/{self._identifier(resume_id)}",
            headers=self._headers(credentials),
            params={"with_creds": "true"},
            retry_safe=True,
        )
        if not isinstance(response.json, dict):
            raise ProviderRequestError("provider_response_invalid")
        return response.json


class GenericAssessmentAdapter:
    provider = "assessment_generic"

    def __init__(
        self,
        http: ProviderHttpClient,
        *,
        validate_endpoint: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self.http = http
        self.validate_endpoint = validate_endpoint

    @staticmethod
    def _headers(
        credentials: dict[str, Any], idempotency_key: str | None = None
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {_required(credentials, 'api_key')}",
            "Accept": "application/json",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def _base(self, configuration: dict) -> str:
        value = str(configuration.get("base_url") or "").rstrip("/")
        if not value:
            raise ProviderRequestError("provider_configuration_missing")
        if self.validate_endpoint is None:
            raise ProviderRequestError("endpoint_not_allowed")
        return (await self.validate_endpoint(value)).rstrip("/")

    async def health(self, credentials: dict[str, Any], configuration: dict) -> dict:
        response = await self.http.request(
            "GET",
            f"{await self._base(configuration)}/v1/health",
            headers=self._headers(credentials),
            retry_safe=True,
        )
        payload = response.json if isinstance(response.json, dict) else {}
        return {"provider_account_id": str(payload.get("account_id") or "") or None}

    async def create_invitation(
        self,
        credentials: dict[str, Any],
        configuration: dict,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict:
        response = await self.http.request(
            "POST",
            f"{await self._base(configuration)}/v1/invitations",
            headers=self._headers(credentials, idempotency_key),
            json=payload,
            expected={200, 201},
            retry_safe=True,
            automatic_retries=False,
        )
        if not isinstance(response.json, dict):
            raise ProviderRequestError("provider_response_invalid")
        return response.json

    async def get_attempt(
        self, credentials: dict[str, Any], configuration: dict, attempt_id: str
    ) -> dict:
        response = await self.http.request(
            "GET",
            f"{await self._base(configuration)}/v1/attempts/{quote(attempt_id, safe='')}",
            headers=self._headers(credentials),
            retry_safe=True,
        )
        if not isinstance(response.json, dict):
            raise ProviderRequestError("provider_response_invalid")
        return response.json

    async def get_result(
        self, credentials: dict[str, Any], configuration: dict, attempt_id: str
    ) -> dict:
        response = await self.http.request(
            "GET",
            f"{await self._base(configuration)}/v1/attempts/{quote(attempt_id, safe='')}/result",
            headers=self._headers(credentials),
            retry_safe=True,
        )
        if not isinstance(response.json, dict):
            raise ProviderRequestError("provider_response_invalid")
        return response.json

    async def erase_candidate_data(
        self,
        credentials: dict[str, Any],
        configuration: dict,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict:
        """Request confirmed, subject-scoped deletion from a specialist vendor.

        The subject and provider identifiers stay in the encrypted job body;
        they are never placed in the request URL or operational summary.
        """

        response = await self.http.request(
            "POST",
            f"{await self._base(configuration)}/v1/privacy/erasures",
            headers=self._headers(credentials, idempotency_key),
            json=payload,
            expected={200},
            retry_safe=True,
            automatic_retries=False,
        )
        if not isinstance(response.json, dict):
            raise ProviderRequestError("assessment_erasure_response_invalid")
        return response.json


def pkce_challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )


def default_adapters(
    http: ProviderHttpClient,
    *,
    hh_user_agent: str,
    outbound_hosts: set[str] | None = None,
) -> dict[str, ProviderAdapter]:
    approved_hosts = outbound_hosts or set()

    async def validate_endpoint(value: str) -> str:
        return await validate_operator_approved_url(value, allowed_hosts=approved_hosts)

    return {
        "google_workspace": GoogleWorkspaceAdapter(http),
        "microsoft_365": Microsoft365Adapter(http),
        "telegram": TelegramAdapter(http),
        "hh_ru": HeadHunterAdapter(http, user_agent=hh_user_agent),
        "assessment_generic": GenericAssessmentAdapter(
            http, validate_endpoint=validate_endpoint
        ),
    }
