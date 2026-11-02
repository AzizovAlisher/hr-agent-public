"""Encrypted, tenant-bound provider cursor persistence.

Provider page tokens and sync checkpoints can contain opaque account metadata,
so they use the same purpose-separated AES-GCM boundary as credentials and job
payloads.  Callers receive plaintext only inside the connector worker.
"""

import json
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integration import IntegrationConnection, IntegrationCursor
from app.services.integrations.crypto import (
    IntegrationCipher,
    IntegrationCryptoError,
    connection_aad,
)

_CURSOR_NAME = re.compile(r"^[a-z][a-z0-9_.:-]{0,95}$")
_MAX_CURSOR_BYTES = 64_000


class IntegrationCursorError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _cursor_aad(connection: IntegrationConnection, cursor_name: str) -> bytes:
    return connection_aad(
        connection.organization_id,
        connection.id,
        f"cursor:{cursor_name}",
    )


def _validate_cursor(cursor_name: str, value: Any) -> bytes:
    if not _CURSOR_NAME.fullmatch(cursor_name):
        raise IntegrationCursorError("cursor_name_invalid")
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise IntegrationCursorError("cursor_value_invalid") from exc
    if len(encoded) > _MAX_CURSOR_BYTES:
        raise IntegrationCursorError("cursor_value_too_large")
    return encoded


async def store_cursor(
    db: AsyncSession,
    *,
    connection: IntegrationConnection,
    cursor_name: str,
    value: Any,
    cipher: IntegrationCipher,
) -> IntegrationCursor:
    """Atomically create or replace one named cursor for a connection."""

    encoded = _validate_cursor(cursor_name, value)
    lock_key = f"integration-cursor:{connection.id}:{cursor_name}"
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
    )
    cursor = (
        await db.execute(
            select(IntegrationCursor).where(
                IntegrationCursor.organization_id == connection.organization_id,
                IntegrationCursor.connection_id == connection.id,
                IntegrationCursor.cursor_name == cursor_name,
            )
        )
    ).scalar_one_or_none()
    encrypted = cipher.encrypt_bytes(
        encoded,
        aad=_cursor_aad(connection, cursor_name),
    )
    if cursor is None:
        cursor = IntegrationCursor(
            organization_id=connection.organization_id,
            connection_id=connection.id,
            cursor_name=cursor_name,
            value_ciphertext=encrypted.ciphertext,
            value_nonce=encrypted.nonce,
            value_key_id=encrypted.key_id,
        )
        db.add(cursor)
    else:
        cursor.value_ciphertext = encrypted.ciphertext
        cursor.value_nonce = encrypted.nonce
        cursor.value_key_id = encrypted.key_id
    await db.flush()
    return cursor


async def load_cursor(
    db: AsyncSession,
    *,
    connection: IntegrationConnection,
    cursor_name: str,
    cipher: IntegrationCipher,
) -> Any | None:
    """Return a decrypted cursor, or ``None`` when it has never been stored."""

    if not _CURSOR_NAME.fullmatch(cursor_name):
        raise IntegrationCursorError("cursor_name_invalid")
    cursor = (
        await db.execute(
            select(IntegrationCursor).where(
                IntegrationCursor.organization_id == connection.organization_id,
                IntegrationCursor.connection_id == connection.id,
                IntegrationCursor.cursor_name == cursor_name,
            )
        )
    ).scalar_one_or_none()
    if cursor is None:
        return None
    try:
        raw = cipher.decrypt_bytes(
            cursor.value_ciphertext,
            cursor.value_nonce,
            aad=_cursor_aad(connection, cursor_name),
            key_id=cursor.value_key_id,
        )
        return json.loads(raw)
    except (IntegrationCryptoError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrationCursorError("cursor_value_unavailable") from exc


async def delete_cursor(
    db: AsyncSession,
    *,
    connection: IntegrationConnection,
    cursor_name: str,
) -> bool:
    if not _CURSOR_NAME.fullmatch(cursor_name):
        raise IntegrationCursorError("cursor_name_invalid")
    cursor = (
        await db.execute(
            select(IntegrationCursor).where(
                IntegrationCursor.organization_id == connection.organization_id,
                IntegrationCursor.connection_id == connection.id,
                IntegrationCursor.cursor_name == cursor_name,
            )
        )
    ).scalar_one_or_none()
    if cursor is None:
        return False
    await db.delete(cursor)
    await db.flush()
    return True
