from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import jwt
from jwt import InvalidTokenError

from app.config import get_settings


MfaChallengePurpose = Literal["login", "setup"]


def encode_access_token(
    user_id: UUID,
    role: str,
    locale: str,
    *,
    token_version: int = 0,
    mfa_verified: bool = True,
    session_id: UUID | None = None,
) -> str:
    s = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iss": s.jwt_issuer,
        "aud": s.jwt_audience,
        "jti": str(uuid4()),
        "typ": "access",
        "ver": token_version,
        "mfa": mfa_verified,
        "sid": str(session_id) if session_id is not None else None,
        "role": role,
        "locale": locale,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=s.jwt_expires_minutes)).timestamp()),
    }
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    s = get_settings()
    try:
        payload = jwt.decode(
            token,
            s.jwt_secret,
            algorithms=[s.jwt_algorithm],
            audience=s.jwt_audience,
            issuer=s.jwt_issuer,
            options={
                "require": [
                    "sub",
                    "iss",
                    "aud",
                    "jti",
                    "iat",
                    "exp",
                    "typ",
                    "ver",
                    "mfa",
                    "role",
                    "locale",
                ]
            },
        )
    except InvalidTokenError as e:
        raise ValueError(f"Invalid token: {e}") from e
    if payload.get("typ") != "access":
        raise ValueError("Invalid token type")
    if (
        not isinstance(payload.get("ver"), int)
        or isinstance(payload.get("ver"), bool)
        or payload["ver"] < 0
    ):
        raise ValueError("Invalid token version")
    if not isinstance(payload.get("mfa"), bool):
        raise ValueError("Invalid MFA claim")
    if "sid" not in payload:
        raise ValueError("Missing session claim")
    if payload.get("sid") is not None:
        try:
            UUID(str(payload["sid"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid session claim") from exc
    return payload


def encode_mfa_challenge(
    user_id: UUID,
    token_version: int,
    purpose: MfaChallengePurpose,
) -> str:
    """Issue a short-lived token that cannot authorize ordinary API routes."""

    s = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iss": s.jwt_issuer,
        "aud": s.jwt_audience,
        "jti": str(uuid4()),
        "typ": "mfa_challenge",
        "purpose": purpose,
        "ver": token_version,
        "iat": int(now.timestamp()),
        "exp": int(
            (now + timedelta(minutes=s.auth_mfa_challenge_minutes)).timestamp()
        ),
    }
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode_mfa_challenge(
    token: str,
    *,
    purpose: MfaChallengePurpose,
) -> dict:
    s = get_settings()
    try:
        payload = jwt.decode(
            token,
            s.jwt_secret,
            algorithms=[s.jwt_algorithm],
            audience=s.jwt_audience,
            issuer=s.jwt_issuer,
            options={
                "require": [
                    "sub",
                    "iss",
                    "aud",
                    "jti",
                    "iat",
                    "exp",
                    "typ",
                    "purpose",
                    "ver",
                ]
            },
        )
    except InvalidTokenError as exc:
        raise ValueError("Invalid MFA challenge") from exc
    if payload.get("typ") != "mfa_challenge" or payload.get("purpose") != purpose:
        raise ValueError("Invalid MFA challenge")
    if (
        not isinstance(payload.get("ver"), int)
        or isinstance(payload.get("ver"), bool)
        or payload["ver"] < 0
    ):
        raise ValueError("Invalid MFA challenge")
    return payload
