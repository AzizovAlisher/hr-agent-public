"""Opt-in full PostgreSQL and private-object recovery release drill."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from ops.recovery import hr_recovery as recovery


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_RECOVERY_DRILL") != "1",
    reason="Set RUN_RECOVERY_DRILL=1 for the destructive isolated release drill.",
)


def _source_database_name(raw_url: str) -> str:
    parsed = urlsplit(raw_url)
    return unquote(parsed.path.lstrip("/"))


def test_live_encrypted_backup_restore_and_rollback(tmp_path: Path) -> None:
    database_url = os.environ.get("HR_RECOVERY_DATABASE_URL", "")
    if not database_url:
        pytest.fail("HR_RECOVERY_DATABASE_URL is required")
    source_database = _source_database_name(database_url)
    if not source_database.endswith("_recovery_test"):
        pytest.fail(
            "The live recovery drill source database must end in '_recovery_test'."
        )
    postgres_container = os.environ.get("RECOVERY_PG_CONTAINER") or None

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    private_key_path = tmp_path / "recovery-private.pem"
    public_key_path = tmp_path / "recovery-public.pem"
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

    source_objects = tmp_path / "source-private-objects"
    source_objects.mkdir(mode=0o700)
    object_bytes = b"recovery-drill-private-object-canary"
    (source_objects / "canary.bin").write_bytes(object_bytes)
    backup_directory = tmp_path / "encrypted-backups"
    environment = dict(os.environ)
    environment["HR_RECOVERY_MANIFEST_HMAC_KEY"] = (
        base64.urlsafe_b64encode(b"ci-recovery-hmac-key-material-32b").decode().rstrip("=")
    )

    backup_result = recovery.backup(
        argparse.Namespace(
            database_url_env="HR_RECOVERY_DATABASE_URL",
            pg_container=postgres_container,
            object_source="local",
            local_object_root=str(source_objects),
            output_dir=str(backup_directory),
            public_key=str(public_key_path),
            maintenance_window="automated-release-drill",
            retention_days=35,
            dry_run=False,
        ),
        environment,
    )
    archive = Path(str(backup_result["archive"]))
    receipt = Path(str(backup_result["receipt"]))
    verification = recovery.verify_backup(
        argparse.Namespace(
            archive=str(archive),
            receipt=str(receipt),
            private_key=str(private_key_path),
        ),
        environment,
    )
    assert verification["status"] == "pass"
    assert verification["object_count"] == 1

    target_database = f"hr_ci_restore_drill_{uuid4().hex[:8]}"
    target_objects = tmp_path / f"objects-restore-drill-{uuid4().hex[:8]}"
    configured_evidence = os.environ.get("RECOVERY_EVIDENCE_PATH")
    evidence_path = (
        Path(configured_evidence).expanduser().resolve()
        if configured_evidence
        else tmp_path / "recovery-evidence.json"
    )
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    result = recovery.restore(
        argparse.Namespace(
            database_url_env="HR_RECOVERY_DATABASE_URL",
            pg_container=postgres_container,
            archive=str(archive),
            receipt=str(receipt),
            private_key=str(private_key_path),
            target_database=target_database,
            confirm_restore_to=target_database,
            target_object_kind="local",
            restore_local_object_root=str(target_objects),
            confirm_object_target=str(target_objects.resolve()),
            evidence=str(evidence_path),
            max_rpo_seconds=24 * 60 * 60,
            max_rto_seconds=4 * 60 * 60,
            cleanup_after_verify=True,
            dry_run=False,
        ),
        environment,
    )

    assert result["status"] == "pass"
    assert result["rollback_verified"] is True
    assert result["objects"]["checksums_match"] is True
    assert result["database"]["candidate_rls_forced"] is True
    assert result["database"]["required_roles_match"] is True
    assert result["rpo_seconds"] <= 24 * 60 * 60
    assert result["rto_seconds"] <= 4 * 60 * 60
    assert not target_objects.exists()

    stored = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert stored["status"] == "pass"
    assert stored["rollback_verified"] is True
    assert stored["objects"]["count"] == 1
