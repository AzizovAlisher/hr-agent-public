"""Tenant metering and protocol enforcement for every model provider."""

from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import ROUND_CEILING, Decimal
from time import perf_counter
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal, set_tenant_context
from app.llm.base import (
    ChatMessage,
    CompletionResult,
    LLMClient,
    LogContext,
    StreamCompletionEvent,
)
from app.models.operations import UsageLedgerEntry
from app.services.entitlements import record_usage


class LLMProtocolError(RuntimeError):
    """A provider response violated the gateway's bounded usage contract."""


class LLMConfigurationError(RuntimeError):
    """Tenant/provider policy prevents an AI call before any data leaves."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


class LLMModelBudgetExceeded(RuntimeError):
    """A mission cannot reserve the provider's worst-case spend."""

    def __init__(
        self,
        *,
        limit_microusd: int,
        used_microusd: int,
        requested_microusd: int,
    ):
        self.limit_microusd = limit_microusd
        self.used_microusd = used_microusd
        self.requested_microusd = requested_microusd
        super().__init__("Mission model budget would be exceeded")


TerminalState = Literal["settled", "refunded", "disputed", "unknown"]
LedgerOperation = Callable[[AsyncSession], Awaitable[None]]


class BudgetedLLMClient:
    """Durably reserve limits, call one provider, then settle exact usage.

    Reservations commit in an independent transaction before provider I/O. A
    process crash therefore leaves a conservative unmatched reservation rather
    than restoring spend capacity after an upstream may already have charged.
    Terminal charge/release rows commit atomically and share one stable call ID.

    Input tokenization differs by model. UTF-8 bytes are a conservative upper
    bound for byte-backed tokenizers; fixed message overhead and an output cap
    keep the token reservation fail-closed. Managed cost uses the exact
    OpenRouter ceilings. Private-provider currency budgets require paired,
    operator-configured conservative rates; otherwise a positive budget is
    rejected before any prompt leaves the deployment.
    """

    def __init__(
        self,
        client: LLMClient,
        *,
        db: AsyncSession,
        organization_id: UUID,
        provider: str,
        managed: bool,
        budget_resource_id: UUID | None = None,
        model_budget_cents: int | None = None,
    ) -> None:
        self._client = client
        # Domain logging remains owned by the caller's session. Usage metering
        # deliberately does not: it must survive a caller rollback or crash.
        self._db = db
        self._organization_id = organization_id
        self._provider = provider
        self._managed = managed
        self._budget_resource_id = budget_resource_id
        self._model_budget_microusd = (
            None if model_budget_cents is None else model_budget_cents * 10_000
        )
        self._settings = get_settings()
        if model_budget_cents is not None and model_budget_cents < 0:
            raise ValueError("model_budget_cents must be non-negative")
        if model_budget_cents is not None and budget_resource_id is None:
            raise ValueError("A model budget requires a budget resource")

        if managed:
            self._prompt_rate = Decimal(
                str(self._settings.openrouter_max_prompt_usd_per_million)
            )
            self._completion_rate = Decimal(
                str(self._settings.openrouter_max_completion_usd_per_million)
            )
            self._meter_cost = True
        else:
            private_prompt_rate = self._settings.llm_private_prompt_usd_per_million
            private_completion_rate = (
                self._settings.llm_private_completion_usd_per_million
            )
            self._meter_cost = (
                private_prompt_rate is not None and private_completion_rate is not None
            )
            self._prompt_rate = (
                Decimal(str(private_prompt_rate)) if self._meter_cost else None
            )
            self._completion_rate = (
                Decimal(str(private_completion_rate)) if self._meter_cost else None
            )
            if (
                model_budget_cents is not None
                and model_budget_cents > 0
                and not self._meter_cost
            ):
                raise LLMConfigurationError(
                    "ai_private_budget_accounting_unavailable",
                    "Positive private-model budgets require operator-configured "
                    "conservative prompt and completion rates",
                )

    def _limits(
        self, messages: list[ChatMessage], max_tokens: int | None
    ) -> tuple[int, int, int | None]:
        requested_output = max_tokens or self._settings.llm_default_max_output_tokens
        if requested_output <= 0:
            raise ValueError("max_tokens must be positive")
        output_limit = min(
            requested_output, self._settings.llm_default_max_output_tokens
        )
        prompt_upper_bound = (
            sum(len(item.content.encode("utf-8")) for item in messages)
            + 64 * len(messages)
            + 2_048
        )
        token_reservation = prompt_upper_bound + output_limit
        if not self._meter_cost:
            return output_limit, token_reservation, None
        assert self._prompt_rate is not None
        assert self._completion_rate is not None
        # USD / million tokens numerically equals microusd / token.
        cost_reservation = int(
            (
                Decimal(prompt_upper_bound) * self._prompt_rate
                + Decimal(output_limit) * self._completion_rate
            ).to_integral_value(rounding=ROUND_CEILING)
        )
        return output_limit, token_reservation, max(1, cost_reservation)

    def _accounted_private_cost(self, *, tokens_in: int, tokens_out: int) -> int:
        if not self._meter_cost or self._managed:
            raise ValueError("Private cost accounting is not configured")
        assert self._prompt_rate is not None
        assert self._completion_rate is not None
        return max(
            1,
            int(
                (
                    Decimal(tokens_in) * self._prompt_rate
                    + Decimal(tokens_out) * self._completion_rate
                ).to_integral_value(rounding=ROUND_CEILING)
            ),
        )

    @staticmethod
    def _key(call_id: UUID, metric: str, event: str) -> str:
        return f"llm:{call_id}:{metric}:{event}"

    @property
    def _resource_type(self) -> str:
        return "hiring_mission" if self._budget_resource_id is not None else "llm_call"

    def _resource_id(self, call_id: UUID) -> UUID:
        return self._budget_resource_id or call_id

    async def _ledger_transaction(self, operation: LedgerOperation) -> None:
        async with SessionLocal() as ledger_db:
            try:
                await set_tenant_context(
                    ledger_db,
                    organization_id=self._organization_id,
                    identity_kind="system",
                )
                await operation(ledger_db)
                await ledger_db.commit()
            except BaseException:
                await ledger_db.rollback()
                raise

    async def _lock_call(self, db: AsyncSession, call_id: UUID) -> None:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"llm-call:{self._organization_id}:{call_id}"},
        )

    async def _record(
        self,
        db: AsyncSession,
        *,
        call_id: UUID,
        metric: str,
        amount: int,
        kind: str,
        event: str,
        metadata: dict,
        enforce_limit: bool,
    ) -> None:
        await record_usage(
            db,
            organization_id=self._organization_id,
            metric=metric,
            amount=amount,
            kind=kind,
            idempotency_key=self._key(call_id, metric, event),
            resource_type=self._resource_type,
            resource_id=self._resource_id(call_id),
            metadata=metadata,
            enforce_limit=enforce_limit,
        )

    async def _reservation_exists(
        self,
        db: AsyncSession,
        *,
        call_id: UUID,
        token_reservation: int,
        cost_reservation: int | None,
        metadata: dict,
    ) -> bool:
        token_row = (
            await db.execute(
                select(UsageLedgerEntry).where(
                    UsageLedgerEntry.organization_id == self._organization_id,
                    UsageLedgerEntry.metric == "ai_tokens",
                    UsageLedgerEntry.idempotency_key
                    == self._key(call_id, "ai_tokens", "reserve"),
                )
            )
        ).scalar_one_or_none()
        cost_row = None
        if cost_reservation is not None:
            cost_row = (
                await db.execute(
                    select(UsageLedgerEntry).where(
                        UsageLedgerEntry.organization_id == self._organization_id,
                        UsageLedgerEntry.metric == "ai_cost_microusd",
                        UsageLedgerEntry.idempotency_key
                        == self._key(call_id, "ai_cost_microusd", "reserve"),
                    )
                )
            ).scalar_one_or_none()
        if token_row is None and cost_row is None:
            return False
        if token_row is None or (cost_reservation is not None and cost_row is None):
            raise LLMProtocolError("LLM reservation ledger is incomplete")
        # Reuse record_usage's strict idempotency validation rather than
        # duplicating the immutable-row comparison contract here.
        await self._record(
            db,
            call_id=call_id,
            metric="ai_tokens",
            amount=token_reservation,
            kind="reserve",
            event="reserve",
            metadata=metadata,
            enforce_limit=True,
        )
        if cost_reservation is not None:
            await self._record(
                db,
                call_id=call_id,
                metric="ai_cost_microusd",
                amount=cost_reservation,
                kind="reserve",
                event="reserve",
                metadata=metadata,
                enforce_limit=True,
            )
        return True

    async def _enforce_model_budget(
        self,
        db: AsyncSession,
        *,
        requested_microusd: int | None,
    ) -> None:
        limit = self._model_budget_microusd
        if limit is None:
            return
        resource_id = self._budget_resource_id
        if resource_id is None:  # guarded by __init__
            raise ValueError("A model budget requires a budget resource")
        if limit == 0:
            raise LLMModelBudgetExceeded(
                limit_microusd=0,
                used_microusd=0,
                requested_microusd=0,
            )
        if requested_microusd is None:
            raise LLMConfigurationError(
                "ai_private_budget_accounting_unavailable",
                "Positive private-model budgets require conservative accounting",
            )
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"llm-mission-budget:{self._organization_id}:{resource_id}"},
        )
        used = int(
            (
                await db.execute(
                    select(func.coalesce(func.sum(UsageLedgerEntry.amount), 0)).where(
                        UsageLedgerEntry.organization_id == self._organization_id,
                        UsageLedgerEntry.metric == "ai_cost_microusd",
                        UsageLedgerEntry.resource_type == "hiring_mission",
                        UsageLedgerEntry.resource_id == resource_id,
                    )
                )
            ).scalar_one()
        )
        used = max(0, used)
        if used + requested_microusd > limit:
            raise LLMModelBudgetExceeded(
                limit_microusd=limit,
                used_microusd=used,
                requested_microusd=requested_microusd,
            )

    async def _reserve(
        self,
        *,
        call_id: UUID,
        token_reservation: int,
        cost_reservation: int | None,
        metadata: dict,
    ) -> None:
        reservation_metadata = {
            **metadata,
            "reservation_state": "reserved",
        }

        async def reserve(db: AsyncSession) -> None:
            await self._lock_call(db, call_id)
            if await self._reservation_exists(
                db,
                call_id=call_id,
                token_reservation=token_reservation,
                cost_reservation=cost_reservation,
                metadata=reservation_metadata,
            ):
                return
            await self._enforce_model_budget(
                db,
                requested_microusd=cost_reservation,
            )
            await self._record(
                db,
                call_id=call_id,
                metric="ai_tokens",
                amount=token_reservation,
                kind="reserve",
                event="reserve",
                metadata=reservation_metadata,
                enforce_limit=True,
            )
            if cost_reservation is not None:
                await self._record(
                    db,
                    call_id=call_id,
                    metric="ai_cost_microusd",
                    amount=cost_reservation,
                    kind="reserve",
                    event="reserve",
                    metadata=reservation_metadata,
                    enforce_limit=True,
                )

        await self._ledger_transaction(reserve)

    async def _terminal_transition(
        self,
        *,
        call_id: UUID,
        state: TerminalState,
        token_reservation: int,
        cost_reservation: int | None,
        token_charge: int | None,
        cost_charge: int | None,
        metadata: dict,
    ) -> None:
        if token_charge is not None and not 0 < token_charge <= token_reservation:
            raise ValueError("Invalid terminal token charge")
        if cost_charge is not None and (
            cost_reservation is None or not 0 < cost_charge <= cost_reservation
        ):
            raise ValueError("Invalid terminal cost charge")
        terminal_metadata = {
            **metadata,
            "reservation_state": state,
            "terminal": True,
        }

        async def transition(db: AsyncSession) -> None:
            await self._lock_call(db, call_id)
            if token_charge is not None:
                await self._record(
                    db,
                    call_id=call_id,
                    metric="ai_tokens",
                    amount=token_charge,
                    kind="settle",
                    event="terminal-charge",
                    metadata=terminal_metadata,
                    enforce_limit=False,
                )
            await self._record(
                db,
                call_id=call_id,
                metric="ai_tokens",
                amount=-token_reservation,
                kind="refund",
                event="terminal-release",
                metadata=terminal_metadata,
                enforce_limit=False,
            )
            if cost_reservation is None:
                return
            if cost_charge is not None:
                await self._record(
                    db,
                    call_id=call_id,
                    metric="ai_cost_microusd",
                    amount=cost_charge,
                    kind="settle",
                    event="terminal-charge",
                    metadata=terminal_metadata,
                    enforce_limit=False,
                )
            await self._record(
                db,
                call_id=call_id,
                metric="ai_cost_microusd",
                amount=-cost_reservation,
                kind="refund",
                event="terminal-release",
                metadata=terminal_metadata,
                enforce_limit=False,
            )

        await self._ledger_transaction(transition)

    async def _refund(
        self,
        *,
        call_id: UUID,
        token_reservation: int,
        cost_reservation: int | None,
        metadata: dict,
    ) -> None:
        """Release a reservation only when the caller proves no external effect."""

        await self._terminal_transition(
            call_id=call_id,
            state="refunded",
            token_reservation=token_reservation,
            cost_reservation=cost_reservation,
            token_charge=None,
            cost_charge=None,
            metadata=metadata,
        )

    async def _retain_reservation(
        self,
        *,
        call_id: UUID,
        state: Literal["disputed", "unknown"],
        token_reservation: int,
        cost_reservation: int | None,
        metadata: dict,
    ) -> None:
        # A terminal charge and matching release make the state inspectable
        # while preserving the original conservative reservation in net usage.
        await self._terminal_transition(
            call_id=call_id,
            state=state,
            token_reservation=token_reservation,
            cost_reservation=cost_reservation,
            token_charge=token_reservation,
            cost_charge=cost_reservation,
            metadata=metadata,
        )

    async def _settle(
        self,
        *,
        call_id: UUID,
        token_reservation: int,
        cost_reservation: int | None,
        tokens_in: int,
        tokens_out: int,
        cost_microusd: int | None,
        metadata: dict,
    ) -> None:
        protocol_error: str | None = None
        if tokens_in < 0 or tokens_out < 0:
            protocol_error = "Provider token usage was negative"
        tokens = tokens_in + tokens_out
        if protocol_error is None and tokens > token_reservation:
            protocol_error = "Provider token usage exceeded the reserved bound"

        accounted_tokens = tokens if tokens > 0 else token_reservation
        accounted_cost: int | None = None
        estimated_cost = cost_reservation is not None and not self._managed
        if cost_reservation is not None:
            if self._managed:
                if cost_microusd is None:
                    accounted_cost = cost_reservation
                    estimated_cost = True
                elif cost_microusd < 0:
                    protocol_error = protocol_error or "Provider cost was negative"
                else:
                    accounted_cost = cost_microusd
            elif tokens > 0 and protocol_error is None:
                accounted_cost = self._accounted_private_cost(
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                )
            else:
                accounted_cost = cost_reservation
                estimated_cost = True
            if accounted_cost is not None and accounted_cost > cost_reservation:
                protocol_error = (
                    protocol_error
                    or "Provider cost exceeded the reserved price ceiling"
                )

        if protocol_error is not None:
            await self._retain_reservation(
                call_id=call_id,
                state="disputed",
                token_reservation=token_reservation,
                cost_reservation=cost_reservation,
                metadata={**metadata, "reason": "provider_usage_out_of_bounds"},
            )
            raise LLMProtocolError(protocol_error)

        await self._terminal_transition(
            call_id=call_id,
            state="settled",
            token_reservation=token_reservation,
            cost_reservation=cost_reservation,
            token_charge=accounted_tokens,
            # A genuinely free managed response needs no positive charge row;
            # the release row still records the terminal settled state.
            cost_charge=accounted_cost
            if accounted_cost and accounted_cost > 0
            else None,
            metadata={
                **metadata,
                "tokens_estimated": tokens <= 0,
                "cost_estimated": estimated_cost,
            },
        )

    def _metadata(
        self,
        *,
        call_id: UUID,
        requested_model: str | None,
        log: LogContext | None,
        output_limit: int,
    ) -> dict:
        return {
            "call_id": str(call_id),
            "provider": self._provider,
            "managed": self._managed,
            "cost_accounting": (
                "managed_reported_or_ceiling"
                if self._managed
                else ("private_configured_rates" if self._meter_cost else "unmetered")
            ),
            "prompt_rate_usd_per_million": (
                str(self._prompt_rate) if self._meter_cost else None
            ),
            "completion_rate_usd_per_million": (
                str(self._completion_rate) if self._meter_cost else None
            ),
            "requested_model": requested_model,
            "purpose": log.purpose if log is not None else "unspecified",
            "prompt_version": log.prompt_version if log is not None else None,
            "max_output_tokens": output_limit,
            "budget_scope": (
                str(self._budget_resource_id)
                if self._budget_resource_id is not None
                else None
            ),
        }

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        log: LogContext | None = None,
    ) -> CompletionResult:
        output_limit, token_reservation, cost_reservation = self._limits(
            messages, max_tokens
        )
        call_id = uuid4()
        metadata = self._metadata(
            call_id=call_id,
            requested_model=model,
            log=log,
            output_limit=output_limit,
        )
        await self._reserve(
            call_id=call_id,
            token_reservation=token_reservation,
            cost_reservation=cost_reservation,
            metadata=metadata,
        )
        started = perf_counter()
        try:
            result = await self._client.complete(
                messages,
                model=model,
                temperature=temperature,
                max_tokens=output_limit,
                log=log,
            )
        except Exception:
            # An exception after handing bytes to a provider is not proof that
            # no inference or charge occurred. Retain the reservation until an
            # operator/provider reconciliation can establish the outcome.
            await self._retain_reservation(
                call_id=call_id,
                state="unknown",
                token_reservation=token_reservation,
                cost_reservation=cost_reservation,
                metadata={**metadata, "reason": "provider_outcome_unknown"},
            )
            raise
        result.latency_ms = max(0, int((perf_counter() - started) * 1_000))
        await self._settle(
            call_id=call_id,
            token_reservation=token_reservation,
            cost_reservation=cost_reservation,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_microusd=result.cost_microusd,
            metadata={
                **metadata,
                "model": result.model_used,
                "provider_used": result.provider_used,
                "latency_ms": result.latency_ms,
                "fallback_from": result.fallback_from,
            },
        )
        return result

    async def stream_complete_events(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        log: LogContext | None = None,
    ) -> AsyncIterator[StreamCompletionEvent]:
        output_limit, token_reservation, cost_reservation = self._limits(
            messages, max_tokens
        )
        call_id = uuid4()
        metadata = self._metadata(
            call_id=call_id,
            requested_model=model,
            log=log,
            output_limit=output_limit,
        )
        await self._reserve(
            call_id=call_id,
            token_reservation=token_reservation,
            cost_reservation=cost_reservation,
            metadata=metadata,
        )
        started = perf_counter()
        terminal: StreamCompletionEvent | None = None
        try:
            async for event in self._client.stream_complete_events(
                messages,
                model=model,
                temperature=temperature,
                max_tokens=output_limit,
                log=log,
            ):
                if event.type == "delta":
                    if terminal is not None:
                        raise LLMProtocolError("Provider emitted data after completion")
                    yield event
                elif terminal is None:
                    terminal = event
                else:
                    raise LLMProtocolError("Provider emitted multiple terminal events")
            if terminal is None:
                raise LLMProtocolError("Provider stream ended without completion")
        except Exception:
            await self._retain_reservation(
                call_id=call_id,
                state="unknown",
                token_reservation=token_reservation,
                cost_reservation=cost_reservation,
                metadata={**metadata, "reason": "provider_or_protocol_outcome_unknown"},
            )
            raise
        terminal.latency_ms = max(0, int((perf_counter() - started) * 1_000))
        await self._settle(
            call_id=call_id,
            token_reservation=token_reservation,
            cost_reservation=cost_reservation,
            tokens_in=terminal.tokens_in,
            tokens_out=terminal.tokens_out,
            cost_microusd=terminal.cost_microusd,
            metadata={
                **metadata,
                "model": terminal.model_used,
                "provider_used": terminal.provider_used,
                "latency_ms": terminal.latency_ms,
                "fallback_from": terminal.fallback_from,
            },
        )
        yield terminal

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
