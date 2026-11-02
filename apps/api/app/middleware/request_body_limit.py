"""Streaming ASGI request-body ceiling applied before multipart parsing.

The middleware never calls ``Request.body()`` and never buffers chunks.  It
therefore preserves streaming webhook consumers while ensuring Starlette's
multipart parser cannot spool an unbounded request before an endpoint-level
file limit is reached.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any


Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
AsgiApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class RequestBodyTooLarge(Exception):
    """Internal control flow raised before a downstream response starts."""


class RequestBodyLimitMiddleware:
    """Reject bodies beyond one bounded global ceiling without buffering."""

    def __init__(self, app: AsgiApp, *, max_body_bytes: int) -> None:
        if max_body_bytes < 1:
            raise ValueError("Request-body limit must be positive")
        self.app = app
        self.max_body_bytes = max_body_bytes

    @staticmethod
    def _content_length(scope: Scope) -> int | None:
        values = [
            value
            for name, value in scope.get("headers", [])
            if name.lower() == b"content-length"
        ]
        if not values:
            return None
        if len(values) != 1:
            raise ValueError("Ambiguous Content-Length")
        try:
            raw = values[0].decode("ascii")
            if not raw or raw.strip() != raw or not raw.isdecimal():
                raise ValueError
            length = int(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("Invalid Content-Length") from exc
        return length

    @staticmethod
    async def _problem(
        scope: Scope,
        send: Send,
        *,
        status: int,
        title: str,
        detail: str,
        code: str,
    ) -> None:
        state = scope.get("state") or {}
        request_id = state.get("request_id")
        payload = {
            "type": "about:blank",
            "title": title,
            "status": status,
            "detail": detail,
            "code": code,
            "request_id": str(request_id) if request_id is not None else None,
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = [
            (b"content-type", b"application/problem+json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"),
        ]
        if request_id is not None:
            headers.append((b"x-request-id", str(request_id).encode("ascii")))
        await send(
            {"type": "http.response.start", "status": status, "headers": headers}
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        try:
            content_length = self._content_length(scope)
            has_transfer_encoding = any(
                name.lower() == b"transfer-encoding"
                for name, _value in scope.get("headers", [])
            )
            if content_length is not None and has_transfer_encoding:
                raise ValueError("Ambiguous HTTP message framing")
        except ValueError:
            await self._problem(
                scope,
                send,
                status=400,
                title="Invalid request body length",
                detail="Content-Length must be one non-negative decimal value.",
                code="request_body_length_invalid",
            )
            return
        if content_length is not None and content_length > self.max_body_bytes:
            await self._problem(
                scope,
                send,
                status=413,
                title="Request body too large",
                detail="The request body exceeds the server-wide safety limit.",
                code="request_body_too_large",
            )
            return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise RequestBodyTooLarge
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except RequestBodyTooLarge:
            if response_started:
                # A streaming endpoint must read and validate its request before
                # starting its response. Do not attempt a second ASGI response.
                raise
            await self._problem(
                scope,
                send,
                status=413,
                title="Request body too large",
                detail="The request body exceeds the server-wide safety limit.",
                code="request_body_too_large",
            )


__all__ = ["RequestBodyLimitMiddleware", "RequestBodyTooLarge"]
