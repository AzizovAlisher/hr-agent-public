from collections.abc import AsyncIterator
from decimal import ROUND_CEILING, Decimal, InvalidOperation

from openai import AsyncOpenAI

from app.config import get_settings
from app.llm.base import (
    ChatMessage,
    CompletionResult,
    LogContext,
    StreamCompletionEvent,
)
from app.llm.logging import log_completion


class OpenRouterClient:
    def __init__(
        self,
        *,
        default_model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        s = get_settings()
        if not s.openrouter_api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set")
        self._client = AsyncOpenAI(
            api_key=s.openrouter_api_key,
            base_url=base_url or s.openrouter_base_url,
            timeout=getattr(s, "llm_http_timeout_seconds", 90),
            # Durable workers own retry policy. Hidden SDK retries can outlive
            # a queue lease and make provider spend impossible to reconcile.
            max_retries=0,
        )
        self._default_model = default_model or s.openrouter_default_model
        self._default_max_tokens = s.llm_default_max_output_tokens
        self._provider_policy = {
            "sort": "price",
            "data_collection": "deny",
            "zdr": s.openrouter_require_zdr,
            "max_price": {
                "prompt": s.openrouter_max_prompt_usd_per_million,
                "completion": s.openrouter_max_completion_usd_per_million,
                "request": 0,
            },
        }

    @staticmethod
    def _cost_microusd(usage) -> int | None:
        if usage is None:
            return None
        value = getattr(usage, "cost", None)
        if value is None and getattr(usage, "model_extra", None):
            value = usage.model_extra.get("cost")
        if value is None:
            return None
        try:
            cost = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        if not cost.is_finite() or cost < 0:
            return None
        return int(
            (cost * Decimal(1_000_000)).to_integral_value(rounding=ROUND_CEILING)
        )

    @staticmethod
    def _provider(response, fallback: str = "openrouter") -> str:
        value = getattr(response, "provider", None)
        if value is None and getattr(response, "model_extra", None):
            value = response.model_extra.get("provider")
        return str(value)[:128] if value else fallback

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
        rsp = await self._client.chat.completions.create(
            model=chosen_model,
            messages=[{"role": m.role, "content": m.content} for m in messages],
            temperature=temperature,
            max_tokens=max_tokens or self._default_max_tokens,
            extra_body={"provider": self._provider_policy},
        )
        choice = rsp.choices[0].message.content or ""
        usage = rsp.usage
        result = CompletionResult(
            content=choice,
            model_used=getattr(rsp, "model", None) or chosen_model,
            tokens_in=getattr(usage, "prompt_tokens", 0) if usage else 0,
            tokens_out=getattr(usage, "completion_tokens", 0) if usage else 0,
            provider_used=self._provider(rsp),
            cost_microusd=self._cost_microusd(usage),
            raw=rsp.model_dump() if hasattr(rsp, "model_dump") else {},
        )
        if log is not None:
            prompt_text = "\n\n".join(f"[{m.role}] {m.content}" for m in messages)
            await log_completion(log, result, prompt_text)
        return result

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
        stream = await self._client.chat.completions.create(
            model=chosen_model,
            messages=[{"role": m.role, "content": m.content} for m in messages],
            temperature=temperature,
            max_tokens=max_tokens or self._default_max_tokens,
            stream=True,
            extra_body={"provider": self._provider_policy},
        )
        usage = None
        reported_model = chosen_model
        reported_provider = "openrouter"
        async for chunk in stream:
            if getattr(chunk, "model", None):
                reported_model = chunk.model
            reported_provider = self._provider(chunk, reported_provider)
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
            provider_used=reported_provider,
            cost_microusd=self._cost_microusd(usage),
        )
        if log is not None:
            prompt_text = "\n\n".join(f"[{m.role}] {m.content}" for m in messages)
            await log_completion(log, result, prompt_text)
        yield StreamCompletionEvent(
            type="done",
            model_used=result.model_used,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            provider_used=result.provider_used,
            cost_microusd=result.cost_microusd,
        )
