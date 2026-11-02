"""Integration egress DNS-pinning and redirect security regressions."""

from __future__ import annotations

import socket
from collections.abc import Callable
from typing import Any

import httpcore
import httpx
import pytest

from app.services.integrations.http import (
    PinnedPublicAsyncHTTPTransport,
    PinnedPublicNetworkBackend,
    ProviderHttpClient,
    ProviderRequestError,
    validate_operator_approved_url,
)

pytestmark = pytest.mark.asyncio


def _answers(*addresses: str) -> list[tuple]:
    return [
        (
            socket.AF_INET6 if ":" in address else socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (address, 443),
        )
        for address in addresses
    ]


class _RecordingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, stream_factory: Callable[[], Any] | None = None) -> None:
        self.calls: list[tuple[str, int]] = []
        self._stream_factory = stream_factory

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        del timeout, local_address, socket_options
        self.calls.append((host, port))
        if self._stream_factory is None:
            return object()
        return self._stream_factory()

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options=None,
    ):
        raise AssertionError(f"unexpected Unix socket: {path}")

    async def sleep(self, seconds: float) -> None:
        del seconds


class _Http11Stream:
    def __init__(self) -> None:
        self.writes = bytearray()
        self.server_hostname: str | None = None
        self._response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 2\r\n"
            b"Connection: close\r\n\r\n{}"
        )

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        del max_bytes, timeout
        response, self._response = self._response, b""
        return response

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        del timeout
        self.writes.extend(buffer)

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self,
        ssl_context,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ):
        del ssl_context, timeout
        self.server_hostname = server_hostname
        return self

    def get_extra_info(self, info: str):
        del info
        return None


async def test_public_validation_then_private_rebind_never_opens_private_socket():
    host = "provider.example.test"
    resolutions = 0

    def rebound_after_checks(*_args):
        nonlocal resolutions
        resolutions += 1
        # Initial business validation and request-boundary validation both see
        # a public answer. The immediate pre-connect lookup is rebound to the
        # cloud metadata range and must never be handed to the socket backend.
        if resolutions <= 2:
            return _answers("8.8.8.8")
        return _answers("169.254.169.254")

    assert (
        await validate_operator_approved_url(
            f"https://{host}/v1/events",
            allowed_hosts={host},
            resolve=rebound_after_checks,
        )
        == f"https://{host}/v1/events"
    )

    recorder = _RecordingBackend()
    transport = PinnedPublicAsyncHTTPTransport(
        allowed_hosts={host},
        resolve=rebound_after_checks,
        backend=recorder,
    )
    async with httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        with pytest.raises(httpx.ConnectError, match="not public"):
            await client.get(f"https://{host}/v1/events")
    assert resolutions == 3
    assert recorder.calls == []


async def test_transport_pins_ip_but_preserves_http_host_and_tls_hostname():
    host = "provider.example.test"
    resolutions = 0

    def resolve(*_args):
        nonlocal resolutions
        resolutions += 1
        if resolutions <= 2:
            return _answers("8.8.4.4")
        return _answers("127.0.0.1")

    streams: list[_Http11Stream] = []

    def stream_factory() -> _Http11Stream:
        stream = _Http11Stream()
        streams.append(stream)
        return stream

    recorder = _RecordingBackend(stream_factory)
    transport = PinnedPublicAsyncHTTPTransport(
        allowed_hosts={host},
        resolve=resolve,
        backend=recorder,
    )
    async with httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        response = await client.get(f"https://{host}/health")
        assert response.status_code == 200
        with pytest.raises(httpx.ConnectError, match="approved public origin"):
            await client.get(f"https://{host}/second")

    assert resolutions == 3
    assert recorder.calls == [("8.8.4.4", 443)]
    assert len(streams) == 1
    assert streams[0].server_hostname == host
    assert b"Host: provider.example.test\r\n" in streams[0].writes


async def test_network_backend_supports_public_ipv6_and_blocks_private_ipv6():
    host = "provider.example.test"
    recorder = _RecordingBackend()

    def public_ipv6(*_args):
        return _answers("2001:4860:4860::8888")

    backend = PinnedPublicNetworkBackend(
        allowed_hosts={host},
        resolve=public_ipv6,
        backend=recorder,
    )
    await backend.connect_tcp(host, 443)
    assert recorder.calls == [("2001:4860:4860::8888", 443)]

    def link_local_ipv6(*_args):
        return _answers("fe80::1")

    rebound = PinnedPublicNetworkBackend(
        allowed_hosts={host},
        resolve=link_local_ipv6,
        backend=recorder,
    )
    with pytest.raises(httpcore.ConnectError, match="not public"):
        await rebound.connect_tcp(host, 443)
    assert recorder.calls == [("2001:4860:4860::8888", 443)]


async def test_mixed_public_and_private_dns_answers_fail_closed():
    def mixed_resolution(*_args):
        return _answers("8.8.8.8", "10.0.0.8")

    with pytest.raises(ProviderRequestError) as blocked:
        await validate_operator_approved_url(
            "https://provider.example.test/callback",
            allowed_hosts={"provider.example.test"},
            resolve=mixed_resolution,
        )
    assert blocked.value.code == "endpoint_not_allowed"


async def test_provider_client_never_follows_redirects_even_if_injected_client_would():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            headers={"location": "https://169.254.169.254/latest/meta-data"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        provider = ProviderHttpClient(client)
        with pytest.raises(ProviderRequestError) as rejected:
            await provider.request("GET", "https://provider.example.test/start")
        assert rejected.value.code == "provider_request_rejected"
        assert rejected.value.response_status == 302
        with pytest.raises(ProviderRequestError) as override:
            await provider.request(
                "GET",
                "https://provider.example.test/start",
                follow_redirects=True,
            )
        assert override.value.code == "provider_redirects_forbidden"
    assert len(requests) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://provider.example.test/path",
        "https://provider.example.test:8443/path",
        "https://other.example.test/path",
        "https://user:password@provider.example.test/path",
    ],
)
async def test_transport_rejects_non_exact_origins_before_dns_or_socket(url: str):
    dns_calls = 0

    def resolve(*_args):
        nonlocal dns_calls
        dns_calls += 1
        return _answers("8.8.8.8")

    recorder = _RecordingBackend()
    transport = PinnedPublicAsyncHTTPTransport(
        allowed_hosts={"provider.example.test"},
        resolve=resolve,
        backend=recorder,
    )
    try:
        with pytest.raises(httpx.ConnectError, match="approved public origin"):
            await transport.handle_async_request(httpx.Request("GET", url))
    finally:
        await transport.aclose()
    assert dns_calls == 0
    assert recorder.calls == []
