"""The global body ceiling counts ASGI chunks without buffering them."""

from __future__ import annotations

import json

import httpx
from fastapi import FastAPI, File, Request, UploadFile

from app.middleware.request_body_limit import RequestBodyLimitMiddleware


def _scope(
    *,
    content_length: str | None = None,
    method: str = "POST",
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> dict:
    headers = []
    if content_length is not None:
        headers.append((b"content-length", content_length.encode("ascii")))
    headers.extend(extra_headers or [])
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": "/upload",
        "raw_path": b"/upload",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
        "state": {"request_id": "body-limit-test"},
    }


async def _invoke(app, scope: dict, messages: list[dict]) -> list[dict]:
    sent: list[dict] = []
    incoming = iter(messages)

    async def receive() -> dict:
        return next(incoming)

    async def send(message: dict) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


async def test_content_length_is_rejected_before_downstream_receive():
    called = False

    async def downstream(_scope, _receive, _send):
        nonlocal called
        called = True

    app = RequestBodyLimitMiddleware(downstream, max_body_bytes=10)
    sent = await _invoke(app, _scope(content_length="11"), [])

    assert called is False
    assert sent[0]["status"] == 413
    problem = json.loads(sent[1]["body"])
    assert problem["code"] == "request_body_too_large"
    assert problem["request_id"] == "body-limit-test"


async def test_chunked_body_is_stopped_at_limit_without_buffering():
    seen: list[bytes] = []

    async def downstream(_scope, receive, send):
        while True:
            message = await receive()
            seen.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = RequestBodyLimitMiddleware(downstream, max_body_bytes=5)
    sent = await _invoke(
        app,
        _scope(),
        [
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"def", "more_body": False},
        ],
    )

    assert seen == [b"abc"]
    assert sent[0]["status"] == 413


async def test_streaming_consumer_receives_original_chunk_boundaries():
    seen: list[bytes] = []

    async def webhook(_scope, receive, send):
        while True:
            message = await receive()
            seen.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 202, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = RequestBodyLimitMiddleware(webhook, max_body_bytes=10)
    sent = await _invoke(
        app,
        _scope(),
        [
            {"type": "http.request", "body": b"one", "more_body": True},
            {"type": "http.request", "body": b"two", "more_body": False},
        ],
    )

    assert seen == [b"one", b"two"]
    assert sent[0]["status"] == 202


async def test_invalid_content_length_fails_closed():
    async def downstream(_scope, _receive, _send):  # pragma: no cover
        raise AssertionError("downstream must not run")

    app = RequestBodyLimitMiddleware(downstream, max_body_bytes=10)
    sent = await _invoke(app, _scope(content_length="+9"), [])
    assert sent[0]["status"] == 400
    assert json.loads(sent[1]["body"])["code"] == "request_body_length_invalid"


async def test_ambiguous_message_framing_and_get_bodies_are_bounded():
    async def downstream(_scope, _receive, _send):  # pragma: no cover
        raise AssertionError("downstream must not run")

    app = RequestBodyLimitMiddleware(downstream, max_body_bytes=10)
    ambiguous = await _invoke(
        app,
        _scope(
            content_length="9",
            extra_headers=[(b"transfer-encoding", b"chunked")],
        ),
        [],
    )
    oversized_get = await _invoke(
        app,
        _scope(content_length="11", method="GET"),
        [],
    )
    assert ambiguous[0]["status"] == 400
    assert oversized_get[0]["status"] == 413


async def test_fastapi_multipart_parser_never_reaches_endpoint_when_oversized():
    application = FastAPI()
    application.add_middleware(RequestBodyLimitMiddleware, max_body_bytes=64)
    called = False

    @application.post("/upload")
    async def upload(file: UploadFile = File(...)) -> dict[str, str]:
        nonlocal called
        called = True
        return {"filename": file.filename or ""}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/upload",
            files={"file": ("large.txt", b"x" * 128, "text/plain")},
        )
    assert response.status_code == 413
    assert response.json()["code"] == "request_body_too_large"
    assert called is False


async def test_fastapi_chunked_stream_returns_413_not_internal_error():
    application = FastAPI()
    application.add_middleware(RequestBodyLimitMiddleware, max_body_bytes=5)

    @application.post("/webhook")
    async def webhook(request: Request) -> dict[str, bool]:
        async for _chunk in request.stream():
            pass
        return {"accepted": True}

    async def chunks():
        yield b"abc"
        yield b"def"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.post("/webhook", content=chunks())
    assert response.status_code == 413
    assert response.json()["code"] == "request_body_too_large"
