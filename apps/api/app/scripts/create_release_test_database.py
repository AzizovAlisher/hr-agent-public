"""Create one explicitly named, isolated release-gate database.

The command never drops or reuses a database and accepts only names ending in
``_perf_test`` or ``_recovery_test``. It is intended for ephemeral CI clusters.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from urllib.parse import urlsplit, urlunsplit

import asyncpg


SAFE_NAME = re.compile(r"^[a-z][a-z0-9_]{2,62}_(?:perf|recovery)_test$")


def _admin_dsn() -> str:
    raw = os.environ.get("TEST_DATABASE_ADMIN_URL", "")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise SystemExit("TEST_DATABASE_ADMIN_URL must be a PostgreSQL URL")
    if parsed.fragment:
        raise SystemExit("TEST_DATABASE_ADMIN_URL must not contain a fragment")
    return urlunsplit(parsed)


async def _create(name: str) -> None:
    if not SAFE_NAME.fullmatch(name):
        raise SystemExit(
            "Database name must be lowercase and end in _perf_test or _recovery_test"
        )
    connection = await asyncpg.connect(_admin_dsn())
    try:
        exists = await connection.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", name
        )
        if exists:
            raise SystemExit("Refusing to reuse an existing release-gate database")
        # SAFE_NAME excludes quotes and all SQL metacharacters.
        await connection.execute(f'CREATE DATABASE "{name}"')
    finally:
        await connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    asyncio.run(_create(args.name))


if __name__ == "__main__":
    main()
