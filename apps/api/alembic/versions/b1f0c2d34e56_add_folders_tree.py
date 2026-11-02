"""two-level folders tree + positions.folder_id

Adds:
- folders table (org-scoped, self-FK parent_id, max depth 2 via trigger),
  with name/color/icon/sort_order/is_archived.
- positions.folder_id (FK -> folders, ON DELETE SET NULL). The legacy
  positions.folder string is kept as a write-synced shadow.
- Seeds the demo org's folder tree: «Демо Холдинг» -> 6 category children,
  high-load-platform (top-level leaf), Архив (is_archived). Backfills
  positions.folder_id from the existing folder strings. Idempotent
  (deterministic UUIDs + ON CONFLICT + guarded UPDATEs).

Revision ID: b1f0c2d34e56
Revises: f3a1c9d72e80
Create Date: 2026-06-24 12:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "b1f0c2d34e56"
down_revision: Union[str, Sequence[str], None] = "f3a1c9d72e80"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ORG = "00000000-0000-0000-0000-000000000001"

# Deterministic folder UUIDs so the data steps are idempotent on retry.
KN = "f01d0000-0000-0000-0000-000000000001"   # Демо Холдинг (root)
IT = "f01d0000-0000-0000-0000-000000000010"
FIN = "f01d0000-0000-0000-0000-000000000011"  # Финансы/Корпоратив
PHARMA = "f01d0000-0000-0000-0000-000000000012"
EDU = "f01d0000-0000-0000-0000-000000000013"
BUILD = "f01d0000-0000-0000-0000-000000000014"
OTHER = "f01d0000-0000-0000-0000-000000000015"
HLP = "f01d0000-0000-0000-0000-000000000002"   # high-load-platform (root leaf)
ARCH = "f01d0000-0000-0000-0000-000000000003"  # Архив (root, is_archived)

# (id, parent_id, name, icon, color, sort_order, is_archived)
ROOTS = [
    (KN, None, "Демо Холдинг", "building-2", "blue", 10, False),
    (HLP, None, "high-load-platform", "server", "violet", 20, False),
    (ARCH, None, "Архив", "archive", "slate", 9000, True),
]
CHILDREN = [
    (IT, KN, "ИТ", "cpu", "blue", 10, False),
    (FIN, KN, "Финансы/Корпоратив", "landmark", "slate", 20, False),
    (PHARMA, KN, "Фарма", "flask-conical", "green", 30, False),
    (EDU, KN, "Образование", "graduation-cap", "amber", 40, False),
    (BUILD, KN, "Строительство", "hard-hat", "orange", 50, False),
    (OTHER, KN, "Прочее", "folder", "slate", 60, False),
]

# legacy folder string -> target folder_id (the 6-category mapping)
STRING_TO_FOLDER = {
    "ИТ и ЦОД": IT,
    "Головной офис": FIN,
    "Лизинговая компания": FIN,
    "Фарма-производство": PHARMA,
    "Фарма-дистрибуция": PHARMA,
    "Фарм. промоция": PHARMA,
    "Медтехника и ветеринария": PHARMA,
    "Тренинг-центр": EDU,
    "Учебный институт": EDU,
    "Девелопер": BUILD,
    "Розничная сеть": OTHER,
    "Прочее подразделение": OTHER,
    # Category folder NAMES themselves — once a position is filed via the API,
    # its shadow `folder` string becomes the folder's name (e.g. "ИТ"). A clean
    # migration re-run (downgrade keeps the string, drops folder_id) must remap
    # those names to the same folder, not dump them into Прочее via 5e.
    "ИТ": IT,
    "Финансы/Корпоратив": FIN,
    "Фарма": PHARMA,
    "Образование": EDU,
    "Строительство": BUILD,
    "Прочее": OTHER,
}

_INSERT_FOLDER = sa.text(
    "INSERT INTO folders "
    "(id, organization_id, parent_id, name, icon, color, sort_order, is_archived, "
    " created_at, updated_at) "
    "VALUES (:id, :org, :parent, :name, :icon, :color, :sort, :arch, now(), now()) "
    "ON CONFLICT (id) DO NOTHING"
)


def upgrade() -> None:
    bind = op.get_bind()

    # ---- Step 1: folders table ----
    op.create_table(
        "folders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("color", sa.String(length=16), nullable=True),
        sa.Column("icon", sa.String(length=40), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "is_archived", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["parent_id"], ["folders.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "organization_id", "parent_id", "name", name="uq_folders_sibling_name"
        ),
    )
    op.create_index("ix_folders_organization_id", "folders", ["organization_id"])
    op.create_index("ix_folders_parent_id", "folders", ["parent_id"])
    op.create_index(
        "ix_folders_org_parent_order",
        "folders",
        ["organization_id", "parent_id", "sort_order"],
    )
    # Root-name uniqueness (the table-level constraint can't cover NULL parents).
    op.execute(
        "CREATE UNIQUE INDEX uq_folders_root_name ON folders (organization_id, name) "
        "WHERE parent_id IS NULL"
    )

    # ---- Step 2: depth-enforcement trigger ----
    op.execute(
        """
        CREATE OR REPLACE FUNCTION folders_enforce_depth() RETURNS trigger AS $$
        BEGIN
          IF NEW.parent_id IS NOT NULL THEN
            -- the chosen parent must itself be a root
            IF (SELECT parent_id FROM folders WHERE id = NEW.parent_id) IS NOT NULL THEN
              RAISE EXCEPTION 'folders: max nesting depth is 2 (parent % already has a parent)', NEW.parent_id;
            END IF;
            -- a folder that already has children cannot itself become a child
            -- (its grandchildren would land at depth 3)
            IF EXISTS (SELECT 1 FROM folders WHERE parent_id = NEW.id) THEN
              RAISE EXCEPTION 'folders: cannot nest folder % — it has children', NEW.id;
            END IF;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_folders_depth
          BEFORE INSERT OR UPDATE OF parent_id ON folders
          FOR EACH ROW EXECUTE FUNCTION folders_enforce_depth();
        """
    )

    # ---- Step 3: positions.folder_id ----
    op.add_column(
        "positions",
        sa.Column("folder_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_positions_folder_id", "positions", ["folder_id"])
    op.create_foreign_key(
        "fk_positions_folder_id",
        "positions",
        "folders",
        ["folder_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ---- Step 4: seed the demo org tree (roots first, then children) ----
    for fid, parent, name, icon, color, sort, arch in ROOTS + CHILDREN:
        bind.execute(
            _INSERT_FOLDER,
            {
                "id": fid,
                "org": ORG,
                "parent": parent,
                "name": name,
                "icon": icon,
                "color": color,
                "sort": sort,
                "arch": arch,
            },
        )

    # ---- Step 5: backfill positions.folder_id (target org), idempotent ----
    # 5a. archived telecom (legacy '_archived')
    bind.execute(
        sa.text(
            "UPDATE positions SET folder_id = :arch "
            "WHERE organization_id = :org AND folder = '_archived' "
            "AND folder_id IS DISTINCT FROM :arch"
        ),
        {"arch": ARCH, "org": ORG},
    )
    # 5b. high-load-platform leaf
    bind.execute(
        sa.text(
            "UPDATE positions SET folder_id = :hlp "
            "WHERE organization_id = :org AND folder = 'high-load-platform' "
            "AND folder_id IS DISTINCT FROM :hlp"
        ),
        {"hlp": HLP, "org": ORG},
    )
    # 5c. the 6-category tree by exact legacy folder string
    for legacy, target in STRING_TO_FOLDER.items():
        bind.execute(
            sa.text(
                "UPDATE positions SET folder_id = :target "
                "WHERE organization_id = :org AND folder = :legacy "
                "AND folder_id IS NULL"
            ),
            {"target": target, "org": ORG, "legacy": legacy},
        )
    # 5d. unsorted (folder IS NULL) -> Прочее
    bind.execute(
        sa.text(
            "UPDATE positions SET folder_id = :other "
            "WHERE organization_id = :org AND folder IS NULL AND folder_id IS NULL"
        ),
        {"other": OTHER, "org": ORG},
    )
    # 5e. catch-all drift: any target-org position still unmapped -> Прочее.
    # Surface the distinct unmapped strings first so a misfile is never silent.
    _log = logging.getLogger("alembic.runtime.migration")
    drift = bind.execute(
        sa.text(
            "SELECT folder, count(*) FROM positions "
            "WHERE organization_id = :org AND folder_id IS NULL AND folder IS NOT NULL "
            "GROUP BY folder"
        ),
        {"org": ORG},
    ).all()
    if drift:
        _log.warning(
            "folders backfill: unmapped folder strings absorbed into Прочее: %s",
            {row[0]: row[1] for row in drift},
        )
    bind.execute(
        sa.text(
            "UPDATE positions SET folder_id = :other "
            "WHERE organization_id = :org AND folder_id IS NULL"
        ),
        {"other": OTHER, "org": ORG},
    )

    # ---- Step 6: generic fallback for any OTHER org (none today) ----
    op.execute(
        """
        INSERT INTO folders
          (id, organization_id, parent_id, name, sort_order, is_archived, created_at, updated_at)
        SELECT gen_random_uuid(), organization_id, NULL, folder, 100, false, now(), now()
        FROM positions
        WHERE folder IS NOT NULL AND folder <> '_archived'
          AND organization_id <> '00000000-0000-0000-0000-000000000001'
        GROUP BY organization_id, folder
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE positions p SET folder_id = f.id
        FROM folders f
        WHERE f.organization_id = p.organization_id
          AND f.parent_id IS NULL AND f.name = p.folder
          AND p.organization_id <> '00000000-0000-0000-0000-000000000001'
          AND p.folder_id IS NULL AND p.folder IS NOT NULL
        """
    )

    # ---- Step 7: structural verification (stable invariants raise; counts log) ----
    def _scalar(sql, **kw):
        return bind.execute(sa.text(sql), kw).scalar()

    unmapped = _scalar(
        "SELECT count(*) FROM positions WHERE organization_id = :o AND folder_id IS NULL",
        o=ORG,
    )
    assert unmapped == 0, f"folders backfill: {unmapped} target-org positions left unmapped"

    kn_children = _scalar(
        "SELECT count(*) FROM folders WHERE parent_id = :kn", kn=KN
    )
    assert kn_children == 6, f"folders backfill: КН must have 6 children, got {kn_children}"

    non_closed_archived = _scalar(
        "SELECT count(*) FROM positions WHERE folder_id = :a AND status <> 'closed'",
        a=ARCH,
    )
    assert (
        non_closed_archived == 0
    ), f"folders backfill: Архив contains {non_closed_archived} non-closed positions"

    log = logging.getLogger("alembic.runtime.migration")
    for label, fid in [
        ("Архив (expect ~31)", ARCH),
        ("Фарма (expect ~15)", PHARMA),
        ("HLP (expect ~23)", HLP),
    ]:
        cnt = _scalar("SELECT count(*) FROM positions WHERE folder_id = :f", f=fid)
        log.info("folders backfill: %s = %s", label, cnt)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_folders_depth ON folders;")
    op.execute("DROP FUNCTION IF EXISTS folders_enforce_depth();")
    op.drop_constraint("fk_positions_folder_id", "positions", type_="foreignkey")
    op.drop_index("ix_positions_folder_id", table_name="positions")
    op.drop_column("positions", "folder_id")
    op.execute("DROP INDEX IF EXISTS uq_folders_root_name;")
    op.drop_index("ix_folders_org_parent_order", table_name="folders")
    op.drop_index("ix_folders_parent_id", table_name="folders")
    op.drop_index("ix_folders_organization_id", table_name="folders")
    op.drop_table("folders")
