"""Shared transport envelope for every API collection."""

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class CursorPage(BaseModel, Generic[T]):
    """A stable keyset page.

    Collection endpoints never return a bare array.  ``next_cursor`` is opaque
    and must be sent back unchanged; ``None`` means the collection is exhausted.
    """

    items: list[T] = Field(default_factory=list)
    next_cursor: str | None = None
