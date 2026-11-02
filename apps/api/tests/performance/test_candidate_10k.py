"""Dedicated 10k-candidate API performance release gate.

This module is skipped during the ordinary deterministic suite. Run it against
an isolated PostgreSQL database whose name ends in ``_perf_test`` with
``RUN_PERFORMANCE_TESTS=1``. It writes machine-readable p95 evidence.
"""

from __future__ import annotations

import json
import math
import os
import platform
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import insert, text
from sqlalchemy.engine import make_url

from app.auth.jwt import encode_access_token
from app.main import app
from app.models.candidate import Candidate
from app.models.organization import Organization
from app.models.user import User

RUN_PERFORMANCE_TESTS = os.environ.get("RUN_PERFORMANCE_TESTS") == "1"
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not RUN_PERFORMANCE_TESTS,
        reason="Set RUN_PERFORMANCE_TESTS=1 for the dedicated release gate.",
    ),
]

DATASET_SIZE = 10_000
READ_RUNS = 30
MUTATION_RUNS = 30
READ_P95_TARGET_SECONDS = 0.500
MUTATION_P95_TARGET_SECONDS = 1.000


def _p95(samples: list[float]) -> float:
    if not samples:
        raise AssertionError("At least one timing sample is required")
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _plan_nodes(value: object) -> list[str]:
    names: list[str] = []
    if isinstance(value, dict):
        node_type = value.get("Node Type")
        if isinstance(node_type, str):
            names.append(node_type)
        for nested in value.values():
            names.extend(_plan_nodes(nested))
    elif isinstance(value, list):
        for nested in value:
            names.extend(_plan_nodes(nested))
    return names


def _write_evidence(payload: dict[str, object]) -> Path:
    configured = os.environ.get(
        "PERFORMANCE_EVIDENCE_PATH",
        "output/performance/candidate-10k.json",
    )
    path = Path(configured).expanduser().resolve()
    if path == Path(path.anchor) or len(path.parts) < 3:
        raise AssertionError("PERFORMANCE_EVIDENCE_PATH must be a bounded path")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex[:8]}.tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


async def test_candidate_search_and_local_mutation_meet_10k_p95_targets(db):
    database_url = os.environ.get("TEST_DATABASE_URL", "")
    database_name = make_url(database_url).database or ""
    if not database_name.endswith("_perf_test"):
        pytest.fail(
            "The performance gate requires an isolated TEST_DATABASE_URL ending "
            "in '_perf_test'."
        )

    organization = Organization(
        name="10k Performance Tenant",
        slug=f"performance-{uuid4().hex[:10]}",
    )
    db.add(organization)
    await db.flush()
    admin = User(
        organization_id=organization.id,
        email=f"performance-{uuid4().hex[:8]}@example.test",
        password_hash="not-used-by-benchmark",
        role="admin",
        locale="en",
    )
    db.add(admin)
    await db.flush()

    now = datetime.now(UTC)
    candidate_ids = [uuid4() for _ in range(DATASET_SIZE)]
    rows = [
        {
            "id": candidate_id,
            "organization_id": organization.id,
            "source": "benchmark",
            "source_ref": (
                f"Benchmark candidate {index} benchmarkneedle"
                if index % 100 == 0
                else f"Benchmark candidate {index} ordinary"
            ),
            "raw_cv": (
                "Benchmark engineering profile with deterministic local content "
                f"and sequence {index}."
            ),
            "created_at": now - timedelta(microseconds=index),
        }
        for index, candidate_id in enumerate(candidate_ids)
    ]
    for offset in range(0, DATASET_SIZE, 1_000):
        await db.execute(insert(Candidate), rows[offset : offset + 1_000])
    await db.commit()

    database_count = int(
        (
            await db.execute(
                text(
                    "SELECT count(*) FROM candidates "
                    "WHERE organization_id = :organization_id"
                ),
                {"organization_id": organization.id},
            )
        ).scalar_one()
    )
    assert database_count == DATASET_SIZE

    plan = (
        await db.execute(
            text(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
                "SELECT id FROM candidates "
                "WHERE organization_id = :organization_id "
                "AND search_document @@ "
                "websearch_to_tsquery('simple'::regconfig, 'benchmarkneedle') "
                "ORDER BY created_at DESC, id DESC LIMIT 51"
            ),
            {"organization_id": organization.id},
        )
    ).scalar_one()
    server_version = (await db.execute(text("SHOW server_version"))).scalar_one()

    token = encode_access_token(admin.id, admin.role, admin.locale)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {token}"}
    read_samples: list[float] = []
    mutation_samples: list[float] = []
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Warm connection pools, permission-policy caches, and PostgreSQL pages;
        # release evidence measures steady-state API behavior.
        for _ in range(5):
            warm = await client.get(
                "/candidate-search",
                params={"q": "benchmarkneedle", "limit": 50},
                headers=headers,
            )
            assert warm.status_code == 200

        for _ in range(READ_RUNS):
            started = time.perf_counter()
            response = await client.get(
                "/candidate-search",
                params={"q": "benchmarkneedle", "limit": 50},
                headers=headers,
            )
            read_samples.append(time.perf_counter() - started)
            assert response.status_code == 200
            body = response.json()
            assert body["total"] == 100
            assert len(body["items"]) == 50
            assert body["next_cursor"]

        target_candidate = candidate_ids[1]
        for index in range(MUTATION_RUNS):
            started = time.perf_counter()
            response = await client.patch(
                f"/candidates/{target_candidate}",
                json={"is_favorite": index % 2 == 0},
                headers=headers,
            )
            mutation_samples.append(time.perf_counter() - started)
            assert response.status_code == 200
            assert response.json()["is_favorite"] is (index % 2 == 0)

    read_p95 = _p95(read_samples)
    mutation_p95 = _p95(mutation_samples)
    evidence: dict[str, object] = {
        "format": "hr-agent-performance-evidence-v1",
        "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "dataset": {
            "candidate_count": database_count,
            "matching_candidate_count": 100,
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "postgresql": str(server_version),
        },
        "indexed_read": {
            "route": "GET /candidate-search?q=benchmarkneedle&limit=50",
            "runs": READ_RUNS,
            "p95_ms": round(read_p95 * 1_000, 3),
            "target_ms": READ_P95_TARGET_SECONDS * 1_000,
            "query_plan_nodes": sorted(set(_plan_nodes(plan))),
            "pass": read_p95 <= READ_P95_TARGET_SECONDS,
        },
        "local_mutation": {
            "route": "PATCH /candidates/{candidate_id}",
            "runs": MUTATION_RUNS,
            "p95_ms": round(mutation_p95 * 1_000, 3),
            "target_ms": MUTATION_P95_TARGET_SECONDS * 1_000,
            "pass": mutation_p95 <= MUTATION_P95_TARGET_SECONDS,
        },
    }
    evidence_path = _write_evidence(evidence)
    print(f"10k performance evidence: {evidence_path}")
    assert read_p95 <= READ_P95_TARGET_SECONDS, evidence
    assert mutation_p95 <= MUTATION_P95_TARGET_SECONDS, evidence
