"""Managed-provider privacy, price ceiling, and usage-provenance contract tests."""

from types import SimpleNamespace

import pytest

from app.llm.base import ChatMessage
from app.llm.openrouter import OpenRouterClient

pytestmark = pytest.mark.asyncio


class _FakeCompletions:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class _FakeOpenAI:
    last_instance = None
    response = None

    def __init__(self, **kwargs):
        self.initialization = kwargs
        self.chat = SimpleNamespace(completions=_FakeCompletions(self.response))
        type(self).last_instance = self


def _settings():
    return SimpleNamespace(
        openrouter_api_key="test-key-never-sent",
        openrouter_base_url="https://openrouter.ai/api/v1",
        openrouter_default_model="managed/default",
        llm_default_max_output_tokens=2048,
        openrouter_require_zdr=True,
        openrouter_max_prompt_usd_per_million=5.0,
        openrouter_max_completion_usd_per_million=20.0,
    )


async def test_complete_enforces_managed_privacy_price_and_usage_contract(monkeypatch):
    usage = SimpleNamespace(
        prompt_tokens=7,
        completion_tokens=3,
        cost="0.0000012",
        model_extra={},
    )
    _FakeOpenAI.response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="safe answer"))],
        usage=usage,
        model="upstream/model-v2",
        provider="Cerebras",
        model_extra={},
        model_dump=lambda: {"id": "response-only"},
    )
    monkeypatch.setattr("app.llm.openrouter.AsyncOpenAI", _FakeOpenAI)
    monkeypatch.setattr("app.llm.openrouter.get_settings", _settings)

    client = OpenRouterClient()
    result = await client.complete(
        [ChatMessage(role="user", content="PII-CANARY-never-in-provider-policy")],
        max_tokens=128,
    )

    fake = _FakeOpenAI.last_instance
    assert fake.initialization == {
        "api_key": "test-key-never-sent",
        "base_url": "https://openrouter.ai/api/v1",
        "timeout": 90,
        "max_retries": 0,
    }
    request = fake.chat.completions.calls[0]
    assert request["max_tokens"] == 128
    assert request["extra_body"] == {
        "provider": {
            "sort": "price",
            "data_collection": "deny",
            "zdr": True,
            "max_price": {"prompt": 5.0, "completion": 20.0, "request": 0},
        }
    }
    assert result.model_used == "upstream/model-v2"
    assert result.provider_used == "Cerebras"
    assert result.tokens_in == 7
    assert result.tokens_out == 3
    assert result.cost_microusd == 2
    assert result.raw == {"id": "response-only"}


async def test_stream_produces_one_terminal_with_upstream_usage(monkeypatch):
    usage = SimpleNamespace(
        prompt_tokens=11,
        completion_tokens=2,
        cost=0.000004,
        model_extra={},
    )

    async def chunks():
        yield SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="one "))],
            model="routed/model",
            provider="Provider-A",
            usage=None,
            model_extra={},
        )
        yield SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="two"))],
            model="routed/model",
            provider="Provider-A",
            usage=usage,
            model_extra={},
        )

    _FakeOpenAI.response = chunks()
    monkeypatch.setattr("app.llm.openrouter.AsyncOpenAI", _FakeOpenAI)
    monkeypatch.setattr("app.llm.openrouter.get_settings", _settings)

    events = [
        event
        async for event in OpenRouterClient().stream_complete_events(
            [ChatMessage(role="user", content="stream")]
        )
    ]

    assert [event.type for event in events] == ["delta", "delta", "done"]
    assert "".join(event.content for event in events[:-1]) == "one two"
    terminal = events[-1]
    assert terminal.model_used == "routed/model"
    assert terminal.provider_used == "Provider-A"
    assert terminal.tokens_in == 11
    assert terminal.tokens_out == 2
    assert terminal.cost_microusd == 4


@pytest.mark.parametrize("value", [None, "not-a-number", "NaN", "-0.1"])
async def test_invalid_or_negative_provider_cost_is_not_trusted(value):
    usage = SimpleNamespace(cost=value, model_extra={})
    assert OpenRouterClient._cost_microusd(usage) is None
