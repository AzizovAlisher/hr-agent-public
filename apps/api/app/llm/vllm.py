import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

import httpcore
import httpx
from openai import AsyncOpenAI

from app.config import get_settings
from app.llm.base import (
    ChatMessage,
    CompletionResult,
    LogContext,
    StreamCompletionEvent,
)
from app.llm.logging import log_completion

if TYPE_CHECKING:
    from httpcore import AsyncNetworkStream


@dataclass(frozen=True, slots=True)
class PinnedModelEndpoint:
    """One operator-approved endpoint and its immutable DNS address set."""

    base_url: str
    scheme: str
    hostname: str
    port: int
    addresses: frozenset[str]


async def resolve_host_addresses(hostname: str, port: int) -> frozenset[str]:
    """Resolve every address used to pin a private model connection."""

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        records = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            port,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )
        return frozenset(str(ipaddress.ip_address(record[4][0])) for record in records)
    return frozenset({str(literal)})


class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect to a validated IP while retaining the original HTTP Host/TLS SNI.

    The address set is resolved again immediately before opening each new
    socket. Any drift fails closed, and the socket is opened against the pinned
    address rather than asking the operating system to resolve the hostname a
    second time. This removes the DNS-rebinding time-of-check/time-of-use gap.
    """

    def __init__(
        self,
        endpoint: PinnedModelEndpoint,
        *,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> "AsyncNetworkStream":
        requested_host = host.rstrip(".").encode("idna").decode("ascii").lower()
        if requested_host != self._endpoint.hostname or port != self._endpoint.port:
            raise httpcore.ConnectError(
                "Private model transport rejected a different origin"
            )
        try:
            current_addresses = await resolve_host_addresses(requested_host, port)
        except OSError as exc:
            raise httpcore.ConnectError(
                "Private model endpoint DNS resolution failed"
            ) from exc
        if current_addresses != self._endpoint.addresses:
            raise httpcore.ConnectError("Private model endpoint DNS binding changed")
        pinned_address = min(self._endpoint.addresses)
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
            "Private model transport does not permit Unix sockets"
        )

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """HTTP transport restricted to one DNS-pinned model origin."""

    def __init__(self, endpoint: PinnedModelEndpoint) -> None:
        super().__init__(trust_env=False, retries=0)
        self._endpoint = endpoint
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            max_connections=20,
            max_keepalive_connections=10,
            keepalive_expiry=5.0,
            retries=0,
            network_backend=PinnedNetworkBackend(endpoint),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        hostname = (
            (request.url.host or "").rstrip(".").encode("idna").decode("ascii").lower()
        )
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        if (
            request.url.scheme.lower() != self._endpoint.scheme
            or hostname != self._endpoint.hostname
            or port != self._endpoint.port
        ):
            raise httpx.ConnectError(
                "Private model transport rejected a different origin",
                request=request,
            )
        return await super().handle_async_request(request)


class VLLMClient:
    """Client for private OpenAI-compatible endpoints with bounded socket life.

    Organization routing creates short-lived instances. The client therefore
    closes its HTTP pool automatically when the last concurrent request exits;
    callers that explicitly pre-warm through ``_ensure_client`` can use
    ``aclose``. A later request safely creates a fresh DNS-pinned pool.
    """

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:8001/v1",
        default_model: str = "default",
        endpoint: PinnedModelEndpoint | None = None,
    ) -> None:
        settings = get_settings()
        self._base_url = (endpoint.base_url if endpoint else base_url).rstrip("/")
        self._endpoint = endpoint
        self._client: AsyncOpenAI | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._active_requests = 0
        self._timeout = getattr(settings, "llm_http_timeout_seconds", 90)
        self._default_model = default_model
        self._provider_name = "private_openai_compatible"

    async def _ensure_client(self) -> AsyncOpenAI:
        async with self._client_lock:
            return await self._ensure_client_locked()

    async def _ensure_client_locked(self) -> AsyncOpenAI:
        if self._client is not None:
            return self._client
        if self._endpoint is None:
            # Direct/global construction follows the same operator allowlist
            # and DNS-pinning path as tenant configuration.
            from app.services.enterprise import resolve_model_endpoint

            self._endpoint = await resolve_model_endpoint(self._base_url)
        self._http_client = httpx.AsyncClient(
            transport=PinnedAsyncHTTPTransport(self._endpoint),
            follow_redirects=False,
            trust_env=False,
            timeout=self._timeout,
        )
        self._client = AsyncOpenAI(
            api_key="not-required",
            base_url=self._endpoint.base_url,
            timeout=self._timeout,
            max_retries=0,
            http_client=self._http_client,
        )
        return self._client

    async def _acquire_client(self) -> AsyncOpenAI:
        async with self._client_lock:
            client = await self._ensure_client_locked()
            self._active_requests += 1
            return client

    async def _release_client(self) -> None:
        http_client: httpx.AsyncClient | None = None
        async with self._client_lock:
            if self._active_requests < 1:
                raise RuntimeError("Private model client request counter underflow")
            self._active_requests -= 1
            if self._active_requests == 0:
                http_client = self._http_client
                self._client = None
                self._http_client = None
        if http_client is not None:
            await http_client.aclose()

    async def aclose(self) -> None:
        """Close an idle pool now; active pools close when their last call exits."""

        http_client: httpx.AsyncClient | None = None
        async with self._client_lock:
            if self._active_requests:
                return
            http_client = self._http_client
            self._client = None
            self._http_client = None
        if http_client is not None:
            await http_client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        await self.aclose()

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        log: LogContext | None = None,
    ) -> CompletionResult:
        chosen_model = model or self._default_model
        client = await self._acquire_client()
        try:
            response = await client.chat.completions.create(
                model=chosen_model,
                messages=[
                    {"role": item.role, "content": item.content} for item in messages
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            content = response.choices[0].message.content or ""
            usage = response.usage
            result = CompletionResult(
                content=content,
                model_used=chosen_model,
                tokens_in=getattr(usage, "prompt_tokens", 0) if usage else 0,
                tokens_out=getattr(usage, "completion_tokens", 0) if usage else 0,
                provider_used=self._provider_name,
                raw=response.model_dump() if hasattr(response, "model_dump") else {},
            )
            if log is not None:
                prompt_text = "\n\n".join(
                    f"[{item.role}] {item.content}" for item in messages
                )
                await log_completion(log, result, prompt_text)
            return result
        finally:
            await self._release_client()

    async def stream_complete(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        log: LogContext | None = None,
    ) -> AsyncIterator[str]:
        async for event in self.stream_complete_events(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            log=log,
        ):
            if event.type == "delta":
                yield event.content

    async def stream_complete_events(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        log: LogContext | None = None,
    ) -> AsyncIterator[StreamCompletionEvent]:
        chosen_model = model or self._default_model
        full = ""
        usage = None
        reported_model = chosen_model
        client = await self._acquire_client()
        try:
            stream = await client.chat.completions.create(
                model=chosen_model,
                messages=[
                    {"role": item.role, "content": item.content} for item in messages
                ],
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                stream_options={"include_usage": True},
            )
            async for chunk in stream:
                if getattr(chunk, "model", None):
                    reported_model = chunk.model
                if chunk.choices and chunk.choices[0].delta.content:
                    token = chunk.choices[0].delta.content
                    full += token
                    yield StreamCompletionEvent(type="delta", content=token)
                if getattr(chunk, "usage", None) is not None:
                    usage = chunk.usage
            result = CompletionResult(
                content=full,
                model_used=reported_model,
                tokens_in=getattr(usage, "prompt_tokens", 0) if usage else 0,
                tokens_out=getattr(usage, "completion_tokens", 0) if usage else 0,
                provider_used=self._provider_name,
            )
            if log is not None:
                prompt_text = "\n\n".join(
                    f"[{item.role}] {item.content}" for item in messages
                )
                await log_completion(log, result, prompt_text)
            yield StreamCompletionEvent(
                type="done",
                model_used=result.model_used,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                provider_used=result.provider_used,
            )
        finally:
            await self._release_client()
