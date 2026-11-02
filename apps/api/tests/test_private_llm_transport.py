"""Private-model allowlist, DNS pinning, and redirect regressions."""

import asyncio
from types import SimpleNamespace
from typing import Any

import httpcore
import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.llm import ChatMessage
from app.llm.vllm import (
    PinnedAsyncHTTPTransport,
    PinnedModelEndpoint,
    PinnedNetworkBackend,
    VLLMClient,
)
from app.services.enterprise import (
    TransientModelEndpointResolutionError,
    UnsafeModelEndpoint,
    resolve_model_endpoint,
    validate_model_base_url,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_private_model_requires_an_exact_operator_allowlist(monkeypatch):
    monkeypatch.setenv(
        "LLM_PRIVATE_BASE_URL_ALLOWLIST", "http://model.internal:11434/v1"
    )
    get_settings.cache_clear()

    assert (
        await validate_model_base_url("http://model.internal:11434/v1")
        == "http://model.internal:11434/v1"
    )
    with pytest.raises(UnsafeModelEndpoint, match="operator-allowlisted"):
        await validate_model_base_url("https://public-model.example:443/v1")
    with pytest.raises(UnsafeModelEndpoint, match="operator-allowlisted"):
        await validate_model_base_url("https://evil-model.internal:443/v1")


async def test_private_model_allowlist_rejects_ambiguous_entries_and_rate_pairs():
    for value in (
        "model.internal",
        "https://model.internal/v1",
        "https://model.internal:8443",
        "https://model.internal:8443/v1/../admin",
        "https://model.internal:8443/v1%2fadmin",
    ):
        with pytest.raises(ValidationError, match=r"exact HTTP\(S\) base URLs"):
            Settings(llm_private_base_url_allowlist=value)

    with pytest.raises(ValidationError, match="must be configured together"):
        Settings(llm_private_prompt_usd_per_million=1.0)


async def test_origin_allowlist_is_scheme_and_port_exact(monkeypatch):
    monkeypatch.setenv(
        "LLM_PRIVATE_BASE_URL_ALLOWLIST", "https://model.example:8443/v1"
    )
    get_settings.cache_clear()

    assert (
        await validate_model_base_url("https://model.example:8443/v1")
        == "https://model.example:8443/v1"
    )
    with pytest.raises(UnsafeModelEndpoint, match="operator-allowlisted"):
        await validate_model_base_url("http://model.example:8443/v1")
    with pytest.raises(UnsafeModelEndpoint, match="operator-allowlisted"):
        await validate_model_base_url("https://model.example:9443/v1")
    with pytest.raises(UnsafeModelEndpoint, match="operator-allowlisted"):
        await validate_model_base_url("https://model.example:8443/v2")
    with pytest.raises(UnsafeModelEndpoint, match="explicit port"):
        await validate_model_base_url("https://model.example/v1")


async def test_allowlisted_hostname_cannot_resolve_to_metadata(monkeypatch):
    monkeypatch.setenv(
        "LLM_PRIVATE_BASE_URL_ALLOWLIST", "http://model.internal:11434/v1"
    )
    get_settings.cache_clear()

    async def metadata_resolution(_hostname: str, _port: int) -> frozenset[str]:
        return frozenset({"169.254.169.254"})

    monkeypatch.setattr(
        "app.llm.vllm.resolve_host_addresses",
        metadata_resolution,
    )
    with pytest.raises(UnsafeModelEndpoint, match="metadata"):
        await resolve_model_endpoint("http://model.internal:11434/v1")


async def test_allowlisted_hostname_resolution_oserror_is_transient(monkeypatch):
    monkeypatch.setenv(
        "LLM_PRIVATE_BASE_URL_ALLOWLIST", "http://model.internal:11434/v1"
    )
    get_settings.cache_clear()

    async def unavailable_resolution(_hostname: str, _port: int) -> frozenset[str]:
        raise OSError("resolver internals")

    monkeypatch.setattr(
        "app.llm.vllm.resolve_host_addresses",
        unavailable_resolution,
    )
    with pytest.raises(
        TransientModelEndpointResolutionError,
        match="temporarily unavailable",
    ):
        await resolve_model_endpoint("http://model.internal:11434/v1")


class _RecordingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.stream = object()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ):
        self.calls.append((host, port))
        return self.stream

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ):
        raise AssertionError("Unix sockets are not expected")

    async def sleep(self, seconds: float) -> None:
        return None


async def test_transport_connects_to_pinned_ip_and_rejects_dns_rebinding(monkeypatch):
    endpoint = PinnedModelEndpoint(
        base_url="http://model.internal:11434/v1",
        scheme="http",
        hostname="model.internal",
        port=11434,
        addresses=frozenset({"10.0.0.8"}),
    )
    recorder = _RecordingBackend()
    backend = PinnedNetworkBackend(endpoint, backend=recorder)

    async def stable_resolution(_hostname: str, _port: int) -> frozenset[str]:
        return frozenset({"10.0.0.8"})

    monkeypatch.setattr(
        "app.llm.vllm.resolve_host_addresses",
        stable_resolution,
    )
    assert await backend.connect_tcp("model.internal", 11434) is recorder.stream
    assert recorder.calls == [("10.0.0.8", 11434)]

    async def rebound_resolution(_hostname: str, _port: int) -> frozenset[str]:
        return frozenset({"169.254.169.254"})

    monkeypatch.setattr(
        "app.llm.vllm.resolve_host_addresses",
        rebound_resolution,
    )
    with pytest.raises(httpcore.ConnectError, match="DNS binding changed"):
        await backend.connect_tcp("model.internal", 11434)
    assert recorder.calls == [("10.0.0.8", 11434)]


async def test_vllm_client_disables_redirects_and_cross_origin_requests():
    endpoint = PinnedModelEndpoint(
        base_url="https://model.example/v1",
        scheme="https",
        hostname="model.example",
        port=443,
        addresses=frozenset({"10.0.0.8"}),
    )
    client = VLLMClient(endpoint=endpoint)
    await client._ensure_client()
    assert client._http_client is not None
    assert client._http_client.follow_redirects is False

    transport = PinnedAsyncHTTPTransport(endpoint)
    with pytest.raises(httpx.ConnectError, match="different origin"):
        await transport.handle_async_request(
            httpx.Request("POST", "https://metadata.invalid/latest")
        )
    await transport.aclose()
    await client.aclose()
    assert client._http_client is None
    assert client._client is None


async def test_vllm_client_closes_one_shared_pool_after_concurrent_calls(monkeypatch):
    entered = 0
    both_entered = asyncio.Event()
    release = asyncio.Event()
    http_clients = []

    class FakeHttpClient:
        def __init__(self, **_kwargs):
            self.closed = 0
            http_clients.append(self)

        async def aclose(self):
            self.closed += 1

    class FakeCompletions:
        async def create(self, **_kwargs):
            nonlocal entered
            entered += 1
            if entered == 2:
                both_entered.set()
            await release.wait()
            usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)
            message = SimpleNamespace(content="ok")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message)],
                usage=usage,
                model_dump=dict,
            )

    class FakeAsyncOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("app.llm.vllm.httpx.AsyncClient", FakeHttpClient)
    monkeypatch.setattr("app.llm.vllm.AsyncOpenAI", FakeAsyncOpenAI)
    endpoint = PinnedModelEndpoint(
        base_url="https://model.example:8443/v1",
        scheme="https",
        hostname="model.example",
        port=8443,
        addresses=frozenset({"10.0.0.8"}),
    )
    client = VLLMClient(endpoint=endpoint)
    messages = [ChatMessage(role="user", content="bounded sockets")]
    first = asyncio.create_task(client.complete(messages))
    second = asyncio.create_task(client.complete(messages))
    await asyncio.wait_for(both_entered.wait(), timeout=1)

    assert len(http_clients) == 1
    assert http_clients[0].closed == 0
    release.set()
    await asyncio.gather(first, second)

    assert http_clients[0].closed == 1
    assert client._http_client is None
    assert client._client is None

    for _ in range(25):
        await client.complete(messages)
    assert len(http_clients) == 26
    assert all(item.closed == 1 for item in http_clients)
    assert client._active_requests == 0
    assert client._http_client is None
