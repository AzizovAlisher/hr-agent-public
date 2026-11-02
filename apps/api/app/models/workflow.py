"""Durable event-driven recruiting workflow rules and run history."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import created_at, updated_at, uuid_pk


class WorkflowRule(Base):
    __tablename__ = "workflow_rules"

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
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    trigger: Mapped[str] = mapped_column(String(64), nullable=False)
    conditions: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    actions: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    last_processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        CheckConstraint(
            "trigger IN ("
            "'candidate_added', 'candidate_scored', 'candidate_rescored', "
            "'stage_changed', 'scorecard_submitted', 'message_received', "
            "'interview_scheduled', 'candidate_inactive', 'feedback_overdue'"
            ")",
            name="ck_workflow_rule_trigger",
        ),
        CheckConstraint("version >= 1", name="ck_workflow_rule_version"),
        Index(
            "ix_workflow_rules_org_active_trigger",
            "organization_id",
            "is_active",
            "trigger",
        ),
        Index(
            "uq_workflow_rule_org_name_ci",
            "organization_id",
            text("lower(name)"),
            unique=True,
        ),
    )


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workflow_rules.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Unlike ``rule_id``, this audit identity survives rule deletion.  Runs
    # execute only the immutable snapshots below, never a mutable live rule.
    rule_id_snapshot: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    rule_version_snapshot: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    position_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("positions.id", ondelete="CASCADE"),
        nullable=True,
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    source_activity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidate_activities.id", ondelete="SET NULL"),
        nullable=True,
    )
    rule_name_snapshot: Mapped[str] = mapped_column(String(160), nullable=False)
    trigger_snapshot: Mapped[str] = mapped_column(String(64), nullable=False)
    conditions_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    actions_snapshot: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    replay_of_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued"
    )
    action_results: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    leased_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    leased_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_workflow_runs_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "replay_of_run_id"],
            ["workflow_runs.organization_id", "workflow_runs.id"],
            name="fk_workflow_run_tenant_replay_source",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "(application_id IS NOT NULL) = "
            "(candidate_id IS NOT NULL AND position_id IS NOT NULL)",
            name="ck_workflow_run_application_target",
        ),
        ForeignKeyConstraint(
            ["organization_id", "application_id", "candidate_id", "position_id"],
            [
                "applications.organization_id",
                "applications.id",
                "applications.candidate_id",
                "applications.position_id",
            ],
            name="fk_workflow_run_exact_application",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "rule_version_snapshot >= 1",
            name="ck_workflow_run_rule_version_snapshot",
        ),
        CheckConstraint(
            "jsonb_typeof(conditions_snapshot) = 'object'",
            name="ck_workflow_run_conditions_snapshot_object",
        ),
        CheckConstraint(
            "jsonb_typeof(actions_snapshot) = 'array'",
            name="ck_workflow_run_actions_snapshot_array",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'retrying', 'completed', "
            "'skipped', 'failed', 'dead_letter', 'cancelled')",
            name="ck_workflow_run_status",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= max_attempts "
            "AND max_attempts BETWEEN 1 AND 10",
            name="ck_workflow_run_attempts",
        ),
        CheckConstraint(
            "((leased_at IS NULL AND leased_by IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL) OR "
            "(leased_at IS NOT NULL AND leased_by IS NOT NULL "
            "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL))",
            name="ck_workflow_run_lease_tuple",
        ),
        CheckConstraint(
            "(status = 'running') = (lease_token IS NOT NULL)",
            name="ck_workflow_run_running_lease",
        ),
        CheckConstraint(
            "status NOT IN ('completed', 'skipped', 'failed', 'dead_letter', "
            "'cancelled') OR completed_at IS NOT NULL",
            name="ck_workflow_run_terminal_completed_at",
        ),
        CheckConstraint(
            "status <> 'cancelled' OR cancelled_at IS NOT NULL",
            name="ck_workflow_run_cancelled_at",
        ),
        # One durable outcome per rule input makes action execution replay-safe.
        # A deleted rule sets rule_id to null; PostgreSQL then permits retained
        # historical rows to coexist without blocking any live rule.
        UniqueConstraint("rule_id", "dedupe_key", name="uq_workflow_run_dedupe"),
        Index(
            "ix_workflow_runs_org_created",
            "organization_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_workflow_runs_claimable",
            "status",
            "available_at",
            "lease_expires_at",
            "created_at",
            "id",
            postgresql_where=text("status IN ('queued', 'retrying', 'running')"),
        ),
        Index(
            "uq_workflow_run_one_replay",
            "replay_of_run_id",
            unique=True,
            postgresql_where=text("replay_of_run_id IS NOT NULL"),
        ),
        Index(
            "ix_workflow_runs_org_application",
            "organization_id",
            "application_id",
        ),
    )
