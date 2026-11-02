"""Seed the isolated browser-test database with two deterministic tenants."""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

from sqlalchemy import select

from app.auth.passwords import hash_password
from app.db import SessionLocal
from app.models.enterprise import PermissionOverride
from app.models.organization import Organization
from app.models.position import Position
from app.models.user import User


ORG_ALPHA_ID = UUID("80000000-0000-4000-8000-000000000001")
ORG_BETA_ID = UUID("80000000-0000-4000-8000-000000000002")
RECRUITER_ID = UUID("80000000-0000-4000-8000-000000000011")
INTERVIEWER_ID = UUID("80000000-0000-4000-8000-000000000012")
READINESS_OPERATOR_ID = UUID("80000000-0000-4000-8000-000000000013")
READINESS_PERMISSION_ID = UUID("80000000-0000-4000-8000-000000000014")
POSITION_ALPHA_ID = UUID("80000000-0000-4000-8000-000000000101")
POSITION_BETA_ID = UUID("80000000-0000-4000-8000-000000000102")

FIXTURE_PASSWORD = "RealStack-Only-Password-2026!"


async def seed() -> None:
    async with SessionLocal() as db:
        existing = await db.scalar(
            select(Organization.id).where(Organization.id == ORG_ALPHA_ID)
        )
        if existing is not None:
            raise RuntimeError(
                "The isolated real-stack fixture was already seeded; refuse to reuse data"
            )

        alpha = Organization(
            id=ORG_ALPHA_ID,
            name="Real Stack Alpha",
            slug="real-stack-alpha",
            accent_color="#2563eb",
        )
        beta = Organization(
            id=ORG_BETA_ID,
            name="Real Stack Beta",
            slug="real-stack-beta",
            accent_color="#7c3aed",
        )
        db.add_all([alpha, beta])
        await db.flush()

        password_hash = hash_password(FIXTURE_PASSWORD)
        recruiter = User(
            id=RECRUITER_ID,
            organization_id=ORG_ALPHA_ID,
            email="recruiter.real-stack@example.com",
            password_hash=password_hash,
            role="recruiter",
            locale="en",
        )
        interviewer = User(
            id=INTERVIEWER_ID,
            organization_id=ORG_BETA_ID,
            email="interviewer.real-stack@example.com",
            password_hash=password_hash,
            role="interviewer",
            locale="ru",
        )
        readiness_operator = User(
            id=READINESS_OPERATOR_ID,
            organization_id=ORG_ALPHA_ID,
            email="readiness.real-stack@example.com",
            password_hash=password_hash,
            role="restricted",
            locale="en",
        )
        db.add_all([recruiter, interviewer, readiness_operator])
        await db.flush()

        # The production detailed readiness endpoint is intentionally private.
        # Keep the browser fixture's probe account otherwise restricted and
        # grant only the one capability needed to exercise that real endpoint.
        db.add(
            PermissionOverride(
                id=READINESS_PERMISSION_ID,
                organization_id=ORG_ALPHA_ID,
                role="restricted",
                permission="enterprise.manage",
                allowed=True,
            )
        )

        alpha_position = Position(
            id=POSITION_ALPHA_ID,
            organization_id=ORG_ALPHA_ID,
            created_by=RECRUITER_ID,
            title="Platform Engineer / Инженер платформы",
            description=(
                "Build reliable hiring services backed by PostgreSQL, Valkey, "
                "object storage, and durable workers."
            ),
            full_description=(
                "Own accessible RU/EN hiring workflows and production-grade "
                "FastAPI and Next.js delivery paths."
            ),
            requirements={
                "company": "Real Stack Alpha",
                "location": "Dushanbe",
                "employment_type": "Full-time",
                "skills": ["Python", "FastAPI", "PostgreSQL", "Playwright"],
            },
            status="open",
            sensitivity="public",
            public_apply_enabled=True,
        )
        beta_position = Position(
            id=POSITION_BETA_ID,
            organization_id=ORG_BETA_ID,
            created_by=INTERVIEWER_ID,
            title="Isolated Beta Role",
            description="A tenant-local control record.",
            requirements={"company": "Real Stack Beta"},
            status="open",
            sensitivity="public",
            public_apply_enabled=True,
        )
        db.add_all([alpha_position, beta_position])
        await db.commit()

    print(
        json.dumps(
            {
                "organizations": [str(ORG_ALPHA_ID), str(ORG_BETA_ID)],
                "users": [
                    "recruiter.real-stack@example.com",
                    "interviewer.real-stack@example.com",
                    "readiness.real-stack@example.com",
                ],
                "positions": [str(POSITION_ALPHA_ID), str(POSITION_BETA_ID)],
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(seed())
