"""Seed an active admin only for an explicitly confirmed development setup.

Usage:
    APP_ENVIRONMENT=development \
    SEED_ADMIN_CONFIRM=CREATE_ACTIVE_DEVELOPMENT_ADMIN \
    SEED_ADMIN_EMAIL=admin@example.com SEED_ADMIN_PASSWORD=secret \
        uv run python -m app.scripts.seed_admin

Private-pilot tenants must use ``app.scripts.provision_tenant`` so the first
admin activates through a hashed, expiring invitation and receives an explicit
contract entitlement set.
"""

import asyncio
import os
import sys
import uuid

from sqlalchemy import select

from app.auth.passwords import hash_password
from app.config import get_settings
from app.db import session_scope
from app.models.organization import Organization
from app.models.user import User

# Matches the migration's seed UUID — guarantees seed_admin lands the admin in
# the same default org as any backfilled rows.
DEFAULT_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def main() -> None:
    if get_settings().app_environment != "development":
        raise RuntimeError("seed_admin is restricted to development")
    if os.environ.get("SEED_ADMIN_CONFIRM") != "CREATE_ACTIVE_DEVELOPMENT_ADMIN":
        raise RuntimeError("Explicit development-admin confirmation is required")
    email = os.environ.get("SEED_ADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("SEED_ADMIN_PASSWORD", "")
    if not email or "@" not in email:
        raise RuntimeError("SEED_ADMIN_EMAIL must be an explicit email address")
    if len(password) < 8 or len(password.encode("utf-8")) > 72:
        raise RuntimeError("SEED_ADMIN_PASSWORD must be 8-72 UTF-8 bytes")
    if password.lower() in {"admin", "password", "admin123"}:
        raise RuntimeError("Refusing a known unsafe development password")

    async with session_scope() as db:
        # Ensure the default org exists (the migration seeds it, but if the DB
        # was created via metadata.create_all rather than alembic, it won't be).
        org = (
            await db.execute(select(Organization).where(Organization.id == DEFAULT_ORG_ID))
        ).scalar_one_or_none()
        if org is None:
            org = Organization(id=DEFAULT_ORG_ID, name="Demo", slug="demo")
            db.add(org)
            await db.flush()
            print(f"Created default org id={org.id} slug=demo")

        existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if existing:
            print(f"User {email} already exists (id={existing.id}). Skipping.")
            return

        user = User(
            organization_id=org.id,
            email=email,
            password_hash=hash_password(password),
            role="admin",
            locale="en",
        )
        db.add(user)
        await db.flush()
        print(f"Created admin user {email} with id {user.id} in org {org.slug}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
