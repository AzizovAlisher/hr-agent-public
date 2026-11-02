"""Bounded, sanitized HTTP boundary shared by external provider adapters."""

import asyncio
import ipaddress
import json
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpcore
import httpx

if TYPE_CHECKING:
    from httpcore import AsyncNetworkStream


# These are the only fixed provider origins implemented by the native adapters.
# Tenant-configured assessment and webhook origins are added explicitly by the
# operator through INTEGRATION_OUTBOUND_HOST_ALLOWLIST.
BUILTIN_PROVIDER_HOSTS = frozenset(
    {
        "api.hh.ru",
        "api.telegram.org",
        "gmail.googleapis.com",
        "graph.microsoft.com",
        "hh.ru",
        "login.microsoftonline.com",
        "oauth2.googleapis.com",
        "www.googleapis.com",
    }
)

Resolver = Callable[..., list]


class ProviderRequestError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        response_status: int | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.response_status = response_status
        super().__init__(code)


@dataclass(frozen=True)
class ProviderHttpResponse:
    status_code: int
    json: dict | list | None
    headers: httpx.Headers


Sleep = Callable[[float], Awaitable[None]]


class ProviderHttpClient:
    """HTTP client with finite retries only when the caller marks a request safe.

    POST operations are not retried by default because many provider APIs do
    not support idempotency. A durable job can then surface an ambiguous result
    for human replay rather than silently duplicating an email or publication.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_attempts: int = 3,
        max_response_bytes: int = 2_000_000,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        if not 1_024 <= max_response_bytes <= 10_000_000:
            raise ValueError("max_response_bytes must be between 1024 and 10000000")
        self.client = client
        self.max_attempts = max_attempts
        self.max_response_bytes = max_response_bytes
        self.sleep = sleep

    async def request(
        self,
        method: str,
        url: str,
        *,
        retry_safe: bool = False,
        retryable_responses: bool = False,
        automatic_retries: bool = True,
        expected: set[int] | None = None,
        **kwargs,
    ) -> ProviderHttpResponse:
        if kwargs.get("follow_redirects") is True:
            raise ProviderRequestError("provider_redirects_forbidden")
        # Keep this request-local as a second line of defense for injected test
        # clients and future call sites that do not use the pinned transport.
        kwargs["follow_redirects"] = False
        expected = expected or set(range(200, 300))
        attempts = self.max_attempts if retry_safe and automatic_retries else 1
        for attempt in range(1, attempts + 1):
            try:
                async with self.client.stream(method, url, **kwargs) as response:
                    if response.status_code in expected:
                        declared_length = response.headers.get("content-length")
                        if declared_length is not None:
                            try:
                                parsed_length = int(declared_length)
                            except ValueError as exc:
                                raise ProviderRequestError(
                                    "provider_response_invalid"
                                ) from exc
                            if parsed_length < 0:
                                raise ProviderRequestError("provider_response_invalid")
                            if parsed_length > self.max_response_bytes:
                                raise ProviderRequestError(
                                    "provider_response_too_large"
                                )
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > self.max_response_bytes:
                                raise ProviderRequestError(
                                    "provider_response_too_large"
                                )
                        parsed: dict | list | None = None
                        if body:
                            try:
                                value = json.loads(body)
                                parsed = (
                                    value if isinstance(value, (dict, list)) else None
                                )
                            except (UnicodeDecodeError, json.JSONDecodeError):
                                parsed = None
                        return ProviderHttpResponse(
                            status_code=response.status_code,
                            json=parsed,
                            headers=httpx.Headers(response.headers),
                        )
                    response_status = response.status_code
            except httpx.RequestError as exc:
                if retry_safe and attempt < attempts:
                    await self.sleep(min(0.25 * (2 ** (attempt - 1)), 2.0))
                    continue
                raise ProviderRequestError(
                    "provider_network_error", retryable=retry_safe
                ) from exc
            transient_response = (
                response_status in {408, 425, 429} or response_status >= 500
            )
            if retry_safe and transient_response and attempt < attempts:
                await self.sleep(min(0.25 * (2 ** (attempt - 1)), 2.0))
                continue
            if response_status in {401, 403}:
                code = "provider_auth_rejected"
            elif response_status == 404:
                code = "provider_resource_not_found"
            elif response_status == 409:
                code = "provider_conflict"
            elif response_status == 429:
                code = "provider_rate_limited"
            elif response_status >= 500:
                code = "provider_unavailable"
            else:
                code = "provider_request_rejected"
            raise ProviderRequestError(
                code,
                # An explicit transient rejection can be durably retried even
                # when replay after an ambiguous transport failure is unsafe.
                # ``retry_safe`` continues to cover both cases; the narrower
                # flag never changes network-error handling or auto-replays.
                retryable=transient_response and (retry_safe or retryable_responses),
                response_status=response_status,
            )
        raise AssertionError("unreachable")


def _is_public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return address.is_global


def _normalize_hostname(value: str) -> str:
    try:
        return value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ProviderRequestError("endpoint_not_allowed") from exc


def _normalized_allowed_hosts(allowed_hosts: set[str] | frozenset[str]) -> set[str]:
    return {_normalize_hostname(item) for item in allowed_hosts}


async def _resolve_public_addresses(
    hostname: str,
    port: int,
    *,
    resolve: Resolver = socket.getaddrinfo,
) -> frozenset[str]:
    """Resolve an origin once and return only concrete, globally routed IPs."""

    try:
        literal = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        try:
            answers = await asyncio.to_thread(
                resolve,
                hostname,
                port,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise ProviderRequestError("endpoint_dns_failed", retryable=True) from exc
        try:
            addresses = frozenset(
                str(ipaddress.ip_address(answer[4][0].split("%", 1)[0]))
                for answer in answers
            )
        except (IndexError, TypeError, ValueError) as exc:
            raise ProviderRequestError("endpoint_dns_failed", retryable=True) from exc
    else:
        addresses = frozenset({str(literal)})
    if not addresses or any(not _is_public_address(item) for item in addresses):
        raise ProviderRequestError("endpoint_not_allowed")
    return addresses


async def _validate_public_endpoint(
    value: str,
    *,
    allowed_hosts: set[str] | frozenset[str],
    resolve: Resolver = socket.getaddrinfo,
) -> tuple[str, int, frozenset[str]]:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ProviderRequestError("endpoint_not_allowed") from exc
    hostname = _normalize_hostname(parsed.hostname or "")
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or hostname not in _normalized_allowed_hosts(allowed_hosts)
    ):
        raise ProviderRequestError("endpoint_not_allowed")
    addresses = await _resolve_public_addresses(
        hostname,
        443,
        resolve=resolve,
    )
    return hostname, 443, addresses


class PinnedPublicNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve, validate, then connect to an IP without a second OS lookup.

    httpcore still owns the original URL origin, so the HTTP Host header and
    TLS SNI/certificate verification continue to use the provider hostname.
    Only the TCP destination is replaced by the validated concrete address.
    """

    def __init__(
        self,
        *,
        allowed_hosts: set[str] | frozenset[str],
        resolve: Resolver = socket.getaddrinfo,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._allowed_hosts = frozenset(_normalized_allowed_hosts(allowed_hosts))
        self._resolve = resolve
        self._backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> "AsyncNetworkStream":
        hostname = _normalize_hostname(host)
        if hostname not in self._allowed_hosts or port != 443:
            raise httpcore.ConnectError(
                "Integration transport rejected a different origin"
            )
        try:
            addresses = await _resolve_public_addresses(
                hostname,
                port,
                resolve=self._resolve,
            )
        except ProviderRequestError as exc:
            raise httpcore.ConnectError(
                "Integration endpoint DNS binding is not public"
            ) from exc
        # Prefer IPv4 when both families are available, while retaining IPv6
        # support for IPv6-only providers. No hostname reaches connect_tcp.
        pinned_address = min(
            addresses,
            key=lambda item: (
                ipaddress.ip_address(item).version,
                int(ipaddress.ip_address(item)),
            ),
        )
        return await self._backend.connect_tcp(
            pinned_address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options=None,
    ) -> "AsyncNetworkStream":
        raise httpcore.ConnectError(
            "Integration transport does not permit Unix sockets"
        )

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class PinnedPublicAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """HTTPS transport restricted to operator-approved public provider hosts."""

    def __init__(
        self,
        *,
        allowed_hosts: set[str] | frozenset[str],
        resolve: Resolver = socket.getaddrinfo,
        backend: httpcore.AsyncNetworkBackend | None = None,
        max_connections: int = 50,
        max_keepalive_connections: int = 20,
    ) -> None:
        hosts = frozenset(
            _normalized_allowed_hosts(BUILTIN_PROVIDER_HOSTS | frozenset(allowed_hosts))
        )
        super().__init__(trust_env=False, retries=0)
        self._allowed_hosts = hosts
        self._resolve = resolve
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
            keepalive_expiry=5.0,
            retries=0,
            network_backend=PinnedPublicNetworkBackend(
                allowed_hosts=hosts,
                resolve=resolve,
                backend=backend,
            ),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Validate DNS on every request, including requests that might otherwise
        # reuse an existing connection. The backend repeats the check immediately
        # before a new socket and connects only to the resulting concrete IP.
        try:
            await _validate_public_endpoint(
                str(request.url),
                allowed_hosts=self._allowed_hosts,
                resolve=self._resolve,
            )
        except ProviderRequestError as exc:
            raise httpx.ConnectError(
                "Integration endpoint is not an approved public origin",
                request=request,
            ) from exc
        return await super().handle_async_request(request)


def create_integration_http_client(
    *,
    allowed_hosts: set[str] | frozenset[str],
    timeout: float,
    max_connections: int = 50,
    max_keepalive_connections: int = 20,
) -> httpx.AsyncClient:
    """Construct the only production HTTP client used by provider adapters."""

    return httpx.AsyncClient(
        transport=PinnedPublicAsyncHTTPTransport(
            allowed_hosts=allowed_hosts,
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        ),
        timeout=httpx.Timeout(timeout),
        follow_redirects=False,
        trust_env=False,
    )


async def validate_operator_approved_url(
    value: str,
    *,
    allowed_hosts: set[str],
    resolve: Callable[..., list] = socket.getaddrinfo,
) -> str:
    """Reject arbitrary and non-public outbound targets before connector calls.

    The host must first be on the operator allowlist. DNS is then resolved and
    every answer must remain public, protecting redirects and rebinding targets
    when this validator is repeated before each request.
    """

    await _validate_public_endpoint(
        value,
        allowed_hosts=allowed_hosts,
        resolve=resolve,
    )
    return value
