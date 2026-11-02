"""Safe, credential-free tests for the operator recovery workflow."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import ClassVar

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from ops.recovery import hr_recovery as recovery


def _keys(tmp_path: Path) -> tuple[Path, Path]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    private_path = tmp_path / "recovery-private.pem"
    public_path = tmp_path / "recovery-public.pem"
    private_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    private_path.chmod(0o600)
    public_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return public_path, private_path


def _environment() -> dict[str, str]:
    return {
        "HR_RECOVERY_DATABASE_URL": (
            "postgresql+asyncpg://recovery_user:never-print-this-password@"
            "127.0.0.1:5432/source_database"
        ),
        "HR_RECOVERY_MANIFEST_HMAC_KEY": base64.urlsafe_b64encode(b"h" * 32)
        .decode()
        .rstrip("="),
    }


class FakePgTools:
    databases: ClassVar[set[str]] = set()
    dropped: ClassVar[list[str]] = []

    def __init__(self, _runner, connection, _container=None):
        self.connection = connection

    def query(self, sql: str, *, database: str | None = None) -> str:
        if "clock_timestamp" in sql:
            return "2026-08-22 01:02:03"
        if sql == "SELECT rolname FROM pg_catalog.pg_roles ORDER BY rolname":
            return "postgres\nhr_agent_broker\nhr_agent_runtime\nhr_agent_worker"
        if "DISTINCT owner_name" in sql:
            return "hr_agent_broker\npostgres"
        if "FROM pg_catalog.pg_roles" in sql:
            return (
                "hr_agent_broker|false|false|false|false|false|true\n"
                "hr_agent_runtime|false|false|false|false|false|false\n"
                "hr_agent_worker|false|false|false|false|false|false"
            )
        if "has_schema_privilege" in sql:
            return "hr_agent_broker|true|true|true|true"
        if "current_database" in sql:
            return database or self.connection.database
        if "count(*) FROM organizations" in sql:
            return "2"
        if "version_num" in sql:
            return "test_migration_head"
        raise AssertionError(f"Unexpected safe test query: {sql}")

    def dump(self, destination: Path) -> None:
        destination.write_bytes(b"valid custom-format PostgreSQL test dump")
        destination.chmod(0o600)

    def database_exists(self, database: str) -> bool:
        return database in self.databases

    def create_database(self, database: str) -> None:
        assert database not in self.databases
        self.databases.add(database)

    def restore(self, source: Path, database: str) -> None:
        assert source.read_bytes().startswith(b"valid custom-format")
        assert database in self.databases

    def drop_database(self, database: str) -> None:
        self.databases.discard(database)
        self.dropped.append(database)


def _create_test_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, dict[str, str]]:
    public_key, private_key = _keys(tmp_path)
    objects = tmp_path / "source-objects"
    (objects / "candidates").mkdir(parents=True)
    (objects / "candidates" / "resume.pdf").write_bytes(b"private resume bytes")
    output = tmp_path / "encrypted-backups"
    environment = _environment()
    monkeypatch.setattr(recovery, "PgTools", FakePgTools)
    result = recovery.backup(
        argparse.Namespace(
            database_url_env="HR_RECOVERY_DATABASE_URL",
            pg_container=None,
            object_source="local",
            local_object_root=str(objects),
            output_dir=str(output),
            public_key=str(public_key),
            maintenance_window="test-read-only-window",
            retention_days=35,
            dry_run=False,
        ),
        environment,
    )
    return (
        Path(str(result["archive"])),
        Path(str(result["receipt"])),
        private_key,
        environment,
    )


def test_database_credentials_are_environment_only_and_plan_is_sanitized(tmp_path):
    environment = _environment()
    connection = recovery.postgres_connection_from_env(
        "HR_RECOVERY_DATABASE_URL", environment
    )
    assert connection.database == "source_database"
    assert connection.environment["PGPASSWORD"] == "never-print-this-password"
    assert "never-print-this-password" not in connection.safe_label

    object_root = tmp_path / "objects"
    output_root = tmp_path / "output"
    result = recovery.backup(
        argparse.Namespace(
            database_url_env="HR_RECOVERY_DATABASE_URL",
            pg_container=None,
            object_source="local",
            local_object_root=str(object_root),
            output_dir=str(output_root),
            public_key=str(tmp_path / "public.pem"),
            maintenance_window="dry-run-window",
            retention_days=35,
            dry_run=True,
        ),
        environment,
    )
    assert result["dry_run"] is True
    assert "never-print-this-password" not in json.dumps(result)
    assert not output_root.exists()


@pytest.mark.parametrize(
    "name",
    ["postgres", "production", "template1", "UPPER_restore", "../restore"],
)
def test_restore_database_target_is_fail_closed(name):
    with pytest.raises(recovery.RecoveryError):
        recovery.validate_isolated_name(name, label="target database")
    assert (
        recovery.validate_isolated_name(
            "pilot_restore_20260822", label="target database"
        )
        == "pilot_restore_20260822"
    )


def test_encrypted_backup_verifies_and_tampering_fails(tmp_path, monkeypatch):
    archive, receipt, private_key, environment = _create_test_backup(
        tmp_path, monkeypatch
    )
    verified = recovery.verify_backup(
        argparse.Namespace(
            archive=str(archive),
            receipt=str(receipt),
            private_key=str(private_key),
        ),
        environment,
    )
    assert verified["status"] == "pass"
    assert verified["object_count"] == 1

    payload = bytearray(archive.read_bytes())
    payload[len(payload) // 2] ^= 1
    archive.write_bytes(payload)
    with pytest.raises(recovery.RecoveryError, match="checksum"):
        recovery.verify_backup(
            argparse.Namespace(
                archive=str(archive),
                receipt=str(receipt),
                private_key=str(private_key),
            ),
            environment,
        )


def test_isolated_restore_drill_verifies_and_rolls_back(tmp_path, monkeypatch):
    FakePgTools.databases = set()
    FakePgTools.dropped = []
    archive, receipt, private_key, environment = _create_test_backup(
        tmp_path, monkeypatch
    )
    target_objects = tmp_path / "pilot-object-drill"
    evidence = tmp_path / "evidence" / "restore-drill.json"
    result = recovery.restore(
        argparse.Namespace(
            database_url_env="HR_RECOVERY_DATABASE_URL",
            pg_container=None,
            archive=str(archive),
            receipt=str(receipt),
            private_key=str(private_key),
            target_database="pilot_restore_drill",
            confirm_restore_to="pilot_restore_drill",
            target_object_kind="local",
            restore_local_object_root=str(target_objects),
            confirm_object_target=str(target_objects.resolve()),
            evidence=str(evidence),
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
    assert result["rto_seconds"] <= result["rto_target_seconds"]
    assert "pilot_restore_drill" in FakePgTools.dropped
    assert "pilot_restore_drill" not in FakePgTools.databases
    assert not target_objects.exists()
    stored = json.loads(evidence.read_text())
    assert stored["status"] == "pass"
    assert stored["database"]["migration_head"] == "test_migration_head"
    assert stored["database"]["candidate_rls_forced"] is True
    assert stored["database"]["required_roles_match"] is True


def test_local_restore_accepts_operator_reserved_empty_directory(tmp_path):
    source = tmp_path / "source-objects"
    source.mkdir()
    (source / "candidate.bin").write_bytes(b"preserved candidate object")
    target = tmp_path / "reserved-object-restore"
    target.mkdir(mode=0o700)

    assert recovery.validate_restore_directory(target) == target.resolve()
    recovery._copy_objects_to_local(source, target)

    assert (target / "candidate.bin").read_bytes() == b"preserved candidate object"
    assert target.stat().st_mode & 0o777 == 0o700
    assert (target / "candidate.bin").stat().st_mode & 0o777 == 0o600


def test_unsafe_tar_entries_are_rejected(tmp_path):
    import io
    import tarfile

    archive = tmp_path / "unsafe.tar"
    with tarfile.open(archive, "w") as bundle:
        entry = tarfile.TarInfo("../../escape")
        body = b"escape"
        entry.size = len(body)
        bundle.addfile(entry, io.BytesIO(body))
    with pytest.raises(recovery.RecoveryError, match="unsafe entry"):
        recovery._safe_extract_tar(archive, tmp_path / "extracted")
    assert not (tmp_path.parent / "escape").exists()


def test_private_key_must_not_be_group_or_world_readable(tmp_path):
    _public, private = _keys(tmp_path)
    private.chmod(0o644)
    with pytest.raises(recovery.RecoveryError, match="permissions"):
        recovery._load_rsa_private_key(private, {})


def test_failed_commands_redact_secret_environment_values(tmp_path):
    executable = tmp_path / "failing-tool"
    executable.write_text(
        "#!/bin/sh\nprintf '%s' \"$PGPASSWORD\" >&2\nexit 7\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    runner = recovery.CommandRunner(environ={})
    with pytest.raises(recovery.RecoveryError) as failed:
        runner.run(
            [str(executable)],
            environment={"PGPASSWORD": "never-leak-this-value"},
            label="Synthetic command",
        )
    assert "never-leak-this-value" not in str(failed.value)
    assert "[redacted]" in str(failed.value)


def test_partial_local_object_restore_is_rolled_back(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "objects-restore-drill"

    def fail_after_partial_copy(_source: Path, destination: Path) -> None:
        destination.mkdir()
        (destination / "partial.bin").write_bytes(b"partial")
        raise recovery.RecoveryError("simulated copy failure")

    monkeypatch.setattr(recovery, "_copy_objects_to_local", fail_after_partial_copy)
    with pytest.raises(recovery.RecoveryError, match="simulated copy failure"):
        recovery._restore_objects(
            source=source,
            target_kind="local",
            target_local_root=str(target),
            confirm_object_target=str(target.resolve()),
            runner=recovery.CommandRunner(environ={}),
            environ={},
        )
    assert not target.exists()


def test_empty_s3_restore_prefix_uses_portable_s3api_inspection(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "resume.pdf").write_bytes(b"private resume bytes")

    class S3Runner:
        def __init__(self) -> None:
            self.commands: list[list[str]] = []
            self.version_id = "owned-version-1"

        def run(self, command, **_kwargs):
            self.commands.append(command)
            if command[1:3] == ["s3api", "list-objects-v2"]:
                return '{"Contents": []}'
            if command[1:3] == ["s3api", "get-bucket-versioning"]:
                return '{"Status": "Enabled"}'
            if command[1:3] == ["s3api", "put-object"]:
                return json.dumps({"VersionId": self.version_id})
            if command[1:3] == ["s3", "sync"] and str(source) not in command:
                destination = Path(command[4])
                destination.mkdir(parents=True, exist_ok=True)
                (destination / "resume.pdf").write_bytes(b"private resume bytes")
            return ""

    runner = S3Runner()
    environment = {
        "HR_RECOVERY_RESTORE_S3_BUCKET": "private-recovery",
        "HR_RECOVERY_RESTORE_S3_PREFIX": "drill/test-run",
        "HR_RECOVERY_S3_ENDPOINT": "http://127.0.0.1:9100",
    }
    target, entries, ownership = recovery._restore_objects(
        source=source,
        target_kind="s3",
        target_local_root=None,
        confirm_object_target="s3://private-recovery/drill/test-run",
        runner=runner,
        environ=environment,
    )

    assert target == "s3://private-recovery/drill/test-run"
    assert entries[0]["key"] == "resume.pdf"
    assert ownership is not None
    assert ownership.bucket == "private-recovery"
    inspection = runner.commands[0]
    assert inspection[:3] == ["aws", "s3api", "list-objects-v2"]
    assert inspection[inspection.index("--prefix") + 1] == "drill/test-run/"
    assert not any("--recursive" in command for command in runner.commands)


def test_nonempty_s3_restore_prefix_is_rejected_before_sync(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    class S3Runner:
        def __init__(self) -> None:
            self.commands: list[list[str]] = []

        def run(self, command, **_kwargs):
            self.commands.append(command)
            return '{"Contents": [{"Key": "drill/test-run/existing"}]}'

    runner = S3Runner()
    environment = {
        "HR_RECOVERY_RESTORE_S3_BUCKET": "private-recovery",
        "HR_RECOVERY_RESTORE_S3_PREFIX": "drill/test-run",
    }
    with pytest.raises(recovery.RecoveryError, match="must be empty"):
        recovery._restore_objects(
            source=source,
            target_kind="s3",
            target_local_root=None,
            confirm_object_target="s3://private-recovery/drill/test-run",
            runner=runner,
            environ=environment,
        )

    assert len(runner.commands) == 1


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://operator:private-endpoint-token@storage.example.test",
        "https://storage.example.test/path",
        "https://storage.example.test?token=private-endpoint-token",
        "https://storage.example.test#private-endpoint-token",
    ],
)
def test_s3_endpoint_rejects_secret_bearing_or_non_origin_urls(endpoint):
    with pytest.raises(recovery.RecoveryError) as failed:
        recovery._aws_endpoint_arguments({"HR_RECOVERY_S3_ENDPOINT": endpoint})
    assert "private-endpoint-token" not in str(failed.value)


def test_failed_commands_redact_generic_url_userinfo(tmp_path):
    executable = tmp_path / "echo-failing-argument"
    executable.write_text(
        "#!/bin/sh\nprintf '%s' \"$1\" >&2\nexit 9\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    runner = recovery.CommandRunner(environ={})
    secret = "private-endpoint-token"
    with pytest.raises(recovery.RecoveryError) as failed:
        runner.run(
            [executable, f"https://operator:{secret}@storage.example.test"],
            label="Synthetic endpoint command",
        )
    assert secret not in str(failed.value)
    assert "[redacted]" in str(failed.value)


def test_failed_s3_restore_deletes_only_owned_versions(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "resume.pdf").write_bytes(b"private resume bytes")

    class ConcurrentS3Runner:
        def __init__(self) -> None:
            self.commands: list[list[str]] = []
            self.versions: dict[str, dict[str, str]] = {}

        @staticmethod
        def _value(command, flag):
            return command[command.index(flag) + 1]

        def run(self, command, **_kwargs):
            command = list(command)
            self.commands.append(command)
            operation = command[1:3]
            if operation == ["s3api", "list-objects-v2"]:
                return '{"Contents": []}'
            if operation == ["s3api", "get-bucket-versioning"]:
                return '{"Status": "Enabled"}'
            if operation == ["s3api", "put-object"]:
                metadata = self._value(command, "--metadata")
                marker = metadata.split(",", 1)[0].split("=", 1)[1]
                self.versions["owned-version"] = {
                    "key": self._value(command, "--key"),
                    "operation": marker,
                }
                return '{"VersionId": "owned-version"}'
            if operation == ["s", "sync"]:
                raise AssertionError("unreachable command spelling")
            if operation == ["s3", "sync"]:
                self.versions["concurrent-version"] = {
                    "key": "drill/test-run/concurrent-writer.bin",
                    "operation": "another-writer",
                }
                raise recovery.RecoveryError("simulated verification failure")
            if operation == ["s3api", "list-object-versions"]:
                return json.dumps(
                    {
                        "Versions": [
                            {"Key": row["key"], "VersionId": version_id}
                            for version_id, row in self.versions.items()
                        ]
                    }
                )
            if operation == ["s3api", "head-object"]:
                version_id = self._value(command, "--version-id")
                return json.dumps(
                    {
                        "Metadata": {
                            "hr-recovery-operation": self.versions[version_id][
                                "operation"
                            ]
                        }
                    }
                )
            if operation == ["s3api", "delete-object"]:
                version_id = self._value(command, "--version-id")
                self.versions.pop(version_id)
                return "{}"
            raise AssertionError(f"Unexpected command: {command}")

    runner = ConcurrentS3Runner()
    environment = {
        "HR_RECOVERY_RESTORE_S3_BUCKET": "private-recovery",
        "HR_RECOVERY_RESTORE_S3_PREFIX": "drill/test-run",
    }
    with pytest.raises(recovery.RecoveryError, match="simulated verification failure"):
        recovery._restore_objects(
            source=source,
            target_kind="s3",
            target_local_root=None,
            confirm_object_target="s3://private-recovery/drill/test-run",
            runner=runner,
            environ=environment,
        )

    assert runner.versions == {
        "concurrent-version": {
            "key": "drill/test-run/concurrent-writer.bin",
            "operation": "another-writer",
        }
    }
    assert not any("--recursive" in command for command in runner.commands)
    delete_commands = [
        command
        for command in runner.commands
        if command[1:3] == ["s3api", "delete-object"]
    ]
    assert len(delete_commands) == 1
    assert "owned-version" in delete_commands[0]
