from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Role = Literal["system", "user", "assistant"]


@dataclass
class ChatMessage:
    role: Role
    content: str


@dataclass
class CompletionResult:
    content: str
    model_used: str
    tokens_in: int = 0
    tokens_out: int = 0
    provider_used: str | None = None
    cost_microusd: int | None = None
    latency_ms: int | None = None
    fallback_from: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamCompletionEvent:
    """A provider-neutral streaming event.

    Providers emit zero or more ``delta`` events followed by exactly one
    ``done`` event carrying the model and usage reported by the provider.
    """

    type: Literal["delta", "done"]
    content: str = ""
    model_used: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    provider_used: str | None = None
    cost_microusd: int | None = None
    latency_ms: int | None = None
    fallback_from: str | None = None


@dataclass
class LogContext:
    """Pass into LLMClient.complete to auto-log the call to the messages table."""

    db: Any  # AsyncSession (untyped to avoid circular import)
    organization_id: Any  # UUID — every message belongs to an org
    session_id: Any | None = None
    purpose: str = "chat"
    request_role: Role = "user"
    prompt_version: str | None = None


class LLMClient(Protocol):
    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        log: LogContext | None = None,
    ) -> CompletionResult: ...

    async def stream_complete(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        log: LogContext | None = None,
    ) -> AsyncIterator[str]: ...

    async def stream_complete_events(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        log: LogContext | None = None,
    ) -> AsyncIterator[StreamCompletionEvent]: ...
