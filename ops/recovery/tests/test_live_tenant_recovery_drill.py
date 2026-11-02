"""Opt-in encrypted tenant-only PostgreSQL and MinIO recovery drill.

The two configured databases and buckets must be disposable, independently
migrated fixtures.  The source must contain both the selected organization and
the excluded organization, while the target must not contain either UUID or
any selected object key.  Infrastructure teardown is intentionally left to
the fixture owner so the signed evidence and restored audit event remain
available for release review.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import UUID

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from ops.recovery import hr_recovery as recovery
from ops.recovery import tenant_recovery as tenant


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_TENANT_RECOVERY_DRILL") != "1",
    reason="Set RUN_TENANT_RECOVERY_DRILL=1 for the isolated tenant drill.",
)


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.fail(f"{name} is required")
    return value


def _disposable_database(raw_url: str, *, label: str) -> str:
    database = unquote(urlsplit(raw_url).path.lstrip("/"))
    if not database.endswith("_recovery_test"):
        pytest.fail(f"The {label} database must end in '_recovery_test'.")
    return database


def _pg(
    environment: dict[str, str],
    *,
    variable: str,
    organization_id: UUID,
    container: str | None,
) -> tenant.TenantPgTools:
    connection = recovery.postgres_connection_from_env(variable, environment)
    scoped = tenant._scoped_connection(connection, organization_id)
    return tenant.TenantPgTools(
        recovery.CommandRunner(environ=environment),
        scoped,
        container,
    )


def test_live_tenant_backup_restore_isolated_and_audited(tmp_path: Path) -> None:
    source_url = _required("HR_TENANT_BACKUP_DATABASE_URL")
    target_url = _required("HR_TENANT_RESTORE_DATABASE_URL")
    source_database = _disposable_database(source_url, label="source")
    target_database = _disposable_database(target_url, label="target")
    if source_database == target_database:
        pytest.fail("Tenant recovery source and target databases must differ.")

    organization_id = tenant._organization_id(
        _required("TENANT_RECOVERY_ORGANIZATION_ID")
    )
    excluded_id = tenant._organization_id(
        _required("TENANT_RECOVERY_EXCLUDED_ORGANIZATION_ID")
    )
    if organization_id == excluded_id:
        pytest.fail("Selected and excluded organizations must differ.")
    source_container = os.environ.get("TENANT_RECOVERY_SOURCE_PG_CONTAINER") or None
    target_container = os.environ.get("TENANT_RECOVERY_TARGET_PG_CONTAINER") or None
    source_bucket = _required("HR_TENANT_BACKUP_S3_BUCKET")
    target_bucket = _required("HR_TENANT_RESTORE_S3_BUCKET")
    if source_bucket == target_bucket:
        pytest.fail("Tenant recovery source and target buckets must differ.")

    environment = dict(os.environ)
    environment["HR_RECOVERY_MANIFEST_HMAC_KEY"] = base64.urlsafe_b64encode(
        os.urandom(32)
    ).decode("ascii").rstrip("=")
    source_pg = _pg(
        environment,
        variable="HR_TENANT_BACKUP_DATABASE_URL",
        organization_id=organization_id,
        container=source_container,
    )
    target_pg = _pg(
        environment,
        variable="HR_TENANT_RESTORE_DATABASE_URL",
        organization_id=organization_id,
        container=target_container,
    )
    excluded_source_pg = _pg(
        environment,
        variable="HR_TENANT_BACKUP_DATABASE_URL",
        organization_id=excluded_id,
        container=source_container,
    )
    excluded_target_pg = _pg(
        environment,
        variable="HR_TENANT_RESTORE_DATABASE_URL",
        organization_id=excluded_id,
        container=target_container,
    )
    source_tables = tenant.tenant_tables(source_pg)
    assert source_tables == tenant.tenant_tables(target_pg)
    source_before = tenant.tenant_inventory(
        source_pg,
        organization_id,
        source_tables,
    )
    excluded_source = tenant.tenant_inventory(
        excluded_source_pg,
        excluded_id,
        source_tables,
    )
    assert source_before["organizations"] == 1
    assert excluded_source["organizations"] == 1
    tenant._target_is_empty(target_pg, organization_id, source_tables)
    tenant._target_is_empty(excluded_target_pg, excluded_id, source_tables)
    assert not tenant.relationship_violations(
        source_pg,
        organization_id,
        source_tables,
    )
    excluded_objects = tenant.object_contracts(
        excluded_source_pg,
        excluded_id,
        local_root=None,
        source_kind="s3",
    )
    assert excluded_objects

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    private_key_path = tmp_path / "tenant-private.pem"
    public_key_path = tmp_path / "tenant-public.pem"
    private_key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    private_key_path.chmod(0o600)
    public_key_path.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    backup = tenant.tenant_backup(
        argparse.Namespace(
            database_url_env="HR_TENANT_BACKUP_DATABASE_URL",
            pg_container=source_container,
            organization_id=str(organization_id),
            confirm_organization_id=str(organization_id),
            operator_id="tenant-backup-drill@example.test",
            authorization_ticket="CHG-tenant-backup-drill",
            maintenance_window="automated-tenant-recovery-drill",
            object_source="s3",
            local_object_root=None,
            output_dir=str(tmp_path / "backups"),
            public_key=str(public_key_path),
            retention_days=35,
            dry_run=False,
        ),
        environment,
    )
    archive = Path(str(backup["archive"]))
    receipt = Path(str(backup["receipt"]))
    verification = tenant.verify_tenant_backup(
        argparse.Namespace(
            archive=str(archive),
            receipt=str(receipt),
            private_key=str(private_key_path),
        ),
        environment,
    )
    assert verification["status"] == "pass"
    assert verification["organization_id"] == str(organization_id)
    assert verification["row_count"] == sum(source_before.values())

    evidence_path = tmp_path / "tenant-restore-evidence.json"
    restore_arguments = argparse.Namespace(
        database_url_env="HR_TENANT_RESTORE_DATABASE_URL",
        pg_container=target_container,
        archive=str(archive),
        receipt=str(receipt),
        private_key=str(private_key_path),
        organization_id=str(organization_id),
        confirm_organization_id=str(organization_id),
        confirm_target_database=target_database,
        operator_id="tenant-restore-drill@example.test",
        authorization_ticket="CHG-tenant-restore-drill",
        target_object_kind="s3",
        restore_local_object_root=None,
        confirm_object_target=f"s3://{target_bucket}",
        evidence=str(evidence_path),
        dry_run=True,
    )
    dry_run = tenant.tenant_restore(restore_arguments, environment)
    assert dry_run["dry_run"] is True
    assert not evidence_path.exists()
    tenant._target_is_empty(target_pg, organization_id, source_tables)

    restore_arguments.dry_run = False
    restored = tenant.tenant_restore(restore_arguments, environment)
    assert restored["status"] == "pass"
    assert restored["source_mutated"] is False
    assert restored["database"]["row_counts_match"] is True
    assert restored["database"]["relationship_violations"] == []
    assert restored["objects"]["checksums_match"] is True

    assert tenant.tenant_inventory(
        source_pg,
        organization_id,
        source_tables,
    ) == source_before
    target_after = tenant.tenant_inventory(
        target_pg,
        organization_id,
        source_tables,
    )
    for table_name, source_count in source_before.items():
        expected = source_count + (1 if table_name == "audit_logs" else 0)
        assert target_after[table_name] == expected
    assert not any(
        tenant.tenant_inventory(
            excluded_target_pg,
            excluded_id,
            source_tables,
        ).values()
    )
    assert not tenant.relationship_violations(
        target_pg,
        organization_id,
        source_tables,
    )

    object_entries = tenant.object_contracts(
        source_pg,
        organization_id,
        local_root=None,
        source_kind="s3",
    )
    runner = recovery.CommandRunner(environ=environment)
    for entry in object_entries:
        versions = tenant._s3_list_exact_versions(
            bucket=target_bucket,
            key=entry.key,
            runner=runner,
            environ=environment,
        )
        assert len(versions) == 1
    for entry in excluded_objects:
        assert not tenant._s3_list_exact_versions(
            bucket=target_bucket,
            key=entry.key,
            runner=runner,
            environ=environment,
        )
    audit_count = target_pg.query(
        "SELECT count(*) FROM public.audit_logs "
        f"WHERE organization_id = '{organization_id}' "
        "AND action = 'operator.tenant_restore'"
    )
    assert audit_count == "1"
    evidence = json.loads(evidence_path.read_text("utf-8"))
    recovery.verify_signed_payload(
        evidence,
        recovery.read_hmac_key(environment),
    )
    assert evidence["status"] == "pass"
