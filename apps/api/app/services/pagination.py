"""Stable, tamper-evident cursor primitives for tenant-scoped collections.

Cursors are deliberately an opaque transport contract: callers must return the
token unchanged.  The signed envelope prevents a client from changing a keyset
boundary, tenant, or filter claim while remaining stable across API replicas.
"""

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TypeAlias
from uuid import UUID

from fastapi import HTTPException, status

from app.config import get_settings

CursorScalar: TypeAlias = str | int | float | bool | None
CursorPayload: TypeAlias = dict[str, CursorScalar]

_CURSOR_VERSION = 1
_MAX_CURSOR_LENGTH = 2_048
_KEY_DOMAIN = b"hr-agent:collection-cursor:v1"


def collection_cursor_scope(
    namespace: str,
    *,
    organization_id: object,
    filters: Mapping[str, CursorScalar] | None = None,
) -> str:
    """Bind one cursor to a tenant and the complete effective query."""

    return cursor_scope_fingerprint(
        namespace,
        {
            "organization_id": str(organization_id),
            **dict(filters or {}),
        },
    )


def encode_collection_cursor(
    namespace: str,
    *,
    scope: str,
    boundary: Mapping[str, CursorScalar],
) -> str:
    """Encode a scoped keyset boundary with deterministic claims."""

    if not boundary or "scope" in boundary:
        raise ValueError("invalid collection cursor boundary")
    return encode_signed_cursor(namespace, {**dict(boundary), "scope": scope})


def decode_collection_cursor(
    value: str,
    *,
    namespace: str,
    scope: str,
    boundary_keys: set[str],
    detail: str = "Invalid collection cursor",
) -> CursorPayload:
    """Validate a scoped cursor and raise the canonical 422 on any mismatch."""

    try:
        payload = decode_signed_cursor(value, namespace=namespace)
        if set(payload) != {*boundary_keys, "scope"} or payload.get("scope") != scope:
            raise ValueError("cursor does not match this collection query")
        return {key: payload[key] for key in boundary_keys}
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "cursor_invalid", "detail": detail},
        ) from exc


def cursor_uuid(
    payload: Mapping[str, CursorScalar],
    key: str = "id",
    *,
    detail: str = "Invalid collection cursor",
) -> UUID:
    try:
        value = payload[key]
        if not isinstance(value, str):
            raise ValueError("cursor UUID must be a string")
        return UUID(value)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "cursor_invalid", "detail": detail},
        ) from exc


def cursor_datetime(
    payload: Mapping[str, CursorScalar],
    key: str,
    *,
    detail: str = "Invalid collection cursor",
) -> datetime:
    try:
        value = payload[key]
        if not isinstance(value, str):
            raise ValueError("cursor timestamp must be a string")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("cursor timestamp must include a timezone")
        return parsed.astimezone(UTC)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "cursor_invalid", "detail": detail},
        ) from exc


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    if not value:
        raise ValueError("empty cursor segment")
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError("invalid cursor encoding") from exc


def _signing_key() -> bytes:
    secret = get_settings().jwt_secret.encode("utf-8")
    return hmac.new(secret, _KEY_DOMAIN, hashlib.sha256).digest()


def encode_signed_cursor(namespace: str, payload: Mapping[str, CursorScalar]) -> str:
    """Return a URL-safe signed cursor for one collection namespace."""

    if not namespace or len(namespace) > 96:
        raise ValueError("invalid cursor namespace")
    envelope = {
        "namespace": namespace,
        "payload": dict(payload),
        "version": _CURSOR_VERSION,
    }
    raw = json.dumps(
        envelope,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(_signing_key(), raw, hashlib.sha256).digest()
    cursor = f"{_b64encode(raw)}.{_b64encode(signature)}"
    if len(cursor) > _MAX_CURSOR_LENGTH:
        raise ValueError("cursor payload is too large")
    return cursor


def decode_signed_cursor(value: str, *, namespace: str) -> CursorPayload:
    """Verify and decode a cursor, rejecting another collection's token."""

    if not value or len(value) > _MAX_CURSOR_LENGTH or value.count(".") != 1:
        raise ValueError("invalid cursor")
    encoded, encoded_signature = value.split(".", 1)
    raw = _b64decode(encoded)
    signature = _b64decode(encoded_signature)
    expected = hmac.new(_signing_key(), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, signature):
        raise ValueError("invalid cursor signature")
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor payload") from exc
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"namespace", "payload", "version"}
        or envelope.get("version") != _CURSOR_VERSION
        or envelope.get("namespace") != namespace
        or not isinstance(envelope.get("payload"), dict)
    ):
        raise ValueError("cursor does not match this collection")
    payload = envelope["payload"]
    if any(
        not isinstance(item, (str, int, float, bool, type(None)))
        for item in payload.values()
    ):
        raise ValueError("invalid cursor claims")
    return payload


def cursor_scope_fingerprint(namespace: str, claims: Mapping[str, CursorScalar]) -> str:
    """Create a deterministic claim binding for tenant and query filters."""

    canonical = json.dumps(
        {"namespace": namespace, "claims": dict(claims)},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
