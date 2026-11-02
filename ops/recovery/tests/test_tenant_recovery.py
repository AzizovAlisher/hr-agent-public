"""Tenant-only archive, integrity, and restore-safety contracts."""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tarfile
import threading
from types import SimpleNamespace
from uuid import UUID

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from ops.recovery import hr_recovery as recovery  # noqa: E402
from ops.recovery import tenant_recovery as tenant  # noqa: E402


ORG_ID = UUID("11111111-1111-1111-1111-111111111111")
OTHER_ORG_ID = UUID("22222222-2222-2222-2222-222222222222")


class QueryPg:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    def query(self, _sql: str) -> str:
        if not self.responses:
            raise AssertionError("Unexpected PostgreSQL query")
        return self.responses.pop(0)


def _security_relation(name: str) -> dict[str, object]:
    column = "id" if name == "organizations" else "organization_id"
    policy_name = (
        "organization_isolation" if name == "organizations" else "tenant_isolation"
    )
    expression = f"{column} = security.current_organization_id()"
    return {
        "name": name,
        "kind": "r",
        "is_partition": False,
        "tenant_column": column,
        "tenant_type": "uuid",
        "tenant_not_null": True,
        "rls_enabled": True,
        "rls_forced": True,
        "owner": "hr_agent_broker",
        "backup_role_is_owner_member": False,
        "select_granted": True,
        "insert_granted": False,
        "update_granted": False,
        "delete_granted": False,
        "truncate_granted": False,
        "trigger_granted": False,
        "references_granted": False,
        "policies": [
            {
                "name": policy_name,
                "command": "*",
                "permissive": True,
                "public_only": True,
                "using": expression,
                "with_check": expression,
            }
        ],
    }


def _security_inventory() -> dict[str, object]:
    tables = ["organizations", "candidates"]
    return {
        "current_role": "tenant_reader",
        "tables": tables,
        "relations": [_security_relation(name) for name in tables],
        "schema_usage": True,
        "schema_create": False,
        "unsafe_memberships": [],
    }


def _tenant_staging(tmp_path: Path, *, key: str | None = None) -> Path:
    root = tmp_path / "extracted"
    objects = root / "objects"
    selected_key = key or f"private/{ORG_ID}/candidates/object.pdf"
    object_path = objects / selected_key
    object_path.parent.mkdir(parents=True)
    payload = b"tenant-private-object"
    object_path.write_bytes(payload)
    dump = root / "tenant.dump"
    dump.write_bytes(b"postgresql tenant dump")
    object_sha256 = hashlib.sha256(payload).hexdigest()
    dump_sha256 = hashlib.sha256(dump.read_bytes()).hexdigest()
    security_inventory = _security_inventory()
    counts = {"organizations": 1, "candidates": 1}
    manifest = {
        "format": tenant.TENANT_FORMAT_VERSION,
        "backup_id": "hr-tenant-20260824T000000Z-1234abcd",
        "organization_id": str(ORG_ID),
        "started_at": "2026-08-24T00:00:00Z",
        "completed_at": "2026-08-24T00:00:01Z",
        "recovery_point_at": "2026-08-24T00:00:01Z",
        "expires_at": "2026-09-20T00:00:01Z",
        "retention_days": 27,
        "maintenance_window": "test",
        "operator": {
            "id": "security.operator@example.test",
            "authorization_ticket_sha256": "a" * 64,
        },
        "database": {
            "format": "postgresql-custom-data-only-column-inserts",
            "migration_head": "fe48b7c1d902",
            "source_role": "tenant_reader",
            "row_security": "forced_non_bypass",
            "tables": ["organizations", "candidates"],
            "row_counts": counts,
            "row_count": 2,
            "snapshot": {
                "isolation": "repeatable_read",
                "exported": True,
                "snapshot_id_sha256": "b" * 64,
            },
            "security_inventory": security_inventory,
            "security_inventory_sha256": hashlib.sha256(
                recovery.canonical_json(security_inventory)
            ).hexdigest(),
            "dump_tenant_proof": {
                "organization_id": str(ORG_ID),
                "row_counts": counts,
                "row_count": 2,
                "verified": True,
            },
            "sha256": dump_sha256,
            "bytes": dump.stat().st_size,
            "relationship_violations": [],
        },
        "objects": {
            "source": "s3",
            "count": 1,
            "bytes": len(payload),
            "entries": [
                {
                    "key": selected_key,
                    "kind": "registry",
                    "category": "candidates",
                    "database_reference": selected_key,
                    "sha256": object_sha256,
                    "bytes": len(payload),
                }
            ],
        },
    }
    recovery.write_json_atomic(root / "manifest.json", manifest)
    return root


def test_manifest_verifies_exact_table_object_hashes_and_counts(tmp_path: Path):
    root = _tenant_staging(tmp_path)

    manifest = tenant._verify_tenant_manifest(root)

    assert manifest["organization_id"] == str(ORG_ID)
    assert manifest["database"]["row_count"] == 2
    assert manifest["objects"]["count"] == 1


def test_manifest_rejects_cross_tenant_registry_object(tmp_path: Path):
    root = _tenant_staging(
        tmp_path,
        key=f"private/{OTHER_ORG_ID}/candidates/object.pdf",
    )

    with pytest.raises(recovery.RecoveryError, match="cross-tenant"):
        tenant._verify_tenant_manifest(root)


def test_manifest_rejects_mismatched_dump_and_object_inventory(tmp_path: Path):
    root = _tenant_staging(tmp_path)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["database"]["sha256"] = "0" * 64
    recovery.write_json_atomic(manifest_path, manifest)

    with pytest.raises(recovery.RecoveryError, match="dump checksum"):
        tenant._verify_tenant_manifest(root)

    root = _tenant_staging(tmp_path / "object-mismatch")
    object_path = next((root / "objects").rglob("*.pdf"))
    object_path.write_bytes(b"tampered")
    with pytest.raises(recovery.RecoveryError, match="object inventory"):
        tenant._verify_tenant_manifest(root)


def test_source_role_must_be_login_non_superuser_non_bypass():
    accepted = QueryPg(
        [f"tenant_reader|false|false|true|false|false|false|true|{ORG_ID}"],
    )
    assert tenant._source_role_contract(accepted, ORG_ID) == "tenant_reader"

    for response in (
        f"postgres|true|false|true|false|false|false|true|{ORG_ID}",
        f"broker|false|true|true|false|false|false|true|{ORG_ID}",
        f"group|false|false|false|false|false|false|true|{ORG_ID}",
        f"creator|false|false|true|false|true|false|true|{ORG_ID}",
        f"tenant_reader|false|false|true|false|false|false|true|{OTHER_ORG_ID}",
    ):
        with pytest.raises(recovery.RecoveryError):
            tenant._source_role_contract(QueryPg([response]), ORG_ID)


def test_target_must_be_empty_across_every_tenant_table():
    empty = QueryPg([json.dumps({"organizations": 0, "candidates": 0})])
    tenant._target_is_empty(empty, ORG_ID, ["organizations", "candidates"])

    occupied = QueryPg([json.dumps({"organizations": 1, "candidates": 0})])
    with pytest.raises(recovery.RecoveryError, match="absent and empty"):
        tenant._target_is_empty(
            occupied,
            ORG_ID,
            ["organizations", "candidates"],
        )


def test_local_target_preflight_checks_only_manifest_keys(tmp_path: Path):
    root = tmp_path / "tenant-restore-objects"
    unrelated = root / f"private/{OTHER_ORG_ID}/candidate.bin"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"other tenant remains")
    key = f"private/{ORG_ID}/candidate.bin"
    entries = [{"key": key}]

    assert tenant._local_target_preflight(root, entries) == root.resolve()
    selected = root / key
    selected.parent.mkdir(parents=True, exist_ok=True)
    selected.write_bytes(b"already present")

    with pytest.raises(recovery.RecoveryError, match="already contains"):
        tenant._local_target_preflight(root, entries)


def test_confirmation_and_object_keys_fail_closed():
    with pytest.raises(recovery.RecoveryError):
        tenant._organization_id("../not-a-tenant")
    with pytest.raises(recovery.RecoveryError):
        tenant._safe_object_key("private/../other/resume.pdf")
    with pytest.raises(recovery.RecoveryError):
        tenant._ticket_digest("bad")
    assert tenant._safe_object_key(f"private/{ORG_ID}/resume.pdf").startswith(
        "private/"
    )


def test_pg_catalog_inventory_requires_forced_exact_policy_and_select_only_role():
    valid = _security_inventory()
    result = tenant.tenant_security_inventory(
        QueryPg([json.dumps(valid)]),
        require_backup_grants=True,
    )
    assert result["tables"] == ["organizations", "candidates"]

    invalid: list[dict[str, object]] = []
    for mutation in (
        ("relations", 1, "rls_forced", False),
        ("relations", 1, "insert_granted", True),
        ("relations", 1, "backup_role_is_owner_member", True),
    ):
        payload = json.loads(json.dumps(valid))
        payload[mutation[0]][mutation[1]][mutation[2]] = mutation[3]
        invalid.append(payload)
    unsafe_policy = json.loads(json.dumps(valid))
    unsafe_policy["relations"][1]["policies"][0]["using"] = "true"
    invalid.append(unsafe_policy)
    schema_owner = json.loads(json.dumps(valid))
    schema_owner["schema_create"] = True
    invalid.append(schema_owner)
    inherited_bypass = json.loads(json.dumps(valid))
    inherited_bypass["unsafe_memberships"] = ["hr_agent_broker"]
    invalid.append(inherited_bypass)

    for payload in invalid:
        with pytest.raises(recovery.RecoveryError):
            tenant.tenant_security_inventory(
                QueryPg([json.dumps(payload)]),
                require_backup_grants=True,
            )


def test_dump_row_proof_rejects_other_tenant_unselected_tables_and_count_drift():
    candidate_id = "33333333-3333-3333-3333-333333333333"
    sql = (
        "SET statement_timeout = 0;\n"
        "INSERT INTO public.organizations (id, name) "
        f"VALUES ('{ORG_ID}', 'Tenant; O''Brien');\n"
        "INSERT INTO public.candidates (id, organization_id, raw_cv) "
        f"VALUES ('{candidate_id}', '{ORG_ID}', E'line;\\nnext');\n"
    ).encode()
    proof = tenant.verify_tenant_dump_rows(
        sql,
        organization_id=ORG_ID,
        tables=["organizations", "candidates"],
        expected_counts={"organizations": 1, "candidates": 1},
    )
    assert proof["verified"] is True

    with pytest.raises(recovery.RecoveryError, match="different organization"):
        tenant.verify_tenant_dump_rows(
            sql.replace(str(ORG_ID).encode(), str(OTHER_ORG_ID).encode(), 1),
            organization_id=ORG_ID,
            tables=["organizations", "candidates"],
            expected_counts={"organizations": 1, "candidates": 1},
        )
    with pytest.raises(recovery.RecoveryError, match="row counts"):
        tenant.verify_tenant_dump_rows(
            sql,
            organization_id=ORG_ID,
            tables=["organizations", "candidates"],
            expected_counts={"organizations": 1, "candidates": 2},
        )
    extra = (
        sql
        + (
            "INSERT INTO public.users (id, organization_id) "
            f"VALUES ('{candidate_id}', '{ORG_ID}');\n"
        ).encode()
    )
    with pytest.raises(recovery.RecoveryError, match="unselected table"):
        tenant.verify_tenant_dump_rows(
            extra,
            organization_id=ORG_ID,
            tables=["organizations", "candidates"],
            expected_counts={"organizations": 1, "candidates": 1},
        )


def test_legacy_keys_are_bound_to_tenant_and_category_namespaces(tmp_path: Path):
    root = tmp_path / "legacy-objects"
    root.mkdir()
    opaque = "33333333-3333-3333-3333-333333333333.txt"
    correct = f"task_files/{ORG_ID}/{opaque}"
    assert (
        tenant._legacy_object_key(
            correct,
            organization_id=ORG_ID,
            category="tasks",
            source_kind="local",
            local_root=root,
        )
        == correct
    )

    for reference in (
        f"task_files/{OTHER_ORG_ID}/{opaque}",
        f"assessment_files/{ORG_ID}/{opaque}",
        f"private/{OTHER_ORG_ID}/tasks/{opaque}",
    ):
        with pytest.raises(recovery.RecoveryError, match="namespace"):
            tenant._legacy_object_key(
                reference,
                organization_id=ORG_ID,
                category="tasks",
                source_kind="local",
                local_root=root,
            )


def test_atomic_evidence_publication_is_concurrent_no_clobber(tmp_path: Path):
    destination = tmp_path / "restore-evidence-v1.json"
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def writer(label: str) -> None:
        barrier.wait()
        try:
            tenant._write_json_no_clobber(destination, {"writer": label})
            outcomes.append(f"pass:{label}")
        except recovery.RecoveryError:
            outcomes.append(f"blocked:{label}")

    threads = [threading.Thread(target=writer, args=(label,)) for label in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert sum(value.startswith("pass:") for value in outcomes) == 1
    assert sum(value.startswith("blocked:") for value in outcomes) == 1
    published = json.loads(destination.read_text("utf-8"))
    assert published["writer"] in {"a", "b"}
    with pytest.raises(recovery.RecoveryError, match="overwrite"):
        tenant._write_json_no_clobber(destination, {"writer": "clobber"})
    assert json.loads(destination.read_text("utf-8")) == published


def test_local_rollback_refuses_to_delete_winner_replacement(tmp_path: Path):
    key = f"private/{ORG_ID}/candidates/object.pdf"
    source = tmp_path / "source"
    target = tmp_path / "target"
    incoming = source / key
    incoming.parent.mkdir(parents=True)
    incoming.write_bytes(b"loser-owned")
    entries = [
        {
            "key": key,
            "sha256": hashlib.sha256(b"loser-owned").hexdigest(),
            "bytes": len(b"loser-owned"),
        }
    ]
    owned = tenant._restore_local_objects(
        source=source,
        target=target,
        entries=entries,
    )
    destination = target / key
    winner = tmp_path / "winner-object"
    winner.write_bytes(b"winner-owned")
    winner.replace(destination)

    with pytest.raises(recovery.RecoveryError, match="no longer owns"):
        tenant._rollback_local_objects(owned, target)
    assert destination.read_bytes() == b"winner-owned"


def test_s3_rollback_deletes_only_the_operation_owned_version():
    operation_id = "a" * 32
    restored = [
        tenant.RestoredS3Version(
            key=f"private/{ORG_ID}/candidates/object.pdf",
            version_id="owned-version",
            operation_id=operation_id,
        )
    ]

    class Runner:
        def __init__(self, owner: str) -> None:
            self.owner = owner
            self.commands: list[list[str]] = []

        def run(self, command, **_kwargs):
            values = list(command)
            self.commands.append(values)
            if "head-object" in values:
                return json.dumps(
                    {
                        "Metadata": {
                            "hr-tenant-recovery-operation": self.owner,
                        }
                    }
                ).encode()
            if "delete-object" in values:
                return b""
            raise AssertionError(values)

    wrong_owner = Runner("b" * 32)
    with pytest.raises(recovery.RecoveryError, match="roll back"):
        tenant._rollback_s3_objects(
            restored,
            "tenant-recovery-test",
            wrong_owner,
            {},
        )
    assert not any("delete-object" in command for command in wrong_owner.commands)

    owner = Runner(operation_id)
    tenant._rollback_s3_objects(
        restored,
        "tenant-recovery-test",
        owner,
        {},
    )
    deleted = next(command for command in owner.commands if "delete-object" in command)
    assert deleted[deleted.index("--version-id") + 1] == "owned-version"


def test_adversarial_concurrent_restore_loser_cannot_delete_winner(monkeypatch):
    operation_ids = ("a" * 32, "b" * 32)
    acquisition_barrier = threading.Barrier(2)
    winner_started = threading.Event()
    loser_checked = threading.Event()
    mutex = threading.Lock()
    target: dict[str, str] = {}
    outcomes: list[tuple[str, str]] = []

    class FakePersistentPsql:
        def __init__(self, _pg, *, label: str) -> None:
            self.label = label
            self.acquired = False
            self.sentinel = ""

        def __enter__(self):
            return self

        def query_scalar(self, sql: str) -> str:
            if "pg_try_advisory_lock" in sql:
                acquisition_barrier.wait()
                self.acquired = mutex.acquire(blocking=False)
                return "true" if self.acquired else "false"
            if "set_config" in sql:
                self.sentinel = next(value for value in operation_ids if value in sql)
                return self.sentinel
            if "current_setting" in sql:
                return self.sentinel
            if "WITH deleted AS" in sql:
                target.clear()
                return "1"
            raise AssertionError(sql)

        def is_active(self) -> bool:
            return self.acquired

        def __exit__(self, *_args) -> None:
            if self.acquired:
                self.acquired = False
                mutex.release()

    monkeypatch.setattr(tenant, "_PersistentPsql", FakePersistentPsql)
    fake_pg = SimpleNamespace(
        connection=SimpleNamespace(database="target_recovery_test")
    )

    def restore(operation_id: str) -> None:
        lease = tenant.TenantRestoreLock(fake_pg, ORG_ID, operation_id)
        try:
            with lease:
                target["owner"] = operation_id
                outcomes.append(("winner", operation_id))
                winner_started.set()
                assert loser_checked.wait(timeout=5)
        except recovery.RecoveryError:
            assert winner_started.wait(timeout=5)
            with pytest.raises(recovery.RecoveryError, match="lock was lost"):
                tenant._rollback_tenant_database(
                    fake_pg,
                    ORG_ID,
                    ["organizations"],
                    lease,
                    operation_id,
                )
            outcomes.append(("loser", operation_id))
            loser_checked.set()

    threads = [
        threading.Thread(target=restore, args=(value,)) for value in operation_ids
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    winner = next(value for status, value in outcomes if status == "winner")
    assert target == {"owner": winner}
    assert sorted(status for status, _value in outcomes) == ["loser", "winner"]


def test_exported_snapshot_barrier_prevents_same_count_torn_archive(
    tmp_path: Path,
    monkeypatch,
):
    snapshot_id = "00000001-00000001-1"
    old_key = f"private/{ORG_ID}/candidates/33333333-3333-3333-3333-333333333333.pdf"
    new_key = f"private/{ORG_ID}/candidates/44444444-4444-4444-4444-444444444444.pdf"
    payload = b"snapshot-object"
    local_root = tmp_path / "objects"
    old_object = local_root / old_key
    old_object.parent.mkdir(parents=True)
    old_object.write_bytes(payload)
    public_key = tmp_path / "public.pem"
    public_key.write_bytes(b"test-only-public-key")
    inventory_seen = threading.Event()
    mutation_done = threading.Event()
    live_state = {"key": old_key, "count": 1}
    observed_snapshots: list[str | None] = []
    dump_snapshots: list[str | None] = []

    def mutate_same_count() -> None:
        assert inventory_seen.wait(timeout=5)
        live_state["key"] = new_key
        live_state["count"] = 1
        mutation_done.set()

    mutator = threading.Thread(target=mutate_same_count)
    mutator.start()

    class FakeTenantPgTools:
        def __init__(self, _runner, connection, _container=None) -> None:
            self.connection = connection
            self.snapshot_id: str | None = None

        def query(self, sql: str) -> str:
            if "FROM pg_catalog.pg_roles WHERE rolname = current_user" in sql:
                return f"tenant_reader|false|false|true|false|false|false|true|{ORG_ID}"
            observed_snapshots.append(self.snapshot_id)
            if "public.alembic_version" in sql:
                return "fe48b7c1d902"
            if "WITH selected AS" in sql:
                return json.dumps(_security_inventory())
            if "tenant_counts" in sql:
                inventory_seen.set()
                assert mutation_done.wait(timeout=5)
                return json.dumps({"organizations": 1, "candidates": 1})
            if "WITH pairs AS" in sql:
                return "[]"
            if "FROM public.private_objects" in sql:
                selected_key = (
                    old_key if self.snapshot_id == snapshot_id else live_state["key"]
                )
                return json.dumps(
                    [
                        {
                            "key": selected_key,
                            "category": "candidates",
                            "sha256": hashlib.sha256(payload).hexdigest(),
                            "bytes": len(payload),
                        }
                    ]
                )
            if "FROM public.candidates WHERE" in sql:
                return "[]"
            raise AssertionError(sql)

        @contextlib.contextmanager
        def exported_snapshot(self):
            yield snapshot_id

        def at_snapshot(self, value: str):
            assert value == snapshot_id
            self.snapshot_id = value
            return self

        def dump_tenant(self, destination: Path, _tables) -> None:
            dump_snapshots.append(self.snapshot_id)
            destination.write_bytes(b"fake-custom-dump")

        def render_dump(self, _source: Path) -> bytes:
            return (
                "INSERT INTO public.organizations (id, name) "
                f"VALUES ('{ORG_ID}', 'Snapshot tenant');\n"
                "INSERT INTO public.candidates (id, organization_id) "
                f"VALUES ('55555555-5555-5555-5555-555555555555', '{ORG_ID}');\n"
            ).encode()

    def fake_encrypt(source: Path, destination: Path, _public_key: Path) -> str:
        shutil.copyfile(source, destination)
        return "c" * 64

    monkeypatch.setattr(tenant, "TenantPgTools", FakeTenantPgTools)
    monkeypatch.setattr(recovery, "encrypt_archive", fake_encrypt)
    environment = {
        "HR_TENANT_BACKUP_DATABASE_URL": (
            "postgresql://tenant_reader@localhost/source_recovery_test"
        ),
        "HR_RECOVERY_MANIFEST_HMAC_KEY": base64.urlsafe_b64encode(b"k" * 32)
        .decode()
        .rstrip("="),
    }
    result = tenant.tenant_backup(
        argparse.Namespace(
            database_url_env="HR_TENANT_BACKUP_DATABASE_URL",
            pg_container=None,
            organization_id=str(ORG_ID),
            confirm_organization_id=str(ORG_ID),
            operator_id="snapshot.operator@example.test",
            authorization_ticket="CHG-snapshot-barrier",
            maintenance_window="snapshot-barrier",
            object_source="local",
            local_object_root=str(local_root),
            output_dir=str(tmp_path / "backups"),
            public_key=str(public_key),
            retention_days=35,
            dry_run=False,
        ),
        environment,
    )
    mutator.join(timeout=5)
    assert not mutator.is_alive()
    assert live_state == {"key": new_key, "count": 1}
    assert observed_snapshots and set(observed_snapshots) == {snapshot_id}
    assert dump_snapshots == [snapshot_id]
    assert result["dump_tenant_verified"] is True

    with tarfile.open(Path(str(result["archive"])), "r:") as archive:
        manifest_handle = archive.extractfile("manifest.json")
        assert manifest_handle is not None
        manifest = json.load(manifest_handle)
    assert manifest["objects"]["entries"][0]["key"] == old_key
    assert manifest["database"]["snapshot"]["exported"] is True
    assert manifest["database"]["dump_tenant_proof"]["verified"] is True
