"""Keyset-pagination and transport regressions for every API collection."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.main import app
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.organization import Organization
from app.models.position import Position
from app.models.talent import TalentPool, TalentPoolMember
from app.models.user import User
from app.routers.talent import list_talent_pool_members
from app.services.applications import list_applications
from app.services.pagination import (
    collection_cursor_scope,
    decode_collection_cursor,
    encode_collection_cursor,
)

pytestmark = pytest.mark.asyncio


async def _tenant(db, suffix: str) -> tuple[Organization, User]:
    organization = Organization(
        name=f"Cursor tenant {suffix}",
        slug=f"cursor-{suffix}-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    user = User(
        organization_id=organization.id,
        email=f"cursor-{suffix}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    db.add(user)
    await db.flush()
    return organization, user


def _tamper(cursor: str) -> str:
    replacement = "A" if cursor[0] != "A" else "B"
    return replacement + cursor[1:]


async def test_application_cursor_is_gap_free_filter_bound_and_tenant_bound(db):
    organization, user = await _tenant(db, "applications")
    other_organization, other_user = await _tenant(db, "applications-other")
    position = Position(
        organization_id=organization.id,
        title="Equal timestamp position",
        status="open",
        created_by=user.id,
    )
    other_position = Position(
        organization_id=other_organization.id,
        title="Other tenant position",
        status="open",
        created_by=other_user.id,
    )
    db.add_all([position, other_position])
    await db.flush()
    shared_time = datetime(2026, 8, 23, 8, 30, tzinfo=UTC)
    applications: list[Application] = []
    candidates: list[Candidate] = []
    for index in range(7):
        candidate = Candidate(
            organization_id=organization.id,
            source="manual",
            source_ref=f"cursor-app-{index}",
            raw_cv=f"Complete cursor pagination candidate {index}. " * 3,
        )
        db.add(candidate)
        await db.flush()
        application = Application(
            organization_id=organization.id,
            candidate_id=candidate.id,
            position_id=position.id,
            stage="new",
            source="manual",
            cycle_number=1,
            created_by_user_id=user.id,
            application_metadata={},
            applied_at=shared_time,
        )
        db.add(application)
        applications.append(application)
        candidates.append(candidate)
    outside_candidate = Candidate(
        organization_id=other_organization.id,
        source="manual",
        source_ref="cursor-app-outside",
        raw_cv="Other tenant candidate profile with enough detail. " * 3,
    )
    db.add(outside_candidate)
    await db.flush()
    db.add(
        Application(
            organization_id=other_organization.id,
            candidate_id=outside_candidate.id,
            position_id=other_position.id,
            stage="new",
            source="manual",
            cycle_number=1,
            created_by_user_id=other_user.id,
            application_metadata={},
            applied_at=shared_time,
        )
    )
    await db.commit()

    expected_ids = sorted((item.id for item in applications), reverse=True)
    seen_ids = []
    cursor = None
    page_sizes = []
    first_cursor = None
    while True:
        page = await list_applications(
            db,
            organization.id,
            limit=3,
            cursor=cursor,
        )
        page_sizes.append(len(page.items))
        seen_ids.extend(item.id for item in page.items)
        if first_cursor is None:
            first_cursor = page.next_cursor
        cursor = page.next_cursor
        if cursor is None:
            break

    assert page_sizes == [3, 3, 1]
    assert seen_ids == expected_ids
    assert len(seen_ids) == len(set(seen_ids)) == 7
    assert first_cursor is not None

    with pytest.raises(HTTPException) as tampered:
        await list_applications(
            db,
            organization.id,
            limit=3,
            cursor=_tamper(first_cursor),
        )
    assert tampered.value.status_code == 422

    with pytest.raises(HTTPException) as different_filter:
        await list_applications(
            db,
            organization.id,
            candidate_id=candidates[0].id,
            limit=3,
            cursor=first_cursor,
        )
    assert different_filter.value.status_code == 422

    with pytest.raises(HTTPException) as different_tenant:
        await list_applications(
            db,
            other_organization.id,
            limit=3,
            cursor=first_cursor,
        )
    assert different_tenant.value.status_code == 422


async def test_talent_member_cursor_is_gap_free_pool_bound_and_tenant_bound(db):
    organization, user = await _tenant(db, "talent")
    other_organization, other_user = await _tenant(db, "talent-other")
    pool = TalentPool(
        organization_id=organization.id,
        created_by_user_id=user.id,
        name="Cursor pool",
    )
    other_pool = TalentPool(
        organization_id=organization.id,
        created_by_user_id=user.id,
        name="Different filter pool",
    )
    outside_pool = TalentPool(
        organization_id=other_organization.id,
        created_by_user_id=other_user.id,
        name="Other tenant pool",
    )
    db.add_all([pool, other_pool, outside_pool])
    await db.flush()
    shared_time = datetime(2026, 8, 23, 9, 45, tzinfo=UTC)
    members: list[TalentPoolMember] = []
    for index in range(6):
        candidate = Candidate(
            organization_id=organization.id,
            source="manual",
            source_ref=f"cursor-talent-{index}",
            raw_cv=f"Complete talent cursor candidate {index}. " * 3,
        )
        db.add(candidate)
        await db.flush()
        member = TalentPoolMember(
            organization_id=organization.id,
            pool_id=pool.id,
            candidate_id=candidate.id,
            added_by_user_id=user.id,
            created_at=shared_time,
        )
        db.add(member)
        members.append(member)
    await db.commit()

    expected_member_ids = sorted((item.id for item in members), reverse=True)
    seen_member_ids = []
    cursor = None
    page_sizes = []
    first_cursor = None
    while True:
        page = await list_talent_pool_members(
            pool.id,
            limit=2,
            cursor=cursor,
            db=db,
            user=user,
        )
        assert page.total == 6
        page_sizes.append(len(page.items))
        seen_member_ids.extend(item.pool_member_id for item in page.items)
        if first_cursor is None:
            first_cursor = page.next_cursor
        cursor = page.next_cursor
        if cursor is None:
            break

    assert page_sizes == [2, 2, 2]
    assert seen_member_ids == expected_member_ids
    assert len(seen_member_ids) == len(set(seen_member_ids)) == 6
    assert first_cursor is not None

    with pytest.raises(HTTPException) as tampered:
        await list_talent_pool_members(
            pool.id,
            limit=2,
            cursor=_tamper(first_cursor),
            db=db,
            user=user,
        )
    assert tampered.value.status_code == 422

    with pytest.raises(HTTPException) as different_pool:
        await list_talent_pool_members(
            other_pool.id,
            limit=2,
            cursor=first_cursor,
            db=db,
            user=user,
        )
    assert different_pool.value.status_code == 422

    with pytest.raises(HTTPException) as different_tenant:
        await list_talent_pool_members(
            outside_pool.id,
            limit=2,
            cursor=first_cursor,
            db=db,
            user=other_user,
        )
    assert different_tenant.value.status_code == 422


async def test_collection_routes_expose_cursor_not_offset_and_bounded_limits():
    openapi = app.openapi()
    expected_limits = {
        "/applications": 200,
        "/talent/pools": 200,
        "/talent/pools/{pool_id}/members": 100,
        "/talent/search": 100,
        "/positions/{position_id}/talent-matches": 100,
        "/talent/nurture-sequences": 200,
    }
    for path, maximum in expected_limits.items():
        operation = openapi["paths"][path]["get"]
        parameters = {item["name"]: item for item in operation["parameters"]}
        assert "offset" not in parameters
        cursor_string = next(
            item
            for item in parameters["cursor"]["schema"]["anyOf"]
            if item.get("type") == "string"
        )
        assert cursor_string["maxLength"] == 2_048
        assert parameters["limit"]["schema"]["maximum"] == maximum


async def test_all_get_collections_use_the_cursor_page_envelope():
    """Prevent a future router from reintroducing a raw array or offset page."""

    openapi = app.openapi()
    schemas = openapi["components"]["schemas"]
    aggregate_paths = {
        # These are single calculated reports/evidence documents, not pageable
        # collection transports; their nested ``items`` are report sections.
        "/integrations/assessment/applications/{application_id}/evidence",
        "/sourcing/analytics",
    }
    static_reference_paths = {
        "/calendar/providers",
        "/integrations/providers",
    }

    def resolve(schema):
        while "$ref" in schema:
            schema = schemas[schema["$ref"].rsplit("/", 1)[-1]]
        return schema

    collection_paths: set[str] = set()
    for path, path_item in openapi["paths"].items():
        operation = path_item.get("get")
        if operation is None:
            continue
        response = operation.get("responses", {}).get("200")
        if response is None:
            continue
        response_schema = (
            response.get("content", {})
            .get("application/json", {})
            .get("schema", {})
        )
        if not response_schema:
            continue
        resolved = resolve(response_schema)
        assert resolved.get("type") != "array", f"{path} returns a bare array"
        properties = resolved.get("properties", {})
        parameter_names = {
            parameter["name"] for parameter in operation.get("parameters", [])
        }
        if "cursor" in parameter_names:
            assert "items" in properties, f"{path} cursor response omits items"
            assert "next_cursor" in properties, f"{path} cursor response omits next_cursor"
        items_schema = properties.get("items")
        if items_schema is None or resolve(items_schema).get("type") != "array":
            continue
        if path in aggregate_paths:
            continue

        collection_paths.add(path)
        assert "next_cursor" in properties, f"{path} omits next_cursor"
        assert "offset" not in parameter_names, f"{path} exposes offset pagination"
        if path not in static_reference_paths:
            assert "cursor" in parameter_names, f"{path} cannot continue a page"

    assert static_reference_paths <= collection_paths
    assert len(collection_paths) >= 70


async def test_signed_collection_cursor_rejects_tamper_namespace_and_scope_reuse():
    tenant_a = uuid4()
    tenant_b = uuid4()
    scope_a = collection_cursor_scope(
        "cursor.contract.example",
        organization_id=tenant_a,
        filters={"status": "open"},
    )
    cursor = encode_collection_cursor(
        "cursor.contract.example",
        scope=scope_a,
        boundary={"created_at": "2026-08-23T10:00:00+00:00", "id": str(uuid4())},
    )
    decoded = decode_collection_cursor(
        cursor,
        namespace="cursor.contract.example",
        scope=scope_a,
        boundary_keys={"created_at", "id"},
    )
    assert set(decoded) == {"created_at", "id"}

    invalid_cases = [
        (_tamper(cursor), "cursor.contract.example", scope_a),
        (cursor, "cursor.contract.other", scope_a),
        (
            cursor,
            "cursor.contract.example",
            collection_cursor_scope(
                "cursor.contract.example",
                organization_id=tenant_a,
                filters={"status": "closed"},
            ),
        ),
        (
            cursor,
            "cursor.contract.example",
            collection_cursor_scope(
                "cursor.contract.example",
                organization_id=tenant_b,
                filters={"status": "open"},
            ),
        ),
    ]
    for invalid_cursor, namespace, scope in invalid_cases:
        with pytest.raises(HTTPException) as invalid:
            decode_collection_cursor(
                invalid_cursor,
                namespace=namespace,
                scope=scope,
                boundary_keys={"created_at", "id"},
            )
        assert invalid.value.status_code == 422
