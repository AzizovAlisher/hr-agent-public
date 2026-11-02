from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class FolderBase(BaseModel):
    # max_length 64 to match positions.folder (shadow-synced on assignment).
    name: str = Field(min_length=1, max_length=64)
    parent_id: UUID | None = None
    color: str | None = Field(default=None, max_length=16)
    icon: str | None = Field(default=None, max_length=40)


class FolderCreate(FolderBase):
    pass


class FolderUpdate(BaseModel):
    """Partial update. The service reads model_dump(exclude_unset=True), so a
    field changes only if present in the request JSON — `{"parent_id": null}`
    promotes to root, `{}` leaves everything untouched."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    parent_id: UUID | None = None
    color: str | None = Field(default=None, max_length=16)
    icon: str | None = Field(default=None, max_length=40)
    is_archived: bool | None = None


class FolderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    parent_id: UUID | None = None
    name: str
    color: str | None = None
    icon: str | None = None
    sort_order: int
    is_archived: bool
    position_count: int = 0  # direct positions (folder_id == this)
    descendant_count: int = 0  # sum of children's position_count (0 for leaves)
    created_at: datetime
    updated_at: datetime


class FolderNode(FolderOut):
    children: list[FolderNode] = []


class FolderReorderRequest(BaseModel):
    parent_id: UUID | None = None
    ordered_ids: list[UUID]


class MovePositionsRequest(BaseModel):
    position_ids: list[UUID]
    folder_id: UUID | None = None  # None = move to Unsorted
