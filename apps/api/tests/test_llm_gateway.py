from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal
from app.llm import ChatMessage, CompletionResult, StreamCompletionEvent
from app.llm.gateway import (
    BudgetedLLMClient,
    LLMConfigurationError,
    LLMModelBudgetExceeded,
    LLMProtocolError,
)
from app.models.operations import OrganizationEntitlement, UsageLedgerEntry
from app.models.organization import Organization
from app.services.entitlements import EntitlementExceeded

pytestmark = pytest.mark.asyncio


async def test_operator_position_scripts_require_tenant_gateway() -> None:
    scripts_root = Path(__file__).resolve().parents[1] / "app" / "scripts"
    for name in (
        "condense_descriptions.py",
        "extract_requirements_for_imports.py",
    ):
        source = (scripts_root / name).read_text(encoding="utf-8")
        assert "organization_llm_client" in source
        assert "untrusted_candidate_block" in source
        assert "--organization-id" in source
        assert "get_llm_client" not in source
        assert "00000000-0000-0000-0000-000000000001" not in source


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _organization(db, suffix: str) -> Organization:
    row = Organization(
        name=f"Gateway {suffix}", slug=f"gateway-{suffix}-{uuid4().hex[:8]}"
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _limit(db, organization_id, metric: str, value: int | None) -> None:
    db.add(
        OrganizationEntitlement(
            organization_id=organization_id,
            metric=metric,
            limit_value=value,
            period="contract",
        )
    )
    await db.commit()


async def test_gateway_reserves_settles_and_never_logs_prompt_content(db):
    organization = await _organization(db, "settle")
    await _limit(db, organization.id, "ai_tokens", 20_000)
    await _limit(db, organization.id, "ai_cost_microusd", 100_000)

    class FakeManaged:
        async def complete(self, _messages, **_kwargs):
            return CompletionResult(
                content="safe output",
                model_used="managed/model",
                tokens_in=10,
                tokens_out=5,
                provider_used="managed-upstream",
                cost_microusd=123,
            )

    gateway = BudgetedLLMClient(
        FakeManaged(),
        db=db,
        organization_id=organization.id,
        provider="openrouter",
        managed=True,
    )
    canary = "CV-SECRET-CANARY-DO-NOT-STORE"
    result = await gateway.complete(
        [ChatMessage(role="user", content=canary)], max_tokens=64
    )
    assert result.cost_microusd == 123
    assert result.latency_ms is not None
    # The usage effect is independent from the caller's domain transaction.
    await db.rollback()

    async with SessionLocal() as ledger_db:
        totals = dict(
            (
                await ledger_db.execute(
                    select(
                        UsageLedgerEntry.metric,
                        func.sum(UsageLedgerEntry.amount),
                    )
                    .where(UsageLedgerEntry.organization_id == organization.id)
                    .group_by(UsageLedgerEntry.metric)
                )
            ).all()
        )
        rows = list(
            (
                await ledger_db.execute(
                    select(UsageLedgerEntry).where(
                        UsageLedgerEntry.organization_id == organization.id
                    )
                )
            ).scalars()
        )
    assert totals == {"ai_tokens": 15, "ai_cost_microusd": 123}
    serialized_metadata = " ".join(str(row.metadata_json) for row in rows)
    assert canary not in serialized_metadata
    assert {row.kind for row in rows} == {"reserve", "settle", "refund"}
    assert len({row.metadata_json["call_id"] for row in rows}) == 1


async def test_refund_is_one_atomic_terminal_transition(db):
    organization = await _organization(db, "refund")
    gateway = BudgetedLLMClient(
        object(),
        db=db,
        organization_id=organization.id,
        provider="openrouter",
        managed=True,
    )
    call_id = uuid4()
    messages = [ChatMessage(role="user", content="known no effect")]
    output_limit, tokens, cost = gateway._limits(messages, 64)
    assert cost is not None
    metadata = gateway._metadata(
        call_id=call_id,
        requested_model=None,
        log=None,
        output_limit=output_limit,
    )
    await gateway._reserve(
        call_id=call_id,
        token_reservation=tokens,
        cost_reservation=cost,
        metadata=metadata,
    )
    await gateway._refund(
        call_id=call_id,
        token_reservation=tokens,
        cost_reservation=cost,
        metadata={**metadata, "reason": "proven_preflight_rejection"},
    )

    async with SessionLocal() as ledger_db:
        totals = dict(
            (
                await ledger_db.execute(
                    select(
                        UsageLedgerEntry.metric,
                        func.sum(UsageLedgerEntry.amount),
                    )
                    .where(UsageLedgerEntry.organization_id == organization.id)
                    .group_by(UsageLedgerEntry.metric)
                )
            ).all()
        )
        terminal = list(
            (
                await ledger_db.execute(
                    select(UsageLedgerEntry).where(
                        UsageLedgerEntry.organization_id == organization.id,
                        UsageLedgerEntry.kind == "refund",
                    )
                )
            ).scalars()
        )
    assert totals == {"ai_tokens": 0, "ai_cost_microusd": 0}
    assert len(terminal) == 2
    assert {row.metadata_json["reservation_state"] for row in terminal} == {"refunded"}


async def test_zero_token_budget_blocks_before_provider_call(db):
    organization = await _organization(db, "blocked")
    await _limit(db, organization.id, "ai_tokens", 0)

    class NeverCalled:
        calls = 0

        async def complete(self, _messages, **_kwargs):
            self.calls += 1
            raise AssertionError("provider must not be called")

    provider = NeverCalled()
    gateway = BudgetedLLMClient(
        provider,
        db=db,
        organization_id=organization.id,
        provider="private",
        managed=False,
    )
    with pytest.raises(EntitlementExceeded):
        await gateway.complete([ChatMessage(role="user", content="blocked")])
    assert provider.calls == 0


async def test_cost_reservation_failure_rolls_back_token_reservation(db):
    organization = await _organization(db, "atomic-reserve")
    await _limit(db, organization.id, "ai_tokens", 20_000)
    await _limit(db, organization.id, "ai_cost_microusd", 0)

    class NeverCalled:
        calls = 0

        async def complete(self, _messages, **_kwargs):
            self.calls += 1
            raise AssertionError("provider must not be called")

    provider = NeverCalled()
    gateway = BudgetedLLMClient(
        provider,
        db=db,
        organization_id=organization.id,
        provider="openrouter",
        managed=True,
    )
    with pytest.raises(EntitlementExceeded) as blocked:
        await gateway.complete([ChatMessage(role="user", content="atomic")])
    assert blocked.value.metric == "ai_cost_microusd"
    assert provider.calls == 0
    async with SessionLocal() as ledger_db:
        count = await ledger_db.scalar(
            select(func.count(UsageLedgerEntry.id)).where(
                UsageLedgerEntry.organization_id == organization.id
            )
        )
    assert count == 0


async def test_stream_without_terminal_retains_unknown_reservation(db):
    organization = await _organization(db, "protocol")

    class BrokenStream:
        async def stream_complete_events(self, _messages, **_kwargs):
            yield StreamCompletionEvent(type="delta", content="partial")

    gateway = BudgetedLLMClient(
        BrokenStream(),
        db=db,
        organization_id=organization.id,
        provider="private",
        managed=False,
    )
    messages = [ChatMessage(role="user", content="stream")]
    reservation = gateway._limits(messages, 64)[1]
    with pytest.raises(LLMProtocolError):
        _ = [
            event
            async for event in gateway.stream_complete_events(messages, max_tokens=64)
        ]
    used = await db.scalar(
        select(func.coalesce(func.sum(UsageLedgerEntry.amount), 0)).where(
            UsageLedgerEntry.organization_id == organization.id,
            UsageLedgerEntry.metric == "ai_tokens",
        )
    )
    assert used == reservation
    states = set(
        (
            await db.execute(
                select(
                    UsageLedgerEntry.metadata_json["reservation_state"].astext
                ).where(UsageLedgerEntry.organization_id == organization.id)
            )
        ).scalars()
    )
    assert states == {"reserved", "unknown"}


async def test_concurrent_reservations_cannot_cross_hard_token_cap(db):
    organization = await _organization(db, "race")
    await _limit(db, organization.id, "ai_tokens", 3_000)

    class ExpensiveFake:
        async def complete(self, _messages, **_kwargs):
            await asyncio.sleep(0)
            return CompletionResult(
                content="ok",
                model_used="private/model",
                tokens_in=2_000,
                tokens_out=0,
                provider_used="private",
            )

    async def invoke():
        async with SessionLocal() as session:
            gateway = BudgetedLLMClient(
                ExpensiveFake(),
                db=session,
                organization_id=organization.id,
                provider="private",
                managed=False,
            )
            try:
                await gateway.complete(
                    [ChatMessage(role="user", content="race")], max_tokens=64
                )
                await session.commit()
                return "ok"
            except EntitlementExceeded:
                await session.rollback()
                return "blocked"

    assert sorted(await asyncio.gather(invoke(), invoke())) == ["blocked", "ok"]
    async with SessionLocal() as session:
        used = await session.scalar(
            select(func.sum(UsageLedgerEntry.amount)).where(
                UsageLedgerEntry.organization_id == organization.id,
                UsageLedgerEntry.metric == "ai_tokens",
            )
        )
    assert used == 2_000


async def test_zero_mission_budget_blocks_private_and_managed_providers(db):
    organization = await _organization(db, "mission-blocked")
    mission_id = uuid4()

    class NeverCalled:
        calls = 0

        async def complete(self, _messages, **_kwargs):
            self.calls += 1
            raise AssertionError("provider must not be called")

    for managed in (False, True):
        provider = NeverCalled()
        gateway = BudgetedLLMClient(
            provider,
            db=db,
            organization_id=organization.id,
            provider="openrouter" if managed else "private",
            managed=managed,
            budget_resource_id=mission_id,
            model_budget_cents=0,
        )
        with pytest.raises(LLMModelBudgetExceeded):
            await gateway.complete(
                [ChatMessage(role="user", content="blocked mission")],
                max_tokens=64,
            )
        assert provider.calls == 0


async def test_concurrent_calls_cannot_cross_per_mission_cost_cap(db):
    organization = await _organization(db, "mission-race")
    mission_id = uuid4()
    messages = [ChatMessage(role="user", content="mission budget race")]

    class ManagedCall:
        async def complete(self, _messages, **_kwargs):
            await asyncio.sleep(0.02)
            return CompletionResult(
                content="ok",
                model_used="managed/model",
                tokens_in=1,
                tokens_out=1,
                provider_used="managed",
                cost_microusd=reservation,
            )

    probe = BudgetedLLMClient(
        ManagedCall(),
        db=db,
        organization_id=organization.id,
        provider="openrouter",
        managed=True,
        budget_resource_id=mission_id,
        model_budget_cents=1_000_000,
    )
    reservation = probe._limits(messages, 64)[2]
    budget_cents = (reservation + 9_999) // 10_000

    async def invoke():
        async with SessionLocal() as session:
            gateway = BudgetedLLMClient(
                ManagedCall(),
                db=session,
                organization_id=organization.id,
                provider="openrouter",
                managed=True,
                budget_resource_id=mission_id,
                model_budget_cents=budget_cents,
            )
            try:
                await gateway.complete(messages, max_tokens=64)
                await session.commit()
                return "ok"
            except LLMModelBudgetExceeded:
                await session.rollback()
                return "blocked"

    assert sorted(await asyncio.gather(invoke(), invoke())) == ["blocked", "ok"]
    async with SessionLocal() as session:
        spent = await session.scalar(
            select(func.coalesce(func.sum(UsageLedgerEntry.amount), 0)).where(
                UsageLedgerEntry.organization_id == organization.id,
                UsageLedgerEntry.metric == "ai_cost_microusd",
                UsageLedgerEntry.resource_type == "hiring_mission",
                UsageLedgerEntry.resource_id == mission_id,
            )
        )
    assert spent == reservation


async def test_private_budget_null_zero_and_missing_accounting_fail_closed(
    db, monkeypatch
):
    organization = await _organization(db, "private-budget-modes")

    class PrivateCall:
        calls = 0

        async def complete(self, _messages, **_kwargs):
            self.calls += 1
            return CompletionResult(
                content="ok",
                model_used="private/model",
                tokens_in=1,
                tokens_out=1,
                provider_used="private",
            )

    provider = PrivateCall()
    unlimited = BudgetedLLMClient(
        provider,
        db=db,
        organization_id=organization.id,
        provider="private",
        managed=False,
        budget_resource_id=uuid4(),
        model_budget_cents=None,
    )
    await unlimited.complete(
        [ChatMessage(role="user", content="unlimited")], max_tokens=64
    )
    assert provider.calls == 1

    blocked = BudgetedLLMClient(
        provider,
        db=db,
        organization_id=organization.id,
        provider="private",
        managed=False,
        budget_resource_id=uuid4(),
        model_budget_cents=0,
    )
    with pytest.raises(LLMModelBudgetExceeded):
        await blocked.complete(
            [ChatMessage(role="user", content="blocked")], max_tokens=64
        )
    assert provider.calls == 1

    with pytest.raises(LLMConfigurationError) as unavailable:
        BudgetedLLMClient(
            provider,
            db=db,
            organization_id=organization.id,
            provider="private",
            managed=False,
            budget_resource_id=uuid4(),
            model_budget_cents=1,
        )
    assert unavailable.value.code == "ai_private_budget_accounting_unavailable"

    monkeypatch.setenv("LLM_PRIVATE_PROMPT_USD_PER_MILLION", "4")
    monkeypatch.setenv("LLM_PRIVATE_COMPLETION_USD_PER_MILLION", "4")
    get_settings.cache_clear()
    configured = BudgetedLLMClient(
        provider,
        db=db,
        organization_id=organization.id,
        provider="private",
        managed=False,
        budget_resource_id=uuid4(),
        model_budget_cents=1,
    )
    await configured.complete(
        [ChatMessage(role="user", content="configured cap")], max_tokens=64
    )
    assert provider.calls == 2


async def test_private_exact_cap_and_concurrent_reservations(db, monkeypatch):
    monkeypatch.setenv("LLM_PRIVATE_PROMPT_USD_PER_MILLION", "4")
    monkeypatch.setenv("LLM_PRIVATE_COMPLETION_USD_PER_MILLION", "4")
    get_settings.cache_clear()
    organization = await _organization(db, "private-exact-cap")
    mission_id = uuid4()
    messages = [ChatMessage(role="user", content="private exact cap")]

    class PrivateCall:
        async def complete(self, _messages, **_kwargs):
            await asyncio.sleep(0.02)
            return CompletionResult(
                content="ok",
                model_used="private/model",
                tokens_in=token_reservation,
                tokens_out=0,
                provider_used="private",
            )

    probe = BudgetedLLMClient(
        PrivateCall(),
        db=db,
        organization_id=organization.id,
        provider="private",
        managed=False,
        budget_resource_id=mission_id,
        model_budget_cents=100,
    )
    _output, token_reservation, cost_reservation = probe._limits(messages, 64)
    assert cost_reservation is not None
    assert cost_reservation < 10_000

    # Bring usage to exactly one cent after the next reservation. The strict
    # comparison must permit equality, not reject it as exhausted.
    db.add(
        UsageLedgerEntry(
            organization_id=organization.id,
            metric="ai_cost_microusd",
            amount=10_000 - cost_reservation,
            kind="adjustment",
            idempotency_key=f"test-private-exact:{mission_id}",
            resource_type="hiring_mission",
            resource_id=mission_id,
            metadata_json={"test_fixture": True},
        )
    )
    await db.commit()
    exact = BudgetedLLMClient(
        PrivateCall(),
        db=db,
        organization_id=organization.id,
        provider="private",
        managed=False,
        budget_resource_id=mission_id,
        model_budget_cents=1,
    )
    await exact.complete(messages, max_tokens=64)

    race_mission_id = uuid4()

    async def invoke():
        async with SessionLocal() as session:
            gateway = BudgetedLLMClient(
                PrivateCall(),
                db=session,
                organization_id=organization.id,
                provider="private",
                managed=False,
                budget_resource_id=race_mission_id,
                model_budget_cents=1,
            )
            try:
                await gateway.complete(messages, max_tokens=64)
                return "ok"
            except LLMModelBudgetExceeded:
                return "blocked"

    assert sorted(await asyncio.gather(invoke(), invoke())) == ["blocked", "ok"]


async def test_crash_after_provider_acceptance_keeps_durable_reservation(db):
    organization = await _organization(db, "kill-point")
    mission_id = uuid4()
    messages = [ChatMessage(role="user", content="accepted then killed")]

    class KilledAfterAcceptance:
        accepted = False

        async def complete(self, _messages, **_kwargs):
            self.accepted = True
            raise asyncio.CancelledError

    provider = KilledAfterAcceptance()
    probe = BudgetedLLMClient(
        provider,
        db=db,
        organization_id=organization.id,
        provider="openrouter",
        managed=True,
        budget_resource_id=mission_id,
        model_budget_cents=100,
    )
    reservation = probe._limits(messages, 64)[2]
    assert reservation is not None
    budget_cents = (reservation + 9_999) // 10_000
    gateway = BudgetedLLMClient(
        provider,
        db=db,
        organization_id=organization.id,
        provider="openrouter",
        managed=True,
        budget_resource_id=mission_id,
        model_budget_cents=budget_cents,
    )
    with pytest.raises(asyncio.CancelledError):
        await gateway.complete(messages, max_tokens=64)
    assert provider.accepted is True

    async with SessionLocal() as ledger_db:
        rows = list(
            (
                await ledger_db.execute(
                    select(UsageLedgerEntry).where(
                        UsageLedgerEntry.organization_id == organization.id,
                        UsageLedgerEntry.resource_id == mission_id,
                    )
                )
            ).scalars()
        )
    assert {row.kind for row in rows} == {"reserve"}
    assert (
        sum(row.amount for row in rows if row.metric == "ai_cost_microusd")
        == reservation
    )

    class NeverCalled:
        calls = 0

        async def complete(self, _messages, **_kwargs):
            self.calls += 1
            raise AssertionError("retry must be blocked by crash-unknown spend")

    retry_provider = NeverCalled()
    retry = BudgetedLLMClient(
        retry_provider,
        db=db,
        organization_id=organization.id,
        provider="openrouter",
        managed=True,
        budget_resource_id=mission_id,
        model_budget_cents=budget_cents,
    )
    with pytest.raises(LLMModelBudgetExceeded):
        await retry.complete(messages, max_tokens=64)
    assert retry_provider.calls == 0


async def test_out_of_bounds_cost_is_atomically_disputed_without_partial_settle(db):
    organization = await _organization(db, "disputed")
    messages = [ChatMessage(role="user", content="disputed usage")]

    class BadCost:
        async def complete(self, _messages, **_kwargs):
            return CompletionResult(
                content="untrusted",
                model_used="managed/model",
                tokens_in=1,
                tokens_out=1,
                provider_used="managed",
                cost_microusd=reservation + 1,
            )

    gateway = BudgetedLLMClient(
        BadCost(),
        db=db,
        organization_id=organization.id,
        provider="openrouter",
        managed=True,
    )
    _output, token_reservation, reservation = gateway._limits(messages, 64)
    assert reservation is not None
    with pytest.raises(LLMProtocolError, match="price ceiling"):
        await gateway.complete(messages, max_tokens=64)

    async with SessionLocal() as ledger_db:
        rows = list(
            (
                await ledger_db.execute(
                    select(UsageLedgerEntry).where(
                        UsageLedgerEntry.organization_id == organization.id
                    )
                )
            ).scalars()
        )
    assert (
        sum(row.amount for row in rows if row.metric == "ai_tokens")
        == token_reservation
    )
    assert (
        sum(row.amount for row in rows if row.metric == "ai_cost_microusd")
        == reservation
    )
    terminal_states = {
        row.metadata_json["reservation_state"]
        for row in rows
        if row.metadata_json.get("terminal")
    }
    assert terminal_states == {"disputed"}
