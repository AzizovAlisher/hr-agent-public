"""Concurrency, privacy, proxy, and fail-closed public abuse controls."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from uuid import uuid4

import httpx
import pytest
from starlette.requests import Request

from app.config import Settings
from app.main import app
from app.services.public_abuse import (
    InMemoryRateLimitBackend,
    PublicRateLimitUnavailable,
    RateLimitDecision,
    ValkeyRateLimitBackend,
    enforce_public_request,
    get_public_rate_limit_backend,
    install_public_rate_limit_backend_for_testing,
    public_network_identifier,
    public_request_requires_limiter,
)

pytestmark = pytest.mark.asyncio


def _request(
    method: str,
    path: str,
    *,
    client: tuple[str, int] = ("198.51.100.20", 50000),
    headers: tuple[tuple[bytes, bytes], ...] = (),
) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": list(headers),
            "client": client,
            "server": ("pilot.example", 443),
        }
    )


async def test_in_memory_sliding_window_is_atomic_under_concurrency():
    now = [1000.0]
    backend = InMemoryRateLimitBackend(clock=lambda: now[0])

    decisions = await asyncio.gather(
        *(
            backend.acquire(key="one", limit=10, window_seconds=60)
            for _index in range(40)
        )
    )

    assert sum(decision.allowed for decision in decisions) == 10
    assert all(
        decision.retry_after == 60 for decision in decisions if not decision.allowed
    )
    now[0] += 61
    recovered = await backend.acquire(key="one", limit=10, window_seconds=60)
    assert recovered.allowed is True
    assert recovered.remaining == 9


async def test_valkey_lua_window_is_atomic_across_concurrent_clients():
    url = os.getenv("TEST_VALKEY_URL")
    if not url:
        pytest.skip("TEST_VALKEY_URL is required for the shared-limiter contract")
    backend = ValkeyRateLimitBackend(url)
    key = f"hr:test:public-rate-limit:{uuid4()}"
    try:
        decisions = await asyncio.gather(
            *(
                backend.acquire(key=key, limit=7, window_seconds=2)
                for _index in range(35)
            )
        )
    finally:
        await backend.close()

    assert sum(decision.allowed for decision in decisions) == 7
    assert all(
        decision.retry_after in {1, 2} for decision in decisions if not decision.allowed
    )


async def test_forwarded_address_is_used_only_from_configured_trusted_proxy():
    forwarded = ((b"x-forwarded-for", b"203.0.113.8, 10.0.0.3"),)
    untrusted_settings = Settings(
        _env_file=None,
        public_rate_limit_trusted_proxy_cidrs="",
    )
    trusted_settings = Settings(
        _env_file=None,
        public_rate_limit_trusted_proxy_cidrs="10.0.0.0/8",
    )

    spoofed = _request("GET", "/public/positions/example", headers=forwarded)
    proxied = _request(
        "GET",
        "/public/positions/example",
        client=("10.0.0.2", 443),
        headers=forwarded,
    )

    assert public_network_identifier(spoofed, untrusted_settings) == "198.51.100.20"
    assert public_network_identifier(proxied, trusted_settings) == "203.0.113.8"


@dataclass
class _CapturingBackend:
    keys: list[str]

    async def acquire(
        self, *, key: str, limit: int, window_seconds: int
    ) -> RateLimitDecision:
        self.keys.append(key)
        return RateLimitDecision(True, limit, limit - 1, 0)

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


async def test_distributed_keys_never_contain_raw_ip_or_capability_token():
    token = "candidate-secret-capability-token-which-must-not-leak"
    backend = _CapturingBackend([])
    previous = await get_public_rate_limit_backend()
    install_public_rate_limit_backend_for_testing(backend)
    try:
        result = await enforce_public_request(
            _request("POST", f"/public/assessments/{token}/attempts")
        )
    finally:
        install_public_rate_limit_backend_for_testing(previous)

    assert result is not None
    assert backend.keys
    assert all(token not in key for key in backend.keys)
    assert all("198.51.100.20" not in key for key in backend.keys)


class _DeniedBackend(_CapturingBackend):
    async def acquire(
        self, *, key: str, limit: int, window_seconds: int
    ) -> RateLimitDecision:
        self.keys.append(key)
        return RateLimitDecision(False, limit, 0, 17)


class _UnavailableBackend(_CapturingBackend):
    async def acquire(
        self, *, key: str, limit: int, window_seconds: int
    ) -> RateLimitDecision:
        raise PublicRateLimitUnavailable("simulated Valkey outage")


async def test_http_boundary_returns_stable_429_problem_before_routing():
    previous = await get_public_rate_limit_backend()
    install_public_rate_limit_backend_for_testing(_DeniedBackend([]))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(f"/public/positions/{uuid4()}")
    finally:
        install_public_rate_limit_backend_for_testing(previous)

    assert response.status_code == 429
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["retry-after"] == "17"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == "rate_limited"
    assert response.json()["request_id"] == response.headers["x-request-id"]


async def test_public_mutations_fail_closed_when_shared_limiter_is_unavailable():
    request = _request("GET", "/public/referrals/opaque-token")
    assert public_request_requires_limiter(request) is True
    assert (
        public_request_requires_limiter(
            _request("GET", "/public/shortlists/opaque-token")
        )
        is True
    )
    assert (
        public_request_requires_limiter(_request("POST", f"/source-webhooks/{uuid4()}"))
        is True
    )
    assert (
        public_request_requires_limiter(
            _request("POST", f"/integrations/webhooks/{uuid4()}")
        )
        is True
    )
    assert (
        public_request_requires_limiter(
            _request("POST", f"/communications/webhooks/sandbox/{uuid4()}")
        )
        is True
    )

    previous = await get_public_rate_limit_backend()
    install_public_rate_limit_backend_for_testing(_UnavailableBackend([]))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(f"/public/positions/{uuid4()}/apply")
            shortlist_response = await client.get(
                "/public/shortlists/opaque-token-that-never-reaches-the-database"
            )
    finally:
        install_public_rate_limit_backend_for_testing(previous)

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json()["code"] == "abuse_protection_unavailable"
    assert shortlist_response.status_code == 503
    assert shortlist_response.headers["retry-after"] == "5"
    assert shortlist_response.json()["code"] == "abuse_protection_unavailable"
