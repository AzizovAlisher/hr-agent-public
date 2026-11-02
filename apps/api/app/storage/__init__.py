"""Private/public object storage factory."""

from app.config import get_settings
from app.storage.base import (
    BlobDeletionReceipt,
    BlobEntry,
    BlobListPage,
    BlobNotFoundError,
    BlobStat,
    BlobStore,
    BlobStoreError,
    private_object_key,
    public_branding_key,
    quarantine_object_key,
    require_tenant_private_key,
    validate_blob_key,
    validate_blob_prefix,
)
from app.storage.legacy import (
    LegacyPrivateObjectError,
    LegacyStorageUnavailableError,
    UnsafeLegacyPrivateObjectError,
    enqueue_legacy_private_object_deletion,
    get_legacy_blob_store,
    settle_legacy_private_object_deletion,
)
from app.storage.local import LocalBlobStore
from app.storage.public import PublicBrandingRedirectApp
from app.storage.reconciliation import (
    ReconciliationMetrics,
    reconcile_private_storage,
)
from app.storage.registry import (
    DeletionSettlement,
    PrivateObjectRegistryError,
    RegisteredBlob,
    StalePrivateObjectDeletionClaim,
    adopt_legacy_private_object_for_erasure,
    enqueue_private_object_deletion,
    register_migrated_ready_private_object,
    register_ready_private_object,
    registered_resource_blob,
    settle_private_object_deletion,
)
from app.storage.s3 import S3BlobStore
from app.storage.scanner import (
    ClamAvScanner,
    FakeMalwareScanner,
    MalwareDetectedError,
    MalwareScanner,
    MalwareScannerUnavailableError,
    ScanResult,
    SignatureScanner,
)


def get_blob_store() -> BlobStore:
    settings = get_settings()
    if settings.blob_store_backend == "local":
        return LocalBlobStore(settings.storage_dir)
    return S3BlobStore(
        endpoint=settings.s3_endpoint or "",
        bucket=settings.s3_bucket or "",
        access_key=settings.s3_access_key or "",
        secret_key=(
            settings.s3_secret_key.get_secret_value()
            if settings.s3_secret_key is not None
            else ""
        ),
        region=settings.s3_region,
        timeout_seconds=settings.s3_timeout_seconds,
    )


def get_public_blob_store() -> BlobStore:
    """Return the public-branding store, physically isolated in production."""

    settings = get_settings()
    if settings.blob_store_backend == "local":
        # Local development retains one owner-only tree. Only the narrowly
        # mounted ``logos/`` directory is anonymously addressable.
        return LocalBlobStore(settings.storage_dir)
    return S3BlobStore(
        endpoint=settings.s3_endpoint or "",
        bucket=settings.s3_public_bucket or "",
        access_key=settings.s3_access_key or "",
        secret_key=(
            settings.s3_secret_key.get_secret_value()
            if settings.s3_secret_key is not None
            else ""
        ),
        region=settings.s3_region,
        public_base_url=settings.s3_public_base_url,
        timeout_seconds=settings.s3_timeout_seconds,
    )


def get_malware_scanner() -> MalwareScanner:
    settings = get_settings()
    if settings.malware_scanner_backend == "clamav":
        return ClamAvScanner(
            host=settings.clamav_host,
            port=settings.clamav_port,
            timeout_seconds=settings.clamav_timeout_seconds,
        )
    return SignatureScanner()


__all__ = [
    "BlobDeletionReceipt",
    "BlobEntry",
    "BlobListPage",
    "BlobNotFoundError",
    "BlobStat",
    "BlobStore",
    "BlobStoreError",
    "ClamAvScanner",
    "DeletionSettlement",
    "FakeMalwareScanner",
    "LegacyPrivateObjectError",
    "LegacyStorageUnavailableError",
    "LocalBlobStore",
    "MalwareDetectedError",
    "MalwareScanner",
    "MalwareScannerUnavailableError",
    "PrivateObjectRegistryError",
    "PublicBrandingRedirectApp",
    "ReconciliationMetrics",
    "RegisteredBlob",
    "S3BlobStore",
    "ScanResult",
    "SignatureScanner",
    "StalePrivateObjectDeletionClaim",
    "UnsafeLegacyPrivateObjectError",
    "adopt_legacy_private_object_for_erasure",
    "enqueue_legacy_private_object_deletion",
    "enqueue_private_object_deletion",
    "get_blob_store",
    "get_public_blob_store",
    "get_legacy_blob_store",
    "get_malware_scanner",
    "private_object_key",
    "public_branding_key",
    "quarantine_object_key",
    "reconcile_private_storage",
    "register_migrated_ready_private_object",
    "register_ready_private_object",
    "registered_resource_blob",
    "require_tenant_private_key",
    "settle_legacy_private_object_deletion",
    "settle_private_object_deletion",
    "validate_blob_key",
    "validate_blob_prefix",
]
