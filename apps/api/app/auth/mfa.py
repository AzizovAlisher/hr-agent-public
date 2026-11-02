"""TOTP and recovery-code primitives with encrypted secret persistence."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings


_TOTP_PERIOD_SECONDS = 30
_TOTP_DIGITS = 6
_RECOVERY_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def _root_key() -> bytes:
    settings = get_settings()
    material = settings.auth_mfa_encryption_key or settings.jwt_secret
    return hashlib.sha256(
        b"hr-agent/auth-mfa-encryption/v1\0" + material.encode("utf-8")
    ).digest()


def encrypt_totp_secret(secret: str) -> str:
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(_root_key()).encrypt(
        nonce,
        secret.encode("ascii"),
        b"hr-agent:totp-secret:v1",
    )
    encoded = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
    return f"v1.{encoded}"


def decrypt_totp_secret(ciphertext: str) -> str:
    try:
        version, encoded = ciphertext.split(".", 1)
        if version != "v1":
            raise ValueError("unsupported secret version")
        payload = base64.urlsafe_b64decode(encoded.encode("ascii"))
        if len(payload) < 29:
            raise ValueError("invalid encrypted secret")
        plaintext = AESGCM(_root_key()).decrypt(
            payload[:12],
            payload[12:],
            b"hr-agent:totp-secret:v1",
        )
        return plaintext.decode("ascii")
    except (InvalidTag, UnicodeError, ValueError) as exc:
        raise ValueError("Unable to decrypt MFA secret") from exc


def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _decode_base32(secret: str) -> bytes:
    padding = "=" * ((8 - len(secret) % 8) % 8)
    try:
        return base64.b32decode((secret + padding).upper(), casefold=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("Invalid TOTP secret") from exc


def totp_code_for_step(secret: str, step: int) -> str:
    digest = hmac.new(
        _decode_base32(secret),
        struct.pack(">Q", step),
        hashlib.sha1,
    ).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % (10**_TOTP_DIGITS):0{_TOTP_DIGITS}d}"


def match_totp(
    secret: str,
    code: str,
    *,
    at: datetime | None = None,
    last_used_step: int | None = None,
) -> int | None:
    """Return the accepted time step, rejecting malformed or replayed codes."""

    if len(code) != _TOTP_DIGITS or not code.isascii() or not code.isdigit():
        return None
    now = at or datetime.now(UTC)
    current_step = int(now.timestamp()) // _TOTP_PERIOD_SECONDS
    # Prefer the current window, then allow one window of clock skew each way.
    for step in (current_step, current_step - 1, current_step + 1):
        if step < 0 or (last_used_step is not None and step <= last_used_step):
            continue
        if hmac.compare_digest(totp_code_for_step(secret, step), code):
            return step
    return None


def provisioning_uri(*, secret: str, email: str) -> str:
    issuer = get_settings().auth_mfa_issuer
    label = quote(f"{issuer}:{email}", safe="")
    query = urlencode(
        {
            "secret": secret,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": str(_TOTP_DIGITS),
            "period": str(_TOTP_PERIOD_SECONDS),
        }
    )
    return f"otpauth://totp/{label}?{query}"


def generate_recovery_codes(count: int = 10) -> list[str]:
    codes: list[str] = []
    for _ in range(count):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(12))
        codes.append(f"{raw[:4]}-{raw[4:8]}-{raw[8:]}")
    return codes


def normalize_recovery_code(code: str) -> str:
    return "".join(character for character in code.upper() if character.isalnum())


def recovery_code_digest(code: str) -> str:
    normalized = normalize_recovery_code(code)
    key = hashlib.sha256(b"hr-agent/mfa-recovery/v1\0" + _root_key()).digest()
    return hmac.new(key, normalized.encode("ascii", "ignore"), hashlib.sha256).hexdigest()
