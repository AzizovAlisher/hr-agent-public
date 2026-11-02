"""Replace legacy public-applicant source references with opaque identifiers.

Revision ID: c85f2a6d1e43
Revises: b74e1d9c0a32
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c85f2a6d1e43"
down_revision: str | Sequence[str] | None = "b74e1d9c0a32"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Older public intake stored a display name and phone number in source_ref.
    # Contact points and parsed candidate data remain authoritative and retain
    # the applicant data under field policy; this broadly projected provenance
    # field must contain only a stable, non-PII identifier. Applications and
    # parsed_cv.applied_via preserve the original acquisition channel.
    op.execute(
        """
        UPDATE candidates
        SET source_ref = 'public:legacy:' || replace(id::text, '-', '')
        WHERE source = 'public'
          AND source_ref IS DISTINCT FROM
              ('public:legacy:' || replace(id::text, '-', ''))
        """
    )


def downgrade() -> None:
    # Deliberately irreversible: a rollback must not reconstruct or re-expose
    # contact PII in source_ref. The opaque values are compatible with the
    # preceding schema and therefore remain in place.
    pass
