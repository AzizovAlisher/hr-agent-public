"""Small async AWS SigV4 BlobStore for S3-compatible services such as MinIO.

The project deliberately keeps provider SDKs out of the API runtime. This
adapter implements only the six object operations used by the application and
is exercised against an HTTP transport contract; MinIO is the reference
deployment documented in ``docs/PRIVATE_OBJECT_STORAGE.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx

from app.storage.base import (
    BlobDeletionReceipt,
    BlobEntry,
    BlobListPage,
    BlobNotFoundError,
    BlobStat,
    BlobStore,
    BlobStoreError,
    blob_deletion_receipt,
    validate_blob_key,
    validate_blob_prefix,
)

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_MAX_VERSION_LIST_BYTES = 2 * 1024 * 1024
_MAX_VERSION_DELETIONS_PER_ATTEMPT = 10_000


def _hmac(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


class S3BlobStore(BlobStore):
    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        public_base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30.0,
    ):
        parsed = urlsplit(endpoint.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("S3 endpoint must be an absolute HTTP(S) URL")
        if not re.fullmatch(
            r"(?=.{3,63}$)(?!\d+\.\d+\.\d+\.\d+$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?",
            bucket,
        ) or any(token in bucket for token in ("..", ".-", "-.")):
            raise ValueError("Invalid S3 bucket")
        if (
            not access_key
            or not secret_key
            or any(ord(character) < 32 for character in access_key + secret_key)
            or len(access_key) > 256
            or len(secret_key) > 1024
        ):
            raise ValueError("S3 credentials are required")
        if not re.fullmatch(r"[a-z0-9-]{1,64}", region):
            raise ValueError("Invalid S3 region")
        if public_base_url is not None:
            parsed_public = urlsplit(public_base_url.rstrip("/"))
            if (
                parsed_public.scheme not in {"http", "https"}
                or not parsed_public.netloc
                or parsed_public.query
                or parsed_public.fragment
                or parsed_public.username is not None
                or parsed_public.password is not None
            ):
                raise ValueError("Invalid public object base URL")
        self._endpoint = parsed
        self._bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region
        self._public_base_url = public_base_url.rstrip("/") if public_base_url else None
        self._transport = transport
        self._timeout = timeout_seconds

    def _object_url(self, key: str) -> tuple[str, str]:
        validated = validate_blob_key(key)
        base_path = self._endpoint.path.rstrip("/")
        canonical_uri = quote(
            f"{base_path}/{self._bucket}/{validated}",
            safe="/-_.~",
        )
        return (
            urlunsplit(
                (
                    self._endpoint.scheme,
                    self._endpoint.netloc,
                    canonical_uri,
                    "",
                    "",
                )
            ),
            canonical_uri,
        )

    @staticmethod
    def _canonical_query(parameters: list[tuple[str, str]]) -> str:
        encoded = (
            (quote(name, safe="-_.~"), quote(value, safe="-_.~"))
            for name, value in parameters
        )
        return "&".join(f"{name}={value}" for name, value in sorted(encoded))

    def _bucket_url(
        self,
        parameters: list[tuple[str, str]],
    ) -> tuple[str, str, str]:
        base_path = self._endpoint.path.rstrip("/")
        canonical_uri = quote(f"{base_path}/{self._bucket}", safe="/-_.~")
        canonical_query = self._canonical_query(parameters)
        return (
            urlunsplit(
                (
                    self._endpoint.scheme,
                    self._endpoint.netloc,
                    canonical_uri,
                    canonical_query,
                    "",
                )
            ),
            canonical_uri,
            canonical_query,
        )

    def _headers(
        self,
        *,
        method: str,
        canonical_uri: str,
        payload_hash: str,
        now: datetime,
        extra: dict[str, str] | None = None,
        canonical_query: str = "",
    ) -> dict[str, str]:
        amz_date = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        date_stamp = amz_date[:8]
        headers = {
            "host": self._endpoint.netloc,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        for name, value in (extra or {}).items():
            headers[name.casefold()] = " ".join(value.strip().split())
        ordered = sorted(headers.items())
        canonical_headers = "".join(f"{name}:{value}\n" for name, value in ordered)
        signed_headers = ";".join(name for name, _value in ordered)
        canonical_request = "\n".join(
            (
                method.upper(),
                canonical_uri,
                canonical_query,
                canonical_headers,
                signed_headers,
                payload_hash,
            )
        )
        scope = f"{date_stamp}/{self._region}/s3/aws4_request"
        string_to_sign = "\n".join(
            (
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            )
        )
        date_key = _hmac(("AWS4" + self._secret_key).encode("utf-8"), date_stamp)
        region_key = _hmac(date_key, self._region)
        service_key = _hmac(region_key, "s3")
        signing_key = _hmac(service_key, "aws4_request")
        signature = hmac.new(
            signing_key,
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers["authorization"] = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self._access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return headers

    async def _request(
        self,
        method: str,
        key: str,
        *,
        content: bytes = b"",
        extra_headers: dict[str, str] | None = None,
        query_parameters: list[tuple[str, str]] | None = None,
    ) -> httpx.Response:
        url, canonical_uri = self._object_url(key)
        canonical_query = self._canonical_query(query_parameters or [])
        if canonical_query:
            url = f"{url}?{canonical_query}"
        payload_hash = hashlib.sha256(content).hexdigest() if content else _EMPTY_SHA256
        headers = self._headers(
            method=method,
            canonical_uri=canonical_uri,
            payload_hash=payload_hash,
            now=datetime.now(UTC),
            extra=extra_headers,
            canonical_query=canonical_query,
        )
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self._timeout,
        ) as client:
            try:
                return await client.request(
                    method,
                    url,
                    headers=headers,
                    content=content,
                )
            except httpx.HTTPError as exc:
                raise BlobStoreError("Object storage is unavailable") from exc

    @staticmethod
    def _raise(response: httpx.Response, operation: str) -> None:
        if response.status_code == 404:
            raise BlobNotFoundError("Object is unavailable")
        if response.status_code >= 400:
            # Provider bodies often include bucket names, internal hosts, and
            # request signatures. Keep them out of application errors/log DTOs.
            raise BlobStoreError(
                f"Object storage {operation} failed with status {response.status_code}"
            )

    async def put_file(
        self,
        key: str,
        source: Path,
        *,
        content_type: str,
    ) -> BlobStat:
        def _metadata() -> tuple[int, str]:
            digest = hashlib.sha256()
            size = 0
            with source.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
            return size, digest.hexdigest()

        try:
            size, payload_hash = await asyncio.to_thread(_metadata)
        except OSError as exc:
            raise BlobStoreError("Upload spool is unavailable") from exc
        url, canonical_uri = self._object_url(key)
        headers = self._headers(
            method="PUT",
            canonical_uri=canonical_uri,
            payload_hash=payload_hash,
            now=datetime.now(UTC),
            extra={
                "content-type": content_type,
                "content-length": str(size),
            },
        )

        async def _content() -> AsyncIterator[bytes]:
            with source.open("rb") as stream:
                while chunk := await asyncio.to_thread(stream.read, 1024 * 1024):
                    yield chunk

        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self._timeout,
        ) as client:
            try:
                response = await client.request(
                    "PUT",
                    url,
                    headers=headers,
                    content=_content(),
                )
            except (httpx.HTTPError, OSError) as exc:
                raise BlobStoreError("Object storage is unavailable") from exc
        self._raise(response, "write")
        return BlobStat(
            key=validate_blob_key(key),
            size_bytes=size,
            content_type=content_type,
        )

    async def promote(self, source_key: str, destination_key: str) -> None:
        validate_blob_key(source_key)
        response = await self._request(
            "PUT",
            destination_key,
            extra_headers={
                "x-amz-copy-source": quote(
                    f"/{self._bucket}/{source_key}", safe="/-_.~"
                )
            },
        )
        self._raise(response, "promotion")
        if b"<Error" in response.content[:4096]:
            raise BlobStoreError("Object storage promotion failed")
        # The copy is now atomically visible. A transient quarantine-delete
        # failure is safe and is handled by lifecycle cleanup; it must not make
        # the caller delete the successfully promoted private object.
        try:
            await self.delete(source_key)
        except BlobStoreError:
            pass

    async def _list_object_versions(
        self,
        key: str,
    ) -> tuple[tuple[str, bool], ...]:
        """List the first version page for exactly one opaque object key.

        S3 orders an exact key before any longer key sharing its prefix. Delete
        repeatedly consumes this first page, avoiding continuation markers
        that can become stale as their referenced versions are removed.
        """

        validated = validate_blob_key(key)
        url, canonical_uri, canonical_query = self._bucket_url(
            [
                ("max-keys", "1000"),
                ("prefix", validated),
                ("versions", ""),
            ]
        )
        headers = self._headers(
            method="GET",
            canonical_uri=canonical_uri,
            canonical_query=canonical_query,
            payload_hash=_EMPTY_SHA256,
            now=datetime.now(UTC),
        )
        content = bytearray()
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self._timeout,
        ) as client:
            try:
                async with client.stream("GET", url, headers=headers) as response:
                    self._raise(response, "version listing")
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > _MAX_VERSION_LIST_BYTES:
                            raise BlobStoreError(
                                "Object storage returned an oversized version listing"
                            )
            except BlobStoreError:
                raise
            except httpx.HTTPError as exc:
                raise BlobStoreError("Object storage is unavailable") from exc
        lowered = bytes(content[:4096]).lower()
        if b"<!doctype" in lowered or b"<!entity" in lowered:
            raise BlobStoreError("Object storage returned an unsafe version listing")
        try:
            root = ElementTree.fromstring(content)
            if root.tag.rsplit("}", 1)[-1] != "ListVersionsResult":
                raise ValueError
            if root.findtext("{*}Prefix") != validated:
                raise ValueError
            versions: list[tuple[str, bool]] = []
            for tag, delete_marker in (("Version", False), ("DeleteMarker", True)):
                for item in root.findall(f".//{{*}}{tag}"):
                    raw_key = item.findtext("{*}Key")
                    version_id = item.findtext("{*}VersionId")
                    if raw_key is None or version_id is None:
                        raise ValueError
                    listed_key = validate_blob_key(raw_key)
                    if listed_key != validated:
                        continue
                    if (
                        not version_id
                        or len(version_id) > 1024
                        or any(ord(character) < 32 for character in version_id)
                    ):
                        raise ValueError
                    versions.append((version_id, delete_marker))
            truncated = (root.findtext("{*}IsTruncated") or "false").casefold()
            if truncated not in {"true", "false"}:
                raise ValueError
            if truncated == "true" and not root.findtext("{*}NextKeyMarker"):
                raise ValueError
        except (ElementTree.ParseError, TypeError, ValueError) as exc:
            raise BlobStoreError(
                "Object storage returned an invalid version listing"
            ) from exc
        return tuple(versions)

    async def delete(self, key: str) -> BlobDeletionReceipt:
        """Permanently remove every S3 version and delete marker for ``key``.

        A normal S3 DELETE only creates a delete marker in a versioned bucket.
        This operation instead requires ListBucketVersions and
        DeleteObjectVersion, consumes all exact-key versions, then verifies an
        authoritative empty listing. Any provider or partial failure raises so
        the durable deletion outbox cannot be settled prematurely.
        """

        validated = validate_blob_key(key)
        versions_deleted = 0
        markers_deleted = 0
        previous_page: tuple[tuple[str, bool], ...] | None = None
        while True:
            versions = await self._list_object_versions(validated)
            if not versions:
                return blob_deletion_receipt(
                    backend="s3",
                    verification="version_listing_empty",
                    key=validated,
                    versions_deleted=versions_deleted,
                    delete_markers_deleted=markers_deleted,
                )
            if versions == previous_page:
                raise BlobStoreError("Object storage version deletion made no progress")
            previous_page = versions
            if (
                versions_deleted + markers_deleted + len(versions)
                > _MAX_VERSION_DELETIONS_PER_ATTEMPT
            ):
                raise BlobStoreError(
                    "Object storage version deletion exceeded its bounded pass"
                )
            for version_id, delete_marker in versions:
                response = await self._request(
                    "DELETE",
                    validated,
                    query_parameters=[("versionId", version_id)],
                )
                if response.status_code not in {200, 202, 204, 404}:
                    self._raise(response, "version delete")
                if b"<Error" in response.content[:4096]:
                    raise BlobStoreError("Object storage version delete failed")
                if delete_marker:
                    markers_deleted += 1
                else:
                    versions_deleted += 1

    async def stat(self, key: str) -> BlobStat | None:
        response = await self._request("HEAD", key)
        if response.status_code == 404:
            return None
        self._raise(response, "stat")
        raw_size = response.headers.get("content-length")
        try:
            size = int(raw_size) if raw_size is not None else -1
        except ValueError as exc:
            raise BlobStoreError("Object storage returned invalid metadata") from exc
        if size < 0:
            raise BlobStoreError("Object storage returned invalid metadata")
        return BlobStat(
            key=validate_blob_key(key),
            size_bytes=size,
            content_type=response.headers.get("content-type"),
        )

    async def list_prefix(
        self,
        prefix: str,
        *,
        limit: int = 1000,
        continuation: str | None = None,
        start_after: str | None = None,
    ) -> BlobListPage:
        validated_prefix = validate_blob_prefix(prefix)
        if not 1 <= limit <= 1000:
            raise ValueError("List limit must be between 1 and 1000")
        if continuation is not None and (not continuation or len(continuation) > 4096):
            raise ValueError("Invalid object-list continuation")
        if continuation is not None and start_after is not None:
            raise ValueError("Continuation and start-after are mutually exclusive")
        if start_after is not None:
            start_after = validate_blob_key(start_after)
            if not start_after.startswith(validated_prefix):
                raise ValueError("Start-after does not match object prefix")
        parameters = [
            ("list-type", "2"),
            ("max-keys", str(limit)),
            ("prefix", validated_prefix),
        ]
        if continuation is not None:
            parameters.append(("continuation-token", continuation))
        if start_after is not None:
            parameters.append(("start-after", start_after))
        canonical_query = "&".join(
            f"{quote(name, safe='-_.~')}={quote(value, safe='-_.~')}"
            for name, value in sorted(parameters)
        )
        base_path = self._endpoint.path.rstrip("/")
        canonical_uri = quote(f"{base_path}/{self._bucket}", safe="/-_.~")
        url = urlunsplit(
            (
                self._endpoint.scheme,
                self._endpoint.netloc,
                canonical_uri,
                canonical_query,
                "",
            )
        )
        headers = self._headers(
            method="GET",
            canonical_uri=canonical_uri,
            canonical_query=canonical_query,
            payload_hash=_EMPTY_SHA256,
            now=datetime.now(UTC),
        )
        content = bytearray()
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self._timeout,
        ) as client:
            try:
                async with client.stream("GET", url, headers=headers) as response:
                    self._raise(response, "list")
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 2 * 1024 * 1024:
                            raise BlobStoreError(
                                "Object storage returned an oversized listing"
                            )
            except BlobStoreError:
                raise
            except httpx.HTTPError as exc:
                raise BlobStoreError("Object storage is unavailable") from exc
        lowered = bytes(content[:4096]).lower()
        if b"<!doctype" in lowered or b"<!entity" in lowered:
            raise BlobStoreError("Object storage returned an unsafe listing")
        try:
            root = ElementTree.fromstring(content)
            entries: list[BlobEntry] = []
            previous_key = start_after
            for item in root.findall(".//{*}Contents"):
                raw_key = item.findtext("{*}Key")
                raw_size = item.findtext("{*}Size")
                raw_modified = item.findtext("{*}LastModified")
                if raw_key is None or raw_size is None or raw_modified is None:
                    raise ValueError
                key = validate_blob_key(raw_key)
                if not key.startswith(validated_prefix):
                    raise ValueError
                if previous_key is not None and key <= previous_key:
                    raise ValueError
                previous_key = key
                size = int(raw_size)
                modified = datetime.fromisoformat(raw_modified.replace("Z", "+00:00"))
                if size < 0 or modified.tzinfo is None or modified.utcoffset() is None:
                    raise ValueError
                entries.append(
                    BlobEntry(
                        key=key,
                        size_bytes=size,
                        modified_at=modified.astimezone(UTC),
                    )
                )
            truncated = (root.findtext("{*}IsTruncated") or "false").casefold()
            if truncated not in {"true", "false"}:
                raise ValueError
            next_token = root.findtext("{*}NextContinuationToken")
            if truncated == "true" and (
                not next_token or len(next_token) > 4096 or not entries
            ):
                raise ValueError
        except (ElementTree.ParseError, TypeError, ValueError) as exc:
            raise BlobStoreError("Object storage returned an invalid listing") from exc
        return BlobListPage(
            tuple(entries),
            continuation=next_token if truncated == "true" else None,
        )

    async def iter_bytes(
        self,
        key: str,
        *,
        chunk_size: int = 1024 * 1024,
    ) -> AsyncIterator[bytes]:
        url, canonical_uri = self._object_url(key)
        headers = self._headers(
            method="GET",
            canonical_uri=canonical_uri,
            payload_hash=_EMPTY_SHA256,
            now=datetime.now(UTC),
        )
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self._timeout,
        ) as client:
            try:
                async with client.stream("GET", url, headers=headers) as response:
                    self._raise(response, "read")
                    async for chunk in response.aiter_bytes(chunk_size):
                        if chunk:
                            yield chunk
            except BlobStoreError:
                raise
            except httpx.HTTPError as exc:
                raise BlobStoreError("Object storage is unavailable") from exc

    def public_url(self, key: str) -> str | None:
        validated = validate_blob_key(key)
        if not validated.startswith("logos/") or self._public_base_url is None:
            return None
        return f"{self._public_base_url}/{quote(validated, safe='/-_.~')}"
