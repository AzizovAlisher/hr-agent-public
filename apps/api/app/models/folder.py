import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
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


class Folder(Base):
    """A grouping folder for positions, max two levels deep.

    NULL ``parent_id`` = top-level folder. A non-NULL ``parent_id`` is a
    depth-1 child whose parent must itself be top-level. Depth is enforced in
    four places: a DB trigger (validates the written row), the service layer
    (rejects making a folder-with-children a child — the descendant-depth-3
    case the trigger can't see), and the UI.

    Folders are org-scoped. ``positions.folder_id`` references this table; the
    legacy ``positions.folder`` string is kept as a write-synced shadow so
    import scripts and string-based readers keep working.
    """

    __tablename__ = "folders"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("folders.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # Width MUST match positions.folder (String(64)) — the shadow-sync writes
    # folder.name into positions.folder, so a longer name would 500 on rename.
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    color: Mapped[str | None] = mapped_column(String(16), nullable=True)  # token name or hex
    icon: Mapped[str | None] = mapped_column(String(40), nullable=True)  # lucide icon id
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    is_archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        # Sibling-name uniqueness for the non-NULL-parent case only (Postgres
        # treats NULLs as distinct, so this does NOT cover roots). Root-name
        # uniqueness is the partial index uq_folders_root_name (see migration).
        UniqueConstraint(
            "organization_id", "parent_id", "name", name="uq_folders_sibling_name"
        ),
        Index(
            "ix_folders_org_parent_order",
            "organization_id",
            "parent_id",
            "sort_order",
        ),
        Index(
            "uq_folders_root_name",
            "organization_id",
            "name",
            unique=True,
            postgresql_where=text("parent_id IS NULL"),
        ),
    )
