"""Opt-in disposable Docker drill for the tenant recovery operator path."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from ops.recovery import hr_recovery as recovery  # noqa: E402
from ops.recovery import tenant_recovery as tenant  # noqa: E402


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_EPHEMERAL_TENANT_RECOVERY_DRILL") != "1",
    reason="Set RUN_EPHEMERAL_TENANT_RECOVERY_DRILL=1 for the Docker drill.",
)

ORG_ID = UUID("11111111-1111-1111-1111-111111111111")
OTHER_ORG_ID = UUID("22222222-2222-2222-2222-222222222222")
DATABASES = (
    "source_recovery_test",
    "target_recovery_test",
    "rollback_recovery_test",
)


def _run(command: list[str]) -> str:
    completed = subprocess.run(command, capture_output=True, check=False, text=True)
    if completed.returncode != 0:
        pytest.fail(
            f"Disposable recovery command failed ({completed.returncode}): "
            f"{completed.stderr[-1000:]}"
        )
    return completed.stdout.strip()


def _psql(container: str, database: str, sql: str) -> str:
    return _run(
        [
            "docker",
            "exec",
            "-i",
            container,
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-Atq",
            "-U",
            "postgres",
            "-d",
            database,
            "-c",
            sql,
        ]
    )


def _schema_sql() -> str:
    return """
    REVOKE CREATE ON SCHEMA public FROM PUBLIC;
    CREATE SCHEMA security AUTHORIZATION hr_recovery_owner;
    CREATE FUNCTION security.current_organization_id()
    RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
    AS $$ SELECT nullif(current_setting('app.organization_id', true), '')::uuid $$;
    ALTER FUNCTION security.current_organization_id() OWNER TO hr_recovery_owner;
    REVOKE ALL ON FUNCTION security.current_organization_id() FROM PUBLIC;
    GRANT USAGE ON SCHEMA public, security TO hr_tenant_reader;
    GRANT EXECUTE ON FUNCTION security.current_organization_id()
      TO hr_tenant_reader;

    CREATE TABLE public.alembic_version (version_num varchar(64) NOT NULL);
    INSERT INTO public.alembic_version VALUES ('fe48b7c1d902');
    CREATE TABLE public.organizations (
      id uuid PRIMARY KEY, name text NOT NULL
    );
    CREATE TABLE public.candidates (
      id uuid PRIMARY KEY, organization_id uuid NOT NULL,
      pdf_path text, private_object_id uuid
    );
    CREATE TABLE public.private_objects (
      id uuid PRIMARY KEY, organization_id uuid NOT NULL,
      object_key text NOT NULL UNIQUE, category text NOT NULL,
      sha256 char(64) NOT NULL, size_bytes integer NOT NULL,
      state text NOT NULL
    );
    CREATE TABLE public.audit_logs (
      id uuid PRIMARY KEY, organization_id uuid NOT NULL,
      actor_user_id uuid, actor_email_snapshot text, action text NOT NULL,
      resource_type text NOT NULL, resource_id uuid, path_template text,
      method text, status_code integer, request_id uuid,
      metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
      occurred_at timestamptz NOT NULL
    );
    ALTER TABLE public.candidates ADD CONSTRAINT candidates_organization_fk
      FOREIGN KEY (organization_id) REFERENCES public.organizations(id)
      ON DELETE CASCADE;
    ALTER TABLE public.private_objects ADD CONSTRAINT private_objects_organization_fk
      FOREIGN KEY (organization_id) REFERENCES public.organizations(id)
      ON DELETE CASCADE;
    ALTER TABLE public.audit_logs ADD CONSTRAINT audit_logs_organization_fk
      FOREIGN KEY (organization_id) REFERENCES public.organizations(id)
      ON DELETE CASCADE;
    ALTER TABLE public.organizations OWNER TO hr_recovery_owner;
    ALTER TABLE public.candidates OWNER TO hr_recovery_owner;
    ALTER TABLE public.private_objects OWNER TO hr_recovery_owner;
    ALTER TABLE public.audit_logs OWNER TO hr_recovery_owner;
    ALTER TABLE public.organizations ENABLE ROW LEVEL SECURITY;
    ALTER TABLE public.organizations FORCE ROW LEVEL SECURITY;
    CREATE POLICY organization_isolation ON public.organizations
      USING (id = security.current_organization_id())
      WITH CHECK (id = security.current_organization_id());
    ALTER TABLE public.candidates ENABLE ROW LEVEL SECURITY;
    ALTER TABLE public.candidates FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON public.candidates
      USING (organization_id = security.current_organization_id())
      WITH CHECK (organization_id = security.current_organization_id());
    ALTER TABLE public.private_objects ENABLE ROW LEVEL SECURITY;
    ALTER TABLE public.private_objects FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON public.private_objects
      USING (organization_id = security.current_organization_id())
      WITH CHECK (organization_id = security.current_organization_id());
    ALTER TABLE public.audit_logs ENABLE ROW LEVEL SECURITY;
    ALTER TABLE public.audit_logs FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON public.audit_logs
      USING (organization_id = security.current_organization_id())
      WITH CHECK (organization_id = security.current_organization_id());
    GRANT SELECT ON public.alembic_version, public.organizations,
      public.candidates, public.private_objects, public.audit_logs
      TO hr_tenant_reader;
    """


def _pg(environment: dict[str, str], variable: str, container: str, org: UUID):
    connection = recovery.postgres_connection_from_env(variable, environment)
    return tenant.TenantPgTools(
        recovery.CommandRunner(environ=environment),
        tenant._scoped_connection(connection, org),
        container,
    )


def test_ephemeral_live_backup_restore_and_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if subprocess.run(["docker", "version"], capture_output=True).returncode != 0:
        pytest.skip("Docker is unavailable")
    container = f"hr-tenant-recovery-{uuid4().hex[:12]}"
    password = f"test-only-{secrets.token_hex(8)}"
    _run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            container,
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "postgres:16-alpine",
        ]
    )
    try:
        for _attempt in range(100):
            logs = subprocess.run(
                ["docker", "logs", container],
                capture_output=True,
                text=True,
            )
            if (
                logs.returncode == 0
                and (logs.stdout + logs.stderr).count(
                    "database system is ready to accept connections"
                )
                >= 2
            ):
                break
            time.sleep(0.1)
        else:
            pytest.fail("Disposable PostgreSQL did not become ready")

        _psql(
            container,
            "postgres",
            "CREATE ROLE hr_recovery_owner NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION BYPASSRLS; "
            "CREATE ROLE hr_tenant_reader LOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD "
            f"'{password}';",
        )
        for database in DATABASES:
            _psql(container, "postgres", f"CREATE DATABASE {database}")
            _psql(container, database, _schema_sql())

        selected_key = (
            f"private/{ORG_ID}/candidates/33333333-3333-3333-3333-333333333333.pdf"
        )
        excluded_key = (
            f"private/{OTHER_ORG_ID}/candidates/"
            "44444444-4444-4444-4444-444444444444.pdf"
        )
        selected_bytes = b"selected tenant object"
        excluded_bytes = b"excluded tenant object"
        object_root = tmp_path / "source-objects"
        for key, value in (
            (selected_key, selected_bytes),
            (excluded_key, excluded_bytes),
        ):
            path = object_root / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        _psql(
            container,
            "source_recovery_test",
            "INSERT INTO public.organizations VALUES "
            f"('{ORG_ID}', 'Selected'), ('{OTHER_ORG_ID}', 'Excluded'); "
            "INSERT INTO public.candidates VALUES "
            f"('55555555-5555-5555-5555-555555555555', '{ORG_ID}', "
            f"'{selected_key}', '77777777-7777-7777-7777-777777777777'), "
            f"('66666666-6666-6666-6666-666666666666', '{OTHER_ORG_ID}', "
            f"'{excluded_key}', '88888888-8888-8888-8888-888888888888'); "
            "INSERT INTO public.private_objects VALUES "
            f"('77777777-7777-7777-7777-777777777777', '{ORG_ID}', "
            f"'{selected_key}', 'candidates', "
            f"'{hashlib.sha256(selected_bytes).hexdigest()}', "
            f"{len(selected_bytes)}, 'ready'), "
            f"('88888888-8888-8888-8888-888888888888', '{OTHER_ORG_ID}', "
            f"'{excluded_key}', 'candidates', "
            f"'{hashlib.sha256(excluded_bytes).hexdigest()}', "
            f"{len(excluded_bytes)}, 'ready');",
        )

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        private_key_path = tmp_path / "private.pem"
        public_key_path = tmp_path / "public.pem"
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
        environment = {
            name: os.environ[name]
            for name in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT")
            if name in os.environ
        }
        environment.update(
            {
                "HR_TENANT_BACKUP_DATABASE_URL": (
                    f"postgresql://hr_tenant_reader:{password}@localhost/"
                    "source_recovery_test"
                ),
                "HR_TENANT_RESTORE_DATABASE_URL": (
                    f"postgresql://postgres:{password}@localhost/target_recovery_test"
                ),
                "HR_ROLLBACK_DATABASE_URL": (
                    f"postgresql://postgres:{password}@localhost/rollback_recovery_test"
                ),
                "HR_RECOVERY_MANIFEST_HMAC_KEY": base64.urlsafe_b64encode(b"h" * 32)
                .decode()
                .rstrip("="),
            }
        )
        backup = tenant.tenant_backup(
            argparse.Namespace(
                database_url_env="HR_TENANT_BACKUP_DATABASE_URL",
                pg_container=container,
                organization_id=str(ORG_ID),
                confirm_organization_id=str(ORG_ID),
                operator_id="ephemeral.backup@example.test",
                authorization_ticket="CHG-ephemeral-backup",
                maintenance_window="ephemeral-drill",
                object_source="local",
                local_object_root=str(object_root),
                output_dir=str(tmp_path / "backups"),
                public_key=str(public_key_path),
                retention_days=35,
                dry_run=False,
            ),
            environment,
        )
        assert backup["dump_tenant_verified"] is True
        verified = tenant.verify_tenant_backup(
            argparse.Namespace(
                archive=backup["archive"],
                receipt=backup["receipt"],
                private_key=str(private_key_path),
            ),
            environment,
        )
        assert verified["organization_id"] == str(ORG_ID)

        target_pg = _pg(
            environment,
            "HR_TENANT_RESTORE_DATABASE_URL",
            container,
            ORG_ID,
        )
        first_lock = target_pg.restore_lock(ORG_ID, "a" * 32)
        second_lock = target_pg.restore_lock(ORG_ID, "b" * 32)
        with first_lock:
            with pytest.raises(recovery.RecoveryError, match="owns the target lock"):
                second_lock.__enter__()

        target_objects = (tmp_path / "target-objects").resolve()
        evidence = tmp_path / "tenant-restore-evidence-v1.json"
        restored = tenant.tenant_restore(
            argparse.Namespace(
                database_url_env="HR_TENANT_RESTORE_DATABASE_URL",
                pg_container=container,
                archive=backup["archive"],
                receipt=backup["receipt"],
                private_key=str(private_key_path),
                organization_id=str(ORG_ID),
                confirm_organization_id=str(ORG_ID),
                confirm_target_database="target_recovery_test",
                operator_id="ephemeral.restore@example.test",
                authorization_ticket="CHG-ephemeral-restore",
                target_object_kind="local",
                restore_local_object_root=str(target_objects),
                confirm_object_target=str(target_objects),
                evidence=str(evidence),
                dry_run=False,
            ),
            environment,
        )
        assert restored["status"] == "pass"
        assert (target_objects / selected_key).read_bytes() == selected_bytes
        assert not (target_objects / excluded_key).exists()
        signed_evidence = json.loads(evidence.read_text("utf-8"))
        recovery.verify_signed_payload(
            signed_evidence,
            recovery.read_hmac_key(environment),
        )
        target_tables = tenant.tenant_tables(target_pg)
        target_counts = tenant.tenant_inventory(target_pg, ORG_ID, target_tables)
        assert target_counts == {
            "audit_logs": 1,
            "candidates": 1,
            "organizations": 1,
            "private_objects": 1,
        }
        excluded_target_pg = _pg(
            environment,
            "HR_TENANT_RESTORE_DATABASE_URL",
            container,
            OTHER_ORG_ID,
        )
        assert not any(
            tenant.tenant_inventory(
                excluded_target_pg,
                OTHER_ORG_ID,
                target_tables,
            ).values()
        )
        selected_source_pg = _pg(
            environment,
            "HR_TENANT_BACKUP_DATABASE_URL",
            container,
            ORG_ID,
        )
        assert tenant.tenant_inventory(
            selected_source_pg,
            ORG_ID,
            target_tables,
        ) == {
            "audit_logs": 0,
            "candidates": 1,
            "organizations": 1,
            "private_objects": 1,
        }

        rollback_objects = (tmp_path / "rollback-objects").resolve()
        rollback_evidence = tmp_path / "tenant-rollback-evidence-v1.json"
        original_writer = tenant._write_json_no_clobber

        def fail_evidence(_path: Path, _payload: object, *, mode: int = 0o600):
            raise recovery.RecoveryError("synthetic evidence publication failure")

        monkeypatch.setattr(tenant, "_write_json_no_clobber", fail_evidence)
        with pytest.raises(recovery.RecoveryError, match="synthetic evidence"):
            tenant.tenant_restore(
                argparse.Namespace(
                    database_url_env="HR_ROLLBACK_DATABASE_URL",
                    pg_container=container,
                    archive=backup["archive"],
                    receipt=backup["receipt"],
                    private_key=str(private_key_path),
                    organization_id=str(ORG_ID),
                    confirm_organization_id=str(ORG_ID),
                    confirm_target_database="rollback_recovery_test",
                    operator_id="ephemeral.rollback@example.test",
                    authorization_ticket="CHG-ephemeral-rollback",
                    target_object_kind="local",
                    restore_local_object_root=str(rollback_objects),
                    confirm_object_target=str(rollback_objects),
                    evidence=str(rollback_evidence),
                    dry_run=False,
                ),
                environment,
            )
        monkeypatch.setattr(tenant, "_write_json_no_clobber", original_writer)
        rollback_pg = _pg(
            environment,
            "HR_ROLLBACK_DATABASE_URL",
            container,
            ORG_ID,
        )
        tables = tenant.tenant_tables(rollback_pg)
        tenant._target_is_empty(rollback_pg, ORG_ID, tables)
        assert not any(path.is_file() for path in rollback_objects.rglob("*"))
        assert not rollback_evidence.exists()
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container],
            capture_output=True,
            check=False,
        )
