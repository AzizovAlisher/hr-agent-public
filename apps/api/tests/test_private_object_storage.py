"""Adversarial contracts for private/public object intake and delivery."""

from __future__ import annotations

import asyncio
import io
import os
import zipfile
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image
from pydantic import SecretStr, ValidationError
from pypdf import PdfWriter
from starlette.datastructures import Headers

from app.config import Settings, get_settings
from app.models.organization import Organization
from app.services.uploads import (
    cleanup_org_logo_after_db_failure,
    ensure_storage_dir,
    private_blob_response,
    safe_content_disposition,
    save_org_logo,
    store_private_upload,
    validated_private_media_type,
)
from app.storage import (
    BlobStoreError,
    FakeMalwareScanner,
    LocalBlobStore,
    PublicBrandingRedirectApp,
    S3BlobStore,
    get_blob_store,
    get_public_blob_store,
    private_object_key,
    require_tenant_private_key,
)
from app.storage.intake import quarantine_scan_promote
from app.storage.validation import UploadValidationError, validate_upload_bytes


def _upload(filename: str, content_type: str, content: bytes) -> UploadFile:
    return UploadFile(
        filename=filename,
        file=io.BytesIO(content),
        headers=Headers({"content-type": content_type}),
    )


def _pdf_bytes() -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(output)
    return output.getvalue()


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), color=(22, 101, 52)).save(output, format="PNG")
    return output.getvalue()


def _files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


async def _response_body(response) -> bytes:
    return b"".join([chunk async for chunk in response.body_iterator])


async def test_local_quarantine_scan_promote_and_private_download(tmp_path: Path):
    organization_id = uuid4()
    store = LocalBlobStore(tmp_path / "objects")
    content = _pdf_bytes()
    stored = await quarantine_scan_promote(
        _upload("resume.pdf", "application/pdf", content),
        organization_id=organization_id,
        category="candidates",
        allowed_suffixes={".pdf"},
        max_bytes=15 * 1024 * 1024,
        store=store,
        scanner=FakeMalwareScanner(),
    )

    assert stored.key.startswith(f"private/{organization_id}/candidates/")
    assert stored.size_bytes == len(content)
    assert stored.scan.engine == "fake"
    assert not any("quarantine" in path.parts for path in _files(store.root))

    response = await private_blob_response(
        stored.key,
        filename='Candidate\r\nX-Evil: yes "resume".pdf',
        media_type="application/pdf",
        store=store,
    )
    assert await _response_body(response) == content
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "\r" not in response.headers["content-disposition"]
    assert "\n" not in response.headers["content-disposition"]
    assert response.headers["content-length"] == str(len(content))


async def test_public_branding_promotions_are_versioned_not_destructive(
    tmp_path: Path,
):
    organization_id = uuid4()
    store = LocalBlobStore(tmp_path / "branding")
    first = await store_private_upload(
        _upload("logo.png", "image/png", _png_bytes()),
        organization_id=organization_id,
        category="branding",
        allowed_suffixes={".png"},
        max_bytes=2 * 1024 * 1024,
        label="Logo",
        public_branding=True,
        store=store,
        scanner=FakeMalwareScanner(),
    )
    second = await store_private_upload(
        _upload("logo.png", "image/png", _png_bytes()),
        organization_id=organization_id,
        category="branding",
        allowed_suffixes={".png"},
        max_bytes=2 * 1024 * 1024,
        label="Logo",
        public_branding=True,
        store=store,
        scanner=FakeMalwareScanner(),
    )
    assert first.key != second.key
    assert first.key.startswith(f"logos/{organization_id}/")
    assert second.key.startswith(f"logos/{organization_id}/")
    assert await store.stat(first.key) is not None
    assert await store.stat(second.key) is not None


async def test_s3_branding_compatibility_path_redirects_only_safe_public_keys():
    organization_id = uuid4()
    object_id = uuid4()
    application = PublicBrandingRedirectApp("https://assets.example.test/base")
    safe_scope = {
        "type": "http",
        "method": "GET",
        "path": f"/storage/logos/{organization_id}/{object_id}.png",
        "root_path": "/storage/logos",
        "headers": [],
    }
    unsafe_scope = {
        **safe_scope,
        "path": f"/{organization_id}/../../private/resume.pdf",
    }

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    safe_messages: list[dict] = []

    async def send_safe(message: dict) -> None:
        safe_messages.append(message)

    await application(safe_scope, receive, send_safe)
    assert safe_messages[0]["status"] == 307
    headers = dict(safe_messages[0]["headers"])
    assert (
        headers[b"location"]
        == (
            f"https://assets.example.test/base/logos/{organization_id}/{object_id}.png"
        ).encode()
    )

    unsafe_messages: list[dict] = []

    async def send_unsafe(message: dict) -> None:
        unsafe_messages.append(message)

    await application(unsafe_scope, receive, send_unsafe)
    assert unsafe_messages[0]["status"] == 404


def test_s3_startup_does_not_require_a_writable_local_storage_tree(
    tmp_path: Path,
    monkeypatch,
):
    target = tmp_path / "must-not-be-created"
    settings = get_settings()
    monkeypatch.setattr(settings, "blob_store_backend", "s3")
    monkeypatch.setattr(settings, "storage_dir", str(target))

    ensure_storage_dir()

    assert target.exists() is False


def test_s3_branding_and_private_objects_use_distinct_buckets(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "blob_store_backend", "s3")
    monkeypatch.setattr(settings, "s3_endpoint", "https://objects.example.test")
    monkeypatch.setattr(settings, "s3_bucket", "hr-agent-private")
    monkeypatch.setattr(settings, "s3_public_bucket", "hr-agent-public")
    monkeypatch.setattr(settings, "s3_access_key", "test-storage-access")
    monkeypatch.setattr(settings, "s3_secret_key", SecretStr("test-storage-secret"))
    monkeypatch.setattr(
        settings, "s3_public_base_url", "https://assets.example.test"
    )

    private_store = get_blob_store()
    branding_store = get_public_blob_store()

    assert isinstance(private_store, S3BlobStore)
    assert isinstance(branding_store, S3BlobStore)
    assert private_store._bucket == "hr-agent-private"
    assert branding_store._bucket == "hr-agent-public"
    assert private_store.public_url("logos/example/logo.png") is None
    assert (
        branding_store.public_url("logos/example/logo.png")
        == "https://assets.example.test/logos/example/logo.png"
    )


async def test_logo_commit_uncertainty_cleanup_checks_authoritative_row(
    db,
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "storage_dir", str(tmp_path / "logos"))
    organization = Organization(
        name="Logo commit uncertainty",
        slug=f"logo-commit-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.commit()
    landed = await save_org_logo(
        org_id=organization.id,
        upload=_upload("logo.png", "image/png", _png_bytes()),
    )
    organization.logo_path = landed
    await db.commit()
    await cleanup_org_logo_after_db_failure(
        org_id=organization.id,
        logo_path=landed,
    )
    assert await get_blob_store().stat(landed) is not None

    orphan = await save_org_logo(
        org_id=organization.id,
        upload=_upload("logo.png", "image/png", _png_bytes()),
    )
    await cleanup_org_logo_after_db_failure(
        org_id=organization.id,
        logo_path=orphan,
    )
    assert await get_blob_store().stat(orphan) is None


@pytest.mark.parametrize(
    "verdict,status_code", [("infected", 422), ("unavailable", 503)]
)
async def test_scanner_failure_never_promotes_or_leaves_quarantine(
    tmp_path: Path,
    verdict: str,
    status_code: int,
):
    store = LocalBlobStore(tmp_path / verdict)
    scanner = FakeMalwareScanner(verdict=verdict)
    with pytest.raises(HTTPException) as caught:
        await store_private_upload(
            _upload("resume.pdf", "application/pdf", _pdf_bytes()),
            organization_id=uuid4(),
            category="candidates",
            allowed_suffixes={".pdf"},
            max_bytes=15 * 1024 * 1024,
            label="CV file",
            store=store,
            scanner=scanner,
        )
    assert caught.value.status_code == status_code
    assert _files(store.root) == []
    assert scanner.scanned_paths and not scanner.scanned_paths[0].exists()


async def test_stream_limit_stops_before_validation_and_cleans_up(tmp_path: Path):
    store = LocalBlobStore(tmp_path / "limit")
    with pytest.raises(HTTPException) as caught:
        await store_private_upload(
            _upload("large.pdf", "application/pdf", b"x" * 65),
            organization_id=uuid4(),
            category="candidates",
            allowed_suffixes={".pdf"},
            max_bytes=64,
            label="CV file",
            store=store,
            scanner=FakeMalwareScanner(),
        )
    assert caught.value.status_code == 413
    assert _files(store.root) == []


class _PromoteThenFailStore(LocalBlobStore):
    async def promote(self, source_key: str, destination_key: str) -> None:
        await super().promote(source_key, destination_key)
        raise BlobStoreError("copy response was lost")


class _PromoteThenCancelStore(LocalBlobStore):
    async def promote(self, source_key: str, destination_key: str) -> None:
        await super().promote(source_key, destination_key)
        raise asyncio.CancelledError


async def test_partial_promotion_failure_removes_destination_and_quarantine(
    tmp_path: Path,
):
    store = _PromoteThenFailStore(tmp_path / "partial-promotion")
    with pytest.raises(HTTPException) as caught:
        await store_private_upload(
            _upload("resume.pdf", "application/pdf", _pdf_bytes()),
            organization_id=uuid4(),
            category="candidates",
            allowed_suffixes={".pdf"},
            max_bytes=15 * 1024 * 1024,
            label="CV file",
            store=store,
            scanner=FakeMalwareScanner(),
        )
    assert caught.value.status_code == 503
    assert _files(store.root) == []


async def test_promotion_cancellation_propagates_after_complete_intake_cleanup(
    tmp_path: Path,
):
    store = _PromoteThenCancelStore(tmp_path / "cancelled-promotion")

    with pytest.raises(asyncio.CancelledError):
        await quarantine_scan_promote(
            _upload("resume.pdf", "application/pdf", _pdf_bytes()),
            organization_id=uuid4(),
            category="candidates",
            allowed_suffixes={".pdf"},
            max_bytes=15 * 1024 * 1024,
            store=store,
            scanner=FakeMalwareScanner(),
        )

    assert _files(store.root) == []


async def test_external_task_cancellation_drains_promotion_before_cleanup(
    tmp_path: Path,
):
    class PausedPromotionStore(LocalBlobStore):
        def __init__(self, root: Path):
            super().__init__(root)
            self.promoted = asyncio.Event()
            self.release = asyncio.Event()

        async def promote(self, source_key: str, destination_key: str) -> None:
            await super().promote(source_key, destination_key)
            self.promoted.set()
            await self.release.wait()

    store = PausedPromotionStore(tmp_path / "externally-cancelled-promotion")
    task = asyncio.create_task(
        quarantine_scan_promote(
            _upload("resume.pdf", "application/pdf", _pdf_bytes()),
            organization_id=uuid4(),
            category="candidates",
            allowed_suffixes={".pdf"},
            max_bytes=15 * 1024 * 1024,
            store=store,
            scanner=FakeMalwareScanner(),
        )
    )
    await store.promoted.wait()
    task.cancel()
    store.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert _files(store.root) == []


def test_magic_mime_active_content_polyglot_and_parser_limits():
    with pytest.raises(UploadValidationError, match="Active SVG"):
        validate_upload_bytes(
            filename="brand.png",
            media_type="image/png",
            content=b"<svg><script>alert(1)</script></svg>",
            allowed_suffixes={".png"},
        )
    with pytest.raises(UploadValidationError, match="content type"):
        validate_upload_bytes(
            filename="resume.pdf",
            media_type="image/png",
            content=_pdf_bytes(),
            allowed_suffixes={".pdf"},
        )
    with pytest.raises(UploadValidationError, match="Polyglot"):
        validate_upload_bytes(
            filename="resume.pdf",
            media_type="application/pdf",
            content=_pdf_bytes() + b"PK\x03\x04hidden.zip",
            allowed_suffixes={".pdf"},
        )
    with pytest.raises(UploadValidationError, match="parsed safely"):
        validate_upload_bytes(
            filename="resume.pdf",
            media_type="application/pdf",
            content=b"%PDF-1.7\nnot-a-pdf",
            allowed_suffixes={".pdf"},
        )
    with pytest.raises(UploadValidationError, match="polyglot"):
        validate_upload_bytes(
            filename="brand.png",
            media_type="image/png",
            content=_png_bytes() + b"PK\x03\x04hidden.zip",
            allowed_suffixes={".png"},
        )


def test_ooxml_zip_bomb_and_active_archive_entries_are_rejected():
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", b"0" * (2 * 1024 * 1024))
    with pytest.raises(UploadValidationError, match="compression ratio"):
        validate_upload_bytes(
            filename="answer.docx",
            media_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            content=bomb.getvalue(),
            allowed_suffixes={".docx"},
        )

    active = io.BytesIO()
    with zipfile.ZipFile(active, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
        archive.writestr("word/embeddings/payload.exe", b"MZ")
    with pytest.raises(UploadValidationError, match="active or embedded"):
        validate_upload_bytes(
            filename="answer.docx",
            media_type="application/octet-stream",
            content=active.getvalue(),
            allowed_suffixes={".docx"},
        )

    external = io.BytesIO()
    with zipfile.ZipFile(external, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
        archive.writestr(
            "word/_rels/document.xml.rels",
            (
                '<Relationships><Relationship Type="attachedTemplate" '
                'Target="file://attacker/share/template.dotm" '
                'TargetMode="External"/></Relationships>'
            ),
        )
    with pytest.raises(UploadValidationError, match="external relationship"):
        validate_upload_bytes(
            filename="answer.docx",
            media_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            content=external.getvalue(),
            allowed_suffixes={".docx"},
        )

    trailing = io.BytesIO()
    with zipfile.ZipFile(trailing, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
    with pytest.raises(UploadValidationError, match="trailing polyglot"):
        validate_upload_bytes(
            filename="answer.docx",
            media_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            content=trailing.getvalue() + b"MZ-appended-payload",
            allowed_suffixes={".docx"},
        )


def test_tenant_keys_and_content_disposition_fail_closed():
    owner = uuid4()
    other = uuid4()
    key = private_object_key(owner, "candidates", uuid4(), ".pdf")
    assert (
        require_tenant_private_key(
            key,
            organization_id=owner,
            category="candidates",
        )
        == key
    )
    with pytest.raises(ValueError):
        require_tenant_private_key(
            key,
            organization_id=other,
            category="candidates",
        )
    with pytest.raises(ValueError):
        require_tenant_private_key(
            "private/../other/resume.pdf",
            organization_id=owner,
            category="candidates",
        )
    disposition = safe_content_disposition('../résumé\r\n".pdf')
    assert disposition == 'attachment; filename="r_sum_.pdf"'
    with pytest.raises(HTTPException) as unsafe_download:
        validated_private_media_type(
            f"private/{owner}/candidates/{uuid4()}.exe",
            allowed_suffixes={".pdf", ".docx"},
        )
    assert unsafe_download.value.status_code == 410


def test_storage_config_rejects_invalid_bucket_and_masks_secret():
    with pytest.raises(ValidationError, match="DNS-compatible"):
        Settings(s3_bucket="Invalid_Bucket")
    with pytest.raises(ValidationError, match="credentials are incomplete"):
        Settings(blob_store_backend="s3", s3_endpoint="http://localhost:9000")
    with pytest.raises(ValidationError, match="CLAMAV_HOST"):
        Settings(clamav_host="http://clamav.internal")
    with pytest.raises(ValidationError, match="bounded absolute directory"):
        Settings(legacy_private_storage_dir="./legacy-files")
    assert (
        Settings(
            legacy_private_storage_dir="/var/lib/hr-agent/legacy-files"
        ).legacy_private_storage_dir
        == "/var/lib/hr-agent/legacy-files"
    )
    settings = Settings(s3_secret_key="do-not-render-this-storage-secret")
    assert "do-not-render-this-storage-secret" not in repr(settings)


async def test_s3_minio_sigv4_copy_stream_and_public_namespace(tmp_path: Path):
    requests: list[tuple[str, str, dict[str, str], bytes]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        requests.append(
            (
                request.method,
                request.url.path,
                dict(request.headers),
                body,
            )
        )
        if request.method == "GET" and "versions" in request.url.params:
            return httpx.Response(
                200,
                content=(
                    b'<ListVersionsResult xmlns="http://s3.amazonaws.com/doc/'
                    b'2006-03-01/"><Prefix>quarantine/'
                    + str(organization_id).encode()
                    + b"/candidates/"
                    + quarantine.rsplit("/", 1)[-1].encode()
                    + b"</Prefix><IsTruncated>false</IsTruncated>"
                    b"</ListVersionsResult>"
                ),
            )
        if request.method == "HEAD":
            return httpx.Response(
                200,
                headers={"content-length": "7", "content-type": "application/pdf"},
            )
        if request.method == "GET":
            return httpx.Response(200, content=b"payload")
        return httpx.Response(204)

    store = S3BlobStore(
        endpoint="https://minio.example.test",
        bucket="hr-agent",
        access_key="access",
        secret_key="secret",
        region="us-east-1",
        public_base_url="https://assets.example.test",
        transport=httpx.MockTransport(handler),
    )
    source = tmp_path / "source.pdf"
    source.write_bytes(b"payload")
    organization_id = uuid4()
    quarantine = f"quarantine/{organization_id}/candidates/{uuid4()}.upload"
    private = private_object_key(organization_id, "candidates", uuid4(), ".pdf")

    await store.put_file(quarantine, source, content_type="application/octet-stream")
    await store.promote(quarantine, private)
    assert (await store.stat(private)).size_bytes == 7
    assert b"".join([chunk async for chunk in store.iter_bytes(private)]) == b"payload"
    assert store.public_url(f"logos/{organization_id}.png") == (
        f"https://assets.example.test/logos/{organization_id}.png"
    )
    assert store.public_url(private) is None

    assert requests[0][0:2] == (
        "PUT",
        f"/hr-agent/{quarantine}",
    )
    assert requests[0][3] == b"payload"
    assert requests[0][2]["authorization"].startswith("AWS4-HMAC-SHA256 ")
    copy = requests[1]
    assert copy[0:2] == ("PUT", f"/hr-agent/{private}")
    assert copy[2]["x-amz-copy-source"] == f"/hr-agent/{quarantine}"


async def test_s3_provider_error_body_is_not_reflected(tmp_path: Path):
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal-bucket secret-provider-detail")

    store = S3BlobStore(
        endpoint="https://minio.example.test",
        bucket="hr-agent",
        access_key="access",
        secret_key="secret",
        transport=httpx.MockTransport(handler),
    )
    source = tmp_path / "source.txt"
    source.write_text("safe", encoding="utf-8")
    with pytest.raises(BlobStoreError) as caught:
        await store.put_file(
            "quarantine/test/file.upload", source, content_type="text/plain"
        )
    assert "secret-provider-detail" not in str(caught.value)


@pytest.mark.skipif(
    not os.environ.get("MINIO_TEST_ENDPOINT"),
    reason="Set MINIO_TEST_ENDPOINT and credentials for live compatibility",
)
async def test_live_minio_put_promote_read_delete(tmp_path: Path):
    store = S3BlobStore(
        endpoint=os.environ["MINIO_TEST_ENDPOINT"],
        bucket=os.environ["MINIO_TEST_BUCKET"],
        access_key=os.environ["MINIO_TEST_ACCESS_KEY"],
        secret_key=os.environ["MINIO_TEST_SECRET_KEY"],
    )
    organization_id = uuid4()
    quarantine = f"quarantine/{organization_id}/tests/{uuid4()}.upload"
    private = private_object_key(organization_id, "tests", uuid4(), ".txt")
    content = b"live MinIO compatibility payload"
    source = tmp_path / "payload.txt"
    source.write_bytes(content)

    await store.put_file(quarantine, source, content_type="text/plain")
    await store.promote(quarantine, private)
    metadata = await store.stat(private)
    assert metadata is not None and metadata.size_bytes == len(content)
    assert b"".join([chunk async for chunk in store.iter_bytes(private)]) == content
    assert await store.stat(quarantine) is None
    await store.delete(private)
    assert await store.stat(private) is None
