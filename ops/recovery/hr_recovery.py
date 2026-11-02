"""Encrypted PostgreSQL + object-store backup and isolated restore drills.

The application-level ``/enterprise/backup`` route is a portability export,
not a disaster-recovery primitive.  This operator tool captures the complete
PostgreSQL cluster database plus every private-object byte into one
authenticated, encrypted archive.  Restore always creates an isolated target;
it never overwrites a named production database or object prefix.

Secrets are accepted only through environment variables.  Commands receive
PostgreSQL passwords through their environment, never argv, and evidence is
deliberately limited to identifiers, hashes, counts, and timings.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from urllib.parse import parse_qs, unquote, urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

FORMAT_VERSION = "hr-agent-recovery-v1"
RECEIPT_VERSION = "hr-agent-recovery-receipt-v1"
FILE_MAGIC = b"HRRECOVERY1\n"
GCM_TAG_BYTES = 16
COPY_CHUNK_BYTES = 1024 * 1024
DEFAULT_RETENTION_DAYS = 35
DEFAULT_MAX_RPO_SECONDS = 24 * 60 * 60
DEFAULT_MAX_RTO_SECONDS = 4 * 60 * 60
TARGET_MARKERS = ("restore", "recovery", "drill")
SAFE_DATABASE_NAME = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
PROTECTED_DATABASES = {"postgres", "template0", "template1"}
BACKUP_ID_PATTERN = re.compile(r"^hr-agent-\d{8}T\d{6}Z-[0-9a-f]{8}$")
REQUIRED_ROLE_ATTRIBUTES = {
    "hr_agent_broker": {
        "superuser": False,
        "create_role": False,
        "create_db": False,
        "login": False,
        "replication": False,
        "bypass_rls": True,
    },
    "hr_agent_runtime": {
        "superuser": False,
        "create_role": False,
        "create_db": False,
        "login": False,
        "replication": False,
        "bypass_rls": False,
    },
    "hr_agent_worker": {
        "superuser": False,
        "create_role": False,
        "create_db": False,
        "login": False,
        "replication": False,
        "bypass_rls": False,
    },
}


class RecoveryError(RuntimeError):
    """Safe operator-facing failure with no secret-bearing command text."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(COPY_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def write_json_atomic(path: Path, payload: object, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with temporary.open("xb") as handle:
            os.chmod(temporary, mode)
            handle.write(canonical_json(payload) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def require_env(name: str, environ: Mapping[str, str]) -> str:
    value = environ.get(name)
    if not value:
        raise RecoveryError(f"Required environment variable {name} is not set")
    return value


def read_hmac_key(environ: Mapping[str, str]) -> bytes:
    encoded = require_env("HR_RECOVERY_MANIFEST_HMAC_KEY", environ)
    try:
        key = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (ValueError, TypeError) as exc:
        raise RecoveryError(
            "HR_RECOVERY_MANIFEST_HMAC_KEY must be URL-safe base64"
        ) from exc
    if len(key) < 32:
        raise RecoveryError(
            "HR_RECOVERY_MANIFEST_HMAC_KEY must decode to at least 32 bytes"
        )
    return key


def sign_payload(payload: Mapping[str, object], key: bytes) -> str:
    unsigned = {name: value for name, value in payload.items() if name != "hmac_sha256"}
    return hmac.new(key, canonical_json(unsigned), hashlib.sha256).hexdigest()


def verify_signed_payload(payload: Mapping[str, object], key: bytes) -> None:
    supplied = payload.get("hmac_sha256")
    if not isinstance(supplied, str) or not hmac.compare_digest(
        supplied, sign_payload(payload, key)
    ):
        raise RecoveryError("Backup receipt authentication failed")


def validate_bounded_absolute_path(
    raw: str | Path,
    *,
    label: str,
    must_exist: bool = False,
) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise RecoveryError(f"{label} must be an absolute path")
    resolved = path.resolve(strict=must_exist)
    if resolved == Path(resolved.anchor) or len(resolved.parts) < 3:
        raise RecoveryError(f"{label} must be a bounded path below a filesystem root")
    return resolved


def validate_isolated_name(name: str, *, label: str) -> str:
    if not SAFE_DATABASE_NAME.fullmatch(name):
        raise RecoveryError(
            f"{label} must use lowercase letters, digits, and underscores"
        )
    if name in PROTECTED_DATABASES or not any(
        marker in name for marker in TARGET_MARKERS
    ):
        raise RecoveryError(
            f"{label} must be an isolated name containing restore, recovery, or drill"
        )
    return name


def validate_restore_directory(raw: str | Path) -> Path:
    target = validate_bounded_absolute_path(raw, label="restore object directory")
    if not any(marker in target.name.lower() for marker in TARGET_MARKERS):
        raise RecoveryError(
            "Restore object directory name must contain restore, recovery, or drill"
        )
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise RecoveryError("Restore object directory must be absent or empty")
    return target


@dataclass(frozen=True, slots=True)
class PostgresConnection:
    environment: dict[str, str]
    database: str
    safe_label: str


@dataclass(frozen=True, slots=True)
class S3RestoreOwnership:
    """Identity used to find only object versions written by one restore."""

    bucket: str
    prefix: str
    operation_id: str


def postgres_connection_from_env(
    env_name: str, environ: Mapping[str, str]
) -> PostgresConnection:
    raw = require_env(env_name, environ)
    parsed = urlsplit(raw)
    scheme = parsed.scheme.split("+", 1)[0]
    if scheme not in {"postgres", "postgresql"}:
        raise RecoveryError(f"{env_name} must be a PostgreSQL URL")
    database = unquote(parsed.path.lstrip("/"))
    if not database or "/" in database or not SAFE_DATABASE_NAME.fullmatch(database):
        raise RecoveryError(f"{env_name} must name one safe PostgreSQL database")
    if parsed.fragment:
        raise RecoveryError(f"{env_name} must not contain a URL fragment")
    query = parse_qs(parsed.query, strict_parsing=True)
    unsupported = set(query) - {"sslmode"}
    if unsupported:
        raise RecoveryError(
            f"{env_name} contains unsupported connection parameters: "
            + ", ".join(sorted(unsupported))
        )
    pg_environment = {
        "PGDATABASE": database,
        "PGAPPNAME": "hr-agent-recovery",
        "PGCONNECT_TIMEOUT": "15",
    }
    if parsed.hostname:
        pg_environment["PGHOST"] = parsed.hostname
    if parsed.port:
        pg_environment["PGPORT"] = str(parsed.port)
    if parsed.username:
        pg_environment["PGUSER"] = unquote(parsed.username)
    if parsed.password:
        pg_environment["PGPASSWORD"] = unquote(parsed.password)
    if query.get("sslmode"):
        pg_environment["PGSSLMODE"] = query["sslmode"][0]
    host_label = parsed.hostname or "local-socket"
    return PostgresConnection(
        environment=pg_environment,
        database=database,
        safe_label=f"{host_label}:{parsed.port or 5432}/{database}",
    )


class CommandRunner:
    """Small subprocess boundary that never renders argv or secret values."""

    def __init__(self, *, environ: Mapping[str, str] | None = None) -> None:
        self.base_environment = dict(environ or os.environ)

    def run(
        self,
        command: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | int | None = subprocess.PIPE,
        label: str,
    ) -> bytes:
        merged = dict(self.base_environment)
        merged.update(environment or {})
        try:
            completed = subprocess.run(
                list(command),
                env=merged,
                stdin=stdin,
                stdout=stdout,
                stderr=subprocess.PIPE,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RecoveryError(f"{label} tool is not installed") from exc
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            for name, value in merged.items():
                if value and any(
                    marker in name.upper()
                    for marker in ("PASSWORD", "SECRET", "TOKEN", "PRIVATE_KEY")
                ):
                    detail = detail.replace(value, "[redacted]")
            # Defense in depth if a provider happens to echo a credentialed URL.
            detail = re.sub(
                r"(postgres(?:ql)?://[^:\s/]+:)[^@\s]+@", r"\1[redacted]@", detail
            )
            detail = re.sub(
                r"(?i)(https?://[^:\s/@]+:)[^@\s/]+@",
                r"\1[redacted]@",
                detail,
            )
            suffix = f": {detail[:600]}" if detail else ""
            raise RecoveryError(
                f"{label} failed with exit code {completed.returncode}{suffix}"
            )
        return completed.stdout if isinstance(completed.stdout, bytes) else b""


@dataclass(frozen=True, slots=True)
class PgTools:
    runner: CommandRunner
    connection: PostgresConnection
    container: str | None = None

    def _command(self, executable: str, *arguments: str) -> list[str]:
        if self.container is None:
            return [executable, *arguments]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.container):
            raise RecoveryError("PostgreSQL container name is invalid")
        propagated = [
            item
            for name in (
                "PGDATABASE",
                "PGHOST",
                "PGPORT",
                "PGUSER",
                "PGPASSWORD",
                "PGSSLMODE",
                "PGAPPNAME",
                "PGCONNECT_TIMEOUT",
            )
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

    def query(self, sql: str, *, database: str | None = None) -> str:
        environment = dict(self.connection.environment)
        if database is not None:
            environment["PGDATABASE"] = database
        result = self.runner.run(
            self._command("psql", "-X", "-v", "ON_ERROR_STOP=1", "-Atqc", sql),
            environment=environment,
            label="PostgreSQL query",
        )
        return result.decode("utf-8", errors="replace").strip()

    def dump(self, destination: Path) -> None:
        with destination.open("xb") as output:
            os.chmod(destination, 0o600)
            self.runner.run(
                self._command(
                    "pg_dump",
                    "--format=custom",
                    "--compress=9",
                ),
                environment=self.connection.environment,
                stdout=output,
                label="PostgreSQL backup",
            )

    def database_exists(self, database: str) -> bool:
        # ``database`` has already passed SAFE_DATABASE_NAME, so this fixed
        # query cannot be extended into operator-controlled SQL.
        return (
            self.query(
                f"SELECT 1 FROM pg_database WHERE datname = '{database}'",
            )
            == "1"
        )

    def create_database(self, database: str) -> None:
        self.runner.run(
            self._command("createdb", database),
            environment=self.connection.environment,
            label="Isolated database creation",
        )

    def restore(self, source: Path, database: str) -> None:
        environment = dict(self.connection.environment)
        environment["PGDATABASE"] = database
        with source.open("rb") as dump:
            self.runner.run(
                self._command(
                    "pg_restore",
                    "--exit-on-error",
                    "--dbname",
                    database,
                ),
                environment=environment,
                stdin=dump,
                label="PostgreSQL restore",
            )

    def drop_database(self, database: str) -> None:
        self.runner.run(
            self._command("dropdb", "--if-exists", database),
            environment=self.connection.environment,
            label="Isolated database rollback",
        )


def required_role_contract(pg: PgTools) -> list[dict[str, object]]:
    role_names = ",".join(f"'{name}'" for name in sorted(REQUIRED_ROLE_ATTRIBUTES))
    raw = pg.query(
        "SELECT rolname || '|' || rolsuper::text || '|' || "
        "rolcreaterole::text || '|' || rolcreatedb::text || '|' || "
        "rolcanlogin::text || '|' || rolreplication::text || '|' || "
        "rolbypassrls::text FROM pg_catalog.pg_roles "
        f"WHERE rolname IN ({role_names}) ORDER BY rolname"
    )
    values: list[dict[str, object]] = []
    for line in raw.splitlines():
        parts = line.split("|")
        if len(parts) != 7:
            raise RecoveryError("PostgreSQL recovery-role inventory is invalid")
        name = parts[0]
        values.append(
            {
                "name": name,
                "superuser": parts[1] == "true",
                "create_role": parts[2] == "true",
                "create_db": parts[3] == "true",
                "login": parts[4] == "true",
                "replication": parts[5] == "true",
                "bypass_rls": parts[6] == "true",
            }
        )
    actual = {
        str(row["name"]): {k: v for k, v in row.items() if k != "name"}
        for row in values
    }
    if actual != REQUIRED_ROLE_ATTRIBUTES:
        raise RecoveryError(
            "PostgreSQL recovery roles are absent or do not match the fail-closed contract"
        )
    return values


def required_owner_roles(pg: PgTools) -> list[str]:
    raw = pg.query(
        "SELECT DISTINCT owner_name FROM ("
        "SELECT pg_get_userbyid(datdba) AS owner_name FROM pg_database "
        "WHERE datname = current_database() UNION ALL "
        "SELECT pg_get_userbyid(nspowner) FROM pg_namespace "
        "WHERE nspname !~ '^pg_' AND nspname <> 'information_schema' UNION ALL "
        "SELECT pg_get_userbyid(relowner) FROM pg_class c JOIN pg_namespace n "
        "ON n.oid = c.relnamespace WHERE n.nspname !~ '^pg_' "
        "AND n.nspname <> 'information_schema' UNION ALL "
        "SELECT pg_get_userbyid(proowner) FROM pg_proc p JOIN pg_namespace n "
        "ON n.oid = p.pronamespace WHERE n.nspname !~ '^pg_' "
        "AND n.nspname <> 'information_schema'"
        ") AS owners WHERE owner_name IS NOT NULL ORDER BY owner_name"
    )
    owners = [line for line in raw.splitlines() if line]
    if not owners:
        raise RecoveryError("PostgreSQL object-owner inventory is empty")
    return owners


def available_role_names(pg: PgTools) -> set[str]:
    return set(
        pg.query(
            "SELECT rolname FROM pg_catalog.pg_roles ORDER BY rolname"
        ).splitlines()
    )


def restored_security_contract(pg: PgTools, database: str) -> dict[str, object]:
    raw = pg.query(
        "SELECT "
        "(SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'security'), "
        "(SELECT relrowsecurity::text || '|' || relforcerowsecurity::text "
        "FROM pg_class WHERE oid = 'public.candidates'::regclass), "
        "has_schema_privilege('hr_agent_runtime', 'public', 'USAGE')::text, "
        "has_schema_privilege('hr_agent_worker', 'public', 'USAGE')::text",
        database=database,
    )
    parts = raw.split("|")
    if parts != ["hr_agent_broker", "true", "true", "true", "true"]:
        raise RecoveryError("Restored database RLS/ownership/grant verification failed")
    return {
        "security_schema_owner": parts[0],
        "candidate_rls_enabled": True,
        "candidate_rls_forced": True,
        "runtime_public_schema_usage": True,
        "worker_public_schema_usage": True,
    }


def _relative_object_entries(root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    if not root.exists():
        return entries
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RecoveryError(
                f"Object snapshot contains a symbolic link: {path.name}"
            )
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if (
            PurePosixPath(relative).is_absolute()
            or ".." in PurePosixPath(relative).parts
        ):
            raise RecoveryError("Object snapshot contains an unsafe key")
        digest, size = sha256_file(path)
        entries.append({"key": relative, "sha256": digest, "bytes": size})
    return entries


def _copy_local_objects(source: Path, destination: Path) -> None:
    source = validate_bounded_absolute_path(
        source, label="local object source", must_exist=True
    )
    if not source.is_dir():
        raise RecoveryError("Local object source must be a directory")
    destination.mkdir(parents=True, mode=0o700)
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise RecoveryError(
                f"Local object source contains a symbolic link: {path.name}"
            )
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_dir():
            target.mkdir(exist_ok=True, mode=0o700)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with path.open("rb") as source_handle, target.open("xb") as target_handle:
                os.chmod(target, 0o600)
                shutil.copyfileobj(source_handle, target_handle, COPY_CHUNK_BYTES)


def _s3_uri(bucket: str, prefix: str = "") -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise RecoveryError("S3 bucket name is invalid")
    clean = prefix.strip("/")
    if clean and (".." in PurePosixPath(clean).parts or "//" in clean):
        raise RecoveryError("S3 prefix is invalid")
    return f"s3://{bucket}/{clean}" if clean else f"s3://{bucket}"


def _aws_endpoint_arguments(environ: Mapping[str, str]) -> list[str]:
    endpoint = environ.get("HR_RECOVERY_S3_ENDPOINT", "").strip()
    if not endpoint:
        return []
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise RecoveryError(
            "HR_RECOVERY_S3_ENDPOINT must be an absolute HTTP(S) origin"
        ) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port is None and parsed.netloc.endswith(":")
    ):
        raise RecoveryError(
            "HR_RECOVERY_S3_ENDPOINT must be an absolute HTTP(S) origin "
            "without credentials, path, query, or fragment"
        )
    return ["--endpoint-url", endpoint]


def capture_objects(
    *,
    source_kind: str,
    destination: Path,
    runner: CommandRunner,
    environ: Mapping[str, str],
    local_root: str | None,
) -> tuple[str, list[dict[str, object]]]:
    if source_kind == "local":
        if local_root is None:
            raise RecoveryError("--local-object-root is required for local objects")
        source = validate_bounded_absolute_path(
            local_root, label="local object source", must_exist=True
        )
        _copy_local_objects(source, destination)
        label = f"local:{source.name}"
    else:
        bucket = require_env("HR_RECOVERY_S3_BUCKET", environ)
        prefix = environ.get("HR_RECOVERY_S3_PREFIX", "")
        uri = _s3_uri(bucket, prefix)
        destination.mkdir(parents=True, mode=0o700)
        runner.run(
            [
                "aws",
                "s3",
                "sync",
                uri,
                str(destination),
                "--only-show-errors",
                *_aws_endpoint_arguments(environ),
            ],
            label="S3 object snapshot",
        )
        label = f"s3://{bucket}/{prefix.strip('/')}"
    return label, _relative_object_entries(destination)


def _load_rsa_public_key(path: Path) -> rsa.RSAPublicKey:
    try:
        key = serialization.load_pem_public_key(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise RecoveryError(
            "Recovery public key is not a valid PEM public key"
        ) from exc
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 3072:
        raise RecoveryError("Recovery public key must be RSA with at least 3072 bits")
    return key


def _load_rsa_private_key(path: Path, environ: Mapping[str, str]) -> rsa.RSAPrivateKey:
    permissions = stat.S_IMODE(path.stat().st_mode)
    if permissions & 0o077:
        raise RecoveryError("Recovery private key permissions must be 0600 or stricter")
    passphrase = environ.get("HR_RECOVERY_PRIVATE_KEY_PASSPHRASE")
    try:
        key = serialization.load_pem_private_key(
            path.read_bytes(),
            password=passphrase.encode() if passphrase else None,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise RecoveryError("Recovery private key could not be loaded") from exc
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 3072:
        raise RecoveryError("Recovery private key must be RSA with at least 3072 bits")
    return key


def public_key_fingerprint(key: rsa.RSAPublicKey) -> str:
    der = key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()


def encrypt_archive(source: Path, destination: Path, public_key_path: Path) -> str:
    public_key = _load_rsa_public_key(public_key_path)
    content_key = secrets.token_bytes(32)
    nonce = secrets.token_bytes(12)
    wrapped_key = public_key.encrypt(
        content_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=b"hr-agent-recovery-v1",
        ),
    )
    header = canonical_json(
        {
            "content_alg": "AES-256-GCM",
            "key_alg": "RSA-OAEP-SHA256",
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "wrapped_key": base64.b64encode(wrapped_key).decode("ascii"),
        }
    )
    encryptor = Cipher(algorithms.AES(content_key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header)
    with source.open("rb") as input_handle, destination.open("xb") as output_handle:
        os.chmod(destination, 0o600)
        output_handle.write(FILE_MAGIC)
        output_handle.write(len(header).to_bytes(4, "big"))
        output_handle.write(header)
        while chunk := input_handle.read(COPY_CHUNK_BYTES):
            output_handle.write(encryptor.update(chunk))
        output_handle.write(encryptor.finalize())
        output_handle.write(encryptor.tag)
        output_handle.flush()
        os.fsync(output_handle.fileno())
    return public_key_fingerprint(public_key)


def decrypt_archive(
    source: Path,
    destination: Path,
    private_key_path: Path,
    environ: Mapping[str, str],
) -> str:
    private_key = _load_rsa_private_key(private_key_path, environ)
    total_size = source.stat().st_size
    with source.open("rb") as input_handle:
        if input_handle.read(len(FILE_MAGIC)) != FILE_MAGIC:
            raise RecoveryError("Encrypted backup file magic is invalid")
        header_length = int.from_bytes(input_handle.read(4), "big")
        if not 100 <= header_length <= 32 * 1024:
            raise RecoveryError("Encrypted backup header length is invalid")
        header = input_handle.read(header_length)
        try:
            header_value = json.loads(header)
            nonce = base64.b64decode(header_value["nonce"], validate=True)
            wrapped_key = base64.b64decode(header_value["wrapped_key"], validate=True)
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            raise RecoveryError("Encrypted backup header is invalid") from exc
        if (
            header_value.get("content_alg") != "AES-256-GCM"
            or header_value.get("key_alg") != "RSA-OAEP-SHA256"
        ):
            raise RecoveryError("Encrypted backup algorithms are unsupported")
        ciphertext_offset = len(FILE_MAGIC) + 4 + header_length
        ciphertext_length = total_size - ciphertext_offset - GCM_TAG_BYTES
        if ciphertext_length <= 0:
            raise RecoveryError("Encrypted backup payload is empty")
        input_handle.seek(total_size - GCM_TAG_BYTES)
        tag = input_handle.read(GCM_TAG_BYTES)
        input_handle.seek(ciphertext_offset)
        try:
            content_key = private_key.decrypt(
                wrapped_key,
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=b"hr-agent-recovery-v1",
                ),
            )
            decryptor = Cipher(
                algorithms.AES(content_key), modes.GCM(nonce, tag)
            ).decryptor()
            decryptor.authenticate_additional_data(header)
            remaining = ciphertext_length
            with destination.open("xb") as output_handle:
                os.chmod(destination, 0o600)
                while remaining:
                    chunk = input_handle.read(min(COPY_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise RecoveryError("Encrypted backup is truncated")
                    remaining -= len(chunk)
                    output_handle.write(decryptor.update(chunk))
                output_handle.write(decryptor.finalize())
                output_handle.flush()
                os.fsync(output_handle.fileno())
        except RecoveryError:
            raise
        except Exception as exc:
            destination.unlink(missing_ok=True)
            raise RecoveryError("Encrypted backup authentication failed") from exc
    return public_key_fingerprint(private_key.public_key())


def _safe_extract_tar(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, mode=0o700)
    with tarfile.open(archive, "r:") as bundle:
        members = bundle.getmembers()
        for member in members:
            member_path = PurePosixPath(member.name)
            if (
                member_path.is_absolute()
                or ".." in member_path.parts
                or member.issym()
                or member.islnk()
                or member.isdev()
            ):
                raise RecoveryError("Backup archive contains an unsafe entry")
        bundle.extractall(destination, members=members, filter="data")


def _verify_manifest(extracted: Path) -> dict[str, object]:
    manifest_path = extracted / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError("Backup manifest is missing or invalid") from exc
    if manifest.get("format") != FORMAT_VERSION:
        raise RecoveryError("Backup manifest format is unsupported")
    database = manifest.get("database")
    objects = manifest.get("objects")
    if not isinstance(database, dict) or not isinstance(objects, dict):
        raise RecoveryError("Backup manifest inventory is invalid")
    dump = extracted / "database.dump"
    digest, size = sha256_file(dump)
    if digest != database.get("sha256") or size != database.get("bytes"):
        raise RecoveryError("PostgreSQL dump checksum does not match the manifest")
    entries = _relative_object_entries(extracted / "objects")
    if entries != objects.get("entries"):
        raise RecoveryError("Private-object inventory does not match the manifest")
    if sum(int(row["bytes"]) for row in entries) != objects.get("bytes"):
        raise RecoveryError("Private-object byte count does not match the manifest")
    return manifest


def _make_tar(staging: Path, destination: Path) -> None:
    with tarfile.open(destination, "x:") as bundle:
        for name in ("manifest.json", "database.dump", "objects"):
            path = staging / name
            if path.exists():
                bundle.add(path, arcname=name, recursive=True)
    os.chmod(destination, 0o600)


def _backup_id(now: datetime) -> str:
    return f"hr-agent-{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"


def backup(args: argparse.Namespace, environ: Mapping[str, str]) -> dict[str, object]:
    database = postgres_connection_from_env(args.database_url_env, environ)
    output_dir = validate_bounded_absolute_path(args.output_dir, label="backup output")
    public_key_path = validate_bounded_absolute_path(
        args.public_key, label="recovery public key", must_exist=not args.dry_run
    )
    if not 1 <= args.retention_days <= DEFAULT_RETENTION_DAYS:
        raise RecoveryError("Retention must be between 1 and 35 days")
    if not args.maintenance_window.strip():
        raise RecoveryError("A maintenance-window identifier is required")
    plan = {
        "action": "backup",
        "database": database.safe_label,
        "object_source": args.object_source,
        "output_directory": str(output_dir),
        "retention_days": args.retention_days,
        "maintenance_window": args.maintenance_window,
        "dry_run": bool(args.dry_run),
    }
    if args.object_source == "local":
        if args.local_object_root is None:
            raise RecoveryError("--local-object-root is required for local objects")
        local_root = validate_bounded_absolute_path(
            args.local_object_root,
            label="local object source",
            must_exist=not args.dry_run,
        )
        plan["object_source_label"] = f"local:{local_root.name}"
    else:
        bucket = require_env("HR_RECOVERY_S3_BUCKET", environ)
        prefix = environ.get("HR_RECOVERY_S3_PREFIX", "")
        plan["object_source_label"] = _s3_uri(bucket, prefix)
        _aws_endpoint_arguments(environ)
    if args.dry_run:
        return plan

    hmac_key = read_hmac_key(environ)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    started = utc_now()
    backup_id = _backup_id(started)
    temporary_root = Path(tempfile.mkdtemp(prefix=".hr-recovery-", dir=output_dir))
    os.chmod(temporary_root, 0o700)
    encrypted_path = output_dir / f"{backup_id}.hrb"
    receipt_path = output_dir / f"{backup_id}.receipt.json"
    encrypted_temporary = output_dir / f".{backup_id}.hrb.tmp"
    try:
        runner = CommandRunner(environ=environ)
        pg = PgTools(runner, database, args.pg_container)
        source_clock = pg.query(
            "SELECT clock_timestamp() AT TIME ZONE 'UTC'",
        )
        role_contract = required_role_contract(pg)
        owner_roles = required_owner_roles(pg)
        dump_path = temporary_root / "database.dump"
        pg.dump(dump_path)
        dump_digest, dump_bytes = sha256_file(dump_path)
        object_label, object_entries = capture_objects(
            source_kind=args.object_source,
            destination=temporary_root / "objects",
            runner=runner,
            environ=environ,
            local_root=args.local_object_root,
        )
        completed = utc_now()
        manifest: dict[str, object] = {
            "format": FORMAT_VERSION,
            "backup_id": backup_id,
            "started_at": iso_utc(started),
            "completed_at": iso_utc(completed),
            "recovery_point_at": iso_utc(completed),
            "source_clock_utc": source_clock,
            "maintenance_window": args.maintenance_window,
            "retention_days": args.retention_days,
            "expires_at": iso_utc(completed + timedelta(days=args.retention_days)),
            "database": {
                "format": "postgresql-custom",
                "sha256": dump_digest,
                "bytes": dump_bytes,
                "required_roles": role_contract,
                "required_owner_roles": owner_roles,
            },
            "objects": {
                "source": object_label,
                "count": len(object_entries),
                "bytes": sum(int(row["bytes"]) for row in object_entries),
                "entries": object_entries,
            },
        }
        write_json_atomic(temporary_root / "manifest.json", manifest)
        tar_path = temporary_root / "bundle.tar"
        _make_tar(temporary_root, tar_path)
        fingerprint = encrypt_archive(tar_path, encrypted_temporary, public_key_path)
        encrypted_temporary.replace(encrypted_path)
        encrypted_digest, encrypted_bytes = sha256_file(encrypted_path)
        receipt: dict[str, object] = {
            "format": RECEIPT_VERSION,
            "backup_id": backup_id,
            "created_at": iso_utc(completed),
            "recovery_point_at": manifest["recovery_point_at"],
            "expires_at": manifest["expires_at"],
            "ciphertext_sha256": encrypted_digest,
            "ciphertext_bytes": encrypted_bytes,
            "encryption": "RSA-OAEP-SHA256+AES-256-GCM",
            "recipient_fingerprint_sha256": fingerprint,
        }
        receipt["hmac_sha256"] = sign_payload(receipt, hmac_key)
        write_json_atomic(receipt_path, receipt)
        return {
            **plan,
            "dry_run": False,
            "backup_id": backup_id,
            "archive": str(encrypted_path),
            "receipt": str(receipt_path),
            "database_bytes": dump_bytes,
            "object_count": len(object_entries),
            "object_bytes": manifest["objects"]["bytes"],  # type: ignore[index]
            "duration_seconds": round((completed - started).total_seconds(), 3),
        }
    except Exception:
        encrypted_path.unlink(missing_ok=True)
        receipt_path.unlink(missing_ok=True)
        encrypted_temporary.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


def _load_authenticated_receipt(
    archive: Path,
    receipt_path: Path,
    environ: Mapping[str, str],
) -> dict[str, object]:
    try:
        receipt = json.loads(receipt_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError("Backup receipt is missing or invalid") from exc
    if receipt.get("format") != RECEIPT_VERSION:
        raise RecoveryError("Backup receipt format is unsupported")
    verify_signed_payload(receipt, read_hmac_key(environ))
    digest, size = sha256_file(archive)
    if digest != receipt.get("ciphertext_sha256") or size != receipt.get(
        "ciphertext_bytes"
    ):
        raise RecoveryError("Encrypted backup checksum does not match its receipt")
    return receipt


def _copy_objects_to_local(source: Path, target: Path) -> None:
    # ``validate_restore_directory`` deliberately accepts either an absent
    # target or an operator-reserved empty directory (for example one created
    # with ``mktemp -d``). Recheck the latter immediately before writing so a
    # path-type swap or unexpected content fails closed.
    if target.exists():
        if target.is_symlink() or not target.is_dir() or any(target.iterdir()):
            raise RecoveryError("Restore object directory must be absent or empty")
        target.chmod(0o700)
    else:
        target.mkdir(parents=True, mode=0o700)
    for entry in sorted(source.rglob("*")):
        if entry.is_symlink():
            raise RecoveryError("Restored object inventory contains a symbolic link")
        relative = entry.relative_to(source)
        destination = target / relative
        if entry.is_dir():
            destination.mkdir(exist_ok=True, mode=0o700)
        elif entry.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with (
                entry.open("rb") as input_handle,
                destination.open("xb") as output_handle,
            ):
                os.chmod(destination, 0o600)
                shutil.copyfileobj(input_handle, output_handle, COPY_CHUNK_BYTES)


def _remove_validated_restore_directory(target: Path) -> None:
    validated = validate_bounded_absolute_path(target, label="restore object directory")
    if not any(marker in validated.name.lower() for marker in TARGET_MARKERS):
        raise RecoveryError("Refusing to remove a non-restore object directory")
    if validated.exists():
        shutil.rmtree(validated)


def _s3_restore_target(environ: Mapping[str, str]) -> tuple[str, str, str]:
    bucket = require_env("HR_RECOVERY_RESTORE_S3_BUCKET", environ)
    prefix = require_env("HR_RECOVERY_RESTORE_S3_PREFIX", environ).strip("/")
    if not prefix or not any(marker in prefix.lower() for marker in TARGET_MARKERS):
        raise RecoveryError(
            "Restore S3 prefix must be non-empty and contain restore, recovery, or drill"
        )
    return bucket, prefix, _s3_uri(bucket, prefix)


def _aws_json(payload: bytes | str, *, label: str) -> dict[str, object]:
    try:
        decoded = json.loads(payload or b"{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"{label} returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise RecoveryError(f"{label} returned an invalid payload")
    return decoded


def _require_s3_bucket_versioning(
    *, bucket: str, runner: CommandRunner, environ: Mapping[str, str]
) -> None:
    payload = _aws_json(
        runner.run(
            [
                "aws",
                "s3api",
                "get-bucket-versioning",
                "--bucket",
                bucket,
                "--output",
                "json",
                *_aws_endpoint_arguments(environ),
            ],
            label="S3 recovery-bucket versioning inspection",
        ),
        label="S3 recovery-bucket versioning inspection",
    )
    if payload.get("Status") != "Enabled":
        raise RecoveryError(
            "Restore S3 bucket must have versioning enabled for exact rollback"
        )


def _owned_s3_object_versions(
    *,
    ownership: S3RestoreOwnership,
    runner: CommandRunner,
    environ: Mapping[str, str],
) -> list[tuple[str, str]]:
    """Return immutable version IDs carrying this restore's random marker."""

    payload = _aws_json(
        runner.run(
            [
                "aws",
                "s3api",
                "list-object-versions",
                "--bucket",
                ownership.bucket,
                "--prefix",
                f"{ownership.prefix}/",
                "--output",
                "json",
                *_aws_endpoint_arguments(environ),
            ],
            label="S3 owned-version inventory",
        ),
        label="S3 owned-version inventory",
    )
    versions = payload.get("Versions", [])
    if not isinstance(versions, list):
        raise RecoveryError("S3 owned-version inventory returned an invalid payload")
    owned: list[tuple[str, str]] = []
    for item in versions:
        if not isinstance(item, dict):
            raise RecoveryError(
                "S3 owned-version inventory returned an invalid version"
            )
        key = item.get("Key")
        version_id = item.get("VersionId")
        if not isinstance(key, str) or not isinstance(version_id, str):
            raise RecoveryError(
                "S3 owned-version inventory omitted a key or version ID"
            )
        if not key.startswith(f"{ownership.prefix}/"):
            raise RecoveryError("S3 owned-version inventory escaped its prefix")
        head = _aws_json(
            runner.run(
                [
                    "aws",
                    "s3api",
                    "head-object",
                    "--bucket",
                    ownership.bucket,
                    "--key",
                    key,
                    "--version-id",
                    version_id,
                    "--output",
                    "json",
                    *_aws_endpoint_arguments(environ),
                ],
                label="S3 version ownership inspection",
            ),
            label="S3 version ownership inspection",
        )
        raw_metadata = head.get("Metadata", {})
        if not isinstance(raw_metadata, dict):
            raise RecoveryError(
                "S3 version ownership inspection returned invalid metadata"
            )
        metadata = {str(name).lower(): value for name, value in raw_metadata.items()}
        if metadata.get("hr-recovery-operation") == ownership.operation_id:
            owned.append((key, version_id))
    return owned


def _delete_owned_s3_object_versions(
    *,
    ownership: S3RestoreOwnership,
    runner: CommandRunner,
    environ: Mapping[str, str],
) -> None:
    """Delete exact versions from this run and preserve every foreign version."""

    for key, version_id in _owned_s3_object_versions(
        ownership=ownership, runner=runner, environ=environ
    ):
        runner.run(
            [
                "aws",
                "s3api",
                "delete-object",
                "--bucket",
                ownership.bucket,
                "--key",
                key,
                "--version-id",
                version_id,
                *_aws_endpoint_arguments(environ),
            ],
            label="S3 exact-version rollback",
        )
    if _owned_s3_object_versions(
        ownership=ownership, runner=runner, environ=environ
    ):
        raise RecoveryError("S3 exact-version rollback left owned object versions")


def _put_versioned_s3_objects(
    *,
    source: Path,
    bucket: str,
    prefix: str,
    runner: CommandRunner,
    environ: Mapping[str, str],
) -> S3RestoreOwnership:
    _require_s3_bucket_versioning(bucket=bucket, runner=runner, environ=environ)
    ownership = S3RestoreOwnership(
        bucket=bucket,
        prefix=prefix,
        operation_id=secrets.token_hex(16),
    )
    try:
        for entry in _relative_object_entries(source):
            relative = str(entry["key"])
            key = f"{prefix}/{relative}"
            payload = _aws_json(
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
                        str(source / PurePosixPath(relative)),
                        "--metadata",
                        (
                            "hr-recovery-operation="
                            f"{ownership.operation_id},"
                            f"hr-recovery-sha256={entry['sha256']}"
                        ),
                        "--output",
                        "json",
                        *_aws_endpoint_arguments(environ),
                    ],
                    label="S3 versioned-object restore",
                ),
                label="S3 versioned-object restore",
            )
            version_id = payload.get("VersionId")
            if not isinstance(version_id, str) or not version_id or version_id == "null":
                raise RecoveryError(
                    "S3 versioned-object restore did not return a version ID"
                )
        return ownership
    except Exception:
        try:
            _delete_owned_s3_object_versions(
                ownership=ownership, runner=runner, environ=environ
            )
        except Exception as cleanup_exc:
            raise RecoveryError(
                "S3 restore failed and exact-version rollback could not be verified"
            ) from cleanup_exc
        raise


def _restore_objects(
    *,
    source: Path,
    target_kind: str,
    target_local_root: str | None,
    confirm_object_target: str,
    runner: CommandRunner,
    environ: Mapping[str, str],
) -> tuple[str, list[dict[str, object]], S3RestoreOwnership | None]:
    if target_kind == "local":
        if target_local_root is None:
            raise RecoveryError("--restore-local-object-root is required")
        target = validate_restore_directory(target_local_root)
        if confirm_object_target != str(target):
            raise RecoveryError(
                "--confirm-object-target must exactly match the restore path"
            )
        try:
            _copy_objects_to_local(source, target)
            return str(target), _relative_object_entries(target), None
        # The target was proven empty above, so partial-copy cleanup cannot
        # remove pre-existing operator data.
        except Exception:
            _remove_validated_restore_directory(target)
            raise
    bucket, prefix, uri = _s3_restore_target(environ)
    if confirm_object_target != uri:
        raise RecoveryError(
            "--confirm-object-target must exactly match the restore S3 URI"
        )
    existing = runner.run(
        [
            "aws",
            "s3api",
            "list-objects-v2",
            "--bucket",
            bucket,
            "--prefix",
            f"{prefix}/",
            "--max-keys",
            "1",
            "--output",
            "json",
            *_aws_endpoint_arguments(environ),
        ],
        label="S3 restore-target inspection",
    )
    existing_payload = _aws_json(existing, label="S3 restore-target inspection")
    existing_objects = existing_payload.get("Contents", [])
    if not isinstance(existing_objects, list):
        raise RecoveryError("S3 restore-target inspection returned an invalid payload")
    if existing_objects:
        raise RecoveryError("Restore S3 prefix must be empty")
    ownership = _put_versioned_s3_objects(
        source=source,
        bucket=bucket,
        prefix=prefix,
        runner=runner,
        environ=environ,
    )
    try:
        verification = Path(tempfile.mkdtemp(prefix="hr-object-verify-"))
        try:
            runner.run(
                [
                    "aws",
                    "s3",
                    "sync",
                    uri,
                    str(verification),
                    "--only-show-errors",
                    *_aws_endpoint_arguments(environ),
                ],
                label="S3 restored-object verification",
            )
            return uri, _relative_object_entries(verification), ownership
        finally:
            shutil.rmtree(verification, ignore_errors=True)
    except Exception:
        _delete_owned_s3_object_versions(
            ownership=ownership, runner=runner, environ=environ
        )
        raise


def _rollback_objects(
    *,
    target_kind: str,
    target_label: str | None,
    s3_ownership: S3RestoreOwnership | None,
    runner: CommandRunner,
    environ: Mapping[str, str],
) -> None:
    if not target_label:
        return
    if target_kind == "local":
        _remove_validated_restore_directory(Path(target_label))
        return
    _bucket, _prefix, expected = _s3_restore_target(environ)
    if target_label != expected:
        raise RecoveryError("Refusing to remove an unexpected S3 restore prefix")
    if s3_ownership is None or (
        s3_ownership.bucket != _bucket or s3_ownership.prefix != _prefix
    ):
        raise RecoveryError("S3 restore rollback is missing exact version ownership")
    _delete_owned_s3_object_versions(
        ownership=s3_ownership, runner=runner, environ=environ
    )


def restore(args: argparse.Namespace, environ: Mapping[str, str]) -> dict[str, object]:
    target_connection = postgres_connection_from_env(args.database_url_env, environ)
    target_database = validate_isolated_name(
        args.target_database, label="target database"
    )
    if args.confirm_restore_to != target_database:
        raise RecoveryError(
            "--confirm-restore-to must exactly match the target database"
        )
    archive = validate_bounded_absolute_path(
        args.archive, label="encrypted backup", must_exist=True
    )
    receipt_path = validate_bounded_absolute_path(
        args.receipt, label="backup receipt", must_exist=True
    )
    private_key = validate_bounded_absolute_path(
        args.private_key, label="recovery private key", must_exist=True
    )
    if args.max_rpo_seconds <= 0 or args.max_rto_seconds <= 0:
        raise RecoveryError("RPO and RTO thresholds must be positive")
    if args.target_object_kind == "local":
        if args.restore_local_object_root is None:
            raise RecoveryError("--restore-local-object-root is required")
        object_target = str(validate_restore_directory(args.restore_local_object_root))
    else:
        object_target = _s3_restore_target(environ)[2]
    if args.confirm_object_target != object_target:
        raise RecoveryError(
            "--confirm-object-target must exactly match the object target"
        )
    plan = {
        "action": "restore",
        "archive": archive.name,
        "target_database": target_database,
        "target_server": target_connection.safe_label.rsplit("/", 1)[0],
        "object_target": object_target,
        "cleanup_after_verify": bool(args.cleanup_after_verify),
        "dry_run": bool(args.dry_run),
    }
    if args.dry_run:
        return plan

    receipt = _load_authenticated_receipt(archive, receipt_path, environ)
    started = utc_now()
    runner = CommandRunner(environ=environ)
    pg = PgTools(runner, target_connection, args.pg_container)
    if pg.database_exists(target_database):
        raise RecoveryError(
            "Target database already exists; restore refuses to overwrite it"
        )
    temporary_root = Path(tempfile.mkdtemp(prefix="hr-recovery-restore-"))
    os.chmod(temporary_root, 0o700)
    database_created = False
    object_target_created: str | None = None
    s3_restore_ownership: S3RestoreOwnership | None = None
    rollback_verified = False
    evidence_path = validate_bounded_absolute_path(
        args.evidence, label="recovery evidence"
    )
    try:
        decrypted_tar = temporary_root / "bundle.tar"
        fingerprint = decrypt_archive(archive, decrypted_tar, private_key, environ)
        if fingerprint != receipt.get("recipient_fingerprint_sha256"):
            raise RecoveryError(
                "Recovery private key does not match the backup receipt"
            )
        extracted = temporary_root / "extracted"
        _safe_extract_tar(decrypted_tar, extracted)
        manifest = _verify_manifest(extracted)
        target_roles = required_role_contract(pg)
        if target_roles != manifest["database"].get("required_roles"):  # type: ignore[union-attr]
            raise RecoveryError(
                "Target PostgreSQL recovery roles differ from the backup"
            )
        expected_owners = set(manifest["database"].get("required_owner_roles", []))  # type: ignore[union-attr]
        if not expected_owners or not expected_owners.issubset(
            available_role_names(pg)
        ):
            raise RecoveryError(
                "Target PostgreSQL is missing a required object-owner role"
            )
        recovery_point = datetime.fromisoformat(str(manifest["recovery_point_at"]))
        rpo_seconds = max(0.0, (started - recovery_point).total_seconds())
        if rpo_seconds > args.max_rpo_seconds:
            raise RecoveryError(
                f"Backup exceeds the configured RPO threshold ({round(rpo_seconds)}s)"
            )
        pg.create_database(target_database)
        database_created = True
        pg.restore(extracted / "database.dump", target_database)
        object_target_created, restored_entries, s3_restore_ownership = _restore_objects(
            source=extracted / "objects",
            target_kind=args.target_object_kind,
            target_local_root=args.restore_local_object_root,
            confirm_object_target=args.confirm_object_target,
            runner=runner,
            environ=environ,
        )
        expected_entries = manifest["objects"]["entries"]  # type: ignore[index]
        if restored_entries != expected_entries:
            raise RecoveryError("Restored private-object checksums do not match")
        database_name = pg.query("SELECT current_database()", database=target_database)
        organization_count_raw = pg.query(
            "SELECT count(*) FROM organizations", database=target_database
        )
        migration_head = pg.query(
            "SELECT version_num FROM alembic_version", database=target_database
        )
        security_contract = restored_security_contract(pg, target_database)
        if database_name != target_database or not migration_head:
            raise RecoveryError("Restored database smoke verification failed")
        completed = utc_now()
        rto_seconds = (completed - started).total_seconds()
        if rto_seconds > args.max_rto_seconds:
            raise RecoveryError(
                f"Restore exceeds the configured RTO threshold ({round(rto_seconds)}s)"
            )
        if args.cleanup_after_verify:
            _rollback_objects(
                target_kind=args.target_object_kind,
                target_label=object_target_created,
                s3_ownership=s3_restore_ownership,
                runner=runner,
                environ=environ,
            )
            object_target_created = None
            s3_restore_ownership = None
            pg.drop_database(target_database)
            database_created = False
            if pg.database_exists(target_database):
                raise RecoveryError("Isolated database rollback verification failed")
            rollback_verified = True
        evidence: dict[str, object] = {
            "format": "hr-agent-recovery-evidence-v1",
            "backup_id": manifest["backup_id"],
            "verified_at": iso_utc(completed),
            "target_database": target_database,
            "target_object_kind": args.target_object_kind,
            "database": {
                "organization_count": int(organization_count_raw),
                "migration_head": migration_head,
                "required_roles_match": True,
                **security_contract,
            },
            "objects": {
                "count": len(restored_entries),
                "bytes": sum(int(row["bytes"]) for row in restored_entries),
                "checksums_match": True,
            },
            "rpo_seconds": round(rpo_seconds, 3),
            "rpo_target_seconds": args.max_rpo_seconds,
            "rto_seconds": round(rto_seconds, 3),
            "rto_target_seconds": args.max_rto_seconds,
            "rollback_verified": rollback_verified,
            "status": "pass",
        }
        write_json_atomic(evidence_path, evidence)
        return {**plan, **evidence, "evidence": str(evidence_path), "dry_run": False}
    except Exception:
        rollback_failures: list[str] = []
        if object_target_created is not None:
            try:
                _rollback_objects(
                    target_kind=args.target_object_kind,
                    target_label=object_target_created,
                    s3_ownership=s3_restore_ownership,
                    runner=runner,
                    environ=environ,
                )
            except Exception:  # noqa: BLE001 - best-effort rollback reports residue
                rollback_failures.append("object target")
        if database_created:
            try:
                pg.drop_database(target_database)
            except Exception:  # noqa: BLE001 - best-effort rollback reports residue
                rollback_failures.append("database target")
        if rollback_failures:
            raise RecoveryError(
                "Restore failed and automatic rollback could not remove: "
                + ", ".join(rollback_failures)
            )
        raise
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


def verify_backup(
    args: argparse.Namespace, environ: Mapping[str, str]
) -> dict[str, object]:
    archive = validate_bounded_absolute_path(
        args.archive, label="encrypted backup", must_exist=True
    )
    receipt_path = validate_bounded_absolute_path(
        args.receipt, label="backup receipt", must_exist=True
    )
    private_key = validate_bounded_absolute_path(
        args.private_key, label="recovery private key", must_exist=True
    )
    receipt = _load_authenticated_receipt(archive, receipt_path, environ)
    temporary_root = Path(tempfile.mkdtemp(prefix="hr-recovery-verify-"))
    try:
        tar_path = temporary_root / "bundle.tar"
        fingerprint = decrypt_archive(archive, tar_path, private_key, environ)
        if fingerprint != receipt.get("recipient_fingerprint_sha256"):
            raise RecoveryError(
                "Recovery private key does not match the backup receipt"
            )
        extracted = temporary_root / "extracted"
        _safe_extract_tar(tar_path, extracted)
        manifest = _verify_manifest(extracted)
        return {
            "action": "verify",
            "status": "pass",
            "backup_id": manifest["backup_id"],
            "object_count": manifest["objects"]["count"],  # type: ignore[index]
            "database_bytes": manifest["database"]["bytes"],  # type: ignore[index]
        }
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


def prune_local(
    args: argparse.Namespace, environ: Mapping[str, str]
) -> dict[str, object]:
    directory = validate_bounded_absolute_path(
        args.directory, label="backup retention directory", must_exist=True
    )
    if not directory.is_dir():
        raise RecoveryError("Backup retention directory must be a directory")
    key = read_hmac_key(environ)
    now = utc_now()
    expired: list[tuple[Path, Path]] = []
    for receipt_path in sorted(directory.glob("*.receipt.json")):
        try:
            receipt = json.loads(receipt_path.read_text("utf-8"))
            verify_signed_payload(receipt, key)
            expires_at = datetime.fromisoformat(str(receipt["expires_at"]))
            backup_id = str(receipt["backup_id"])
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            raise RecoveryError(f"Invalid signed receipt: {receipt_path.name}") from exc
        if not BACKUP_ID_PATTERN.fullmatch(backup_id):
            raise RecoveryError(f"Invalid signed receipt: {receipt_path.name}")
        archive = directory / f"{backup_id}.hrb"
        if expires_at <= now and archive.exists():
            expired.append((archive, receipt_path))
    if args.apply:
        expected = now.date().isoformat()
        if args.confirm_delete_before != expected:
            raise RecoveryError(
                f"--confirm-delete-before must equal today's UTC date ({expected})"
            )
        for archive, receipt in expired:
            archive.unlink()
            receipt.unlink()
    return {
        "action": "prune-local",
        "apply": bool(args.apply),
        "expired_count": len(expired),
        "expired_backup_ids": [archive.stem for archive, _receipt in expired],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup", help="create an encrypted backup")
    backup_parser.add_argument("--database-url-env", default="HR_RECOVERY_DATABASE_URL")
    backup_parser.add_argument("--pg-container")
    backup_parser.add_argument(
        "--object-source", choices=("local", "s3"), required=True
    )
    backup_parser.add_argument("--local-object-root")
    backup_parser.add_argument("--output-dir", required=True)
    backup_parser.add_argument("--public-key", required=True)
    backup_parser.add_argument("--maintenance-window", required=True)
    backup_parser.add_argument(
        "--retention-days", type=int, default=DEFAULT_RETENTION_DAYS
    )
    backup_parser.add_argument("--dry-run", action="store_true")

    restore_parser = subparsers.add_parser(
        "restore", help="restore and verify an isolated recovery target"
    )
    restore_parser.add_argument(
        "--database-url-env", default="HR_RECOVERY_DATABASE_URL"
    )
    restore_parser.add_argument("--pg-container")
    restore_parser.add_argument("--archive", required=True)
    restore_parser.add_argument("--receipt", required=True)
    restore_parser.add_argument("--private-key", required=True)
    restore_parser.add_argument("--target-database", required=True)
    restore_parser.add_argument("--confirm-restore-to", required=True)
    restore_parser.add_argument(
        "--target-object-kind", choices=("local", "s3"), required=True
    )
    restore_parser.add_argument("--restore-local-object-root")
    restore_parser.add_argument("--confirm-object-target", required=True)
    restore_parser.add_argument("--evidence", required=True)
    restore_parser.add_argument(
        "--max-rpo-seconds", type=int, default=DEFAULT_MAX_RPO_SECONDS
    )
    restore_parser.add_argument(
        "--max-rto-seconds", type=int, default=DEFAULT_MAX_RTO_SECONDS
    )
    restore_parser.add_argument("--cleanup-after-verify", action="store_true")
    restore_parser.add_argument("--dry-run", action="store_true")

    verify_parser = subparsers.add_parser(
        "verify", help="authenticate, decrypt, and checksum an archive"
    )
    verify_parser.add_argument("--archive", required=True)
    verify_parser.add_argument("--receipt", required=True)
    verify_parser.add_argument("--private-key", required=True)

    prune_parser = subparsers.add_parser(
        "prune-local", help="list or remove expired encrypted local backups"
    )
    prune_parser.add_argument("--directory", required=True)
    prune_parser.add_argument("--apply", action="store_true")
    prune_parser.add_argument("--confirm-delete-before")
    return parser


def main(
    argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None
) -> int:
    args = build_parser().parse_args(argv)
    values = dict(environ or os.environ)
    try:
        if args.command == "backup":
            result = backup(args, values)
        elif args.command == "restore":
            result = restore(args, values)
        elif args.command == "verify":
            result = verify_backup(args, values)
        else:
            result = prune_local(args, values)
    except RecoveryError as exc:
        print(json.dumps({"status": "fail", "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
