"""Organization-wide folder authorization and tenant-isolation regressions."""

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.auth.authorization import AuthMode, ResourceScope, operation_policy
from app.auth.jwt import encode_access_token
from app.main import app
from app.models.enterprise import OrganizationApiKey, PermissionOverride
from app.models.folder import Folder
from app.models.organization import Organization
from app.models.position import Position
from app.models.requisition import Requisition
from app.models.user import User
from app.services.enterprise import new_api_secret

pytestmark = pytest.mark.asyncio


def _bearer(user: User) -> dict[str, str]:
    token = encode_access_token(
        user.id,
        user.role,
        user.locale,
        token_version=user.token_version,
    )
    return {"Authorization": f"Bearer {token}"}


async def _new_user(db, organization_id, role: str, label: str) -> User:
    user = User(
        organization_id=organization_id,
        email=f"{label}-{uuid4().hex[:10]}@example.test",
        password_hash="unused",
        role=role,
        locale="en",
    )
    db.add(user)
    await db.flush()
    return user


async def _seed_mixed_scope_tree(db) -> dict:
    organization = Organization(
        name="Folder authorization tenant",
        slug=f"folder-authorization-{uuid4().hex[:10]}",
    )
    other_organization = Organization(
        name="Other folder tenant",
        slug=f"folder-authorization-other-{uuid4().hex[:10]}",
    )
    db.add_all([organization, other_organization])
    await db.flush()

    users = {
        role: await _new_user(db, organization.id, role, role)
        for role in (
            "admin",
            "recruiter",
            "hiring_manager",
            "interviewer",
            "restricted",
        )
    }
    other_admin = await _new_user(db, other_organization.id, "admin", "other-admin")

    shared = Folder(
        organization_id=organization.id,
        name="Confidential shared grouping",
        sort_order=10,
    )
    target = Folder(
        organization_id=organization.id,
        name="Organization-only target",
        sort_order=20,
    )
    other_folder = Folder(
        organization_id=other_organization.id,
        name="Other tenant grouping canary",
        sort_order=10,
    )
    db.add_all([shared, target, other_folder])
    await db.flush()

    assigned_position = Position(
        organization_id=organization.id,
        title="Manager-assigned Position A",
        created_by=users["recruiter"].id,
        folder_id=shared.id,
        folder=shared.name,
    )
    unassigned_position = Position(
        organization_id=organization.id,
        title="Unassigned confidential Position Z",
        created_by=users["recruiter"].id,
        folder_id=shared.id,
        folder=shared.name,
    )
    other_position = Position(
        organization_id=other_organization.id,
        title="Other tenant Position Z canary",
        created_by=other_admin.id,
        folder_id=other_folder.id,
        folder=other_folder.name,
    )
    db.add_all([assigned_position, unassigned_position, other_position])
    await db.flush()
    db.add(
        Requisition(
            organization_id=organization.id,
            created_by_user_id=users["recruiter"].id,
            hiring_manager_user_id=users["hiring_manager"].id,
            position_id=assigned_position.id,
            title=assigned_position.title,
            business_justification="Explicit manager assignment for Position A",
        )
    )

    # Deliberately grant every assignment-scoped/non-operational role the
    # coarse folder actions. The structural role gate must remain authoritative.
    db.add_all(
        PermissionOverride(
            organization_id=organization.id,
            role=role,
            permission=permission,
            allowed=True,
        )
        for role in ("hiring_manager", "interviewer", "restricted")
        for permission in ("candidates.read", "candidates.write")
    )

    api_secret, api_prefix, api_digest = new_api_secret()
    api_key = OrganizationApiKey(
        organization_id=organization.id,
        created_by_user_id=users["admin"].id,
        name="Folder isolation key",
        key_prefix=api_prefix,
        key_digest=api_digest,
        scopes=["candidates:read", "candidates:write"],
    )
    db.add(api_key)
    await db.commit()
    return {
        "organization": organization,
        "other_organization": other_organization,
        "users": users,
        "other_admin": other_admin,
        "shared": shared,
        "target": target,
        "other_folder": other_folder,
        "assigned_position": assigned_position,
        "unassigned_position": unassigned_position,
        "other_position": other_position,
        "api_key": api_key,
        "api_secret": api_secret,
    }


async def _tree_snapshot(db) -> tuple[list[tuple], list[tuple]]:
    folders = (
        await db.execute(
            select(
                Folder.id,
                Folder.organization_id,
                Folder.parent_id,
                Folder.name,
                Folder.sort_order,
                Folder.is_archived,
            ).order_by(Folder.id)
        )
    ).all()
    positions = (
        await db.execute(
            select(
                Position.id,
                Position.organization_id,
                Position.folder_id,
                Position.folder,
            ).order_by(Position.id)
        )
    ).all()
    return [tuple(row) for row in folders], [tuple(row) for row in positions]


async def test_folder_catalog_declares_exact_bearer_actions_and_organization_scope():
    expected = {
        ("GET", "/folders"): ("candidates.read",),
        ("POST", "/folders"): ("candidates.write",),
        ("PATCH", "/folders/reorder"): ("candidates.write",),
        ("PATCH", "/folders/{folder_id}"): ("candidates.write",),
        ("DELETE", "/folders/{folder_id}"): ("candidates.write",),
    }
    for (method, path), actions in expected.items():
        policy = operation_policy(method, path)
        assert policy is not None
        assert policy.auth_mode is AuthMode.BEARER
        assert policy.actions == actions
        assert policy.scope is ResourceScope.ORGANIZATION


@pytest.mark.parametrize("role", ["hiring_manager", "interviewer", "restricted"])
async def test_assignment_scoped_roles_cannot_read_or_mutate_shared_mixed_scope_folder(
    db,
    role: str,
):
    seeded = await _seed_mixed_scope_tree(db)
    user = seeded["users"][role]
    shared = seeded["shared"]
    target = seeded["target"]
    before = await _tree_snapshot(db)

    requests = [
        ("GET", "/folders", None),
        (
            "POST",
            "/folders",
            {"name": f"{role} denied create", "parent_id": str(shared.id)},
        ),
        (
            "PATCH",
            "/folders/reorder",
            {"parent_id": None, "ordered_ids": [str(target.id), str(shared.id)]},
        ),
        ("PATCH", f"/folders/{shared.id}", {"name": f"{role} denied rename"}),
        ("PATCH", f"/folders/{shared.id}", {"is_archived": True}),
        ("PATCH", f"/folders/{shared.id}", {"parent_id": str(target.id)}),
        ("DELETE", f"/folders/{shared.id}?force=true", None),
        (
            "DELETE",
            f"/folders/{shared.id}?reassign_to={target.id}",
            None,
        ),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        responses = [
            await client.request(
                method,
                path,
                headers=_bearer(user),
                json=body,
            )
            for method, path, body in requests
        ]

    assert [response.status_code for response in responses] == [403] * len(requests)
    assert all(
        response.json()["detail"] == "Organization-wide folder access required"
        for response in responses
    )
    db.expire_all()
    assert await _tree_snapshot(db) == before


async def test_valid_api_key_is_not_a_folder_identity_and_performs_no_read(db):
    seeded = await _seed_mixed_scope_tree(db)
    api_key_id = seeded["api_key"].id
    before = await _tree_snapshot(db)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/folders",
            headers={"X-API-Key": seeded["api_secret"]},
        )

    assert response.status_code == 401
    assert "Confidential shared grouping" not in response.text
    db.expire_all()
    assert await _tree_snapshot(db) == before
    key = await db.get(OrganizationApiKey, api_key_id)
    assert key is not None
    assert key.last_used_at is None


async def test_admin_and_recruiter_keep_folder_access_with_permission_and_tenant_checks(
    db,
):
    seeded = await _seed_mixed_scope_tree(db)
    admin = seeded["users"]["admin"]
    recruiter = seeded["users"]["recruiter"]
    shared = seeded["shared"]
    other_folder = seeded["other_folder"]
    shared_id = shared.id
    other_folder_id = other_folder.id

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        admin_list = await client.get("/folders", headers=_bearer(admin))
        recruiter_list = await client.get("/folders", headers=_bearer(recruiter))
        created = await client.post(
            "/folders",
            headers=_bearer(recruiter),
            json={"name": "Recruiter legitimate child", "parent_id": str(shared.id)},
        )
        cross_tenant_update = await client.patch(
            f"/folders/{other_folder.id}",
            headers=_bearer(admin),
            json={"name": "Cross-tenant mutation must fail"},
        )
        cross_tenant_delete = await client.delete(
            f"/folders/{other_folder.id}?force=true",
            headers=_bearer(admin),
        )

    assert admin_list.status_code == 200
    assert recruiter_list.status_code == 200
    for response in (admin_list, recruiter_list):
        payload = response.json()
        names = {item["name"] for item in payload["items"]}
        assert "Confidential shared grouping" in names
        assert "Other tenant grouping canary" not in names
    assert created.status_code == 201
    assert created.json()["parent_id"] == str(shared.id)
    assert cross_tenant_update.status_code == 404
    assert cross_tenant_delete.status_code == 404

    db.add(
        PermissionOverride(
            organization_id=seeded["organization"].id,
            role="recruiter",
            permission="candidates.write",
            allowed=False,
        )
    )
    await db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.patch(
            f"/folders/{shared.id}",
            headers=_bearer(recruiter),
            json={"name": "Permission bypass must fail"},
        )
        still_readable = await client.get("/folders", headers=_bearer(recruiter))

    assert denied.status_code == 403
    assert denied.json()["detail"] == "Permission denied"
    assert still_readable.status_code == 200
    db.expire_all()
    persisted_other = await db.get(Folder, other_folder_id)
    persisted_shared = await db.get(Folder, shared_id)
    assert persisted_other is not None
    assert persisted_other.name == "Other tenant grouping canary"
    assert persisted_shared is not None
    assert persisted_shared.name == "Confidential shared grouping"
