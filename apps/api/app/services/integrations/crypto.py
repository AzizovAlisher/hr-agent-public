"""Purpose-bound AES-GCM storage for integration credentials and payloads."""

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class IntegrationCryptoError(RuntimeError):
    """Raised without exposing whether ciphertext, key, or context was wrong."""


@dataclass(frozen=True)
class EncryptedValue:
    ciphertext: bytes
    nonce: bytes
    key_id: str


def decode_integration_key(value: str) -> bytes:
    """Decode a URL-safe base64 256-bit key.

    Production configuration uses an explicit random key instead of deriving an
    encryption key from JWT or database credentials.
    """

    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode())
    except (ValueError, TypeError) as exc:
        raise IntegrationCryptoError("Integration encryption key is invalid") from exc
    if len(decoded) != 32:
        raise IntegrationCryptoError(
            "Integration encryption key must decode to exactly 32 bytes"
        )
    return decoded


class IntegrationCipher:
    def __init__(self, key: bytes, *, key_id: str | None = None) -> None:
        if len(key) != 32:
            raise ValueError("IntegrationCipher requires a 32-byte key")
        self._cipher = AESGCM(key)
        self.key_id = key_id or hashlib.sha256(key).hexdigest()[:16]

    @classmethod
    def from_base64(cls, value: str, *, key_id: str | None = None) -> "IntegrationCipher":
        return cls(decode_integration_key(value), key_id=key_id)

    def encrypt_bytes(self, value: bytes, *, aad: bytes) -> EncryptedValue:
        nonce = os.urandom(12)
        return EncryptedValue(
            ciphertext=self._cipher.encrypt(nonce, value, aad),
            nonce=nonce,
            key_id=self.key_id,
        )

    def decrypt_bytes(
        self,
        ciphertext: bytes,
        nonce: bytes,
        *,
        aad: bytes,
        key_id: str,
    ) -> bytes:
        if key_id != self.key_id:
            raise IntegrationCryptoError("Integration ciphertext cannot be decrypted")
        try:
            return self._cipher.decrypt(nonce, ciphertext, aad)
        except (InvalidTag, ValueError) as exc:
            raise IntegrationCryptoError(
                "Integration ciphertext cannot be decrypted"
            ) from exc

    def encrypt_json(self, value: Any, *, aad: bytes) -> EncryptedValue:
        try:
            payload = json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        except (TypeError, ValueError) as exc:
            raise IntegrationCryptoError("Integration value is not JSON serializable") from exc
        return self.encrypt_bytes(payload, aad=aad)

    def decrypt_json(
        self,
        ciphertext: bytes,
        nonce: bytes,
        *,
        aad: bytes,
        key_id: str,
    ) -> Any:
        raw = self.decrypt_bytes(
            ciphertext, nonce, aad=aad, key_id=key_id
        )
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrationCryptoError("Integration ciphertext cannot be decoded") from exc


def connection_aad(organization_id: object, connection_id: object, purpose: str) -> bytes:
    return f"hr-agent:integration:v1:{organization_id}:{connection_id}:{purpose}".encode()
