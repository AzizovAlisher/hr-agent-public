import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import created_at, updated_at, uuid_pk


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="recruiter")
    locale: Mapped[str] = mapped_column(String(8), nullable=False, default="en")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    mfa_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # TOTP material is encrypted by the application before persistence.  It is
    # deliberately absent from every API response schema.
    mfa_secret_ciphertext: Mapped[str | None] = mapped_column(
        String(2048), nullable=True
    )
    mfa_pending_secret_ciphertext: Mapped[str | None] = mapped_column(
        String(2048), nullable=True
    )
    mfa_last_used_step: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        CheckConstraint(
            "role IN ('admin', 'recruiter', 'hiring_manager', 'interviewer', 'restricted')",
            name="ck_users_role",
        ),
        CheckConstraint("token_version >= 0", name="ck_user_token_version"),
        CheckConstraint(
            "mfa_last_used_step IS NULL OR mfa_last_used_step >= 0",
            name="ck_user_mfa_last_used_step",
        ),
        CheckConstraint(
            "mfa_enabled = false OR mfa_secret_ciphertext IS NOT NULL",
            name="ck_user_enabled_mfa_has_secret",
        ),
    )


class AuthLoginThrottle(Base):
    """Durable, privacy-preserving counters for account and network limits."""

    __tablename__ = "auth_login_throttles"

    id: Mapped[uuid.UUID] = uuid_pk()
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    identifier_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    failed_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    blocked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        UniqueConstraint(
            "scope", "identifier_digest", name="uq_auth_throttle_scope_digest"
        ),
        CheckConstraint(
            "scope IN ('account', 'network')", name="ck_auth_throttle_scope"
        ),
        CheckConstraint(
            "failed_attempts >= 0", name="ck_auth_throttle_failed_attempts"
        ),
        Index("ix_auth_throttle_blocked_until", "blocked_until"),
    )


class RevokedAccessToken(Base):
    """One-token logout denylist, keyed by a digest of the random JWT id."""

    __tablename__ = "revoked_access_tokens"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    jti_digest: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    revoked_at: Mapped[datetime] = created_at()


class AuthRefreshSession(Base):
    """A revocable device/session family that survives token rotation."""

    __tablename__ = "auth_refresh_sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoke_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        CheckConstraint(
            "token_version >= 0", name="ck_auth_refresh_session_token_version"
        ),
        Index("ix_auth_refresh_session_user_active", "user_id", "revoked_at"),
    )


class AuthRefreshToken(Base):
    """One opaque refresh-token generation; raw tokens are never persisted."""

    __tablename__ = "auth_refresh_tokens"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auth_refresh_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_digest: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    replaced_by_token_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auth_refresh_tokens.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = created_at()


class UserMfaRecoveryCode(Base):
    """Single-use recovery codes; only keyed digests are stored."""

    __tablename__ = "user_mfa_recovery_codes"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        UniqueConstraint(
            "user_id", "code_digest", name="uq_user_mfa_recovery_code"
        ),
    )


class UserAccessToken(Base):
    """One-time invitation or password-reset capability.

    Raw tokens are returned or delivered once and are never persisted. State
    remains queryable by administrators without exposing credential material.
    """

    __tablename__ = "user_access_tokens"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    token_digest: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        CheckConstraint(
            "purpose IN ('invitation', 'password_reset')",
            name="ck_user_access_token_purpose",
        ),
        CheckConstraint(
            "expires_at > created_at", name="ck_user_access_token_expiry"
        ),
        CheckConstraint(
            "consumed_at IS NULL OR revoked_at IS NULL",
            name="ck_user_access_token_terminal_state",
        ),
        Index(
            "uq_user_access_token_active",
            "organization_id",
            "user_id",
            "purpose",
            unique=True,
            postgresql_where=text("consumed_at IS NULL AND revoked_at IS NULL"),
        ),
        Index(
            "ix_user_access_tokens_org_purpose_created",
            "organization_id",
            "purpose",
            text("created_at DESC"),
        ),
    )
