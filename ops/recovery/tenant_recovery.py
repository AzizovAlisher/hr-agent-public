"""Encrypted, tenant-scoped PostgreSQL and private-object recovery.

This operator command deliberately restores the source organization UUID. It
does not attempt unsafe generic UUID rewriting. A target is eligible only when
that organization UUID is absent from the organization row and every tenant
table. Other organizations may remain in the target database and bucket.

Backup reads must use a SELECT-only LOGIN role that cannot inherit table-owner,
superuser, or BYPASSRLS rights. The source connection is pinned to
``app.organization_id``; inventory, object references, and pg_dump share one
exported repeatable-read snapshot, and the rendered dump is inspected row by
row. Restore requires the authenticated archive, database credentials,
explicit database/organization/object confirmations, and a named
operator/change ticket. A live database/organization advisory lock and
operation ownership sentinels protect restore rollback. Versioned evidence is
HMAC authenticated, atomically published without overwrite, and a successful
target receives an operator audit event.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from ops.recovery import hr_recovery as recovery

TENANT_FORMAT_VERSION = "hr-agent-tenant-recovery-v2"
TENANT_RECEIPT_VERSION = "hr-agent-tenant-recovery-receipt-v2"
TENANT_EVIDENCE_VERSION = "hr-agent-tenant-recovery-evidence-v2"
MAX_RETENTION_DAYS = 35
IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
OPERATOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._:-]{2,127}$")
LEGACY_OBJECT_COLUMNS = (
    ("candidates", "pdf_path", "private_object_id", "candidates"),
    (
        "assessment_files",
        "storage_path",
        "private_object_id",
        "assessments",
    ),
    ("task_submissions", "file_path", "private_object_id", "tasks"),
    ("interview_recordings", "media_path", "private_object_id", "recordings"),
)
LEGACY_PREFIXES = {
    "candidates": ("cvs",),
    "assessments": ("assessment_files",),
    "tasks": ("task_files",),
    "recordings": ("recordings",),
}
SNAPSHOT_ID = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{8}-[0-9]+$")
RESTORE_OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class ObjectContract:
    key: str
    kind: str
    category: str
    expected_sha256: str | None
    expected_bytes: int | None
    database_reference: str


@dataclass(frozen=True, slots=True)
class RestoredS3Version:
    key: str
    version_id: str
    operation_id: str


@dataclass(frozen=True, slots=True)
class RestoredLocalObject:
    path: Path
    device: int
    inode: int
    sha256: str | None
    bytes: int | None


class _PersistentPsql:
    """One live psql backend used for exported snapshots or advisory locks."""

    def __init__(self, pg: "TenantPgTools", *, label: str) -> None:
        self.pg = pg
        self.label = label
        self.process: subprocess.Popen[bytes] | None = None
        self.stderr: object | None = None

    def __enter__(self) -> "_PersistentPsql":
        environment = dict(self.pg.runner.base_environment)
        environment.update(self.pg.connection.environment)
        self.stderr = tempfile.TemporaryFile(mode="w+b")
        try:
            self.process = subprocess.Popen(
                self.pg._command(
                    "psql",
                    "-X",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-Atq",
                    "-P",
                    "pager=off",
                    "-f",
                    "-",
                ),
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.stderr,
            )
        except FileNotFoundError as exc:
            self._close_stderr()
            raise recovery.RecoveryError(f"{self.label} tool is not installed") from exc
        return self

    def _detail(self) -> str:
        handle = self.stderr
        if handle is None or not hasattr(handle, "seek"):
            return ""
        handle.seek(0)  # type: ignore[union-attr]
        raw = handle.read(600)  # type: ignore[union-attr]
        detail = raw.decode("utf-8", errors="replace").strip()
        for name, value in self.pg.connection.environment.items():
            if value and any(
                marker in name.upper()
                for marker in ("PASSWORD", "SECRET", "TOKEN", "PRIVATE_KEY")
            ):
                detail = detail.replace(value, "[redacted]")
        return detail

    def query_scalar(self, sql: str) -> str:
        process = self.process
        if (
            process is None
            or process.poll() is not None
            or process.stdin is None
            or process.stdout is None
        ):
            raise recovery.RecoveryError(f"{self.label} session is not active")
        marker = f"hr_recovery_{secrets.token_hex(16)}"
        try:
            process.stdin.write((sql.rstrip().rstrip(";") + ";\n").encode("utf-8"))
            process.stdin.write(
                ("SELECT " + _sql_literal(marker) + ";\n").encode("utf-8")
            )
            process.stdin.flush()
            values: list[str] = []
            while True:
                line = process.stdout.readline()
                if not line:
                    detail = self._detail()
                    suffix = f": {detail}" if detail else ""
                    raise recovery.RecoveryError(f"{self.label} failed{suffix}")
                value = line.decode("utf-8", errors="replace").rstrip("\r\n")
                if value == marker:
                    break
                values.append(value)
        except (BrokenPipeError, OSError) as exc:
            raise recovery.RecoveryError(f"{self.label} session failed") from exc
        if len(values) != 1:
            raise recovery.RecoveryError(f"{self.label} returned invalid output")
        return values[0]

    def is_active(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _close_stderr(self) -> None:
        handle = self.stderr
        self.stderr = None
        if handle is not None and hasattr(handle, "close"):
            handle.close()  # type: ignore[union-attr]

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        process = self.process
        self.process = None
        if process is not None:
            if process.poll() is None and process.stdin is not None:
                with contextlib.suppress(BrokenPipeError, OSError):
                    process.stdin.write(b"ROLLBACK;\n\\q\n")
                    process.stdin.flush()
                    process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
        self._close_stderr()


class ExportedTenantSnapshot:
    """Keeps the exporting repeatable-read transaction alive."""

    def __init__(self, pg: "TenantPgTools") -> None:
        self.pg = pg
        self.session: _PersistentPsql | None = None
        self.snapshot_id: str | None = None

    def __enter__(self) -> str:
        session = _PersistentPsql(
            self.pg,
            label="Tenant PostgreSQL exported snapshot",
        )
        session.__enter__()
        try:
            snapshot_id = session.query_scalar(
                "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY; "
                "SELECT pg_catalog.pg_export_snapshot()"
            )
            if not SNAPSHOT_ID.fullmatch(snapshot_id):
                raise recovery.RecoveryError(
                    "PostgreSQL exported snapshot ID is invalid"
                )
        except Exception:
            session.__exit__(None, None, None)
            raise
        self.session = session
        self.snapshot_id = snapshot_id
        return snapshot_id

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        session = self.session
        self.session = None
        self.snapshot_id = None
        if session is not None:
            session.__exit__(exc_type, exc, traceback)


class TenantRestoreLock:
    """Per-database/per-organization advisory lock with an owner sentinel."""

    def __init__(
        self,
        pg: "TenantPgTools",
        organization_id: UUID,
        operation_id: str,
    ) -> None:
        if not RESTORE_OPERATION_ID.fullmatch(operation_id):
            raise recovery.RecoveryError("Tenant restore operation ID is invalid")
        self.pg = pg
        self.organization_id = organization_id
        self.operation_id = operation_id
        self.session: _PersistentPsql | None = None

    def __enter__(self) -> "TenantRestoreLock":
        session = _PersistentPsql(self.pg, label="Tenant restore advisory lock")
        session.__enter__()
        lock_name = (
            "hr-agent-tenant-restore:"
            + self.pg.connection.database
            + ":"
            + str(self.organization_id)
        )
        try:
            locked = session.query_scalar(
                "SELECT pg_catalog.pg_try_advisory_lock("
                "pg_catalog.hashtextextended("
                f"{_sql_literal(lock_name)}, 0))::text"
            )
            if locked != "true":
                raise recovery.RecoveryError(
                    "Another tenant restore operation owns the target lock"
                )
            sentinel = session.query_scalar(
                "SELECT pg_catalog.set_config("
                "'hr_agent.tenant_restore_operation_id', "
                f"{_sql_literal(self.operation_id)}, false)"
            )
            if sentinel != self.operation_id:
                raise recovery.RecoveryError(
                    "Tenant restore ownership sentinel could not be established"
                )
        except Exception:
            session.__exit__(None, None, None)
            raise
        self.session = session
        return self

    def assert_owned(self, operation_id: str) -> None:
        if operation_id != self.operation_id:
            raise recovery.RecoveryError("Tenant restore operation ownership changed")
        session = self.session
        if session is None or not session.is_active():
            raise recovery.RecoveryError("Tenant restore operation lock was lost")

    def delete_owned_tenant(self, organization_id: UUID) -> None:
        self.assert_owned(self.operation_id)
        if organization_id != self.organization_id:
            raise recovery.RecoveryError("Tenant restore rollback target changed")
        assert self.session is not None
        sentinel = self.session.query_scalar(
            "SELECT COALESCE(current_setting("
            "'hr_agent.tenant_restore_operation_id', true), '')"
        )
        if sentinel != self.operation_id:
            raise recovery.RecoveryError("Tenant restore ownership sentinel was lost")
        deleted = self.session.query_scalar(
            "WITH deleted AS (DELETE FROM public.organizations "
            f"WHERE id = '{organization_id}' RETURNING 1) "
            "SELECT count(*)::text FROM deleted"
        )
        if deleted not in {"0", "1"}:
            raise recovery.RecoveryError("Tenant restore rollback result is invalid")

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        session = self.session
        self.session = None
        if session is not None:
            session.__exit__(exc_type, exc, traceback)


class TenantPgTools:
    """PostgreSQL command boundary with PGOPTIONS propagated into Docker."""

    def __init__(
        self,
        runner: recovery.CommandRunner,
        connection: recovery.PostgresConnection,
        container: str | None = None,
        snapshot_id: str | None = None,
    ) -> None:
        if snapshot_id is not None and not SNAPSHOT_ID.fullmatch(snapshot_id):
            raise recovery.RecoveryError("PostgreSQL exported snapshot ID is invalid")
        self.runner = runner
        self.connection = connection
        self.container = container
        self.snapshot_id = snapshot_id

    def _command(self, executable: str, *arguments: str) -> list[str]:
        if self.container is None:
            return [executable, *arguments]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.container):
            raise recovery.RecoveryError("PostgreSQL container name is invalid")
        names = (
            "PGDATABASE",
            "PGHOST",
            "PGPORT",
            "PGUSER",
            "PGPASSWORD",
            "PGSSLMODE",
            "PGAPPNAME",
            "PGCONNECT_TIMEOUT",
            "PGOPTIONS",
        )
        propagated = [
            item
            for name in names
            if name in self.connection.environment
            for item in ("-e", name)
        ]
        return [
            "docker",
            "exec",
            "-i",
            *propagated,
            self.container,
            executable,
            *arguments,
        ]

    def query(self, sql: str) -> str:
        if self.snapshot_id is not None:
            sql = (
                "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY; "
                f"SET TRANSACTION SNAPSHOT {_sql_literal(self.snapshot_id)}; "
                + sql.rstrip().rstrip(";")
                + "; COMMIT"
            )
        with tempfile.TemporaryFile(mode="w+b") as input_handle:
            input_handle.write((sql + "\n").encode("utf-8"))
            input_handle.seek(0)
            result = self.runner.run(
                self._command(
                    "psql",
                    "-X",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-Atq",
                    "-f",
                    "-",
                ),
                environment=self.connection.environment,
                stdin=input_handle,
                label="Tenant PostgreSQL query",
            )
        return result.decode("utf-8", errors="replace").strip()

    def at_snapshot(self, snapshot_id: str) -> "TenantPgTools":
        return TenantPgTools(
            self.runner,
            self.connection,
            self.container,
            snapshot_id,
        )

    def exported_snapshot(self) -> ExportedTenantSnapshot:
        if self.snapshot_id is not None:
            raise recovery.RecoveryError("Cannot export a snapshot from a snapshot")
        return ExportedTenantSnapshot(self)

    def restore_lock(
        self,
        organization_id: UUID,
        operation_id: str,
    ) -> TenantRestoreLock:
        if self.snapshot_id is not None:
            raise recovery.RecoveryError("Cannot restore through a read-only snapshot")
        return TenantRestoreLock(self, organization_id, operation_id)

    def dump_tenant(self, destination: Path, tables: Sequence[str]) -> None:
        if self.snapshot_id is None:
            raise recovery.RecoveryError(
                "Tenant PostgreSQL backup requires one exported snapshot"
            )
        arguments = [
            "--format=custom",
            "--compress=9",
            "--data-only",
            "--column-inserts",
            "--rows-per-insert=1",
            "--enable-row-security",
            "--no-owner",
            "--no-privileges",
            f"--snapshot={self.snapshot_id}",
        ]
        for table in tables:
            arguments.append(f"--table=public.{_identifier(table)}")
        with destination.open("xb") as output:
            os.chmod(destination, 0o600)
            self.runner.run(
                self._command("pg_dump", *arguments),
                environment=self.connection.environment,
                stdout=output,
                label="Tenant PostgreSQL backup",
            )

    def render_dump(self, source: Path) -> bytes:
        with source.open("rb") as dump:
            return self.runner.run(
                self._command(
                    "pg_restore",
                    "--data-only",
                    "--no-owner",
                    "--no-privileges",
                    "--file=-",
                ),
                environment=self.connection.environment,
                stdin=dump,
                label="Tenant PostgreSQL dump inspection",
            )

    def restore_tenant(self, source: Path) -> None:
        with source.open("rb") as dump:
            self.runner.run(
                self._command(
                    "pg_restore",
                    "--exit-on-error",
                    "--single-transaction",
                    "--data-only",
                    "--no-owner",
                    "--no-privileges",
                    "--dbname",
                    self.connection.database,
                ),
                environment=self.connection.environment,
                stdin=dump,
                label="Tenant PostgreSQL restore",
            )


def _identifier(value: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise recovery.RecoveryError("PostgreSQL returned an unsafe identifier")
    return value


def _organization_id(value: str | UUID) -> UUID:
    try:
        parsed = UUID(str(value))
    except ValueError as exc:
        raise recovery.RecoveryError("Organization ID must be a UUID") from exc
    if str(parsed) != str(value).lower():
        raise recovery.RecoveryError("Organization ID must be canonical")
    return parsed


def _operator(value: str, *, label: str) -> str:
    if not OPERATOR_ID.fullmatch(value):
        raise recovery.RecoveryError(f"{label} is invalid")
    return value


def _ticket_digest(value: str) -> str:
    if not 4 <= len(value) <= 256 or any(ord(item) < 32 for item in value):
        raise recovery.RecoveryError("Authorization ticket is invalid")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sql_literal(value: str) -> str:
    if "\x00" in value:
        raise recovery.RecoveryError("Unsafe PostgreSQL literal")
    return "'" + value.replace("'", "''") + "'"


def _json_query(pg: TenantPgTools, sql: str, *, label: str) -> object:
    raw = pg.query(sql)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise recovery.RecoveryError(f"{label} returned invalid JSON") from exc


def _scoped_connection(
    connection: recovery.PostgresConnection,
    organization_id: UUID,
) -> recovery.PostgresConnection:
    environment = dict(connection.environment)
    environment["PGOPTIONS"] = (
        f"-c app.organization_id={organization_id} -c row_security=on "
        "-c max_parallel_workers_per_gather=0"
    )
    return replace(connection, environment=environment)


def _source_role_contract(pg: TenantPgTools, organization_id: UUID) -> str:
    raw = pg.query(
        "SELECT current_user || '|' || rolsuper::text || '|' || "
        "rolbypassrls::text || '|' || rolcanlogin::text || '|' || "
        "rolcreatedb::text || '|' || rolcreaterole::text || '|' || "
        "rolreplication::text || '|' || rolinherit::text || '|' || "
        "COALESCE(current_setting('app.organization_id', true), '') "
        "FROM pg_catalog.pg_roles WHERE rolname = current_user"
    )
    parts = raw.split("|")
    if len(parts) != 9:
        raise recovery.RecoveryError("Tenant backup role contract is invalid")
    (
        role,
        superuser,
        bypass_rls,
        login,
        create_db,
        create_role,
        replication,
        _inherit,
        scoped_id,
    ) = parts
    if (
        superuser != "false"
        or bypass_rls != "false"
        or login != "true"
        or create_db != "false"
        or create_role != "false"
        or replication != "false"
    ):
        raise recovery.RecoveryError(
            "Tenant backup requires a least-privilege non-bypass LOGIN role"
        )
    if scoped_id != str(organization_id):
        raise recovery.RecoveryError("Tenant backup role is not scoped to the tenant")
    return role


def _migration_head(pg: TenantPgTools) -> str:
    head = pg.query("SELECT version_num FROM public.alembic_version")
    if not re.fullmatch(r"[a-z0-9]{4,64}", head):
        raise recovery.RecoveryError("PostgreSQL migration head is invalid")
    return head


def _policy_expression(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise recovery.RecoveryError("Tenant table policy expression is invalid")
    return re.sub(r"[\s()]+", "", value)


def tenant_security_inventory(
    pg: TenantPgTools,
    *,
    require_backup_grants: bool,
) -> dict[str, object]:
    """Read and validate the complete selected-table contract from pg_catalog."""

    value = _json_query(
        pg,
        "WITH selected AS ("
        " SELECT relation.oid, relation.relname, relation.relkind,"
        " relation.relispartition,"
        " relation.relrowsecurity, relation.relforcerowsecurity,"
        " relation.relowner, attribute.attname AS tenant_column,"
        " attribute.attnotnull,"
        " pg_catalog.format_type(attribute.atttypid, attribute.atttypmod)"
        " AS tenant_type"
        " FROM pg_catalog.pg_class AS relation"
        " JOIN pg_catalog.pg_namespace AS namespace"
        " ON namespace.oid = relation.relnamespace"
        " JOIN pg_catalog.pg_attribute AS attribute"
        " ON attribute.attrelid = relation.oid"
        " AND attribute.attname = CASE WHEN relation.relname = 'organizations'"
        " THEN 'id' ELSE 'organization_id' END"
        " AND NOT attribute.attisdropped"
        " WHERE namespace.nspname = 'public'"
        " AND (relation.relname = 'organizations' OR EXISTS ("
        " SELECT 1 FROM pg_catalog.pg_attribute AS tenant_attribute"
        " WHERE tenant_attribute.attrelid = relation.oid"
        " AND tenant_attribute.attname = 'organization_id'"
        " AND NOT tenant_attribute.attisdropped))"
        " AND relation.relkind IN ('r', 'p', 'f', 'v', 'm')"
        ") SELECT pg_catalog.json_build_object("
        " 'current_role', current_user,"
        " 'schema_usage', pg_catalog.has_schema_privilege("
        " current_user, 'public', 'USAGE'),"
        " 'schema_create', pg_catalog.has_schema_privilege("
        " current_user, 'public', 'CREATE'),"
        " 'unsafe_memberships', COALESCE((SELECT pg_catalog.json_agg(role.rolname"
        " ORDER BY role.rolname) FROM pg_catalog.pg_roles AS role"
        " WHERE (role.rolsuper OR role.rolbypassrls OR role.rolcreaterole"
        " OR role.rolcreatedb OR role.rolreplication)"
        " AND pg_catalog.pg_has_role(current_user, role.rolname, 'MEMBER')),"
        " '[]'::json),"
        " 'relations', COALESCE(pg_catalog.json_agg(pg_catalog.json_build_object("
        " 'name', selected.relname, 'kind', selected.relkind,"
        " 'is_partition', selected.relispartition,"
        " 'tenant_column', selected.tenant_column,"
        " 'tenant_type', selected.tenant_type,"
        " 'tenant_not_null', selected.attnotnull,"
        " 'rls_enabled', selected.relrowsecurity,"
        " 'rls_forced', selected.relforcerowsecurity,"
        " 'owner', owner.rolname,"
        " 'backup_role_is_owner_member', pg_catalog.pg_has_role("
        " current_user, owner.rolname, 'MEMBER'),"
        " 'select_granted', pg_catalog.has_table_privilege("
        " current_user, selected.oid, 'SELECT'),"
        " 'insert_granted', pg_catalog.has_table_privilege("
        " current_user, selected.oid, 'INSERT'),"
        " 'update_granted', pg_catalog.has_table_privilege("
        " current_user, selected.oid, 'UPDATE'),"
        " 'delete_granted', pg_catalog.has_table_privilege("
        " current_user, selected.oid, 'DELETE'),"
        " 'truncate_granted', pg_catalog.has_table_privilege("
        " current_user, selected.oid, 'TRUNCATE'),"
        " 'trigger_granted', pg_catalog.has_table_privilege("
        " current_user, selected.oid, 'TRIGGER'),"
        " 'references_granted', pg_catalog.has_table_privilege("
        " current_user, selected.oid, 'REFERENCES'),"
        " 'policies', COALESCE((SELECT pg_catalog.json_agg("
        " pg_catalog.json_build_object("
        " 'name', policy.polname, 'command', policy.polcmd,"
        " 'permissive', policy.polpermissive,"
        " 'public_only', policy.polroles = ARRAY[0]::oid[],"
        " 'using', pg_catalog.pg_get_expr("
        " policy.polqual, policy.polrelid, true),"
        " 'with_check', pg_catalog.pg_get_expr("
        " policy.polwithcheck, policy.polrelid, true))"
        " ORDER BY policy.polname) FROM pg_catalog.pg_policy AS policy"
        " WHERE policy.polrelid = selected.oid), '[]'::json))"
        " ORDER BY selected.relname), '[]'::json))"
        " FROM selected JOIN pg_catalog.pg_roles AS owner"
        " ON owner.oid = selected.relowner",
        label="Tenant pg_catalog security inventory",
    )
    if not isinstance(value, dict):
        raise recovery.RecoveryError("Tenant pg_catalog security inventory is invalid")
    relations = value.get("relations")
    if not isinstance(relations, list) or not relations:
        raise recovery.RecoveryError("Tenant pg_catalog security inventory is empty")
    if not value.get("schema_usage"):
        raise recovery.RecoveryError("Tenant backup role lacks public schema usage")
    if require_backup_grants:
        if value.get("schema_create"):
            raise recovery.RecoveryError(
                "Tenant backup role must not create objects in the public schema"
            )
        unsafe = value.get("unsafe_memberships")
        if not isinstance(unsafe, list) or unsafe:
            raise recovery.RecoveryError(
                "Tenant backup role inherits an unsafe PostgreSQL role"
            )

    normalized: list[dict[str, object]] = []
    names: set[str] = set()
    for raw in relations:
        if not isinstance(raw, dict):
            raise recovery.RecoveryError(
                "Tenant pg_catalog security inventory is invalid"
            )
        name = _identifier(str(raw.get("name", "")))
        if name in names:
            raise recovery.RecoveryError("Tenant table inventory contains duplicates")
        names.add(name)
        column = "id" if name == "organizations" else "organization_id"
        if (
            raw.get("kind") != "r"
            or raw.get("is_partition") is not False
            or raw.get("tenant_column") != column
            or raw.get("tenant_type") != "uuid"
            or raw.get("tenant_not_null") is not True
        ):
            raise recovery.RecoveryError(
                f"Tenant table {name} has an unsafe relation/tenant-column contract"
            )
        if raw.get("rls_enabled") is not True or raw.get("rls_forced") is not True:
            raise recovery.RecoveryError(
                f"Tenant table {name} must enable and force row-level security"
            )
        policies = raw.get("policies")
        expected_name = (
            "organization_isolation" if name == "organizations" else "tenant_isolation"
        )
        expected_expression = f"{column}=security.current_organization_id()"
        if not isinstance(policies, list) or len(policies) != 1:
            raise recovery.RecoveryError(
                f"Tenant table {name} must have exactly one isolation policy"
            )
        policy = policies[0]
        if (
            not isinstance(policy, dict)
            or policy.get("name") != expected_name
            or policy.get("command") != "*"
            or policy.get("permissive") is not True
            or policy.get("public_only") is not True
            or _policy_expression(policy.get("using"))
            != _policy_expression(expected_expression)
            or _policy_expression(policy.get("with_check"))
            != _policy_expression(expected_expression)
        ):
            raise recovery.RecoveryError(
                f"Tenant table {name} isolation policy is missing or unsafe"
            )
        if require_backup_grants:
            if raw.get("backup_role_is_owner_member") is not False:
                raise recovery.RecoveryError(
                    f"Tenant backup role must not own or inherit owner rights on {name}"
                )
            if raw.get("select_granted") is not True or any(
                raw.get(privilege) is not False
                for privilege in (
                    "insert_granted",
                    "update_granted",
                    "delete_granted",
                    "truncate_granted",
                    "trigger_granted",
                    "references_granted",
                )
            ):
                raise recovery.RecoveryError(
                    f"Tenant backup role must have SELECT-only access to {name}"
                )
        normalized.append(raw)
    if "organizations" not in names:
        raise recovery.RecoveryError("Tenant organization table is unavailable")
    tables = ["organizations", *sorted(names - {"organizations"})]
    return {
        "current_role": str(value.get("current_role", "")),
        "tables": tables,
        "relations": normalized,
        "schema_usage": True,
        "schema_create": bool(value.get("schema_create")),
        "unsafe_memberships": value.get("unsafe_memberships", []),
    }


def tenant_tables(pg: TenantPgTools) -> list[str]:
    inventory = tenant_security_inventory(pg, require_backup_grants=False)
    return list(inventory["tables"])  # type: ignore[arg-type]


def tenant_inventory(
    pg: TenantPgTools,
    organization_id: UUID,
    tables: Sequence[str],
) -> dict[str, int]:
    selects: list[str] = []
    for table in tables:
        column = "id" if table == "organizations" else "organization_id"
        safe_table = _identifier(table)
        selects.append(
            f"SELECT '{safe_table}'::text AS table_name, count(*)::bigint "
            f"AS row_count FROM public.{safe_table} "
            f"WHERE {_identifier(column)} = '{organization_id}'"
        )
    value = _json_query(
        pg,
        "SELECT COALESCE(json_object_agg(table_name, row_count), '{}'::json) "
        f"FROM ({' UNION ALL '.join(selects)}) AS tenant_counts",
        label="Tenant row inventory",
    )
    if not isinstance(value, dict) or set(value) != set(tables):
        raise recovery.RecoveryError("Tenant row inventory is invalid")
    inventory: dict[str, int] = {}
    for table, raw in value.items():
        try:
            count = int(raw)
        except (TypeError, ValueError) as exc:
            raise recovery.RecoveryError("Tenant row inventory is invalid") from exc
        if count < 0:
            raise recovery.RecoveryError("Tenant row inventory is invalid")
        inventory[_identifier(str(table))] = count
    return inventory


def _foreign_key_contracts(pg: TenantPgTools) -> list[dict[str, object]]:
    value = _json_query(
        pg,
        "WITH pairs AS ("
        " SELECT constraint_row.oid, constraint_row.conname,"
        " child.relname AS child_table, parent.relname AS parent_table,"
        " child_attr.attname AS child_column,"
        " parent_attr.attname AS parent_column, child_key.ordinality AS ord"
        " FROM pg_catalog.pg_constraint AS constraint_row"
        " JOIN pg_catalog.pg_class AS child"
        " ON child.oid = constraint_row.conrelid"
        " JOIN pg_catalog.pg_namespace AS child_namespace"
        " ON child_namespace.oid = child.relnamespace"
        " JOIN pg_catalog.pg_class AS parent"
        " ON parent.oid = constraint_row.confrelid"
        " JOIN LATERAL unnest(constraint_row.conkey) WITH ORDINALITY"
        " AS child_key(attnum, ordinality) ON true"
        " JOIN LATERAL unnest(constraint_row.confkey) WITH ORDINALITY"
        " AS parent_key(attnum, ordinality)"
        " ON parent_key.ordinality = child_key.ordinality"
        " JOIN pg_catalog.pg_attribute AS child_attr"
        " ON child_attr.attrelid = child.oid"
        " AND child_attr.attnum = child_key.attnum"
        " JOIN pg_catalog.pg_attribute AS parent_attr"
        " ON parent_attr.attrelid = parent.oid"
        " AND parent_attr.attnum = parent_key.attnum"
        " WHERE constraint_row.contype = 'f'"
        " AND child_namespace.nspname = 'public'"
        "), grouped AS ("
        " SELECT oid, conname, child_table, parent_table,"
        " json_agg(json_build_object('child', child_column,"
        " 'parent', parent_column) ORDER BY ord) AS columns"
        " FROM pairs GROUP BY oid, conname, child_table, parent_table"
        ") SELECT COALESCE(json_agg(json_build_object("
        " 'name', conname, 'child_table', child_table,"
        " 'parent_table', parent_table, 'columns', columns)"
        " ORDER BY child_table, conname), '[]'::json) FROM grouped",
        label="Tenant relationship inventory",
    )
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise recovery.RecoveryError("Tenant relationship inventory is invalid")
    return value


def relationship_violations(
    pg: TenantPgTools,
    organization_id: UUID,
    tenant_table_names: Sequence[str],
) -> list[str]:
    tenant_tables_set = set(tenant_table_names)
    violations: list[str] = []
    probes: list[str] = []
    contracts = _foreign_key_contracts(pg)
    tenant_secured_pairs: set[tuple[str, str]] = set()
    for contract in contracts:
        columns = contract.get("columns")
        if not isinstance(columns, list):
            continue
        pairs = {
            (str(item.get("child", "")), str(item.get("parent", "")))
            for item in columns
            if isinstance(item, dict)
        }
        if ("organization_id", "organization_id") in pairs:
            tenant_secured_pairs.add(
                (
                    str(contract.get("child_table", "")),
                    str(contract.get("parent_table", "")),
                )
            )
    for contract in contracts:
        name = _identifier(str(contract.get("name", "")))
        child = _identifier(str(contract.get("child_table", "")))
        parent = _identifier(str(contract.get("parent_table", "")))
        columns = contract.get("columns")
        if child not in tenant_tables_set or not isinstance(columns, list):
            continue
        pairs: list[tuple[str, str]] = []
        for item in columns:
            if not isinstance(item, dict):
                raise recovery.RecoveryError("Tenant relationship inventory is invalid")
            pairs.append(
                (
                    _identifier(str(item.get("child", ""))),
                    _identifier(str(item.get("parent", ""))),
                )
            )
        if not pairs:
            continue
        if parent == "organizations" and pairs == [("organization_id", "id")]:
            join = "parent.id = child.organization_id"
            required = "child.organization_id IS NOT NULL"
        elif ("organization_id", "organization_id") in pairs:
            join = " AND ".join(
                f"parent.{parent_column} = child.{child_column}"
                for child_column, parent_column in pairs
            )
            required = " AND ".join(
                f"child.{child_column} IS NOT NULL"
                for child_column, _parent_column in pairs
            )
        else:
            # A tenant table must not retain a single-column relationship to a
            # tenant parent; the migration contract upgrades these to include
            # organization_id. Flag it rather than trusting a global UUID.
            if (
                parent in tenant_tables_set
                and (child, parent) not in tenant_secured_pairs
            ):
                violations.append(f"{name}:missing_tenant_key")
            continue
        parent_probe = pairs[0][1]
        probes.append(
            f"SELECT '{name}'::text AS constraint_name, count(*)::bigint "
            f"AS violation_count FROM public.{child} AS child LEFT JOIN "
            f"public.{parent} AS parent ON {join} WHERE "
            f"child.organization_id = '{organization_id}' AND {required} "
            f"AND parent.{parent_probe} IS NULL"
        )
    if probes:
        value = _json_query(
            pg,
            "SELECT COALESCE(json_object_agg(constraint_name, violation_count), "
            "'{}'::json) FROM ("
            + " UNION ALL ".join(probes)
            + ") AS tenant_relationship_checks",
            label="Tenant relationship validation",
        )
        if not isinstance(value, dict):
            raise recovery.RecoveryError("Tenant relationship validation is invalid")
        for name, count in value.items():
            try:
                if int(count) > 0:
                    violations.append(_identifier(str(name)))
            except (TypeError, ValueError) as exc:
                raise recovery.RecoveryError(
                    "Tenant relationship validation is invalid"
                ) from exc
    return sorted(violations)


def _safe_object_key(value: str) -> str:
    if not value or len(value) > 1024 or "\x00" in value or "\\" in value:
        raise recovery.RecoveryError("Private-object key is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise recovery.RecoveryError("Private-object key is invalid")
    if path.as_posix() != value:
        raise recovery.RecoveryError("Private-object key is invalid")
    return value


def _legacy_object_key(
    reference: str,
    *,
    organization_id: UUID,
    category: str,
    source_kind: str,
    local_root: Path | None,
) -> str:
    """Resolve only canonical tenant/category legacy references."""

    if category not in LEGACY_PREFIXES:
        raise recovery.RecoveryError("Legacy private-object category is invalid")
    raw_path = Path(reference)
    if raw_path.is_absolute():
        if source_kind != "local" or local_root is None:
            raise recovery.RecoveryError(
                "Absolute legacy private objects require local object storage"
            )
        try:
            key = (
                raw_path.resolve(strict=False)
                .relative_to(local_root.resolve(strict=True))
                .as_posix()
            )
        except (OSError, ValueError) as exc:
            raise recovery.RecoveryError(
                "Legacy private object escapes the configured root"
            ) from exc
    else:
        key = reference
    key = _safe_object_key(key)
    _validate_legacy_key_namespace(
        key,
        organization_id=organization_id,
        category=category,
        source_kind=source_kind,
    )
    return key


def _validate_legacy_key_namespace(
    key: str,
    *,
    organization_id: UUID,
    category: str,
    source_kind: str,
) -> None:
    key = _safe_object_key(key)
    parts = PurePosixPath(key).parts
    canonical_prefix = ("private", str(organization_id), category)
    if tuple(parts[:3]) == canonical_prefix and len(parts) == 4:
        return
    if parts and parts[0] == "private":
        raise recovery.RecoveryError(
            "Legacy private object maps another tenant/category namespace"
        )
    if source_kind != "local":
        raise recovery.RecoveryError(
            "Pre-registry legacy private objects require local object storage"
        )
    expected = LEGACY_PREFIXES[category]
    if category != "candidates":
        expected = (*expected, str(organization_id))
    if tuple(parts[:-1]) != expected:
        raise recovery.RecoveryError(
            "Legacy private object maps another tenant/category namespace"
        )
    filename = PurePosixPath(parts[-1])
    try:
        opaque_id = UUID(filename.stem)
    except ValueError as exc:
        raise recovery.RecoveryError(
            "Legacy private-object filename is not opaque"
        ) from exc
    if filename.stem != str(opaque_id) or filename.suffix != filename.suffix.lower():
        raise recovery.RecoveryError("Legacy private-object filename is not canonical")


def object_contracts(
    pg: TenantPgTools,
    organization_id: UUID,
    *,
    local_root: Path | None,
    source_kind: str,
    tenant_table_names: Sequence[str] | None = None,
) -> list[ObjectContract]:
    registry = _json_query(
        pg,
        "SELECT COALESCE(json_agg(json_build_object("
        "'key', object_key, 'category', category,"
        " 'sha256', sha256, 'bytes', size_bytes)"
        " ORDER BY object_key), '[]'::json) FROM public.private_objects "
        f"WHERE organization_id = '{organization_id}' AND state = 'ready'",
        label="Private-object registry inventory",
    )
    if not isinstance(registry, list):
        raise recovery.RecoveryError("Private-object registry inventory is invalid")
    values: dict[str, ObjectContract] = {}
    expected_prefix = f"private/{organization_id}/"
    for item in registry:
        if not isinstance(item, dict):
            raise recovery.RecoveryError("Private-object registry inventory is invalid")
        key = _safe_object_key(str(item.get("key", "")))
        category = str(item.get("category", ""))
        sha256 = str(item.get("sha256", ""))
        try:
            size = int(item.get("bytes", -1))
        except (TypeError, ValueError) as exc:
            raise recovery.RecoveryError(
                "Private-object registry inventory is invalid"
            ) from exc
        if not key.startswith(expected_prefix):
            raise recovery.RecoveryError(
                "Private-object registry contains a cross-tenant key"
            )
        key_parts = PurePosixPath(key).parts
        if (
            len(key_parts) != 4
            or key_parts[2] != category
            or not IDENTIFIER.fullmatch(category)
        ):
            raise recovery.RecoveryError(
                "Private-object registry key/category contract is invalid"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", sha256) or size <= 0:
            raise recovery.RecoveryError("Private-object registry digest is invalid")
        values[key] = ObjectContract(
            key,
            "registry",
            category,
            sha256,
            size,
            key,
        )

    table_names = set(tenant_table_names or tenant_tables(pg))
    for table, path_column, object_column, category in LEGACY_OBJECT_COLUMNS:
        if table not in table_names:
            continue
        result = _json_query(
            pg,
            f"SELECT COALESCE(json_agg({path_column} ORDER BY {path_column}),"
            " '[]'::json) FROM public."
            f"{_identifier(table)} WHERE organization_id = '{organization_id}' "
            f"AND {_identifier(object_column)} IS NULL "
            f"AND {_identifier(path_column)} IS NOT NULL",
            label="Legacy private-object inventory",
        )
        if not isinstance(result, list) or any(
            not isinstance(row, str) for row in result
        ):
            raise recovery.RecoveryError("Legacy private-object inventory is invalid")
        for reference in result:
            key = _legacy_object_key(
                reference,
                organization_id=organization_id,
                category=category,
                source_kind=source_kind,
                local_root=local_root,
            )
            existing = values.get(key)
            if existing is not None and existing.database_reference != reference:
                raise recovery.RecoveryError("Private-object references are ambiguous")
            values.setdefault(
                key,
                ObjectContract(
                    key,
                    "legacy",
                    category,
                    None,
                    None,
                    reference,
                ),
            )
    return [values[key] for key in sorted(values)]


def _object_destination(root: Path, key: str) -> Path:
    target = (root / _safe_object_key(key)).resolve()
    if not target.is_relative_to(root.resolve()):
        raise recovery.RecoveryError("Private-object key escapes its object root")
    return target


def _capture_objects(
    *,
    contracts: Sequence[ObjectContract],
    destination: Path,
    source_kind: str,
    local_root: Path | None,
    runner: recovery.CommandRunner,
    environ: Mapping[str, str],
) -> list[dict[str, object]]:
    destination.mkdir(parents=True, mode=0o700)
    if source_kind == "local":
        if local_root is None:
            raise recovery.RecoveryError("Local object root is required")
        root = recovery.validate_bounded_absolute_path(
            local_root,
            label="tenant local object source",
            must_exist=True,
        )
        if not root.is_dir():
            raise recovery.RecoveryError(
                "Tenant local object source is not a directory"
            )
    else:
        bucket = recovery.require_env("HR_TENANT_BACKUP_S3_BUCKET", environ)
        recovery._s3_uri(bucket)
        recovery._aws_endpoint_arguments(environ)
        root = None

    entries: list[dict[str, object]] = []
    for contract in contracts:
        target = _object_destination(destination, contract.key)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if source_kind == "local":
            assert root is not None
            source = _object_destination(root, contract.key)
            try:
                before = source.lstat()
            except OSError as exc:
                raise recovery.RecoveryError(
                    "Referenced private object is unavailable"
                ) from exc
            if not stat.S_ISREG(before.st_mode) or source.is_symlink():
                raise recovery.RecoveryError("Referenced private object is unavailable")
            with source.open("rb") as input_handle, target.open("xb") as output_handle:
                os.chmod(target, 0o600)
                shutil.copyfileobj(
                    input_handle,
                    output_handle,
                    recovery.COPY_CHUNK_BYTES,
                )
            try:
                after = source.lstat()
            except OSError as exc:
                raise recovery.RecoveryError(
                    "Private object changed while its snapshot was captured"
                ) from exc
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise recovery.RecoveryError(
                    "Private object changed while its snapshot was captured"
                )
        else:
            bucket = recovery.require_env("HR_TENANT_BACKUP_S3_BUCKET", environ)
            fetched = recovery._aws_json(
                runner.run(
                    [
                        "aws",
                        "s3api",
                        "get-object",
                        "--bucket",
                        bucket,
                        "--key",
                        contract.key,
                        str(target),
                        "--output",
                        "json",
                        *recovery._aws_endpoint_arguments(environ),
                    ],
                    label="Tenant private-object read",
                ),
                label="Tenant private-object read",
            )
            after = recovery._aws_json(
                runner.run(
                    [
                        "aws",
                        "s3api",
                        "head-object",
                        "--bucket",
                        bucket,
                        "--key",
                        contract.key,
                        "--output",
                        "json",
                        *recovery._aws_endpoint_arguments(environ),
                    ],
                    label="Tenant private-object stability check",
                ),
                label="Tenant private-object stability check",
            )
            identity_fields = ("ETag", "ContentLength", "VersionId", "LastModified")
            if any(fetched.get(name) != after.get(name) for name in identity_fields):
                raise recovery.RecoveryError(
                    "Private object changed while its snapshot was captured"
                )
            try:
                target.chmod(0o600)
            except OSError as exc:
                raise recovery.RecoveryError(
                    "Tenant private-object snapshot permissions failed"
                ) from exc
        sha256, size = recovery.sha256_file(target)
        if size <= 0:
            raise recovery.RecoveryError("Private-object snapshot is empty")
        if contract.expected_sha256 is not None and sha256 != contract.expected_sha256:
            raise recovery.RecoveryError(
                "Private-object content does not match its registry digest"
            )
        if contract.expected_bytes is not None and size != contract.expected_bytes:
            raise recovery.RecoveryError(
                "Private-object content does not match its registry size"
            )
        entries.append(
            {
                "key": contract.key,
                "kind": contract.kind,
                "category": contract.category,
                "database_reference": contract.database_reference,
                "sha256": sha256,
                "bytes": size,
            }
        )
    if len(entries) != len({str(row["key"]) for row in entries}):
        raise recovery.RecoveryError("Private-object manifest contains duplicate keys")
    return entries


def _tenant_backup_id(now: datetime) -> str:
    return f"hr-tenant-{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"


def _make_tenant_tar(staging: Path, destination: Path) -> None:
    with tarfile.open(destination, "x:") as bundle:
        for name in ("manifest.json", "tenant.dump", "objects"):
            path = staging / name
            if path.exists():
                bundle.add(path, arcname=name, recursive=True)
    os.chmod(destination, 0o600)


def _publish_no_clobber(temporary: Path, destination: Path) -> None:
    """Atomically publish one same-filesystem file without replacing evidence."""

    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.link(temporary, destination)
    except FileExistsError as exc:
        raise recovery.RecoveryError(
            f"Refusing to overwrite existing output: {destination.name}"
        ) from exc
    except OSError as exc:
        raise recovery.RecoveryError(
            f"Atomic no-clobber publication failed: {destination.name}"
        ) from exc
    try:
        directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise recovery.RecoveryError(
            f"Atomic output directory sync failed: {destination.name}"
        ) from exc
    temporary.unlink(missing_ok=True)


def _write_json_no_clobber(path: Path, payload: object, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("xb") as handle:
            os.chmod(temporary, mode)
            handle.write(recovery.canonical_json(payload) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        _publish_no_clobber(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sql_statements(rendered: bytes) -> list[str]:
    try:
        sql = rendered.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise recovery.RecoveryError(
            "Tenant PostgreSQL dump inspection is not UTF-8"
        ) from exc
    statements: list[str] = []
    current: list[str] = []
    index = 0
    quote: str | None = None
    escape_string = False
    dollar_tag: str | None = None
    block_depth = 0
    line_start = True
    while index < len(sql):
        char = sql[index]
        following = sql[index + 1] if index + 1 < len(sql) else ""
        if block_depth:
            if char == "/" and following == "*":
                block_depth += 1
                index += 2
                continue
            if char == "*" and following == "/":
                block_depth -= 1
                index += 2
                continue
            line_start = char == "\n"
            index += 1
            continue
        if dollar_tag is not None:
            if sql.startswith(dollar_tag, index):
                current.append(dollar_tag)
                index += len(dollar_tag)
                dollar_tag = None
                line_start = False
                continue
            current.append(char)
            line_start = char == "\n"
            index += 1
            continue
        if quote == "'":
            current.append(char)
            if char == "'" and following == "'":
                current.append(following)
                index += 2
                continue
            if escape_string and char == "\\" and following:
                current.append(following)
                index += 2
                continue
            if char == "'":
                quote = None
                escape_string = False
            line_start = char == "\n"
            index += 1
            continue
        if quote == '"':
            current.append(char)
            if char == '"' and following == '"':
                current.append(following)
                index += 2
                continue
            if char == '"':
                quote = None
            line_start = char == "\n"
            index += 1
            continue
        if char == "-" and following == "-":
            newline = sql.find("\n", index + 2)
            if newline == -1:
                break
            current.append("\n")
            index = newline + 1
            line_start = True
            continue
        if char == "/" and following == "*":
            block_depth = 1
            index += 2
            continue
        if line_start and char in " \t\r":
            current.append(char)
            index += 1
            continue
        if line_start and char == "\\":
            newline = sql.find("\n", index + 1)
            if newline == -1:
                break
            current.append("\n")
            index = newline + 1
            line_start = True
            continue
        if char == "'":
            previous = "".join(current).rstrip()
            escape_string = (
                bool(previous)
                and previous[-1] in {"e", "E"}
                and (
                    len(previous) == 1
                    or not (previous[-2].isalnum() or previous[-2] == "_")
                )
            )
            quote = "'"
            current.append(char)
            line_start = False
            index += 1
            continue
        if char == '"':
            quote = '"'
            current.append(char)
            line_start = False
            index += 1
            continue
        if char == "$":
            match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql[index:])
            if match:
                dollar_tag = match.group(0)
                current.append(dollar_tag)
                index += len(dollar_tag)
                line_start = False
                continue
        current.append(char)
        line_start = char == "\n"
        index += 1
        if char == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            line_start = True
    if quote is not None or dollar_tag is not None or block_depth:
        raise recovery.RecoveryError("Tenant PostgreSQL dump SQL is malformed")
    if "".join(current).strip():
        raise recovery.RecoveryError("Tenant PostgreSQL dump SQL is truncated")
    return statements


def _split_sql_values(value: str) -> list[str]:
    values: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escape_string = False
    index = 0
    while index < len(value):
        char = value[index]
        following = value[index + 1] if index + 1 < len(value) else ""
        if quote == "'":
            if char == "'" and following == "'":
                index += 2
                continue
            if escape_string and char == "\\" and following:
                index += 2
                continue
            if char == "'":
                quote = None
                escape_string = False
            index += 1
            continue
        if quote == '"':
            if char == '"' and following == '"':
                index += 2
                continue
            if char == '"':
                quote = None
            index += 1
            continue
        if char == "'":
            previous = value[:index].rstrip()
            escape_string = (
                bool(previous)
                and previous[-1] in {"e", "E"}
                and (
                    len(previous) == 1
                    or not (previous[-2].isalnum() or previous[-2] == "_")
                )
            )
            quote = "'"
        elif char == '"':
            quote = '"'
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
            if depth < 0:
                raise recovery.RecoveryError("Tenant PostgreSQL INSERT is malformed")
        elif char == "," and depth == 0:
            values.append(value[start:index].strip())
            start = index + 1
        index += 1
    if quote is not None or depth != 0:
        raise recovery.RecoveryError("Tenant PostgreSQL INSERT is malformed")
    values.append(value[start:].strip())
    return values


def verify_tenant_dump_rows(
    rendered: bytes,
    *,
    organization_id: UUID,
    tables: Sequence[str],
    expected_counts: Mapping[str, int],
) -> dict[str, object]:
    """Prove every archived row is in one selected tenant/table inventory."""

    counts = {table: 0 for table in tables}
    insert_pattern = re.compile(
        r'^INSERT\s+INTO\s+(?:public|"public")\.'
        r'(?P<table>"(?:""|[^"])+"|[a-z_][a-z0-9_]*)\s*'
        r"\((?P<columns>.*?)\)\s+VALUES\s*\((?P<values>.*)\)\s*;$",
        re.IGNORECASE | re.DOTALL,
    )
    for statement in _sql_statements(rendered):
        normalized = statement.lstrip()
        upper = normalized.upper()
        if upper.startswith(("COPY ", "UPDATE ", "DELETE ", "MERGE ")) or re.match(
            r"^SELECT\s+(?:PG_CATALOG\.)?SETVAL\s*\(", upper
        ):
            raise recovery.RecoveryError(
                "Tenant PostgreSQL dump contains unsupported data mutation"
            )
        if not upper.startswith("INSERT "):
            continue
        match = insert_pattern.fullmatch(normalized)
        if match is None:
            raise recovery.RecoveryError("Tenant PostgreSQL INSERT is not canonical")
        raw_table = match.group("table")
        table = (
            raw_table[1:-1].replace('""', '"')
            if raw_table.startswith('"')
            else raw_table
        )
        table = _identifier(table)
        if table not in counts:
            raise recovery.RecoveryError(
                "Tenant PostgreSQL dump contains an unselected table"
            )
        columns = [
            item[1:-1].replace('""', '"') if item.startswith('"') else item
            for item in (part.strip() for part in match.group("columns").split(","))
        ]
        if any(not IDENTIFIER.fullmatch(column) for column in columns):
            raise recovery.RecoveryError("Tenant PostgreSQL column list is invalid")
        values = _split_sql_values(match.group("values"))
        if len(columns) != len(values) or len(columns) != len(set(columns)):
            raise recovery.RecoveryError("Tenant PostgreSQL INSERT shape is invalid")
        tenant_column = "id" if table == "organizations" else "organization_id"
        if tenant_column not in columns:
            raise recovery.RecoveryError(
                "Tenant PostgreSQL INSERT omits its tenant identity"
            )
        literal = values[columns.index(tenant_column)]
        uuid_match = re.fullmatch(
            r"'([0-9A-Fa-f-]{36})'(?:\s*::\s*(?:pg_catalog\.)?uuid)?",
            literal,
        )
        if uuid_match is None:
            raise recovery.RecoveryError(
                "Tenant PostgreSQL INSERT tenant identity is not a UUID literal"
            )
        try:
            row_organization = UUID(uuid_match.group(1))
        except ValueError as exc:
            raise recovery.RecoveryError(
                "Tenant PostgreSQL INSERT tenant identity is invalid"
            ) from exc
        if row_organization != organization_id:
            raise recovery.RecoveryError(
                "Tenant PostgreSQL dump contains a different organization"
            )
        counts[table] += 1
    normalized_expected = {
        table: int(expected_counts.get(table, -1)) for table in tables
    }
    if counts != normalized_expected:
        raise recovery.RecoveryError(
            "Tenant PostgreSQL dump row counts differ from its snapshot inventory"
        )
    return {
        "organization_id": str(organization_id),
        "row_counts": counts,
        "row_count": sum(counts.values()),
        "verified": True,
    }


def _manifest_objects(extracted: Path) -> list[dict[str, object]]:
    entries = recovery._relative_object_entries(extracted / "objects")
    return [
        {"key": row["key"], "sha256": row["sha256"], "bytes": row["bytes"]}
        for row in entries
    ]


def _verify_tenant_manifest(extracted: Path) -> dict[str, object]:
    try:
        manifest = json.loads((extracted / "manifest.json").read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise recovery.RecoveryError(
            "Tenant backup manifest is missing or invalid"
        ) from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != TENANT_FORMAT_VERSION
    ):
        raise recovery.RecoveryError("Tenant backup manifest format is unsupported")
    organization_id = _organization_id(str(manifest.get("organization_id", "")))
    database = manifest.get("database")
    objects = manifest.get("objects")
    operator = manifest.get("operator")
    if not isinstance(database, dict) or not isinstance(objects, dict):
        raise recovery.RecoveryError("Tenant backup manifest inventory is invalid")
    if not isinstance(operator, dict):
        raise recovery.RecoveryError("Tenant backup operator evidence is invalid")
    _operator(str(operator.get("id", "")), label="Operator ID")
    ticket_sha256 = str(operator.get("authorization_ticket_sha256", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", ticket_sha256):
        raise recovery.RecoveryError("Tenant backup operator evidence is invalid")

    dump = extracted / "tenant.dump"
    dump_sha256, dump_bytes = recovery.sha256_file(dump)
    if dump_sha256 != database.get("sha256") or dump_bytes != database.get("bytes"):
        raise recovery.RecoveryError("Tenant PostgreSQL dump checksum is invalid")
    head = database.get("migration_head")
    if not isinstance(head, str) or not re.fullmatch(r"[a-z0-9]{4,64}", head):
        raise recovery.RecoveryError("Tenant backup migration head is invalid")
    tables = database.get("tables")
    counts = database.get("row_counts")
    if (
        not isinstance(tables, list)
        or not isinstance(counts, dict)
        or not tables
        or tables[0] != "organizations"
        or any(not isinstance(item, str) for item in tables)
    ):
        raise recovery.RecoveryError("Tenant table manifest is invalid")
    normalized_tables = [_identifier(str(item)) for item in tables]
    if len(normalized_tables) != len(set(normalized_tables)):
        raise recovery.RecoveryError("Tenant table manifest contains duplicates")
    if set(counts) != set(normalized_tables):
        raise recovery.RecoveryError("Tenant row-count manifest is incomplete")
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise recovery.RecoveryError("Tenant row-count manifest is invalid")
    if counts.get("organizations") != 1:
        raise recovery.RecoveryError("Tenant backup must contain one organization row")
    if database.get("format") != "postgresql-custom-data-only-column-inserts":
        raise recovery.RecoveryError("Tenant PostgreSQL dump format is invalid")
    if database.get("row_count") != sum(int(value) for value in counts.values()):
        raise recovery.RecoveryError("Tenant database row total is invalid")
    if database.get("relationship_violations") != []:
        raise recovery.RecoveryError("Tenant relationship proof is invalid")
    snapshot = database.get("snapshot")
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("isolation") != "repeatable_read"
        or snapshot.get("exported") is not True
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(snapshot.get("snapshot_id_sha256", "")),
        )
    ):
        raise recovery.RecoveryError("Tenant exported-snapshot proof is invalid")
    security_inventory = database.get("security_inventory")
    security_sha256 = str(database.get("security_inventory_sha256", ""))
    if (
        not isinstance(security_inventory, dict)
        or not re.fullmatch(r"[0-9a-f]{64}", security_sha256)
        or hashlib.sha256(recovery.canonical_json(security_inventory)).hexdigest()
        != security_sha256
        or security_inventory.get("tables") != normalized_tables
    ):
        raise recovery.RecoveryError("Tenant pg_catalog security proof is invalid")
    security_relations = security_inventory.get("relations")
    if not isinstance(security_relations, list) or {
        str(item.get("name", ""))
        for item in security_relations
        if isinstance(item, dict)
    } != set(normalized_tables):
        raise recovery.RecoveryError("Tenant pg_catalog relation proof is incomplete")
    for relation in security_relations:
        if (
            not isinstance(relation, dict)
            or relation.get("rls_enabled") is not True
            or relation.get("rls_forced") is not True
            or not isinstance(relation.get("policies"), list)
            or len(relation["policies"]) != 1
        ):
            raise recovery.RecoveryError("Tenant pg_catalog relation proof is unsafe")
    dump_proof = database.get("dump_tenant_proof")
    if (
        not isinstance(dump_proof, dict)
        or dump_proof.get("verified") is not True
        or dump_proof.get("organization_id") != str(organization_id)
        or dump_proof.get("row_counts") != counts
        or dump_proof.get("row_count") != database.get("row_count")
    ):
        raise recovery.RecoveryError("Tenant PostgreSQL row-isolation proof is invalid")

    manifest_entries = objects.get("entries")
    if not isinstance(manifest_entries, list):
        raise recovery.RecoveryError("Tenant private-object manifest is invalid")
    object_source = objects.get("source")
    if object_source not in {"local", "s3"}:
        raise recovery.RecoveryError("Tenant private-object source is invalid")
    expected_keys: set[str] = set()
    projected: list[dict[str, object]] = []
    expected_prefix = f"private/{organization_id}/"
    for item in manifest_entries:
        if not isinstance(item, dict):
            raise recovery.RecoveryError("Tenant private-object manifest is invalid")
        key = _safe_object_key(str(item.get("key", "")))
        kind = item.get("kind")
        if kind not in {"registry", "legacy"}:
            raise recovery.RecoveryError("Tenant private-object kind is invalid")
        category = str(item.get("category", ""))
        if not IDENTIFIER.fullmatch(category):
            raise recovery.RecoveryError("Tenant private-object category is invalid")
        if kind == "registry":
            parts = PurePosixPath(key).parts
            if (
                not key.startswith(expected_prefix)
                or len(parts) != 4
                or parts[2] != category
            ):
                raise recovery.RecoveryError(
                    "Tenant backup contains a cross-tenant private-object key"
                )
        else:
            _validate_legacy_key_namespace(
                key,
                organization_id=organization_id,
                category=category,
                source_kind=str(object_source),
            )
        if key in expected_keys:
            raise recovery.RecoveryError(
                "Tenant private-object manifest contains duplicate keys"
            )
        expected_keys.add(key)
        sha256 = str(item.get("sha256", ""))
        size = item.get("bytes")
        if (
            not re.fullmatch(r"[0-9a-f]{64}", sha256)
            or type(size) is not int
            or size < 0
        ):
            raise recovery.RecoveryError("Tenant private-object digest is invalid")
        reference = item.get("database_reference")
        if not isinstance(reference, str) or not reference:
            raise recovery.RecoveryError("Tenant private-object reference is invalid")
        if not Path(reference).is_absolute() and reference != key:
            raise recovery.RecoveryError(
                "Tenant legacy object key/reference mapping is ambiguous"
            )
        projected.append({"key": key, "sha256": sha256, "bytes": size})
    actual = _manifest_objects(extracted)
    if projected != actual:
        raise recovery.RecoveryError(
            "Tenant private-object inventory does not match the manifest"
        )
    if objects.get("count") != len(projected) or objects.get("bytes") != sum(
        int(row["bytes"]) for row in projected
    ):
        raise recovery.RecoveryError("Tenant private-object totals are invalid")
    return manifest


def _load_tenant_receipt(
    archive: Path,
    receipt_path: Path,
    environ: Mapping[str, str],
) -> dict[str, object]:
    try:
        receipt = json.loads(receipt_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise recovery.RecoveryError(
            "Tenant backup receipt is missing or invalid"
        ) from exc
    if not isinstance(receipt, dict) or receipt.get("format") != TENANT_RECEIPT_VERSION:
        raise recovery.RecoveryError("Tenant backup receipt format is unsupported")
    recovery.verify_signed_payload(receipt, recovery.read_hmac_key(environ))
    sha256, size = recovery.sha256_file(archive)
    if sha256 != receipt.get("ciphertext_sha256") or size != receipt.get(
        "ciphertext_bytes"
    ):
        raise recovery.RecoveryError("Tenant encrypted backup checksum is invalid")
    return receipt


def tenant_backup(
    args: argparse.Namespace,
    environ: Mapping[str, str],
) -> dict[str, object]:
    organization_id = _organization_id(args.organization_id)
    if args.confirm_organization_id != str(organization_id):
        raise recovery.RecoveryError(
            "--confirm-organization-id must exactly match the source organization"
        )
    operator_id = _operator(args.operator_id, label="Operator ID")
    ticket_sha256 = _ticket_digest(args.authorization_ticket)
    if not args.maintenance_window.strip():
        raise recovery.RecoveryError("A maintenance-window identifier is required")
    if not 1 <= args.retention_days <= MAX_RETENTION_DAYS:
        raise recovery.RecoveryError("Retention must be between 1 and 35 days")
    output_dir = recovery.validate_bounded_absolute_path(
        args.output_dir,
        label="tenant backup output",
    )
    public_key = recovery.validate_bounded_absolute_path(
        args.public_key,
        label="tenant recovery public key",
        must_exist=True,
    )
    local_root: Path | None = None
    if args.object_source == "local":
        if args.local_object_root is None:
            raise recovery.RecoveryError("--local-object-root is required")
        local_root = recovery.validate_bounded_absolute_path(
            args.local_object_root,
            label="tenant local object source",
            must_exist=True,
        )
    else:
        recovery.require_env("HR_TENANT_BACKUP_S3_BUCKET", environ)
        recovery._aws_endpoint_arguments(environ)

    source = recovery.postgres_connection_from_env(args.database_url_env, environ)
    scoped = _scoped_connection(source, organization_id)
    runner = recovery.CommandRunner(environ=environ)
    base_pg = TenantPgTools(runner, scoped, args.pg_container)
    source_role = _source_role_contract(base_pg, organization_id)
    hmac_key = None if args.dry_run else recovery.read_hmac_key(environ)
    started = recovery.utc_now()
    temporary_root: Path | None = None
    archive: Path | None = None
    receipt_path: Path | None = None
    archive_temporary: Path | None = None
    archive_published = False
    receipt_published = False
    try:
        with base_pg.exported_snapshot() as snapshot_id:
            pg = base_pg.at_snapshot(snapshot_id)
            migration_head = _migration_head(pg)
            security_inventory = tenant_security_inventory(
                pg,
                require_backup_grants=True,
            )
            if security_inventory.get("current_role") != source_role:
                raise recovery.RecoveryError(
                    "Tenant backup role changed inside the exported snapshot"
                )
            tables = list(security_inventory["tables"])  # type: ignore[arg-type]
            inventory = tenant_inventory(pg, organization_id, tables)
            if inventory.get("organizations") != 1:
                raise recovery.RecoveryError("Source organization is unavailable")
            violations = relationship_violations(pg, organization_id, tables)
            if violations:
                raise recovery.RecoveryError(
                    "Source tenant contains cross-tenant relationship violations: "
                    + ", ".join(violations[:10])
                )
            contracts = object_contracts(
                pg,
                organization_id,
                local_root=local_root,
                source_kind=args.object_source,
                tenant_table_names=tables,
            )
            snapshot_sha256 = hashlib.sha256(snapshot_id.encode("ascii")).hexdigest()
            plan = {
                "action": "tenant-backup",
                "organization_id": str(organization_id),
                "source_database": source.safe_label,
                "source_role": source_role,
                "migration_head": migration_head,
                "table_count": len(tables),
                "row_count": sum(inventory.values()),
                "object_count": len(contracts),
                "object_source": args.object_source,
                "operator_id": operator_id,
                "authorization_ticket_sha256": ticket_sha256,
                "maintenance_window": args.maintenance_window,
                "output_directory": str(output_dir),
                "snapshot_isolation": "repeatable_read_exported",
                "snapshot_id_sha256": snapshot_sha256,
                "dry_run": bool(args.dry_run),
            }
            if args.dry_run:
                return plan

            assert hmac_key is not None
            output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            backup_id = _tenant_backup_id(started)
            temporary_root = Path(
                tempfile.mkdtemp(prefix=".hr-tenant-", dir=output_dir)
            )
            os.chmod(temporary_root, 0o700)
            archive = output_dir / f"{backup_id}.hrt"
            receipt_path = output_dir / f"{backup_id}.receipt.json"
            if (
                archive.exists()
                or archive.is_symlink()
                or receipt_path.exists()
                or receipt_path.is_symlink()
            ):
                raise recovery.RecoveryError(
                    "Tenant backup ID collides with an existing versioned output"
                )
            archive_temporary = output_dir / (
                f".{backup_id}.{secrets.token_hex(8)}.hrt.tmp"
            )
            dump = temporary_root / "tenant.dump"
            pg.dump_tenant(dump, tables)
            dump_proof = verify_tenant_dump_rows(
                pg.render_dump(dump),
                organization_id=organization_id,
                tables=tables,
                expected_counts=inventory,
            )
            dump_sha256, dump_bytes = recovery.sha256_file(dump)
            object_entries = _capture_objects(
                contracts=contracts,
                destination=temporary_root / "objects",
                source_kind=args.object_source,
                local_root=local_root,
                runner=runner,
                environ=environ,
            )
            completed = recovery.utc_now()
            security_sha256 = hashlib.sha256(
                recovery.canonical_json(security_inventory)
            ).hexdigest()
            manifest: dict[str, object] = {
                "format": TENANT_FORMAT_VERSION,
                "backup_id": backup_id,
                "organization_id": str(organization_id),
                "started_at": recovery.iso_utc(started),
                "completed_at": recovery.iso_utc(completed),
                "recovery_point_at": recovery.iso_utc(started),
                "expires_at": recovery.iso_utc(
                    completed + timedelta(days=args.retention_days)
                ),
                "retention_days": args.retention_days,
                "maintenance_window": args.maintenance_window,
                "operator": {
                    "id": operator_id,
                    "authorization_ticket_sha256": ticket_sha256,
                },
                "database": {
                    "format": "postgresql-custom-data-only-column-inserts",
                    "migration_head": migration_head,
                    "source_role": source_role,
                    "row_security": "forced_non_bypass",
                    "snapshot": {
                        "isolation": "repeatable_read",
                        "exported": True,
                        "snapshot_id_sha256": snapshot_sha256,
                    },
                    "security_inventory": security_inventory,
                    "security_inventory_sha256": security_sha256,
                    "tables": tables,
                    "row_counts": inventory,
                    "row_count": sum(inventory.values()),
                    "dump_tenant_proof": dump_proof,
                    "sha256": dump_sha256,
                    "bytes": dump_bytes,
                    "relationship_violations": [],
                },
                "objects": {
                    "source": args.object_source,
                    "count": len(object_entries),
                    "bytes": sum(int(row["bytes"]) for row in object_entries),
                    "entries": object_entries,
                },
            }
            recovery.write_json_atomic(temporary_root / "manifest.json", manifest)
            bundle = temporary_root / "tenant-bundle.tar"
            _make_tenant_tar(temporary_root, bundle)
            fingerprint = recovery.encrypt_archive(
                bundle,
                archive_temporary,
                public_key,
            )
            _publish_no_clobber(archive_temporary, archive)
            archive_published = True
            cipher_sha256, cipher_bytes = recovery.sha256_file(archive)
            receipt: dict[str, object] = {
                "format": TENANT_RECEIPT_VERSION,
                "backup_id": backup_id,
                "organization_id": str(organization_id),
                "created_at": recovery.iso_utc(completed),
                "recovery_point_at": manifest["recovery_point_at"],
                "expires_at": manifest["expires_at"],
                "operator_id": operator_id,
                "authorization_ticket_sha256": ticket_sha256,
                "ciphertext_sha256": cipher_sha256,
                "ciphertext_bytes": cipher_bytes,
                "encryption": "RSA-OAEP-SHA256+AES-256-GCM",
                "recipient_fingerprint_sha256": fingerprint,
                "table_count": len(tables),
                "row_count": sum(inventory.values()),
                "object_count": len(object_entries),
                "object_bytes": manifest["objects"]["bytes"],  # type: ignore[index]
                "snapshot_id_sha256": snapshot_sha256,
            }
            receipt["hmac_sha256"] = recovery.sign_payload(receipt, hmac_key)
            _write_json_no_clobber(receipt_path, receipt)
            receipt_published = True
            return {
                **plan,
                "dry_run": False,
                "backup_id": backup_id,
                "archive": str(archive),
                "receipt": str(receipt_path),
                "ciphertext_sha256": cipher_sha256,
                "table_count": len(tables),
                "row_count": sum(inventory.values()),
                "object_count": len(object_entries),
                "object_bytes": manifest["objects"]["bytes"],  # type: ignore[index]
                "dump_tenant_verified": True,
                "duration_seconds": round((completed - started).total_seconds(), 3),
            }
    except Exception:
        if receipt_published and receipt_path is not None:
            receipt_path.unlink(missing_ok=True)
        if archive_published and archive is not None:
            archive.unlink(missing_ok=True)
        if archive_temporary is not None:
            archive_temporary.unlink(missing_ok=True)
        raise
    finally:
        if temporary_root is not None:
            shutil.rmtree(temporary_root, ignore_errors=True)


def _target_is_empty(
    pg: TenantPgTools,
    organization_id: UUID,
    tables: Sequence[str],
) -> None:
    existing = tenant_inventory(pg, organization_id, tables)
    nonempty = {table: count for table, count in existing.items() if count}
    if nonempty:
        raise recovery.RecoveryError(
            "Target tenant must be absent and empty before restore"
        )


def _manifest_object_entries(manifest: Mapping[str, object]) -> list[dict[str, object]]:
    objects = manifest.get("objects")
    if not isinstance(objects, dict) or not isinstance(objects.get("entries"), list):
        raise recovery.RecoveryError("Tenant private-object manifest is invalid")
    entries = objects["entries"]
    if any(not isinstance(item, dict) for item in entries):
        raise recovery.RecoveryError("Tenant private-object manifest is invalid")
    return entries  # type: ignore[return-value]


def _local_target_preflight(
    root: Path,
    entries: Sequence[Mapping[str, object]],
) -> Path:
    target = recovery.validate_bounded_absolute_path(
        root,
        label="tenant restore local object root",
    )
    if target.exists() and (not target.is_dir() or target.is_symlink()):
        raise recovery.RecoveryError("Tenant restore object root is invalid")
    for entry in entries:
        path = _object_destination(target, str(entry["key"]))
        if path.exists() or path.is_symlink():
            raise recovery.RecoveryError(
                "Target already contains a tenant private-object key"
            )
    return target


def _s3_list_exact_versions(
    *,
    bucket: str,
    key: str,
    runner: recovery.CommandRunner,
    environ: Mapping[str, str],
) -> list[tuple[str, str]]:
    payload = recovery._aws_json(
        runner.run(
            [
                "aws",
                "s3api",
                "list-object-versions",
                "--bucket",
                bucket,
                "--prefix",
                key,
                "--output",
                "json",
                *recovery._aws_endpoint_arguments(environ),
            ],
            label="Tenant target object-version inspection",
        ),
        label="Tenant target object-version inspection",
    )
    found: list[tuple[str, str]] = []
    for field in ("Versions", "DeleteMarkers"):
        values = payload.get(field, [])
        if not isinstance(values, list):
            raise recovery.RecoveryError(
                "Tenant target object-version inventory is invalid"
            )
        for item in values:
            if not isinstance(item, dict):
                raise recovery.RecoveryError(
                    "Tenant target object-version inventory is invalid"
                )
            item_key = item.get("Key")
            version_id = item.get("VersionId")
            if item_key == key:
                if not isinstance(version_id, str) or not version_id:
                    raise recovery.RecoveryError(
                        "Tenant target object version ID is invalid"
                    )
                found.append((str(item_key), version_id))
    return found


def _s3_target_preflight(
    *,
    bucket: str,
    entries: Sequence[Mapping[str, object]],
    runner: recovery.CommandRunner,
    environ: Mapping[str, str],
) -> None:
    recovery._s3_uri(bucket)
    recovery._require_s3_bucket_versioning(
        bucket=bucket,
        runner=runner,
        environ=environ,
    )
    for entry in entries:
        key = _safe_object_key(str(entry["key"]))
        if _s3_list_exact_versions(
            bucket=bucket,
            key=key,
            runner=runner,
            environ=environ,
        ):
            raise recovery.RecoveryError(
                "Target already contains a tenant private-object version"
            )


def _restore_local_objects(
    *,
    source: Path,
    target: Path,
    entries: Sequence[Mapping[str, object]],
) -> list[RestoredLocalObject]:
    created: list[RestoredLocalObject] = []
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        for entry in entries:
            key = _safe_object_key(str(entry["key"]))
            incoming = _object_destination(source, key)
            destination = _object_destination(target, key)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with (
                incoming.open("rb") as source_handle,
                destination.open("xb") as target_handle,
            ):
                os.chmod(destination, 0o600)
                metadata = os.fstat(target_handle.fileno())
                created.append(
                    RestoredLocalObject(
                        path=destination,
                        device=metadata.st_dev,
                        inode=metadata.st_ino,
                        sha256=None,
                        bytes=None,
                    )
                )
                shutil.copyfileobj(
                    source_handle,
                    target_handle,
                    recovery.COPY_CHUNK_BYTES,
                )
                target_handle.flush()
                os.fsync(target_handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise recovery.RecoveryError(
                    "Restored tenant private object is not a regular file"
                )
            sha256, size = recovery.sha256_file(destination)
            created[-1] = RestoredLocalObject(
                path=destination,
                device=metadata.st_dev,
                inode=metadata.st_ino,
                sha256=sha256,
                bytes=size,
            )
            if sha256 != entry.get("sha256") or size != entry.get("bytes"):
                raise recovery.RecoveryError(
                    "Restored tenant private-object checksum is invalid"
                )
    except Exception:
        _rollback_local_objects(created, target)
        raise
    return created


def _rollback_local_objects(
    created: Sequence[RestoredLocalObject],
    root: Path,
) -> None:
    failures = 0
    for item in reversed(created):
        try:
            metadata = item.path.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_dev != item.device
            or metadata.st_ino != item.inode
        ):
            failures += 1
            continue
        if item.sha256 is not None:
            sha256, size = recovery.sha256_file(item.path)
            if sha256 != item.sha256 or size != item.bytes:
                failures += 1
                continue
        item.path.unlink()
    for path in sorted(
        (item for item in root.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        try:
            path.rmdir()
        except OSError:
            pass
    if failures:
        raise recovery.RecoveryError(
            "Tenant restore refused to delete local objects it no longer owns"
        )


def _restore_s3_objects(
    *,
    source: Path,
    bucket: str,
    entries: Sequence[Mapping[str, object]],
    operation_id: str,
    runner: recovery.CommandRunner,
    environ: Mapping[str, str],
) -> list[RestoredS3Version]:
    restored: list[RestoredS3Version] = []
    verification_root = Path(tempfile.mkdtemp(prefix="hr-tenant-object-verify-"))
    os.chmod(verification_root, 0o700)
    try:
        for entry in entries:
            key = _safe_object_key(str(entry["key"]))
            incoming = _object_destination(source, key)
            result = recovery._aws_json(
                runner.run(
                    [
                        "aws",
                        "s3api",
                        "put-object",
                        "--bucket",
                        bucket,
                        "--key",
                        key,
                        "--body",
                        str(incoming),
                        "--metadata",
                        f"hr-tenant-recovery-operation={operation_id}",
                        "--output",
                        "json",
                        *recovery._aws_endpoint_arguments(environ),
                    ],
                    label="Tenant private-object restore",
                ),
                label="Tenant private-object restore",
            )
            version_id = result.get("VersionId")
            if not isinstance(version_id, str) or not version_id:
                raise recovery.RecoveryError(
                    "Tenant private-object restore did not return a version ID"
                )
            restored.append(
                RestoredS3Version(
                    key=key,
                    version_id=version_id,
                    operation_id=operation_id,
                )
            )
            verification = _object_destination(verification_root, key)
            verification.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            runner.run(
                [
                    "aws",
                    "s3api",
                    "get-object",
                    "--bucket",
                    bucket,
                    "--key",
                    key,
                    "--version-id",
                    version_id,
                    str(verification),
                    *recovery._aws_endpoint_arguments(environ),
                ],
                label="Tenant restored-object verification",
            )
            sha256, size = recovery.sha256_file(verification)
            if sha256 != entry.get("sha256") or size != entry.get("bytes"):
                raise recovery.RecoveryError(
                    "Restored tenant private-object checksum is invalid"
                )
            verification.unlink()
    except Exception:
        _rollback_s3_objects(restored, bucket, runner, environ)
        raise
    finally:
        shutil.rmtree(verification_root, ignore_errors=True)
    return restored


def _rollback_s3_objects(
    restored: Sequence[RestoredS3Version],
    bucket: str,
    runner: recovery.CommandRunner,
    environ: Mapping[str, str],
) -> None:
    failures = 0
    for item in reversed(restored):
        try:
            head = recovery._aws_json(
                runner.run(
                    [
                        "aws",
                        "s3api",
                        "head-object",
                        "--bucket",
                        bucket,
                        "--key",
                        item.key,
                        "--version-id",
                        item.version_id,
                        "--output",
                        "json",
                        *recovery._aws_endpoint_arguments(environ),
                    ],
                    label="Tenant private-object rollback ownership check",
                ),
                label="Tenant private-object rollback ownership check",
            )
            metadata = head.get("Metadata")
            if (
                not isinstance(metadata, dict)
                or metadata.get("hr-tenant-recovery-operation") != item.operation_id
            ):
                failures += 1
                continue
            runner.run(
                [
                    "aws",
                    "s3api",
                    "delete-object",
                    "--bucket",
                    bucket,
                    "--key",
                    item.key,
                    "--version-id",
                    item.version_id,
                    *recovery._aws_endpoint_arguments(environ),
                ],
                label="Tenant private-object rollback",
            )
        except recovery.RecoveryError:
            failures += 1
    if failures:
        raise recovery.RecoveryError(
            "Tenant restore could not roll back all owned object versions"
        )


def _rollback_tenant_database(
    pg: TenantPgTools,
    organization_id: UUID,
    tables: Sequence[str],
    restore_lock: TenantRestoreLock,
    operation_id: str,
) -> None:
    restore_lock.assert_owned(operation_id)
    restore_lock.delete_owned_tenant(organization_id)
    _target_is_empty(pg, organization_id, tables)


def _write_target_audit(
    pg: TenantPgTools,
    *,
    organization_id: UUID,
    operator_id: str,
    ticket_sha256: str,
    backup_id: str,
    object_count: int,
    manifest_sha256: str,
    operation_id: str,
) -> UUID:
    request_id = uuid4()
    metadata = json.dumps(
        {
            "operator_id": operator_id,
            "authorization_ticket_sha256": ticket_sha256,
            "backup_id": backup_id,
            "object_count": object_count,
            "manifest_sha256": manifest_sha256,
            "restore_operation_id": operation_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    pg.query(
        "INSERT INTO public.audit_logs "
        "(id, organization_id, actor_user_id, actor_email_snapshot, action, "
        "resource_type, resource_id, path_template, method, status_code, "
        "request_id, metadata, occurred_at) VALUES ("
        f"gen_random_uuid(), '{organization_id}', NULL, NULL, "
        "'operator.tenant_restore', 'organization', "
        f"'{organization_id}', 'operator://tenant-restore', 'CLI', 201, "
        f"'{request_id}', {_sql_literal(metadata)}::jsonb, clock_timestamp()) "
        "RETURNING id"
    )
    return request_id


def tenant_restore(
    args: argparse.Namespace,
    environ: Mapping[str, str],
) -> dict[str, object]:
    organization_id = _organization_id(args.organization_id)
    if args.confirm_organization_id != str(organization_id):
        raise recovery.RecoveryError(
            "--confirm-organization-id must exactly match the restored organization"
        )
    operator_id = _operator(args.operator_id, label="Operator ID")
    ticket_sha256 = _ticket_digest(args.authorization_ticket)
    target_connection = recovery.postgres_connection_from_env(
        args.database_url_env,
        environ,
    )
    if args.confirm_target_database != target_connection.database:
        raise recovery.RecoveryError(
            "--confirm-target-database must exactly match the target database"
        )
    archive = recovery.validate_bounded_absolute_path(
        args.archive,
        label="tenant encrypted backup",
        must_exist=True,
    )
    receipt_path = recovery.validate_bounded_absolute_path(
        args.receipt,
        label="tenant backup receipt",
        must_exist=True,
    )
    private_key = recovery.validate_bounded_absolute_path(
        args.private_key,
        label="tenant recovery private key",
        must_exist=True,
    )
    evidence_path = recovery.validate_bounded_absolute_path(
        args.evidence,
        label="tenant restore evidence",
    )
    if evidence_path.suffix != ".json":
        raise recovery.RecoveryError("Tenant restore evidence must be versioned JSON")
    if evidence_path.exists() or evidence_path.is_symlink():
        raise recovery.RecoveryError(
            "Tenant restore evidence already exists; choose a new versioned path"
        )
    receipt = _load_tenant_receipt(archive, receipt_path, environ)
    if receipt.get("organization_id") != str(organization_id):
        raise recovery.RecoveryError(
            "Tenant backup receipt belongs to another organization"
        )

    runner = recovery.CommandRunner(environ=environ)
    target_scoped = _scoped_connection(target_connection, organization_id)
    pg = TenantPgTools(runner, target_scoped, args.pg_container)
    temporary_root = Path(tempfile.mkdtemp(prefix="hr-tenant-restore-"))
    os.chmod(temporary_root, 0o700)
    local_created: list[RestoredLocalObject] = []
    s3_created: list[RestoredS3Version] = []
    database_restore_attempted = False
    target_local_root: Path | None = None
    target_s3_bucket: str | None = None
    manifest: dict[str, object] | None = None
    manifest_sha256: str | None = None
    operation_id = secrets.token_hex(16)
    restore_lock: TenantRestoreLock | None = None
    started = recovery.utc_now()
    try:
        decrypted = temporary_root / "tenant-bundle.tar"
        fingerprint = recovery.decrypt_archive(
            archive,
            decrypted,
            private_key,
            environ,
        )
        if fingerprint != receipt.get("recipient_fingerprint_sha256"):
            raise recovery.RecoveryError(
                "Tenant recovery private key does not match the receipt"
            )
        extracted = temporary_root / "extracted"
        recovery._safe_extract_tar(decrypted, extracted)
        manifest = _verify_tenant_manifest(extracted)
        manifest_sha256, _manifest_bytes = recovery.sha256_file(
            extracted / "manifest.json"
        )
        if manifest.get("organization_id") != str(organization_id):
            raise recovery.RecoveryError(
                "Tenant backup manifest belongs to another organization"
            )
        if manifest.get("backup_id") != receipt.get("backup_id"):
            raise recovery.RecoveryError("Tenant backup manifest and receipt mismatch")
        expires_at = datetime.fromisoformat(
            str(manifest["expires_at"]).replace("Z", "+00:00")
        )
        if expires_at.tzinfo is None or expires_at < started:
            raise recovery.RecoveryError("Tenant backup has expired")

        restore_lock = pg.restore_lock(organization_id, operation_id)
        restore_lock.__enter__()

        database = manifest.get("database")
        if not isinstance(database, dict):
            raise recovery.RecoveryError("Tenant database manifest is invalid")
        tables = [str(item) for item in database.get("tables", [])]
        target_tables = tenant_tables(pg)
        if tables != target_tables:
            raise recovery.RecoveryError(
                "Target tenant-table contract differs from the backup"
            )
        if _migration_head(pg) != database.get("migration_head"):
            raise recovery.RecoveryError(
                "Target migration head differs from the tenant backup"
            )
        _target_is_empty(pg, organization_id, target_tables)
        empty_violations = relationship_violations(pg, organization_id, target_tables)
        if empty_violations:
            raise recovery.RecoveryError(
                "Target tenant relationship contract is incomplete: "
                + ", ".join(empty_violations[:10])
            )

        object_entries = _manifest_object_entries(manifest)
        if args.target_object_kind == "local":
            if args.restore_local_object_root is None:
                raise recovery.RecoveryError("--restore-local-object-root is required")
            target_local_root = _local_target_preflight(
                Path(args.restore_local_object_root),
                object_entries,
            )
            object_target = str(target_local_root)
            for item in object_entries:
                reference = str(item["database_reference"])
                if Path(reference).is_absolute():
                    expected = _object_destination(
                        target_local_root,
                        str(item["key"]),
                    )
                    if expected != Path(reference).resolve(strict=False):
                        raise recovery.RecoveryError(
                            "Absolute legacy object paths require the original root"
                        )
        else:
            target_s3_bucket = recovery.require_env(
                "HR_TENANT_RESTORE_S3_BUCKET",
                environ,
            )
            _s3_target_preflight(
                bucket=target_s3_bucket,
                entries=object_entries,
                runner=runner,
                environ=environ,
            )
            object_target = recovery._s3_uri(target_s3_bucket)
        if args.confirm_object_target != object_target:
            raise recovery.RecoveryError(
                "--confirm-object-target must exactly match the target object store"
            )

        plan = {
            "action": "tenant-restore",
            "backup_id": manifest["backup_id"],
            "organization_id": str(organization_id),
            "target_database": target_connection.safe_label,
            "object_target": object_target,
            "operator_id": operator_id,
            "authorization_ticket_sha256": ticket_sha256,
            "table_count": len(target_tables),
            "row_count": database["row_count"],
            "object_count": len(object_entries),
            "dry_run": bool(args.dry_run),
        }
        if args.dry_run:
            return plan

        if args.target_object_kind == "local":
            assert target_local_root is not None
            local_created = _restore_local_objects(
                source=extracted / "objects",
                target=target_local_root,
                entries=object_entries,
            )
        else:
            assert target_s3_bucket is not None
            s3_created = _restore_s3_objects(
                source=extracted / "objects",
                bucket=target_s3_bucket,
                entries=object_entries,
                operation_id=operation_id,
                runner=runner,
                environ=environ,
            )

        database_restore_attempted = True
        pg.restore_tenant(extracted / "tenant.dump")
        restored_inventory = tenant_inventory(pg, organization_id, target_tables)
        if restored_inventory != database.get("row_counts"):
            raise recovery.RecoveryError(
                "Restored tenant row counts do not match the manifest"
            )
        violations = relationship_violations(pg, organization_id, target_tables)
        if violations:
            raise recovery.RecoveryError(
                "Restored tenant contains cross-tenant relationship violations: "
                + ", ".join(violations[:10])
            )
        assert manifest_sha256 is not None
        audit_request_id = _write_target_audit(
            pg,
            organization_id=organization_id,
            operator_id=operator_id,
            ticket_sha256=ticket_sha256,
            backup_id=str(manifest["backup_id"]),
            object_count=len(object_entries),
            manifest_sha256=manifest_sha256,
            operation_id=operation_id,
        )
        completed = recovery.utc_now()
        evidence: dict[str, object] = {
            "format": TENANT_EVIDENCE_VERSION,
            "status": "pass",
            "backup_id": manifest["backup_id"],
            "organization_id": str(organization_id),
            "operator_id": operator_id,
            "authorization_ticket_sha256": ticket_sha256,
            "restore_operation_id": operation_id,
            "verified_at": recovery.iso_utc(completed),
            "target_database": target_connection.safe_label,
            "target_object_kind": args.target_object_kind,
            "manifest_sha256": manifest_sha256,
            "database": {
                "migration_head": database["migration_head"],
                "table_count": len(target_tables),
                "row_count": sum(restored_inventory.values()),
                "row_counts_match": True,
                "relationship_violations": [],
                "audit_request_id": str(audit_request_id),
            },
            "objects": {
                "count": len(object_entries),
                "bytes": manifest["objects"]["bytes"],  # type: ignore[index]
                "checksums_match": True,
                "owned_version_count": len(s3_created),
            },
            "source_mutated": False,
            "duration_seconds": round((completed - started).total_seconds(), 3),
        }
        evidence["hmac_sha256"] = recovery.sign_payload(
            evidence,
            recovery.read_hmac_key(environ),
        )
        _write_json_no_clobber(evidence_path, evidence)
        return {
            **plan,
            **evidence,
            "evidence": str(evidence_path),
            "dry_run": False,
        }
    except Exception as exc:
        rollback_failures: list[str] = []
        if (
            database_restore_attempted
            and manifest is not None
            and restore_lock is not None
        ):
            try:
                database = manifest.get("database")
                tables = (
                    database.get("tables", []) if isinstance(database, dict) else []
                )
                _rollback_tenant_database(
                    pg,
                    organization_id,
                    [str(item) for item in tables],
                    restore_lock,
                    operation_id,
                )
            except Exception:  # noqa: BLE001 - residue is reported fail-closed
                rollback_failures.append("tenant database rows")
        if local_created and target_local_root is not None:
            try:
                if restore_lock is None:
                    raise recovery.RecoveryError(
                        "Tenant restore operation lock was not established"
                    )
                restore_lock.assert_owned(operation_id)
                _rollback_local_objects(local_created, target_local_root)
            except Exception:  # noqa: BLE001 - residue is reported fail-closed
                rollback_failures.append("local private objects")
        if s3_created and target_s3_bucket is not None:
            try:
                if restore_lock is None:
                    raise recovery.RecoveryError(
                        "Tenant restore operation lock was not established"
                    )
                restore_lock.assert_owned(operation_id)
                _rollback_s3_objects(s3_created, target_s3_bucket, runner, environ)
            except Exception:  # noqa: BLE001 - residue is reported fail-closed
                rollback_failures.append("S3 private-object versions")
        if rollback_failures:
            raise recovery.RecoveryError(
                "Tenant restore failed and rollback left: "
                + ", ".join(rollback_failures)
            ) from exc
        raise
    finally:
        if restore_lock is not None:
            restore_lock.__exit__(None, None, None)
        shutil.rmtree(temporary_root, ignore_errors=True)


def verify_tenant_backup(
    args: argparse.Namespace,
    environ: Mapping[str, str],
) -> dict[str, object]:
    archive = recovery.validate_bounded_absolute_path(
        args.archive,
        label="tenant encrypted backup",
        must_exist=True,
    )
    receipt_path = recovery.validate_bounded_absolute_path(
        args.receipt,
        label="tenant backup receipt",
        must_exist=True,
    )
    private_key = recovery.validate_bounded_absolute_path(
        args.private_key,
        label="tenant recovery private key",
        must_exist=True,
    )
    receipt = _load_tenant_receipt(archive, receipt_path, environ)
    temporary_root = Path(tempfile.mkdtemp(prefix="hr-tenant-verify-"))
    os.chmod(temporary_root, 0o700)
    try:
        decrypted = temporary_root / "tenant-bundle.tar"
        fingerprint = recovery.decrypt_archive(
            archive,
            decrypted,
            private_key,
            environ,
        )
        if fingerprint != receipt.get("recipient_fingerprint_sha256"):
            raise recovery.RecoveryError(
                "Tenant recovery private key does not match the receipt"
            )
        extracted = temporary_root / "extracted"
        recovery._safe_extract_tar(decrypted, extracted)
        manifest = _verify_tenant_manifest(extracted)
        if manifest.get("backup_id") != receipt.get("backup_id"):
            raise recovery.RecoveryError("Tenant backup manifest and receipt mismatch")
        return {
            "status": "pass",
            "backup_id": manifest["backup_id"],
            "organization_id": manifest["organization_id"],
            "migration_head": manifest["database"]["migration_head"],  # type: ignore[index]
            "table_count": len(manifest["database"]["tables"]),  # type: ignore[index]
            "row_count": manifest["database"]["row_count"],  # type: ignore[index]
            "object_count": manifest["objects"]["count"],  # type: ignore[index]
            "object_bytes": manifest["objects"]["bytes"],  # type: ignore[index]
        }
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    backup_parser = commands.add_parser(
        "backup",
        help="create an encrypted tenant-only backup",
    )
    backup_parser.add_argument(
        "--database-url-env",
        default="HR_TENANT_BACKUP_DATABASE_URL",
    )
    backup_parser.add_argument("--pg-container")
    backup_parser.add_argument("--organization-id", required=True)
    backup_parser.add_argument("--confirm-organization-id", required=True)
    backup_parser.add_argument("--operator-id", required=True)
    backup_parser.add_argument("--authorization-ticket", required=True)
    backup_parser.add_argument("--maintenance-window", required=True)
    backup_parser.add_argument(
        "--object-source",
        choices=("local", "s3"),
        required=True,
    )
    backup_parser.add_argument("--local-object-root")
    backup_parser.add_argument("--output-dir", required=True)
    backup_parser.add_argument("--public-key", required=True)
    backup_parser.add_argument("--retention-days", type=int, default=35)
    backup_parser.add_argument("--dry-run", action="store_true")

    restore_parser = commands.add_parser(
        "restore",
        help="restore an authenticated tenant into an empty target",
    )
    restore_parser.add_argument(
        "--database-url-env",
        default="HR_TENANT_RESTORE_DATABASE_URL",
    )
    restore_parser.add_argument("--pg-container")
    restore_parser.add_argument("--archive", required=True)
    restore_parser.add_argument("--receipt", required=True)
    restore_parser.add_argument("--private-key", required=True)
    restore_parser.add_argument("--organization-id", required=True)
    restore_parser.add_argument("--confirm-organization-id", required=True)
    restore_parser.add_argument("--confirm-target-database", required=True)
    restore_parser.add_argument("--operator-id", required=True)
    restore_parser.add_argument("--authorization-ticket", required=True)
    restore_parser.add_argument(
        "--target-object-kind",
        choices=("local", "s3"),
        required=True,
    )
    restore_parser.add_argument("--restore-local-object-root")
    restore_parser.add_argument("--confirm-object-target", required=True)
    restore_parser.add_argument("--evidence", required=True)
    restore_parser.add_argument("--dry-run", action="store_true")

    verify_parser = commands.add_parser(
        "verify",
        help="authenticate and inspect a tenant backup",
    )
    verify_parser.add_argument("--archive", required=True)
    verify_parser.add_argument("--receipt", required=True)
    verify_parser.add_argument("--private-key", required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    values = dict(environ or os.environ)
    try:
        if args.command == "backup":
            result = tenant_backup(args, values)
        elif args.command == "restore":
            result = tenant_restore(args, values)
        else:
            result = verify_tenant_backup(args, values)
    except recovery.RecoveryError as exc:
        print(json.dumps({"status": "fail", "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
