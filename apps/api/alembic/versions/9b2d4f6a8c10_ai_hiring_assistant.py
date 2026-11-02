"""AI Hiring Assistant persistence and candidate activity attribution.

Revision ID: 9b2d4f6a8c10
Revises: 8f6d0c4a2b75
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "9b2d4f6a8c10"
down_revision: str | Sequence[str] | None = "8f6d0c4a2b75"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def _id() -> sa.Column:
    return sa.Column("id", UUID, nullable=False)


def _organization_id() -> sa.Column:
    return sa.Column("organization_id", UUID, nullable=False)


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def _updated_at() -> sa.Column:
    return sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def _json_object(name: str) -> sa.Column:
    return sa.Column(
        name, JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False
    )


def _json_array(name: str) -> sa.Column:
    return sa.Column(
        name, JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "ai_organization_settings",
        _id(),
        _organization_id(),
        sa.Column(
            "feature_enabled", sa.Boolean(), server_default="false", nullable=False
        ),
        sa.Column(
            "default_headcount", sa.Integer(), server_default="1", nullable=False
        ),
        sa.Column(
            "default_shortlist_size",
            sa.Integer(),
            server_default="5",
            nullable=False,
        ),
        sa.Column("default_model_budget_cents", sa.Integer(), nullable=True),
        _json_object("hiring_defaults"),
        _json_object("autonomy_policy"),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(
            "default_headcount BETWEEN 1 AND 100",
            name="ck_ai_settings_default_headcount",
        ),
        sa.CheckConstraint(
            "default_shortlist_size BETWEEN 1 AND 20",
            name="ck_ai_settings_shortlist_size",
        ),
        sa.CheckConstraint(
            "default_model_budget_cents IS NULL "
            "OR default_model_budget_cents >= 0",
            name="ck_ai_settings_model_budget",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id"),
    )

    op.create_table(
        "ai_sessions",
        _id(),
        _organization_id(),
        sa.Column("created_by_user_id", UUID, nullable=True),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column(
            "status", sa.String(length=16), server_default="active", nullable=False
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(
            "status IN ('active', 'archived')", name="ck_ai_session_status"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_sessions_organization_id", "ai_sessions", ["organization_id"]
    )
    op.create_index(
        "ix_ai_sessions_org_status_updated",
        "ai_sessions",
        ["organization_id", "status", sa.text("updated_at DESC")],
    )

    op.create_table(
        "hiring_missions",
        _id(),
        _organization_id(),
        sa.Column("session_id", UUID, nullable=False),
        sa.Column("created_by_user_id", UUID, nullable=True),
        sa.Column("position_id", UUID, nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("original_request", sa.Text(), nullable=False),
        _json_object("brief"),
        _json_object("rubric"),
        _json_array("assumptions"),
        _json_array("blockers"),
        sa.Column("headcount", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "shortlist_size", sa.Integer(), server_default="5", nullable=False
        ),
        sa.Column(
            "status", sa.String(length=24), server_default="draft", nullable=False
        ),
        sa.Column(
            "current_plan_version",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint("btrim(title) <> ''", name="ck_hiring_mission_title"),
        sa.CheckConstraint(
            "btrim(original_request) <> ''", name="ck_hiring_mission_request"
        ),
        sa.CheckConstraint(
            "headcount BETWEEN 1 AND 100", name="ck_hiring_mission_headcount"
        ),
        sa.CheckConstraint(
            "shortlist_size BETWEEN 1 AND 20",
            name="ck_hiring_mission_shortlist_size",
        ),
        sa.CheckConstraint(
            "current_plan_version >= 0", name="ck_hiring_mission_plan_version"
        ),
        sa.CheckConstraint(
            "status IN ("
            "'draft', 'planning', 'running', 'awaiting_approval', "
            "'completed', 'stopped', 'failed'"
            ")",
            name="ck_hiring_mission_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["ai_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["position_id"], ["positions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id"),
        sa.UniqueConstraint("correlation_id"),
    )
    op.create_index(
        "ix_hiring_missions_organization_id",
        "hiring_missions",
        ["organization_id"],
    )
    op.create_index(
        "ix_hiring_missions_position_id", "hiring_missions", ["position_id"]
    )
    op.create_index(
        "ix_hiring_missions_org_status_updated",
        "hiring_missions",
        ["organization_id", "status", sa.text("updated_at DESC")],
    )

    op.create_table(
        "mission_plan_versions",
        _id(),
        _organization_id(),
        sa.Column("mission_id", UUID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default="active", nullable=False
        ),
        _json_object("brief_snapshot"),
        _json_object("rubric_snapshot"),
        _json_object("plan"),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        _created_at(),
        sa.CheckConstraint("version >= 1", name="ck_mission_plan_version_number"),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'completed', 'cancelled')",
            name="ck_mission_plan_status",
        ),
        sa.CheckConstraint(
            "input_hash ~ '^[0-9a-f]{64}$'", name="ck_mission_plan_input_hash"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["mission_id"], ["hiring_missions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "mission_id", "version", name="uq_mission_plan_version"
        ),
    )
    op.create_index(
        "ix_mission_plan_versions_organization_id",
        "mission_plan_versions",
        ["organization_id"],
    )
    op.create_index(
        "ix_mission_plan_versions_mission_created",
        "mission_plan_versions",
        ["mission_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "mission_steps",
        _id(),
        _organization_id(),
        sa.Column("mission_id", UUID, nullable=False),
        sa.Column("plan_version_id", UUID, nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("step_type", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        _json_array("dependencies"),
        sa.Column(
            "status", sa.String(length=16), server_default="pending", nullable=False
        ),
        _json_object("input_data"),
        _json_object("safe_result"),
        sa.Column("policy_outcome", sa.String(length=24), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint("btrim(key) <> ''", name="ck_mission_step_key"),
        sa.CheckConstraint(
            "btrim(step_type) <> ''", name="ck_mission_step_type"
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_mission_step_ordinal"),
        sa.CheckConstraint(
            "max_attempts BETWEEN 1 AND 10", name="ck_mission_step_max_attempts"
        ),
        sa.CheckConstraint(
            "status IN ("
            "'pending', 'ready', 'running', 'completed', 'failed', "
            "'blocked', 'cancelled', 'skipped'"
            ")",
            name="ck_mission_step_status",
        ),
        sa.CheckConstraint(
            "policy_outcome IS NULL OR policy_outcome IN ("
            "'auto', 'auto_notify', 'approval_required', 'forbidden'"
            ")",
            name="ck_mission_step_policy_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["mission_id"], ["hiring_missions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["plan_version_id"], ["mission_plan_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plan_version_id", "key", name="uq_mission_step_plan_key"
        ),
        sa.UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_mission_step_org_idempotency",
        ),
    )
    op.create_index(
        "ix_mission_steps_organization_id", "mission_steps", ["organization_id"]
    )
    op.create_index(
        "ix_mission_steps_claimable",
        "mission_steps",
        ["status", "available_at", "lease_expires_at"],
    )
    op.create_index(
        "ix_mission_steps_mission_plan_ordinal",
        "mission_steps",
        ["mission_id", "plan_version_id", "ordinal"],
    )

    op.create_table(
        "ai_agent_runs",
        _id(),
        _organization_id(),
        sa.Column("session_id", UUID, nullable=True),
        sa.Column("mission_id", UUID, nullable=False),
        sa.Column("step_id", UUID, nullable=True),
        sa.Column("initiated_by_user_id", UUID, nullable=True),
        sa.Column("agent_type", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=24), server_default="queued", nullable=False
        ),
        sa.Column("model_provider", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("tool_name", sa.String(length=128), nullable=True),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        _json_object("safe_input_metadata"),
        _json_object("safe_result"),
        sa.Column(
            "validation_outcome",
            sa.String(length=24),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("cost_cents", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column("causation_id", UUID, nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.CheckConstraint(
            "status IN ("
            "'queued', 'running', 'completed', 'failed', 'cancelled', "
            "'budget_exhausted'"
            ")",
            name="ck_ai_agent_run_status",
        ),
        sa.CheckConstraint(
            "validation_outcome IN ('pending', 'valid', 'invalid', 'not_applicable')",
            name="ck_ai_agent_run_validation",
        ),
        sa.CheckConstraint(
            "input_hash ~ '^[0-9a-f]{64}$'", name="ck_ai_agent_run_input_hash"
        ),
        sa.CheckConstraint(
            "tokens_in IS NULL OR tokens_in >= 0", name="ck_ai_agent_run_tokens_in"
        ),
        sa.CheckConstraint(
            "tokens_out IS NULL OR tokens_out >= 0",
            name="ck_ai_agent_run_tokens_out",
        ),
        sa.CheckConstraint(
            "cost_cents IS NULL OR cost_cents >= 0", name="ck_ai_agent_run_cost"
        ),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0", name="ck_ai_agent_run_latency"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["ai_sessions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["mission_id"], ["hiring_missions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["step_id"], ["mission_steps.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["initiated_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_agent_runs_organization_id", "ai_agent_runs", ["organization_id"]
    )
    op.create_index(
        "ix_ai_agent_runs_correlation_id", "ai_agent_runs", ["correlation_id"]
    )
    op.create_index(
        "ix_ai_agent_runs_mission_created",
        "ai_agent_runs",
        ["mission_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "ai_messages",
        _id(),
        _organization_id(),
        sa.Column("session_id", UUID, nullable=False),
        sa.Column("actor_user_id", UUID, nullable=True),
        sa.Column("agent_run_id", UUID, nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        _json_object("structured_content"),
        _json_array("attachments"),
        _json_object("redaction_metadata"),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="completed",
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'system', 'tool')",
            name="ck_ai_message_role",
        ),
        sa.CheckConstraint(
            "status IN ('streaming', 'completed', 'failed')",
            name="ck_ai_message_status",
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_ai_message_sequence"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["ai_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["agent_run_id"], ["ai_agent_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id", "sequence", name="uq_ai_message_session_sequence"
        ),
    )
    op.create_index(
        "ix_ai_messages_organization_id", "ai_messages", ["organization_id"]
    )
    op.create_index(
        "ix_ai_messages_org_session_sequence",
        "ai_messages",
        ["organization_id", "session_id", "sequence"],
    )

    op.create_table(
        "mission_step_attempts",
        _id(),
        _organization_id(),
        sa.Column("step_id", UUID, nullable=False),
        sa.Column("agent_run_id", UUID, nullable=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default="running", nullable=False
        ),
        sa.Column("worker_id", sa.String(length=255), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        _json_object("safe_result"),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.CheckConstraint(
            "attempt_number BETWEEN 1 AND 10",
            name="ck_mission_step_attempt_number",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'cancelled')",
            name="ck_mission_step_attempt_status",
        ),
        sa.CheckConstraint(
            "input_hash ~ '^[0-9a-f]{64}$'",
            name="ck_mission_step_attempt_input_hash",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["step_id"], ["mission_steps.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["agent_run_id"], ["ai_agent_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "step_id", "attempt_number", name="uq_mission_step_attempt_number"
        ),
    )
    op.create_index(
        "ix_mission_step_attempts_organization_id",
        "mission_step_attempts",
        ["organization_id"],
    )
    op.create_index(
        "ix_mission_step_attempts_step_created",
        "mission_step_attempts",
        ["step_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "mission_candidate_assessments",
        _id(),
        _organization_id(),
        sa.Column("mission_id", UUID, nullable=False),
        sa.Column("plan_version_id", UUID, nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("candidate_id", UUID, nullable=False),
        sa.Column("agent_run_id", UUID, nullable=True),
        sa.Column("assessment_version", sa.Integer(), nullable=False),
        sa.Column("overall_score", sa.Numeric(5, 2), nullable=False),
        sa.Column("confidence", sa.String(length=16), nullable=False),
        sa.Column("recommendation", sa.String(length=32), nullable=False),
        _json_array("criteria"),
        _json_array("concerns"),
        _json_array("missing_evidence"),
        sa.Column(
            "evidence_coverage",
            sa.Numeric(5, 4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("concise_rationale", sa.Text(), nullable=True),
        _created_at(),
        sa.CheckConstraint(
            "plan_version >= 1", name="ck_mission_assessment_plan_version"
        ),
        sa.CheckConstraint(
            "assessment_version >= 1", name="ck_mission_assessment_version"
        ),
        sa.CheckConstraint(
            "overall_score BETWEEN 0 AND 100",
            name="ck_mission_assessment_score",
        ),
        sa.CheckConstraint(
            "evidence_coverage BETWEEN 0 AND 1",
            name="ck_mission_assessment_evidence_coverage",
        ),
        sa.CheckConstraint(
            "confidence IN ('low', 'medium', 'high')",
            name="ck_mission_assessment_confidence",
        ),
        sa.CheckConstraint(
            "recommendation IN ("
            "'strong_fit', 'fit', 'possible_fit', "
            "'insufficient_evidence', 'not_recommended'"
            ")",
            name="ck_mission_assessment_recommendation",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["mission_id"], ["hiring_missions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["plan_version_id"], ["mission_plan_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["candidates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["agent_run_id"], ["ai_agent_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "mission_id",
            "candidate_id",
            "plan_version",
            "assessment_version",
            name="uq_mission_candidate_assessment_version",
        ),
    )
    op.create_index(
        "ix_mission_candidate_assessments_organization_id",
        "mission_candidate_assessments",
        ["organization_id"],
    )
    op.create_index(
        "ix_mission_assessments_rank",
        "mission_candidate_assessments",
        [
            "mission_id",
            "plan_version",
            sa.text("overall_score DESC"),
            sa.text("evidence_coverage DESC"),
            "candidate_id",
        ],
    )
    op.create_index(
        "ix_mission_assessments_candidate_latest",
        "mission_candidate_assessments",
        ["mission_id", "candidate_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "evidence_claims",
        _id(),
        _organization_id(),
        sa.Column("mission_id", UUID, nullable=False),
        sa.Column("plan_version_id", UUID, nullable=False),
        sa.Column("assessment_id", UUID, nullable=False),
        sa.Column("candidate_id", UUID, nullable=False),
        sa.Column("agent_run_id", UUID, nullable=True),
        sa.Column("criterion_key", sa.String(length=64), nullable=False),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", UUID, nullable=True),
        _json_object("source_locator"),
        sa.Column("stance", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        _created_at(),
        sa.CheckConstraint("btrim(claim) <> ''", name="ck_evidence_claim_nonempty"),
        sa.CheckConstraint(
            "stance IN ('supports', 'contradicts', 'context')",
            name="ck_evidence_claim_stance",
        ),
        sa.CheckConstraint(
            "confidence BETWEEN 0 AND 1", name="ck_evidence_claim_confidence"
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_evidence_claim_hash"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["mission_id"], ["hiring_missions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["plan_version_id"], ["mission_plan_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["assessment_id"],
            ["mission_candidate_assessments.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["candidates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["agent_run_id"], ["ai_agent_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_evidence_claims_organization_id",
        "evidence_claims",
        ["organization_id"],
    )
    op.create_index(
        "ix_evidence_claims_assessment_criterion",
        "evidence_claims",
        ["assessment_id", "criterion_key", "created_at"],
    )
    op.create_index(
        "ix_evidence_claims_mission_candidate",
        "evidence_claims",
        ["mission_id", "candidate_id"],
    )

    op.create_table(
        "ai_action_bundles",
        _id(),
        _organization_id(),
        sa.Column("mission_id", UUID, nullable=False),
        sa.Column("plan_version_id", UUID, nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("proposed_by_agent_run_id", UUID, nullable=True),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=False),
        _json_object("preview"),
        _json_object("action_payload"),
        sa.Column("required_permission", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default="pending", nullable=False
        ),
        sa.Column("approval_idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("approved_by_user_id", UUID, nullable=True),
        sa.Column("decision_comment", sa.Text(), nullable=True),
        _json_object("execution_result"),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(
            "plan_version >= 1", name="ck_ai_action_bundle_plan_version"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'executed', 'failed', 'stale')",
            name="ck_ai_action_bundle_status",
        ),
        sa.CheckConstraint(
            "btrim(action_type) <> ''", name="ck_ai_action_bundle_type"
        ),
        sa.CheckConstraint(
            "btrim(summary) <> ''", name="ck_ai_action_bundle_summary"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["mission_id"], ["hiring_missions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["plan_version_id"], ["mission_plan_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["proposed_by_agent_run_id"], ["ai_agent_runs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "approval_idempotency_key",
            name="uq_ai_action_bundle_approval_idempotency",
        ),
    )
    op.create_index(
        "ix_ai_action_bundles_organization_id",
        "ai_action_bundles",
        ["organization_id"],
    )
    op.create_index(
        "ix_ai_action_bundles_org_status_created",
        "ai_action_bundles",
        ["organization_id", "status", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_ai_action_bundles_mission_plan",
        "ai_action_bundles",
        ["mission_id", "plan_version"],
    )

    op.create_table(
        "mission_events",
        _id(),
        _organization_id(),
        sa.Column("mission_id", UUID, nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("actor_user_id", UUID, nullable=True),
        sa.Column("agent_run_id", UUID, nullable=True),
        sa.Column("subject_type", sa.String(length=64), nullable=True),
        sa.Column("subject_id", UUID, nullable=True),
        _json_object("payload"),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _created_at(),
        sa.CheckConstraint("sequence >= 1", name="ck_mission_event_sequence"),
        sa.CheckConstraint(
            "actor_kind IN ('human', 'ai', 'system', 'public')",
            name="ck_mission_event_actor_kind",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["mission_id"], ["hiring_missions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["agent_run_id"], ["ai_agent_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "mission_id", "sequence", name="uq_mission_event_sequence"
        ),
    )
    op.create_index(
        "ix_mission_events_organization_id", "mission_events", ["organization_id"]
    )
    op.create_index(
        "ix_mission_events_mission_time",
        "mission_events",
        ["mission_id", sa.text("occurred_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_mission_events_org_type_time",
        "mission_events",
        ["organization_id", "event_type", sa.text("occurred_at DESC")],
    )

    # Preserve existing semantics: a historical activity with a user actor is
    # human; a null actor represents a system/background action.
    op.add_column(
        "candidate_activities",
        sa.Column("actor_kind", sa.String(length=16), nullable=True),
    )
    op.execute(
        """
        UPDATE candidate_activities
        SET actor_kind = CASE
            WHEN actor_user_id IS NOT NULL THEN 'human'
            ELSE 'system'
        END
        """
    )
    op.alter_column(
        "candidate_activities",
        "actor_kind",
        existing_type=sa.String(length=16),
        nullable=False,
        server_default="system",
    )
    op.create_check_constraint(
        "ck_candidate_activity_actor_kind",
        "candidate_activities",
        "actor_kind IN ('human', 'ai', 'system', 'public')",
    )
    op.add_column(
        "candidate_activities", sa.Column("agent_run_id", UUID, nullable=True)
    )
    op.create_foreign_key(
        "fk_candidate_activities_agent_run_id_ai_agent_runs",
        "candidate_activities",
        "ai_agent_runs",
        ["agent_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_candidate_activities_agent_run_id",
        "candidate_activities",
        ["agent_run_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_candidate_activities_agent_run_id", table_name="candidate_activities"
    )
    op.drop_constraint(
        "fk_candidate_activities_agent_run_id_ai_agent_runs",
        "candidate_activities",
        type_="foreignkey",
    )
    op.drop_column("candidate_activities", "agent_run_id")
    op.drop_constraint(
        "ck_candidate_activity_actor_kind",
        "candidate_activities",
        type_="check",
    )
    op.drop_column("candidate_activities", "actor_kind")

    op.drop_index("ix_mission_events_org_type_time", table_name="mission_events")
    op.drop_index("ix_mission_events_mission_time", table_name="mission_events")
    op.drop_index("ix_mission_events_organization_id", table_name="mission_events")
    op.drop_table("mission_events")

    op.drop_index(
        "ix_ai_action_bundles_mission_plan", table_name="ai_action_bundles"
    )
    op.drop_index(
        "ix_ai_action_bundles_org_status_created", table_name="ai_action_bundles"
    )
    op.drop_index(
        "ix_ai_action_bundles_organization_id", table_name="ai_action_bundles"
    )
    op.drop_table("ai_action_bundles")

    op.drop_index(
        "ix_evidence_claims_mission_candidate", table_name="evidence_claims"
    )
    op.drop_index(
        "ix_evidence_claims_assessment_criterion", table_name="evidence_claims"
    )
    op.drop_index(
        "ix_evidence_claims_organization_id", table_name="evidence_claims"
    )
    op.drop_table("evidence_claims")

    op.drop_index(
        "ix_mission_assessments_candidate_latest",
        table_name="mission_candidate_assessments",
    )
    op.drop_index(
        "ix_mission_assessments_rank",
        table_name="mission_candidate_assessments",
    )
    op.drop_index(
        "ix_mission_candidate_assessments_organization_id",
        table_name="mission_candidate_assessments",
    )
    op.drop_table("mission_candidate_assessments")

    op.drop_index(
        "ix_mission_step_attempts_step_created", table_name="mission_step_attempts"
    )
    op.drop_index(
        "ix_mission_step_attempts_organization_id",
        table_name="mission_step_attempts",
    )
    op.drop_table("mission_step_attempts")

    op.drop_index(
        "ix_ai_messages_org_session_sequence", table_name="ai_messages"
    )
    op.drop_index("ix_ai_messages_organization_id", table_name="ai_messages")
    op.drop_table("ai_messages")

    op.drop_index(
        "ix_ai_agent_runs_mission_created", table_name="ai_agent_runs"
    )
    op.drop_index("ix_ai_agent_runs_correlation_id", table_name="ai_agent_runs")
    op.drop_index("ix_ai_agent_runs_organization_id", table_name="ai_agent_runs")
    op.drop_table("ai_agent_runs")

    op.drop_index(
        "ix_mission_steps_mission_plan_ordinal", table_name="mission_steps"
    )
    op.drop_index("ix_mission_steps_claimable", table_name="mission_steps")
    op.drop_index("ix_mission_steps_organization_id", table_name="mission_steps")
    op.drop_table("mission_steps")

    op.drop_index(
        "ix_mission_plan_versions_mission_created",
        table_name="mission_plan_versions",
    )
    op.drop_index(
        "ix_mission_plan_versions_organization_id",
        table_name="mission_plan_versions",
    )
    op.drop_table("mission_plan_versions")

    op.drop_index(
        "ix_hiring_missions_org_status_updated", table_name="hiring_missions"
    )
    op.drop_index("ix_hiring_missions_position_id", table_name="hiring_missions")
    op.drop_index(
        "ix_hiring_missions_organization_id", table_name="hiring_missions"
    )
    op.drop_table("hiring_missions")

    op.drop_index("ix_ai_sessions_org_status_updated", table_name="ai_sessions")
    op.drop_index("ix_ai_sessions_organization_id", table_name="ai_sessions")
    op.drop_table("ai_sessions")
    op.drop_table("ai_organization_settings")
