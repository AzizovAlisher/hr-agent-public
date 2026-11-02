"""HTTP-boundary regressions for storage, authorization, and upload safety."""

from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException, UploadFile
from pypdf import PdfWriter
from starlette.datastructures import Headers
from starlette.routing import Mount

from app.auth.jwt import encode_access_token
from app.config import get_settings
from app.main import app
from app.models.candidate import Candidate
from app.models.enterprise import PermissionOverride
from app.models.organization import Organization
from app.models.user import User
from app.routers.candidates import download_candidate_cv
from app.routers.positions import MAX_POSITION_UPLOAD_BYTES, _extract_uploaded_text
from app.schemas.candidates import CandidateOut
from app.schemas.enrichment import RecordingOut, TaskOut
from app.services import uploads

pytestmark = pytest.mark.asyncio


def valid_pdf() -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(output)
    return output.getvalue()


def auth(user: User) -> dict[str, str]:
    token = encode_access_token(user.id, user.role, user.locale)
    return {"Authorization": f"Bearer {token}"}


async def seed_roles(db):
    organization = Organization(
        name="Security boundary organization",
        slug=f"security-{uuid4().hex[:10]}",
    )
    db.add(organization)
    await db.flush()
    users = {}
    for role in ("admin", "recruiter", "hiring_manager"):
        user = User(
            organization_id=organization.id,
            email=f"{role}-{uuid4().hex[:10]}@example.test",
            password_hash="unused",
            role=role,
            locale="en",
        )
        db.add(user)
        users[role] = user
    await db.commit()
    return organization, users


async def test_core_routes_enforce_role_permissions_at_http_boundary(db):
    _organization, users = await seed_roles(db)
    recruiter = users["recruiter"]
    manager = users["hiring_manager"]
    admin = users["admin"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.get("/candidates", headers=auth(manager))
        ).status_code == 200

        denied_candidate = await client.post(
            "/candidates/upload/text",
            headers=auth(manager),
            json={"raw_text": "x" * 80, "source_ref": "manager write"},
        )
        assert denied_candidate.status_code == 403
        problem = denied_candidate.json()
        assert problem["detail"] == "Permission denied"
        assert problem["code"] == "forbidden"
        assert problem["status"] == 403
        assert problem["request_id"] == denied_candidate.headers["X-Request-ID"]
        assert denied_candidate.headers["content-type"].startswith(
            "application/problem+json"
        )

        created_candidate = await client.post(
            "/candidates/upload/text",
            headers=auth(recruiter),
            json={"raw_text": "x" * 80, "source_ref": "recruiter write"},
        )
        assert created_candidate.status_code == 201
        candidate_id = created_candidate.json()["id"]

        assert (
            await client.post(
                "/positions",
                headers=auth(manager),
                json={"title": "Manager must not create this"},
            )
        ).status_code == 403
        assert (
            await client.post(
                "/positions",
                headers=auth(recruiter),
                json={"title": "Recruiter-owned role"},
            )
        ).status_code == 201
        assert (
            await client.post(
                "/positions",
                headers=auth(admin),
                json={"title": "Admin-owned unstructured role"},
            )
        ).status_code == 201

        assert (
            await client.get("/workflows/rules", headers=auth(recruiter))
        ).status_code == 200
        assert (
            await client.post(
                "/workflows/rules",
                headers=auth(recruiter),
                json={
                    "name": "Recruiter must not manage workflows",
                    "trigger": "candidate_added",
                    "conditions": {},
                    "actions": [{"type": "notify_owner", "title": "Review"}],
                    "is_active": True,
                },
            )
        ).status_code == 403
        assert (
            await client.post("/workflows/process", headers=auth(recruiter))
        ).status_code == 403
        assert (
            await client.get("/workflows/rules", headers=auth(admin))
        ).status_code == 200

        assert (
            await client.get(f"/candidates/{candidate_id}/tasks", headers=auth(manager))
        ).status_code == 404
        assert (
            await client.post(
                f"/candidates/{candidate_id}/tasks",
                headers=auth(manager),
                json={"title": "Unauthorized task", "submission_type": "text"},
            )
        ).status_code == 404
        assert (
            await client.post(
                f"/candidates/{candidate_id}/tasks",
                headers=auth(recruiter),
                json={"title": "Authorized task", "submission_type": "text"},
            )
        ).status_code == 201

        assert (
            await client.get(
                f"/candidates/{candidate_id}/recordings", headers=auth(manager)
            )
        ).status_code == 404
        assert (
            await client.post(
                f"/candidates/{candidate_id}/recordings",
                headers=auth(manager),
                json={"title": "Unauthorized recording"},
            )
        ).status_code == 404
        assert (
            await client.post(
                f"/candidates/{candidate_id}/recordings",
                headers=auth(recruiter),
                json={"title": "Authorized recording"},
            )
        ).status_code == 201


async def test_permission_override_is_applied_to_real_route(db):
    organization, users = await seed_roles(db)
    recruiter = users["recruiter"]
    db.add(
        PermissionOverride(
            organization_id=organization.id,
            role="recruiter",
            permission="candidates.write",
            allowed=False,
        )
    )
    await db.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/positions",
            headers=auth(recruiter),
            json={"title": "Denied by organization override"},
        )
    assert response.status_code == 403
    assert response.json()["detail"] == "Permission denied"
    assert response.json()["code"] == "forbidden"


async def test_private_storage_is_not_mounted_and_security_headers_are_present():
    mounts = {
        route.path: route.name
        for route in app.routes
        if isinstance(route, Mount) and route.path.startswith("/storage")
    }
    assert mounts == {"/storage/logos": "branding-storage"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "camera=()" in response.headers["permissions-policy"]
    assert response.headers["content-security-policy"].startswith("default-src 'none'")


async def test_invalid_bearer_error_is_sanitized():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/candidates",
            headers={"Authorization": "Bearer not-a-jwt"},
        )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid bearer token"
    assert response.json()["code"] == "unauthorized"
    assert response.headers["www-authenticate"] == "Bearer"


async def test_candidate_dto_replaces_filesystem_path_with_authenticated_url():
    candidate_id = uuid4()
    source = SimpleNamespace(
        id=candidate_id,
        source="manual",
        source_ref="candidate",
        raw_cv="private CV",
        parsed_cv=None,
        pdf_path="/srv/private/cvs/secret.pdf",
        github_login=None,
        github_data=None,
        github_fetched_at=None,
        is_favorite=False,
        viewed_at=None,
        created_at=datetime.now(UTC),
    )
    payload = CandidateOut.model_validate(source).model_dump(mode="json")
    assert payload["has_original_file"] is True
    assert payload["pdf_path"] == f"/candidates/{candidate_id}/cv"
    assert "/srv/private" not in repr(payload)


async def test_enrichment_dtos_expose_presence_not_filesystem_paths():
    now = datetime.now(UTC)
    recording = RecordingOut.model_validate(
        {
            "id": uuid4(),
            "candidate_id": uuid4(),
            "title": "Interview",
            "status": "uploaded",
            "media_path": "/srv/private/recordings/interview.webm",
            "created_at": now,
            "updated_at": now,
        }
    ).model_dump(mode="json")
    assert recording["has_media_file"] is True
    assert "media_path" not in recording
    assert "/srv/private" not in repr(recording)

    task = TaskOut.model_validate(
        {
            "id": uuid4(),
            "candidate_id": uuid4(),
            "title": "Take-home",
            "submission_type": "file",
            "status": "submitted",
            "file_path": "/srv/private/tasks/submission.zip",
            "created_at": now,
            "updated_at": now,
        }
    ).model_dump(mode="json")
    assert task["has_submission_file"] is True
    assert "file_path" not in task
    assert "/srv/private" not in repr(task)


async def test_upload_signature_validation_and_parser_failure_cleanup(
    tmp_path, monkeypatch
):
    with pytest.raises(HTTPException) as mismatch:
        uploads.validate_upload_content(
            filename="resume.docx",
            media_type="application/pdf",
            content=b"%PDF-not-a-docx",
            allowed_suffixes={".pdf", ".docx"},
        )
    assert mismatch.value.status_code == 415

    storage_root = tmp_path / "private-storage"
    monkeypatch.setattr(get_settings(), "storage_dir", str(storage_root))

    def fail_parser(_path):
        raise RuntimeError("parser failed")

    monkeypatch.setattr(uploads, "extract_text_from_pdf", fail_parser)
    with pytest.raises(RuntimeError, match="parser failed"):
        uploads.candidate_from_pdf(valid_pdf())
    assert list((storage_root / "cvs").glob("*")) == []


async def test_position_document_upload_is_bounded_before_parsing():
    oversized = UploadFile(
        file=BytesIO(b"x" * (MAX_POSITION_UPLOAD_BYTES + 1)),
        filename="position.txt",
        headers=Headers({"content-type": "text/plain"}),
    )
    with pytest.raises(HTTPException) as exc:
        await _extract_uploaded_text(oversized, max_chars=20_000)
    assert exc.value.status_code == 413


async def test_public_branding_rejects_active_or_mismatched_content():
    with pytest.raises(HTTPException) as active:
        await uploads.save_org_logo(
            org_id=uuid4(),
            upload=UploadFile(
                file=BytesIO(b"<svg><script>alert(1)</script></svg>"),
                filename="logo.svg",
                headers=Headers({"content-type": "image/svg+xml"}),
            ),
        )
    assert active.value.status_code == 415
    with pytest.raises(HTTPException) as mismatch:
        await uploads.save_org_logo(
            org_id=uuid4(),
            upload=UploadFile(
                file=BytesIO(b"not really a png"),
                filename="logo.png",
                headers=Headers({"content-type": "image/png"}),
            ),
        )
    assert mismatch.value.status_code == 422


async def test_authenticated_cv_download_is_private_and_uses_safe_disposition(
    db, tmp_path, monkeypatch
):
    organization, users = await seed_roles(db)
    storage_root = tmp_path / "private-storage"
    monkeypatch.setattr(get_settings(), "storage_dir", str(storage_root))
    stored_cv = storage_root / "cvs" / "opaque.pdf"
    stored_cv.parent.mkdir(parents=True)
    stored_cv.write_bytes(b"%PDF-private")
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref='unsafe"\r\nX-Injected: yes',
        raw_cv="private CV contents",
        pdf_path=str(stored_cv),
    )
    db.add(candidate)
    await db.commit()

    response = await download_candidate_cv(
        candidate.id,
        db=db,
        user=users["recruiter"],
    )
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["x-content-type-options"] == "nosniff"
    disposition = response.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    assert "X-Injected:" not in disposition
    assert str(stored_cv) not in disposition
    assert b"".join([part async for part in response.body_iterator]) == b"%PDF-private"
