from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import current_user
from app.db import get_session
from app.models.folder import Folder
from app.models.position import Position
from app.models.user import User
from app.schemas.folders import (
    FolderCreate,
    FolderNode,
    FolderOut,
    FolderReorderRequest,
    FolderUpdate,
)
from app.schemas.pagination import CursorPage
from app.services.pagination import (
    collection_cursor_scope,
    cursor_datetime,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)

router = APIRouter(
    prefix="/folders", tags=["folders"], dependencies=[Depends(current_user)]
)

_ORGANIZATION_FOLDER_ROLES = frozenset({"admin", "recruiter"})


# ---------- helpers ----------


def _require_organization_folder_role(user: User) -> None:
    """Keep the shared folder tree organization-wide and non-delegable.

    Folder names, hierarchy, counts, and mutations span every position in the
    tenant. Assignment-scoped roles therefore cannot safely consume a partial
    projection or mutate a mixed-scope folder. Permission overrides still
    govern the canonical ``candidates.read``/``candidates.write`` actions, but
    they deliberately cannot promote a manager, interviewer, or restricted
    identity into this organization-wide structural role.
    """

    if user.role not in _ORGANIZATION_FOLDER_ROLES:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Organization-wide folder access required",
        )


async def _get_folder(db: AsyncSession, user: User, folder_id: UUID) -> Folder:
    """Fetch a folder scoped to the caller's org. 404 on cross-org / missing."""
    f = (
        await db.execute(
            select(Folder).where(
                Folder.id == folder_id,
                Folder.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if f is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    return f


def _assert_depth_ok(parent: Folder | None) -> None:
    """A child's parent must itself be a root (max depth 2)."""
    if parent is not None and parent.parent_id is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "max_depth_exceeded")


async def _has_children(db: AsyncSession, folder_id: UUID) -> bool:
    n = (
        await db.execute(
            select(func.count())
            .select_from(Folder)
            .where(Folder.parent_id == folder_id)
        )
    ).scalar_one()
    return n > 0


async def _assert_unique_name(
    db: AsyncSession,
    org_id: UUID,
    parent_id: UUID | None,
    name: str,
    exclude_id: UUID | None = None,
) -> None:
    """Reject a duplicate name among siblings (roots compared against roots)."""
    sib = (
        Folder.parent_id == parent_id
        if parent_id is not None
        else Folder.parent_id.is_(None)
    )
    conds = [Folder.organization_id == org_id, Folder.name == name, sib]
    if exclude_id is not None:
        conds.append(Folder.id != exclude_id)
    dup = (await db.execute(select(Folder.id).where(*conds))).first()
    if dup is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "duplicate_name")


async def _sync_position_strings(
    db: AsyncSession, org_id: UUID, folder_id: UUID, name: str
) -> None:
    """Keep the legacy positions.folder shadow in sync after a rename."""
    await db.execute(
        update(Position)
        .where(Position.folder_id == folder_id, Position.organization_id == org_id)
        .values(folder=name)
    )


async def _folder_out(db: AsyncSession, user: User, f: Folder) -> FolderOut:
    """Single-folder response with direct + descendant position counts."""
    direct = (
        await db.execute(
            select(func.count())
            .select_from(Position)
            .where(
                Position.folder_id == f.id,
                Position.organization_id == user.organization_id,
            )
        )
    ).scalar_one()
    desc = (
        await db.execute(
            select(func.count())
            .select_from(Position)
            .join(Folder, Folder.id == Position.folder_id)
            .where(
                Folder.parent_id == f.id,
                Position.organization_id == user.organization_id,
            )
        )
    ).scalar_one()
    out = FolderOut.model_validate(f)
    out.position_count = direct
    out.descendant_count = desc
    return out


# ---------- endpoints ----------


@router.get("", response_model=CursorPage[FolderNode])
async def list_folders(
    include_archived: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[FolderNode]:
    """The org's folders as a 2-level tree with position counts.

    Ordered by (sort_order, created_at, id) so ties are deterministic. Archived
    folders are excluded unless include_archived=true."""
    _require_organization_folder_role(user)
    folders = (
        (
            await db.execute(
                select(Folder)
                .where(Folder.organization_id == user.organization_id)
                .order_by(Folder.sort_order, Folder.created_at, Folder.id)
            )
        )
        .scalars()
        .all()
    )

    count_rows = (
        await db.execute(
            select(Position.folder_id, func.count())
            .where(
                Position.organization_id == user.organization_id,
                Position.folder_id.is_not(None),
            )
            .group_by(Position.folder_id)
        )
    ).all()
    counts = {fid: c for fid, c in count_rows}

    nodes: dict[UUID, FolderNode] = {}
    for f in folders:
        node = FolderNode.model_validate(f)
        node.position_count = counts.get(f.id, 0)
        node.descendant_count = 0
        node.children = []
        nodes[f.id] = node

    roots: list[FolderNode] = []
    for f in folders:
        node = nodes[f.id]
        if f.parent_id is None:
            roots.append(node)
        else:
            parent = nodes.get(f.parent_id)
            if parent is not None:
                parent.children.append(node)
                parent.descendant_count += node.position_count

    if not include_archived:
        roots = [r for r in roots if not r.is_archived]
        for r in roots:
            # Drop archived children AND remove their positions from the parent's
            # descendant_count so the badge matches the visible subtree.
            for c in r.children:
                if c.is_archived:
                    r.descendant_count -= c.position_count
            r.children = [c for c in r.children if not c.is_archived]
    namespace = "folders.tree"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"include_archived": include_archived},
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"sort_order", "created_at", "id"},
            detail="Invalid folders cursor",
        )
        try:
            cursor_order = int(payload["sort_order"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"code": "cursor_invalid", "detail": "Invalid folders cursor"},
            ) from exc
        cursor_time = cursor_datetime(
            payload, "created_at", detail="Invalid folders cursor"
        )
        cursor_id = cursor_uuid(payload, detail="Invalid folders cursor")
        roots = [
            root
            for root in roots
            if (root.sort_order, root.created_at, root.id)
            > (cursor_order, cursor_time, cursor_id)
        ]
    has_more = len(roots) > limit
    roots = roots[:limit]
    return CursorPage(
        items=roots,
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "sort_order": roots[-1].sort_order,
                    "created_at": roots[-1].created_at.isoformat(),
                    "id": str(roots[-1].id),
                },
            )
            if has_more and roots
            else None
        ),
    )


@router.post("", response_model=FolderOut, status_code=status.HTTP_201_CREATED)
async def create_folder(
    body: FolderCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> FolderOut:
    _require_organization_folder_role(user)
    if body.parent_id is not None:
        parent = await _get_folder(db, user, body.parent_id)
        _assert_depth_ok(parent)
    await _assert_unique_name(db, user.organization_id, body.parent_id, body.name)

    sib = (
        Folder.parent_id == body.parent_id
        if body.parent_id is not None
        else Folder.parent_id.is_(None)
    )
    max_order = (
        await db.execute(
            select(func.max(Folder.sort_order)).where(
                Folder.organization_id == user.organization_id, sib
            )
        )
    ).scalar()

    f = Folder(
        organization_id=user.organization_id,
        parent_id=body.parent_id,
        name=body.name,
        color=body.color,
        icon=body.icon,
        sort_order=(max_order or 0) + 10,
    )
    db.add(f)
    await db.commit()
    await db.refresh(f)
    return await _folder_out(db, user, f)


@router.patch("/reorder", response_model=list[FolderOut])
async def reorder_folders(
    body: FolderReorderRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> list[FolderOut]:
    """Rewrite sort_order for a set of siblings. Registered BEFORE /{folder_id}
    so 'reorder' isn't captured as a folder id."""
    _require_organization_folder_role(user)
    sib = (
        Folder.parent_id == body.parent_id
        if body.parent_id is not None
        else Folder.parent_id.is_(None)
    )
    siblings = (
        (
            await db.execute(
                select(Folder).where(
                    Folder.organization_id == user.organization_id, sib
                )
            )
        )
        .scalars()
        .all()
    )
    by_id = {f.id: f for f in siblings}
    # ordered_ids may be a SUBSET (the client only sees non-archived siblings),
    # but every id must be a real sibling and there must be no duplicates.
    if len(set(body.ordered_ids)) != len(body.ordered_ids):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "ordered_ids has duplicates")
    for oid in body.ordered_ids:
        if oid not in by_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "ordered_ids must be siblings of parent_id"
            )
    # Reassign sort_order across the FULL sibling set in one stride so nothing
    # collides: the provided ids first (in the requested order), then any
    # omitted siblings (e.g. archived/hidden) keeping their prior relative order.
    provided = list(body.ordered_ids)
    provided_set = set(provided)
    omitted = [
        f.id
        for f in sorted(siblings, key=lambda x: (x.sort_order, x.created_at))
        if f.id not in provided_set
    ]
    for idx, fid in enumerate([*provided, *omitted]):
        by_id[fid].sort_order = idx * 10
    await db.commit()
    ordered = []
    for oid in provided:
        f = by_id[oid]
        await db.refresh(f)
        ordered.append(await _folder_out(db, user, f))
    return ordered


@router.patch("/{folder_id}", response_model=FolderOut)
async def update_folder(
    folder_id: UUID,
    body: FolderUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> FolderOut:
    """Rename / recolor / re-icon / re-parent / archive in one endpoint.

    Partial via exclude_unset: a field changes only if present in the body.
    """
    _require_organization_folder_role(user)
    f = await _get_folder(db, user, folder_id)
    updates = body.model_dump(exclude_unset=True)

    reparenting = "parent_id" in updates and updates["parent_id"] != f.parent_id
    renaming = "name" in updates and updates["name"] != f.name
    new_parent_id = updates["parent_id"] if "parent_id" in updates else f.parent_id
    new_name = updates["name"] if "name" in updates else f.name

    if reparenting:
        tgt = updates["parent_id"]
        if tgt == f.id:
            raise HTTPException(status.HTTP_409_CONFLICT, "cannot_self_parent")
        if tgt is not None:
            new_parent = await _get_folder(db, user, tgt)
            _assert_depth_ok(new_parent)
            # Moving a folder that HAS children under a root would push the
            # grandchildren to depth 3 — the DB trigger can't see this.
            if await _has_children(db, f.id):
                raise HTTPException(
                    status.HTTP_409_CONFLICT, "has_children_cannot_nest"
                )

    if reparenting or renaming:
        await _assert_unique_name(
            db, user.organization_id, new_parent_id, new_name, exclude_id=f.id
        )

    if "name" in updates:
        f.name = updates["name"]
    if "parent_id" in updates:
        f.parent_id = updates["parent_id"]
    if "color" in updates:
        f.color = updates["color"]
    if "icon" in updates:
        f.icon = updates["icon"]
    if "is_archived" in updates:
        f.is_archived = updates["is_archived"]
        # Cascade ONLY when archiving a parent — its whole subtree goes away with
        # it. On unarchive we do NOT force-resurface children that were archived
        # individually; the user re-shows those per folder.
        if updates["is_archived"]:
            await db.execute(
                update(Folder)
                .where(
                    Folder.parent_id == f.id,
                    Folder.organization_id == user.organization_id,
                )
                .values(is_archived=True)
            )

    await db.commit()
    if renaming:
        await _sync_position_strings(db, user.organization_id, f.id, f.name)
        await db.commit()
    await db.refresh(f)
    return await _folder_out(db, user, f)


@router.delete("/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_folder(
    folder_id: UUID,
    reassign_to: UUID | None = None,
    force: bool = False,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> None:
    """Delete a folder. Positions are never deleted — they orphan to Unsorted
    (FK SET NULL) unless reassign_to moves them first.

    A folder with children needs an explicit decision: 409 unless force=true or
    reassign_to is given. Children CASCADE-delete; their positions SET NULL.
    """
    _require_organization_folder_role(user)
    f = await _get_folder(db, user, folder_id)

    child_ids = (
        (
            await db.execute(
                select(Folder.id).where(
                    Folder.parent_id == f.id,
                    Folder.organization_id == user.organization_id,
                )
            )
        )
        .scalars()
        .all()
    )
    has_kids = len(child_ids) > 0

    if has_kids and not force and reassign_to is None:
        scope = [f.id, *child_ids]
        pos_n = (
            await db.execute(
                select(func.count())
                .select_from(Position)
                .where(
                    Position.folder_id.in_(scope),
                    Position.organization_id == user.organization_id,
                )
            )
        ).scalar_one()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "folder_has_children",
                "children": len(child_ids),
                "positions": pos_n,
            },
        )

    if reassign_to is not None:
        target = await _get_folder(db, user, reassign_to)
        # The target must survive the delete — reassigning into the folder being
        # deleted (or one of its children, which CASCADE-delete) would silently
        # re-orphan the positions to Unsorted via the FK SET NULL.
        if target.id == f.id or target.id in set(child_ids):
            raise HTTPException(
                status.HTTP_409_CONFLICT, "reassign_to_within_deleted_subtree"
            )
        scope = [f.id, *child_ids]
        await db.execute(
            update(Position)
            .where(
                Position.folder_id.in_(scope),
                Position.organization_id == user.organization_id,
            )
            .values(folder_id=target.id, folder=target.name)
        )

    await db.execute(
        delete(Folder).where(
            Folder.id == f.id, Folder.organization_id == user.organization_id
        )
    )
    await db.commit()
