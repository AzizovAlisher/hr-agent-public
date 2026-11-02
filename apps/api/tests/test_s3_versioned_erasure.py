"""Permanent, retry-safe erasure contracts for versioned S3/MinIO objects."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

import httpx
import pytest

from app.storage import BlobStoreError, LocalBlobStore, S3BlobStore, private_object_key


pytestmark = pytest.mark.asyncio


def _version_listing(
    key: str,
    entries: dict[str, bool],
    *,
    suffix_key: str | None = None,
) -> bytes:
    items: list[str] = []
    for version_id, delete_marker in entries.items():
        tag = "DeleteMarker" if delete_marker else "Version"
        items.append(
            f"<{tag}><Key>{key}</Key><VersionId>{version_id}</VersionId>"
            f"<IsLatest>false</IsLatest></{tag}>"
        )
    if suffix_key is not None:
        items.append(
            f"<Version><Key>{suffix_key}</Key><VersionId>shadow-version</VersionId>"
            "<IsLatest>false</IsLatest></Version>"
        )
    return (
        '<ListVersionsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"<Prefix>{key}</Prefix>{''.join(items)}"
        "<IsTruncated>false</IsTruncated></ListVersionsResult>"
    ).encode()


async def test_local_delete_returns_verified_idempotent_receipt(tmp_path: Path):
    store = LocalBlobStore(tmp_path / "objects")
    key = "private/test/tasks/answer.bin"
    source = tmp_path / "answer.bin"
    source.write_bytes(b"private")
    await store.put_file(key, source, content_type="application/octet-stream")

    first = await store.delete(key)
    second = await store.delete(key)

    assert first.backend == "local"
    assert first.verification == "path_absent"
    assert first.versions_deleted == 1
    assert second.versions_deleted == 0
    assert len(first.receipt_sha256) == 64
    assert await store.stat(key) is None


async def test_s3_version_delete_retries_partial_failure_and_verifies_empty():
    key = "private/11111111-1111-1111-1111-111111111111/tasks/file.bin"
    entries = {"version-new": False, "version-old": False, "delete-marker": True}
    fail_once = True
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fail_once
        requests.append(request)
        if request.method == "GET" and "versions" in request.url.params:
            return httpx.Response(200, content=_version_listing(key, entries))
        if request.method == "DELETE":
            version_id = request.url.params.get("versionId")
            if version_id == "version-old" and fail_once:
                fail_once = False
                return httpx.Response(503, text="provider detail must stay private")
            if version_id is not None:
                entries.pop(version_id, None)
                return httpx.Response(204)
        return httpx.Response(500)

    store = S3BlobStore(
        endpoint="https://minio.example.test",
        bucket="hr-agent-private",
        access_key="access",
        secret_key="secret",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(BlobStoreError) as failed:
        await store.delete(key)
    assert "provider detail" not in str(failed.value)
    assert "version-new" not in entries
    assert entries == {"version-old": False, "delete-marker": True}

    receipt = await store.delete(key)

    assert entries == {}
    assert receipt.backend == "s3"
    assert receipt.verification == "version_listing_empty"
    assert receipt.versions_deleted == 1
    assert receipt.delete_markers_deleted == 1
    assert requests[-1].method == "GET"
    assert "versions" in requests[-1].url.params
    assert all(
        request.headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
        for request in requests
    )


async def test_s3_version_delete_never_removes_a_longer_prefix_key():
    key = "private/11111111-1111-1111-1111-111111111111/tasks/file.bin"
    suffix_key = f"{key}.unrelated"
    entries = {"owned-version": False}
    deleted: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                content=_version_listing(key, entries, suffix_key=suffix_key),
            )
        if request.method == "DELETE":
            version_id = request.url.params.get("versionId")
            if version_id is not None:
                deleted.append(version_id)
                entries.pop(version_id, None)
                return httpx.Response(204)
        return httpx.Response(500)

    store = S3BlobStore(
        endpoint="https://minio.example.test",
        bucket="hr-agent-private",
        access_key="access",
        secret_key="secret",
        transport=httpx.MockTransport(handler),
    )

    receipt = await store.delete(key)

    assert receipt.versions_deleted == 1
    assert deleted == ["owned-version"]
    assert "shadow-version" not in deleted


async def test_s3_version_delete_fails_closed_when_provider_makes_no_progress():
    key = "private/11111111-1111-1111-1111-111111111111/tasks/file.bin"
    entries = {"still-recoverable": False}
    listing_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal listing_count
        if request.method == "GET":
            listing_count += 1
            return httpx.Response(200, content=_version_listing(key, entries))
        if request.method == "DELETE":
            # Some gateways hide permission errors as 404. The authoritative
            # re-list must prevent that response from settling erasure.
            return httpx.Response(404)
        return httpx.Response(500)

    store = S3BlobStore(
        endpoint="https://minio.example.test",
        bucket="hr-agent-private",
        access_key="access",
        secret_key="secret",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(BlobStoreError, match="made no progress"):
        await store.delete(key)

    assert listing_count == 2
    assert entries == {"still-recoverable": False}


async def test_reference_minio_policy_grants_only_required_version_operations():
    project_root = Path(__file__).resolve().parents[3]
    policy = json.loads(
        (project_root / "ops/minio/runtime-policy.json").read_text(encoding="utf-8")
    )
    actions = {
        action
        for statement in policy["Statement"]
        for action in statement["Action"]
    }

    assert "s3:ListBucketVersions" in actions
    assert "s3:DeleteObjectVersion" in actions
    assert "s3:PutBucketVersioning" not in actions
    assert "s3:DeleteBucket" not in actions
    assert "s3:*" not in actions

    private = json.loads(
        (project_root / "ops/minio/private-lifecycle.json").read_text(
            encoding="utf-8"
        )
    )
    by_id = {rule["ID"]: rule for rule in private["Rules"]}
    assert by_id["private-noncurrent-version-expiry"][
        "NoncurrentVersionExpiration"
    ] == {"NoncurrentDays": 35}
    assert by_id["quarantine-current-expiry"]["Expiration"] == {"Days": 1}


async def _best_effort_enable_versioning(store: S3BlobStore) -> None:
    """Enable the CI contract bucket when its credentials are bucket admins.

    Reference runtime credentials intentionally lack this permission; its
    bootstrap enables versioning before issuing the least-privilege account.
    """

    body = (
        b'<VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        b"<Status>Enabled</Status></VersioningConfiguration>"
    )
    base_path = store._endpoint.path.rstrip("/")
    canonical_uri = quote(f"{base_path}/{store._bucket}", safe="/-_.~")
    url = urlunsplit(
        (
            store._endpoint.scheme,
            store._endpoint.netloc,
            canonical_uri,
            "versioning=",
            "",
        )
    )
    headers = store._headers(
        method="PUT",
        canonical_uri=canonical_uri,
        canonical_query="versioning=",
        payload_hash=hashlib.sha256(body).hexdigest(),
        now=datetime.now(UTC),
        extra={
            "content-length": str(len(body)),
            "content-type": "application/xml",
        },
    )
    async with httpx.AsyncClient(timeout=store._timeout) as client:
        response = await client.put(url, headers=headers, content=body)
    if response.status_code not in {200, 204, 403}:
        pytest.fail(f"Unable to prepare versioned MinIO bucket: {response.status_code}")


@pytest.mark.skipif(
    not os.environ.get("MINIO_TEST_ENDPOINT"),
    reason="Set MINIO_TEST_ENDPOINT and credentials for live compatibility",
)
async def test_live_versioned_minio_erasure_removes_versions_and_markers(
    tmp_path: Path,
):
    store = S3BlobStore(
        endpoint=os.environ["MINIO_TEST_ENDPOINT"],
        bucket=os.environ["MINIO_TEST_BUCKET"],
        access_key=os.environ["MINIO_TEST_ACCESS_KEY"],
        secret_key=os.environ["MINIO_TEST_SECRET_KEY"],
    )
    await _best_effort_enable_versioning(store)
    key = private_object_key(uuid4(), "tasks", uuid4(), ".bin")
    source = tmp_path / "version.bin"

    for payload in (b"version-one", b"version-two"):
        source.write_bytes(payload)
        await store.put_file(key, source, content_type="application/octet-stream")
    # Create a real delete marker between versions, then make another version
    # current so HEAD-only verification would miss both historical states.
    marker_response = await store._request("DELETE", key)
    assert marker_response.status_code in {200, 202, 204}
    source.write_bytes(b"version-three")
    await store.put_file(key, source, content_type="application/octet-stream")

    before = await store._list_object_versions(key)
    assert sum(not marker for _version, marker in before) >= 3
    assert sum(marker for _version, marker in before) >= 1

    receipt = await store.delete(key)

    # This assertion is an authoritative ListObjectVersions check. A HEAD 404
    # by itself would pass while old content remained recoverable.
    assert await store._list_object_versions(key) == ()
    assert receipt.versions_deleted >= 3
    assert receipt.delete_markers_deleted >= 1
    assert (await store.delete(key)).versions_deleted == 0
