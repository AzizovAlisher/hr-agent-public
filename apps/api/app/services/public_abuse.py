"""Distributed, privacy-preserving limits for unauthenticated HTTP surfaces."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
import re
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import ip_address, ip_network
from typing import Protocol

from fastapi import Request
from redis.exceptions import RedisError

from app.config import Settings, get_settings


class PublicRateLimitUnavailable(RuntimeError):
    """The shared limiter cannot safely authorize an external mutation."""


class PublicRateLimitExceeded(RuntimeError):
    def __init__(self, *, limit: int, remaining: int, retry_after: int) -> None:
        super().__init__("public request rate limit exceeded")
        self.limit = limit
        self.remaining = remaining
        self.retry_after = max(1, retry_after)


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int


class RateLimitBackend(Protocol):
    async def acquire(
        self, *, key: str, limit: int, window_seconds: int
    ) -> RateLimitDecision: ...

    async def ping(self) -> bool: ...

    async def close(self) -> None: ...


class InMemoryRateLimitBackend:
    """Deterministic sliding windows for tests and single-process development."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 50_000,
    ) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self, *, key: str, limit: int, window_seconds: int
    ) -> RateLimitDecision:
        if limit < 1 or window_seconds < 1:
            raise ValueError("Rate-limit bounds must be positive")
        now = self._clock()
        cutoff = now - window_seconds
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self._max_keys:
                    self._prune(cutoff)
                if len(self._buckets) >= self._max_keys:
                    raise PublicRateLimitUnavailable("local limiter capacity exhausted")
                bucket = deque()
                self._buckets[key] = bucket
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = math.ceil(window_seconds - (now - bucket[0]))
                return RateLimitDecision(False, limit, 0, max(1, retry_after))
            bucket.append(now)
            return RateLimitDecision(True, limit, limit - len(bucket), 0)

    def _prune(self, cutoff: float) -> None:
        for key, bucket in tuple(self._buckets.items()):
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if not bucket:
                self._buckets.pop(key, None)

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        async with self._lock:
            self._buckets.clear()


_SLIDING_WINDOW_LUA = """
local current = redis.call('TIME')
local now_ms = (tonumber(current[1]) * 1000) + math.floor(tonumber(current[2]) / 1000)
local window_ms = tonumber(ARGV[1])
local maximum = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms - window_ms)
local count = redis.call('ZCARD', KEYS[1])
if count >= maximum then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  local retry_ms = window_ms
  if oldest[2] then
    retry_ms = math.max(1, window_ms - (now_ms - tonumber(oldest[2])))
  end
  redis.call('PEXPIRE', KEYS[1], window_ms)
  return {0, count, retry_ms}
end
redis.call('ZADD', KEYS[1], now_ms, ARGV[3])
redis.call('PEXPIRE', KEYS[1], window_ms)
return {1, count + 1, 0}
"""


class ValkeyRateLimitBackend:
    """Atomic sliding windows shared by every API replica."""

    def __init__(self, url: str) -> None:
        from redis.asyncio import Redis

        self._client = Redis.from_url(
            url,
            decode_responses=False,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
            health_check_interval=30,
        )

    async def acquire(
        self, *, key: str, limit: int, window_seconds: int
    ) -> RateLimitDecision:
        try:
            raw = await self._client.eval(
                _SLIDING_WINDOW_LUA,
                1,
                key,
                window_seconds * 1000,
                limit,
                secrets.token_hex(16),
            )
            allowed, count, retry_ms = (int(value) for value in raw)
        except (RedisError, TypeError, ValueError) as exc:
            raise PublicRateLimitUnavailable("shared limiter unavailable") from exc
        return RateLimitDecision(
            allowed=bool(allowed),
            limit=limit,
            remaining=max(0, limit - count),
            retry_after=max(1, math.ceil(retry_ms / 1000)) if not allowed else 0,
        )

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except RedisError:
            return False

    async def close(self) -> None:
        await self._client.aclose()


@dataclass(frozen=True, slots=True)
class _RequestClass:
    group: str
    target: str | None
    mutation: bool
    expensive: bool


@dataclass(frozen=True, slots=True)
class _Rule:
    name: str
    limit: int
    window_seconds: int
    include_target: bool = False


_APPLY_PATHS = (
    re.compile(r"^/public/positions/([^/]+)/apply$"),
    re.compile(r"^/public/v1/careers/([^/]+)/positions/([^/]+)/apply$"),
)
_ASSESSMENT_FILE_PATH = re.compile(
    r"^/public/assessments/([^/]+)/attempts/[^/]+/questions/[^/]+/files$"
)
_ASSESSMENT_PATH = re.compile(r"^/public/assessments/([^/]+)(?:/|$)")
_SCHEDULING_MANAGE_PATH = re.compile(r"^/public/scheduling/manage/([^/]+)(?:/|$)")
_SCHEDULING_PATH = re.compile(r"^/public/scheduling/([^/]+)(?:/|$)")
_CAPABILITY_PATHS = (
    re.compile(r"^/public/offers/([^/]+)(?:/|$)"),
    re.compile(r"^/public/shortlists/([^/]+)(?:/|$)"),
    re.compile(r"^/public/referrals/([^/]+)(?:/|$)"),
)
_SOURCE_WEBHOOK_PATH = re.compile(r"^/source-webhooks/([^/]+)$")
_INTEGRATION_WEBHOOK_PATH = re.compile(r"^/integrations/webhooks/([^/]+)$")
_COMMUNICATION_SANDBOX_WEBHOOK_PATH = re.compile(
    r"^/communications/webhooks/sandbox/([^/]+)$"
)


def _classify(method: str, path: str) -> _RequestClass | None:
    if method == "OPTIONS":
        return None
    for pattern in (
        _SOURCE_WEBHOOK_PATH,
        _INTEGRATION_WEBHOOK_PATH,
        _COMMUNICATION_SANDBOX_WEBHOOK_PATH,
    ):
        match = pattern.match(path)
        if match and method == "POST":
            return _RequestClass("webhook", match.group(1), True, True)
    if not path.startswith("/public/"):
        return None
    for pattern in _APPLY_PATHS:
        match = pattern.match(path)
        if match and method == "POST":
            return _RequestClass("application", "|".join(match.groups()), True, True)
    match = _ASSESSMENT_FILE_PATH.match(path)
    if match and method == "POST":
        return _RequestClass("assessment_file", match.group(1), True, True)
    match = _ASSESSMENT_PATH.match(path)
    if match:
        return _RequestClass(
            "assessment",
            match.group(1),
            method in {"POST", "PUT", "PATCH", "DELETE"},
            False,
        )
    match = _SCHEDULING_MANAGE_PATH.match(path) or _SCHEDULING_PATH.match(path)
    if match:
        mutation = method in {"POST", "PUT", "PATCH", "DELETE"}
        return _RequestClass("scheduling", match.group(1), mutation, False)
    for pattern in _CAPABILITY_PATHS:
        match = pattern.match(path)
        if match:
            # Referral resolution increments a counter and shortlist reads
            # append a sensitive-read audit row.  Both therefore receive the
            # mutation budget and fail closed during a shared-limiter outage,
            # even though their public contracts remain GET.
            mutation = method in {"POST", "PUT", "PATCH", "DELETE"} or path.startswith(
                ("/public/referrals/", "/public/shortlists/")
            )
            return _RequestClass("capability", match.group(1), mutation, False)
    return _RequestClass(
        "public",
        None,
        method in {"POST", "PUT", "PATCH", "DELETE"},
        False,
    )


def public_request_requires_limiter(request: Request) -> bool:
    request_class = _classify(request.method.upper(), request.url.path)
    return bool(request_class and request_class.mutation)


def _trusted_networks(settings: Settings):
    return tuple(
        ip_network(value, strict=False)
        for value in settings.public_rate_limit_trusted_proxy_cidrs.split(",")
        if value
    )


def public_network_identifier(request: Request, settings: Settings) -> str:
    """Return the client network address without trusting spoofable headers."""

    peer_raw = request.client.host if request.client else ""
    try:
        peer = ip_address(peer_raw)
    except ValueError:
        return "unknown"
    trusted = _trusted_networks(settings)
    if not any(peer in network for network in trusted):
        return peer.compressed
    forwarded = request.headers.get("x-forwarded-for", "")
    try:
        chain = [
            ip_address(value.strip()) for value in forwarded.split(",") if value.strip()
        ]
    except ValueError:
        return peer.compressed
    for address in reversed(chain):
        if not any(address in network for network in trusted):
            return address.compressed
    return chain[0].compressed if chain else peer.compressed


def _hmac_secret(settings: Settings) -> bytes:
    configured = settings.public_rate_limit_hmac_secret
    if configured:
        return configured.get_secret_value().encode("utf-8")
    # Development/test fallback is purpose-separated. Production validation
    # requires a dedicated secret and therefore never reaches this branch.
    return hmac.new(
        settings.jwt_secret.encode("utf-8"),
        b"public-rate-limit-development-key",
        hashlib.sha256,
    ).digest()


def _digest(settings: Settings, purpose: str, value: str) -> str:
    return hmac.new(
        _hmac_secret(settings),
        f"{purpose}\0{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _rules(settings: Settings, request_class: _RequestClass) -> tuple[_Rule, ...]:
    if request_class.mutation:
        rules = [
            _Rule(
                "mutation-network",
                settings.public_rate_limit_mutation_requests,
                settings.public_rate_limit_mutation_window_seconds,
            )
        ]
        if request_class.target is not None:
            rules.append(
                _Rule(
                    "mutation-capability",
                    settings.public_rate_limit_mutation_requests,
                    settings.public_rate_limit_mutation_window_seconds,
                    include_target=True,
                )
            )
    else:
        rules = [
            _Rule(
                "read-network",
                settings.public_rate_limit_read_requests,
                settings.public_rate_limit_read_window_seconds,
            )
        ]
        if request_class.target is not None:
            rules.append(
                _Rule(
                    "read-capability",
                    settings.public_rate_limit_read_requests,
                    settings.public_rate_limit_read_window_seconds,
                    include_target=True,
                )
            )
    if request_class.expensive:
        rules.extend(
            (
                _Rule(
                    "expensive-network",
                    settings.public_rate_limit_expensive_requests,
                    settings.public_rate_limit_expensive_window_seconds,
                ),
                _Rule(
                    "expensive-target",
                    settings.public_rate_limit_expensive_requests,
                    settings.public_rate_limit_expensive_window_seconds,
                    include_target=True,
                ),
            )
        )
    return tuple(rules)


_backend: RateLimitBackend | None = None
_backend_lock = asyncio.Lock()


async def get_public_rate_limit_backend() -> RateLimitBackend:
    global _backend
    if _backend is not None:
        return _backend
    async with _backend_lock:
        if _backend is None:
            settings = get_settings()
            if settings.public_rate_limit_backend == "valkey":
                if not settings.public_rate_limit_valkey_url:
                    raise PublicRateLimitUnavailable("shared limiter is not configured")
                _backend = ValkeyRateLimitBackend(settings.public_rate_limit_valkey_url)
            else:
                _backend = InMemoryRateLimitBackend()
    return _backend


async def enforce_public_request(request: Request) -> RateLimitDecision | None:
    settings = get_settings()
    request_class = _classify(request.method.upper(), request.url.path)
    if request_class is None:
        return None
    backend = await get_public_rate_limit_backend()
    network = _digest(
        settings,
        "network",
        public_network_identifier(request, settings),
    )
    target = (
        _digest(settings, request_class.group, request_class.target)
        if request_class.target is not None
        else None
    )
    narrowest: RateLimitDecision | None = None
    for rule in _rules(settings, request_class):
        parts = ["hr", "public", request_class.group, rule.name, network]
        if rule.include_target and target is not None:
            parts.append(target)
        decision = await backend.acquire(
            key=":".join(parts),
            limit=rule.limit,
            window_seconds=rule.window_seconds,
        )
        if not decision.allowed:
            raise PublicRateLimitExceeded(
                limit=decision.limit,
                remaining=decision.remaining,
                retry_after=decision.retry_after,
            )
        if narrowest is None or decision.remaining < narrowest.remaining:
            narrowest = decision
    return narrowest


async def public_rate_limit_health() -> tuple[bool, str]:
    settings = get_settings()
    try:
        backend = await get_public_rate_limit_backend()
        healthy = await backend.ping()
    except PublicRateLimitUnavailable:
        healthy = False
    return healthy, settings.public_rate_limit_backend


async def close_public_rate_limiter() -> None:
    global _backend
    async with _backend_lock:
        backend, _backend = _backend, None
    if backend is not None:
        await backend.close()


def install_public_rate_limit_backend_for_testing(
    backend: RateLimitBackend | None,
) -> None:
    """Synchronous test seam; callers must restore/close their backend."""

    global _backend
    _backend = backend


__all__ = [
    "InMemoryRateLimitBackend",
    "PublicRateLimitExceeded",
    "PublicRateLimitUnavailable",
    "RateLimitDecision",
    "ValkeyRateLimitBackend",
    "close_public_rate_limiter",
    "enforce_public_request",
    "get_public_rate_limit_backend",
    "install_public_rate_limit_backend_for_testing",
    "public_network_identifier",
    "public_rate_limit_health",
    "public_request_requires_limiter",
]
