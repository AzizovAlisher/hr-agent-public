"""Organization (tenant). Every user, position, candidate, chat, etc. belongs
to exactly one organization. Single-org-per-user — no org switching, no
membership table, no invites in v1.
"""
import uuid
from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import created_at, updated_at, uuid_pk


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = uuid_pk()

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    # Branding shown on the public-facing surfaces (apply page + shortlist).
    # `logo_path` is a relative path under the storage dir (e.g.
    # "logos/abc.png") served through the branding-only store; the web prepends
    # API_BASE_URL on render. `accent_color` is a hex string applied as a
    # CSS variable on those pages so the buyer's brand color tints CTAs.
    logo_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    accent_color: Mapped[str | None] = mapped_column(String(16), nullable=True)

    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
