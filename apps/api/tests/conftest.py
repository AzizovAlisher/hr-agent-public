"""PostgreSQL integration-test fixtures.

Tests intentionally refuse to touch a database whose name does not end in
``_test``. Set TEST_DATABASE_URL explicitly before running pytest.
"""

import os

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
if TEST_DATABASE_URL:
    database_name = make_url(TEST_DATABASE_URL).database or ""
    if not database_name.endswith("_test"):
        raise RuntimeError("TEST_DATABASE_URL database name must end in '_test'.")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL

import app.models  # noqa: F401
from app.db import Base, SessionLocal, engine
from app.services.public_abuse import (
    InMemoryRateLimitBackend,
    install_public_rate_limit_backend_for_testing,
)


@pytest_asyncio.fixture(autouse=True)
async def isolated_public_rate_limits():
    """Keep public-request budgets deterministic and isolated per test."""

    backend = InMemoryRateLimitBackend()
    install_public_rate_limit_backend_for_testing(backend)
    try:
        yield
    finally:
        install_public_rate_limit_backend_for_testing(None)
        await backend.close()


@pytest_asyncio.fixture(autouse=True)
async def clean_database():
    if not TEST_DATABASE_URL:
        import pytest

        pytest.skip("Set TEST_DATABASE_URL to a PostgreSQL database ending in _test.")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        table_names = [table.name for table in reversed(Base.metadata.sorted_tables)]
        if table_names:
            quoted = ", ".join(f'"{name}"' for name in table_names)
            await connection.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))
    yield


@pytest_asyncio.fixture
async def db():
    async with SessionLocal() as session:
        yield session
        await session.rollback()
