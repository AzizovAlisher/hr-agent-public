"""Persistence for the organization-shared AI Hiring Assistant.

The AI workspace deliberately owns its conversation, planning, execution, and
evidence records while referencing the canonical recruiting entities.  Models
that describe AI output are versioned or append-only; mutations of candidates,
positions, and other human-platform records are represented by approval
bundles and executed by deterministic application services.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import created_at, updated_at, uuid_pk


class AiOrganizationSettings(Base):
    __tablename__ = "ai_organization_settings"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    feature_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    default_headcount: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    default_shortlist_size: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default="5"
    )
    default_model_budget_cents: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    hiring_defaults: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    autonomy_policy: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        CheckConstraint(
            "default_headcount BETWEEN 1 AND 100",
            name="ck_ai_settings_default_headcount",
        ),
        CheckConstraint(
            "default_shortlist_size BETWEEN 1 AND 20",
            name="ck_ai_settings_shortlist_size",
        ),
        CheckConstraint(
            "default_model_budget_cents IS NULL "
            "OR default_model_budget_cents >= 0",
            name="ck_ai_settings_model_budget",
        ),
    )


class AiSession(Base):
    __tablename__ = "ai_sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    messages: Mapped[list["AiMessage"]] = relationship(
        "AiMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="AiMessage.sequence",
    )
    mission: Mapped["HiringMission | None"] = relationship(
        "HiringMission",
        back_populates="session",
        cascade="all, delete-orphan",
        uselist=False,
    )
    agent_runs: Mapped[list["AiAgentRun"]] = relationship(
        "AiAgentRun", back_populates="session"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'archived')", name="ck_ai_session_status"
        ),
        Index(
            "ix_ai_sessions_org_status_updated",
            "organization_id",
            "status",
            text("updated_at DESC"),
        ),
    )


class HiringMission(Base):
    __tablename__ = "hiring_missions"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_sessions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    position_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("positions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    original_request: Mapped[str] = mapped_column(Text, nullable=False)
    brief: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    rubric: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    assumptions: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    blockers: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    headcount: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    shortlist_size: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default="5"
    )
    model_budget_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="draft", server_default="draft"
    )
    current_plan_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=uuid.uuid4, unique=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stopped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    session: Mapped[AiSession] = relationship("AiSession", back_populates="mission")
    plan_versions: Mapped[list["MissionPlanVersion"]] = relationship(
        "MissionPlanVersion",
        back_populates="mission",
        cascade="all, delete-orphan",
        order_by="MissionPlanVersion.version",
    )
    steps: Mapped[list["MissionStep"]] = relationship(
        "MissionStep", back_populates="mission", cascade="all, delete-orphan"
    )
    assessments: Mapped[list["MissionCandidateAssessment"]] = relationship(
        "MissionCandidateAssessment",
        back_populates="mission",
        cascade="all, delete-orphan",
    )
    evidence_claims: Mapped[list["EvidenceClaim"]] = relationship(
        "EvidenceClaim", back_populates="mission", cascade="all, delete-orphan"
    )
    action_bundles: Mapped[list["AiActionBundle"]] = relationship(
        "AiActionBundle", back_populates="mission", cascade="all, delete-orphan"
    )
    agent_runs: Mapped[list["AiAgentRun"]] = relationship(
        "AiAgentRun", back_populates="mission", cascade="all, delete-orphan"
    )
    events: Mapped[list["MissionEvent"]] = relationship(
        "MissionEvent",
        back_populates="mission",
        cascade="all, delete-orphan",
        order_by="MissionEvent.sequence",
    )

    __table_args__ = (
        CheckConstraint("btrim(title) <> ''", name="ck_hiring_mission_title"),
        CheckConstraint(
            "btrim(original_request) <> ''", name="ck_hiring_mission_request"
        ),
        CheckConstraint(
            "headcount BETWEEN 1 AND 100", name="ck_hiring_mission_headcount"
        ),
        CheckConstraint(
            "shortlist_size BETWEEN 1 AND 20",
            name="ck_hiring_mission_shortlist_size",
        ),
        CheckConstraint(
            "current_plan_version >= 0", name="ck_hiring_mission_plan_version"
        ),
        CheckConstraint(
            "model_budget_cents IS NULL OR model_budget_cents >= 0",
            name="ck_hiring_mission_model_budget",
        ),
        CheckConstraint(
            "status IN ("
            "'draft', 'planning', 'running', 'awaiting_approval', "
            "'completed', 'stopped', 'failed'"
            ")",
            name="ck_hiring_mission_status",
        ),
        Index(
            "ix_hiring_missions_org_status_updated",
            "organization_id",
            "status",
            text("updated_at DESC"),
        ),
    )


class MissionPlanVersion(Base):
    __tablename__ = "mission_plan_versions"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    mission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hiring_missions.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    brief_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    rubric_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    plan: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = created_at()

    mission: Mapped[HiringMission] = relationship(
        "HiringMission", back_populates="plan_versions"
    )
    steps: Mapped[list["MissionStep"]] = relationship(
        "MissionStep",
        back_populates="plan_version",
        cascade="all, delete-orphan",
        order_by="MissionStep.ordinal",
    )
    assessments: Mapped[list["MissionCandidateAssessment"]] = relationship(
        "MissionCandidateAssessment",
        back_populates="plan_version_record",
        foreign_keys="MissionCandidateAssessment.plan_version_id",
    )
    evidence_claims: Mapped[list["EvidenceClaim"]] = relationship(
        "EvidenceClaim", back_populates="plan_version"
    )
    action_bundles: Mapped[list["AiActionBundle"]] = relationship(
        "AiActionBundle", back_populates="plan_version_record"
    )

    __table_args__ = (
        UniqueConstraint(
            "mission_id", "version", name="uq_mission_plan_version"
        ),
        UniqueConstraint(
            "organization_id",
            "id",
            "mission_id",
            "version",
            name="uq_mission_plan_exact_identity",
        ),
        CheckConstraint("version >= 1", name="ck_mission_plan_version_number"),
        CheckConstraint(
            "status IN ('active', 'superseded', 'completed', 'cancelled')",
            name="ck_mission_plan_status",
        ),
        CheckConstraint(
            "input_hash ~ '^[0-9a-f]{64}$'", name="ck_mission_plan_input_hash"
        ),
        Index(
            "ix_mission_plan_versions_mission_created",
            "mission_id",
            text("created_at DESC"),
        ),
    )


class MissionStep(Base):
    __tablename__ = "mission_steps"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    mission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hiring_missions.id", ondelete="CASCADE"),
        nullable=False,
    )
    plan_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mission_plan_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    step_type: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    dependencies: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    input_data: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    safe_result: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    policy_outcome: Mapped[str | None] = mapped_column(String(24), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    mission: Mapped[HiringMission] = relationship(
        "HiringMission", back_populates="steps"
    )
    plan_version: Mapped[MissionPlanVersion] = relationship(
        "MissionPlanVersion", back_populates="steps"
    )
    attempts: Mapped[list["MissionStepAttempt"]] = relationship(
        "MissionStepAttempt",
        back_populates="step",
        cascade="all, delete-orphan",
        order_by="MissionStepAttempt.attempt_number",
    )
    agent_runs: Mapped[list["AiAgentRun"]] = relationship(
        "AiAgentRun", back_populates="step"
    )

    __table_args__ = (
        UniqueConstraint(
            "plan_version_id", "key", name="uq_mission_step_plan_key"
        ),
        UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_mission_step_org_idempotency",
        ),
        CheckConstraint("btrim(key) <> ''", name="ck_mission_step_key"),
        CheckConstraint(
            "btrim(step_type) <> ''", name="ck_mission_step_type"
        ),
        CheckConstraint("ordinal >= 0", name="ck_mission_step_ordinal"),
        CheckConstraint(
            "max_attempts BETWEEN 1 AND 10", name="ck_mission_step_max_attempts"
        ),
        CheckConstraint(
            "status IN ("
            "'pending', 'ready', 'running', 'completed', 'failed', "
            "'blocked', 'cancelled', 'skipped'"
            ")",
            name="ck_mission_step_status",
        ),
        CheckConstraint(
            "policy_outcome IS NULL OR policy_outcome IN ("
            "'auto', 'auto_notify', 'approval_required', 'forbidden'"
            ")",
            name="ck_mission_step_policy_outcome",
        ),
        Index(
            "ix_mission_steps_claimable",
            "status",
            "available_at",
            "lease_expires_at",
        ),
        Index(
            "ix_mission_steps_mission_plan_ordinal",
            "mission_id",
            "plan_version_id",
            "ordinal",
        ),
    )


class AiAgentRun(Base):
    __tablename__ = "ai_agent_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    mission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hiring_missions.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mission_steps.id", ondelete="SET NULL"),
        nullable=True,
    )
    initiated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    agent_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="queued", server_default="queued"
    )
    model_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    safe_input_metadata: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    safe_result: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    validation_outcome: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default="pending"
    )
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=uuid.uuid4, index=True
    )
    causation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()

    session: Mapped[AiSession | None] = relationship(
        "AiSession", back_populates="agent_runs"
    )
    mission: Mapped[HiringMission] = relationship(
        "HiringMission", back_populates="agent_runs"
    )
    step: Mapped[MissionStep | None] = relationship(
        "MissionStep", back_populates="agent_runs"
    )
    messages: Mapped[list["AiMessage"]] = relationship(
        "AiMessage", back_populates="agent_run"
    )
    attempts: Mapped[list["MissionStepAttempt"]] = relationship(
        "MissionStepAttempt", back_populates="agent_run"
    )
    assessments: Mapped[list["MissionCandidateAssessment"]] = relationship(
        "MissionCandidateAssessment",
        back_populates="agent_run",
        foreign_keys="MissionCandidateAssessment.agent_run_id",
    )
    evidence_claims: Mapped[list["EvidenceClaim"]] = relationship(
        "EvidenceClaim",
        back_populates="agent_run",
        foreign_keys="EvidenceClaim.agent_run_id",
    )
    proposed_bundles: Mapped[list["AiActionBundle"]] = relationship(
        "AiActionBundle", back_populates="proposed_by_agent_run"
    )
    events: Mapped[list["MissionEvent"]] = relationship(
        "MissionEvent", back_populates="agent_run"
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "id",
            "mission_id",
            name="uq_ai_agent_run_mission_identity",
        ),
        CheckConstraint(
            "status IN ("
            "'queued', 'running', 'completed', 'failed', 'cancelled', "
            "'budget_exhausted'"
            ")",
            name="ck_ai_agent_run_status",
        ),
        CheckConstraint(
            "validation_outcome IN ('pending', 'valid', 'invalid', 'not_applicable')",
            name="ck_ai_agent_run_validation",
        ),
        CheckConstraint(
            "input_hash ~ '^[0-9a-f]{64}$'", name="ck_ai_agent_run_input_hash"
        ),
        CheckConstraint(
            "tokens_in IS NULL OR tokens_in >= 0", name="ck_ai_agent_run_tokens_in"
        ),
        CheckConstraint(
            "tokens_out IS NULL OR tokens_out >= 0",
            name="ck_ai_agent_run_tokens_out",
        ),
        CheckConstraint(
            "cost_cents IS NULL OR cost_cents >= 0", name="ck_ai_agent_run_cost"
        ),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0", name="ck_ai_agent_run_latency"
        ),
        Index(
            "ix_ai_agent_runs_mission_created",
            "mission_id",
            text("created_at DESC"),
        ),
    )


class AiMessage(Base):
    __tablename__ = "ai_messages"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_agent_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    structured_content: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    attachments: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    redaction_metadata: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="completed", server_default="completed"
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()

    session: Mapped[AiSession] = relationship("AiSession", back_populates="messages")
    agent_run: Mapped[AiAgentRun | None] = relationship(
        "AiAgentRun", back_populates="messages"
    )

    __table_args__ = (
        UniqueConstraint(
            "session_id", "sequence", name="uq_ai_message_session_sequence"
        ),
        CheckConstraint(
            "role IN ('user', 'assistant', 'system', 'tool')",
            name="ck_ai_message_role",
        ),
        CheckConstraint(
            "status IN ('streaming', 'completed', 'failed')",
            name="ck_ai_message_status",
        ),
        CheckConstraint("sequence >= 1", name="ck_ai_message_sequence"),
        Index(
            "ix_ai_messages_org_session_sequence",
            "organization_id",
            "session_id",
            "sequence",
        ),
    )


class MissionStepAttempt(Base):
    __tablename__ = "mission_step_attempts"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mission_steps.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_agent_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="running", server_default="running"
    )
    worker_id: Mapped[str] = mapped_column(String(255), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    safe_result: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()

    step: Mapped[MissionStep] = relationship(
        "MissionStep", back_populates="attempts"
    )
    agent_run: Mapped[AiAgentRun | None] = relationship(
        "AiAgentRun", back_populates="attempts"
    )

    __table_args__ = (
        UniqueConstraint(
            "step_id", "attempt_number", name="uq_mission_step_attempt_number"
        ),
        CheckConstraint(
            "attempt_number BETWEEN 1 AND 10",
            name="ck_mission_step_attempt_number",
        ),
        CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'cancelled')",
            name="ck_mission_step_attempt_status",
        ),
        CheckConstraint(
            "input_hash ~ '^[0-9a-f]{64}$'",
            name="ck_mission_step_attempt_input_hash",
        ),
        Index(
            "ix_mission_step_attempts_step_created",
            "step_id",
            text("created_at DESC"),
        ),
    )


class MissionCandidateAssessment(Base):
    """Append-only, mission-specific candidate assessment."""

    __tablename__ = "mission_candidate_assessments"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    mission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hiring_missions.id", ondelete="CASCADE"),
        nullable=False,
    )
    plan_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mission_plan_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_agent_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    assessment_version: Mapped[int] = mapped_column(Integer, nullable=False)
    overall_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False)
    recommendation: Mapped[str] = mapped_column(String(32), nullable=False)
    criteria: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    concerns: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    missing_evidence: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    evidence_coverage: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), nullable=False, default=0, server_default="0"
    )
    concise_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at()

    mission: Mapped[HiringMission] = relationship(
        "HiringMission", back_populates="assessments"
    )
    plan_version_record: Mapped[MissionPlanVersion] = relationship(
        "MissionPlanVersion",
        back_populates="assessments",
        foreign_keys=[plan_version_id],
    )
    agent_run: Mapped[AiAgentRun | None] = relationship(
        "AiAgentRun", back_populates="assessments", foreign_keys=[agent_run_id]
    )
    evidence_claims: Mapped[list["EvidenceClaim"]] = relationship(
        "EvidenceClaim",
        back_populates="assessment",
        cascade="all, delete-orphan",
        foreign_keys="EvidenceClaim.assessment_id",
    )

    __table_args__ = (
        UniqueConstraint(
            "mission_id",
            "candidate_id",
            "plan_version",
            "assessment_version",
            name="uq_mission_candidate_assessment_version",
        ),
        UniqueConstraint(
            "organization_id",
            "id",
            "mission_id",
            "plan_version_id",
            "candidate_id",
            name="uq_mission_assessment_exact_identity",
        ),
        ForeignKeyConstraint(
            ["organization_id", "plan_version_id", "mission_id", "plan_version"],
            [
                "mission_plan_versions.organization_id",
                "mission_plan_versions.id",
                "mission_plan_versions.mission_id",
                "mission_plan_versions.version",
            ],
            name="fk_mission_assessment_exact_plan",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["organization_id", "agent_run_id", "mission_id"],
            [
                "ai_agent_runs.organization_id",
                "ai_agent_runs.id",
                "ai_agent_runs.mission_id",
            ],
            name="fk_mission_assessment_exact_agent_run",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "plan_version >= 1", name="ck_mission_assessment_plan_version"
        ),
        CheckConstraint(
            "assessment_version >= 1",
            name="ck_mission_assessment_version",
        ),
        CheckConstraint(
            "overall_score BETWEEN 0 AND 100",
            name="ck_mission_assessment_score",
        ),
        CheckConstraint(
            "evidence_coverage BETWEEN 0 AND 1",
            name="ck_mission_assessment_evidence_coverage",
        ),
        CheckConstraint(
            "confidence IN ('low', 'medium', 'high')",
            name="ck_mission_assessment_confidence",
        ),
        CheckConstraint(
            "recommendation IN ("
            "'strong_fit', 'fit', 'possible_fit', "
            "'insufficient_evidence', 'not_recommended'"
            ")",
            name="ck_mission_assessment_recommendation",
        ),
        Index(
            "ix_mission_assessments_rank",
            "mission_id",
            "plan_version",
            text("overall_score DESC"),
            text("evidence_coverage DESC"),
            "candidate_id",
        ),
        Index(
            "ix_mission_assessments_candidate_latest",
            "mission_id",
            "candidate_id",
            text("created_at DESC"),
        ),
    )


class EvidenceClaim(Base):
    """Append-only evidence supporting or contradicting an assessment."""

    __tablename__ = "evidence_claims"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    mission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hiring_missions.id", ondelete="CASCADE"),
        nullable=False,
    )
    plan_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mission_plan_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mission_candidate_assessments.id", ondelete="CASCADE"),
        nullable=False,
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_agent_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    criterion_key: Mapped[str] = mapped_column(String(64), nullable=False)
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Source records may be deleted while their safe evidence history remains.
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    source_locator: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    stance: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = created_at()

    mission: Mapped[HiringMission] = relationship(
        "HiringMission", back_populates="evidence_claims"
    )
    plan_version: Mapped[MissionPlanVersion] = relationship(
        "MissionPlanVersion", back_populates="evidence_claims"
    )
    assessment: Mapped[MissionCandidateAssessment] = relationship(
        "MissionCandidateAssessment",
        back_populates="evidence_claims",
        foreign_keys=[assessment_id],
    )
    agent_run: Mapped[AiAgentRun | None] = relationship(
        "AiAgentRun", back_populates="evidence_claims", foreign_keys=[agent_run_id]
    )

    __table_args__ = (
        ForeignKeyConstraint(
            [
                "organization_id",
                "assessment_id",
                "mission_id",
                "plan_version_id",
                "candidate_id",
            ],
            [
                "mission_candidate_assessments.organization_id",
                "mission_candidate_assessments.id",
                "mission_candidate_assessments.mission_id",
                "mission_candidate_assessments.plan_version_id",
                "mission_candidate_assessments.candidate_id",
            ],
            name="fk_evidence_claim_exact_assessment",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["organization_id", "agent_run_id", "mission_id"],
            [
                "ai_agent_runs.organization_id",
                "ai_agent_runs.id",
                "ai_agent_runs.mission_id",
            ],
            name="fk_evidence_claim_exact_agent_run",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("btrim(claim) <> ''", name="ck_evidence_claim_nonempty"),
        CheckConstraint(
            "stance IN ('supports', 'contradicts', 'context')",
            name="ck_evidence_claim_stance",
        ),
        CheckConstraint(
            "confidence BETWEEN 0 AND 1", name="ck_evidence_claim_confidence"
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_evidence_claim_hash"
        ),
        Index(
            "ix_evidence_claims_assessment_criterion",
            "assessment_id",
            "criterion_key",
            "created_at",
        ),
        Index(
            "ix_evidence_claims_mission_candidate",
            "mission_id",
            "candidate_id",
        ),
    )


class AiActionBundle(Base):
    __tablename__ = "ai_action_bundles"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    mission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hiring_missions.id", ondelete="CASCADE"),
        nullable=False,
    )
    plan_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mission_plan_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    proposed_by_agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_agent_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    preview: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    action_payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    required_permission: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    approval_idempotency_key: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_result: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    mission: Mapped[HiringMission] = relationship(
        "HiringMission", back_populates="action_bundles"
    )
    plan_version_record: Mapped[MissionPlanVersion] = relationship(
        "MissionPlanVersion", back_populates="action_bundles"
    )
    proposed_by_agent_run: Mapped[AiAgentRun | None] = relationship(
        "AiAgentRun", back_populates="proposed_bundles"
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "approval_idempotency_key",
            name="uq_ai_action_bundle_approval_idempotency",
        ),
        CheckConstraint(
            "plan_version >= 1", name="ck_ai_action_bundle_plan_version"
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'executed', 'failed', 'stale')",
            name="ck_ai_action_bundle_status",
        ),
        CheckConstraint(
            "btrim(action_type) <> ''", name="ck_ai_action_bundle_type"
        ),
        CheckConstraint(
            "btrim(summary) <> ''", name="ck_ai_action_bundle_summary"
        ),
        Index(
            "ix_ai_action_bundles_org_status_created",
            "organization_id",
            "status",
            text("created_at DESC"),
        ),
        Index(
            "ix_ai_action_bundles_mission_plan",
            "mission_id",
            "plan_version",
        ),
    )


class MissionEvent(Base):
    """Append-only mission activity with deterministic per-mission order."""

    __tablename__ = "mission_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    mission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hiring_missions.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_agent_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    subject_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The subject may be deleted while the event remains available.
    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )
    created_at: Mapped[datetime] = created_at()

    mission: Mapped[HiringMission] = relationship(
        "HiringMission", back_populates="events"
    )
    agent_run: Mapped[AiAgentRun | None] = relationship(
        "AiAgentRun", back_populates="events"
    )

    __table_args__ = (
        UniqueConstraint(
            "mission_id", "sequence", name="uq_mission_event_sequence"
        ),
        CheckConstraint("sequence >= 1", name="ck_mission_event_sequence"),
        CheckConstraint(
            "actor_kind IN ('human', 'ai', 'system', 'public')",
            name="ck_mission_event_actor_kind",
        ),
        Index(
            "ix_mission_events_mission_time",
            "mission_id",
            text("occurred_at DESC"),
            text("id DESC"),
        ),
        Index(
            "ix_mission_events_org_type_time",
            "organization_id",
            "event_type",
            text("occurred_at DESC"),
        ),
    )
