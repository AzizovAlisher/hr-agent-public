#!/usr/bin/env python3
"""Validate structural traceability between the defect ledger and E18 index.

This validator deliberately does not prove that a test ran, independently
verify a fix, or authorize a release. Those decisions require immutable hosted
artifacts and external reviewer attestations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any


DEFECT_ID_RE = re.compile(r"^(?:SEC|PRIV|DOM|AI|RUN|INT|UX|DEP|OPS|DEF)-\d+$")
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ROW_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ROOT_FIELDS = {"schema_version", "candidate", "composition", "contexts", "records"}
REQUIRED_CONTEXT_FIELDS = {
    "tenant",
    "role",
    "locale",
    "device_browser_or_na",
    "environment",
}
REQUIRED_RECORD_FIELDS = {
    "owner",
    "fix_commit",
    "context",
    "reproduction",
    "regression_locators",
    "independent_retest",
    "adjacent_flow_result",
    "evidence_paths",
    "severity",
    "status",
    "area",
    "ledger_row_sha256",
}
OWNERS = {
    "integration_lead",
    "security_data",
    "hiring_domain",
    "runtime_integration",
    "frontend_ux",
}
COMPLETION_REPORT = "docs/PILOT_COMPLETION_REPORT_2026-08-24.md"


def ledger_rows(path: Path) -> dict[str, dict[str, Any]]:
    """Parse exact defect-table rows and preserve their review text verbatim."""

    rows: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| ") or not line.endswith(" |"):
            continue
        cells = line[2:-2].split(" | ")
        if len(cells) != 6 or not DEFECT_ID_RE.fullmatch(cells[0]):
            continue
        defect_id, severity, status, area, fix_evidence, independent_retest = cells
        try:
            parsed_severity = int(severity)
        except ValueError:
            continue
        rows[defect_id] = {
            "severity": parsed_severity,
            "status": status,
            "area": area,
            "fix_and_regression_evidence": fix_evidence,
            "independent_retest": independent_retest,
            "row_sha256": hashlib.sha256(line.encode("utf-8")).hexdigest(),
        }
    return rows


def _exact_fields(value: Any, expected: set[str], path: str, errors: list[str]) -> bool:
    if not isinstance(value, dict):
        errors.append(f"{path}: object required")
        return False
    if set(value) != expected:
        errors.append(
            f"{path}: exact fields required missing={sorted(expected - set(value))} "
            f"extra={sorted(set(value) - expected)}"
        )
        return False
    return True


def _locator_exists(root: Path, locator: Any) -> bool:
    if not isinstance(locator, str) or not locator.strip():
        return False
    if locator != locator.strip() or "::" in locator or "#" in locator:
        return False
    relative = Path(locator)
    if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
        return False
    root_resolved = root.resolve()
    target = (root_resolved / relative).resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError:
        return False
    return target.is_file()


def validate(
    payload: Any,
    *,
    ledger_path: Path,
    repository_root: Path,
    verify_commits: bool = True,
) -> list[str]:
    errors: list[str] = []
    if not _exact_fields(payload, ROOT_FIELDS, "$", errors):
        return errors
    if payload["schema_version"] != 1:
        errors.append("$.schema_version: expected 1")
    if payload["candidate"] != "E18":
        errors.append("$.candidate: expected E18")
    composition = payload["composition"]
    if not isinstance(composition, str) or "does not replace execution artifacts" not in composition:
        errors.append("$.composition: execution-authority disclaimer required")

    contexts = payload["contexts"]
    if not isinstance(contexts, dict) or not contexts:
        errors.append("$.contexts: nonempty object required")
        contexts = {}
    else:
        for context_id, context in contexts.items():
            context_path = f"$.contexts.{context_id}"
            if not _exact_fields(context, REQUIRED_CONTEXT_FIELDS, context_path, errors):
                continue
            for field in REQUIRED_CONTEXT_FIELDS:
                if not isinstance(context[field], str) or not context[field].strip():
                    errors.append(f"{context_path}.{field}: nonempty string required")

    expected = ledger_rows(ledger_path)
    if not expected:
        errors.append("$: no defect rows parsed from ledger")
    records = payload["records"]
    if not isinstance(records, dict):
        errors.append("$.records: object required")
        return errors
    if set(records) != set(expected):
        missing = sorted(set(expected) - set(records))
        extra = sorted(set(records) - set(expected))
        errors.append(f"$.records: ledger parity failure missing={missing} extra={extra}")

    for defect_id, ledger in expected.items():
        path = f"$.records.{defect_id}"
        record = records.get(defect_id)
        if not _exact_fields(record, REQUIRED_RECORD_FIELDS, path, errors):
            continue
        if record["owner"] not in OWNERS:
            errors.append(f"{path}.owner: unknown owner")

        commit = record["fix_commit"]
        if not isinstance(commit, str) or not COMMIT_SHA_RE.fullmatch(commit):
            errors.append(f"{path}.fix_commit: full Git SHA required")
        elif verify_commits:
            result = subprocess.run(
                ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
                cwd=repository_root,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                errors.append(f"{path}.fix_commit: commit is not present")

        if record["context"] not in contexts:
            errors.append(f"{path}.context: unknown context")
        reproduction = record["reproduction"]
        if (
            not isinstance(reproduction, str)
            or defect_id not in reproduction
            or not reproduction.strip()
        ):
            errors.append(f"{path}.reproduction: must explicitly identify the defect")
        elif ledger["fix_and_regression_evidence"] not in reproduction:
            errors.append(f"{path}.reproduction: exact ledger condition/evidence text required")

        locators = record["regression_locators"]
        if not isinstance(locators, list) or not locators or not all(
            _locator_exists(repository_root, locator) for locator in locators
        ):
            errors.append(f"{path}.regression_locators: existing current-tree locators required")
        elif not all(locator in reproduction for locator in locators):
            errors.append(f"{path}.reproduction: every regression locator must be named")

        for field in ("severity", "status", "area", "independent_retest"):
            if record[field] != ledger[field]:
                errors.append(
                    f"{path}.{field}: must exactly match the ledger row "
                    f"({ledger[field]!r})"
                )

        row_hash = record["ledger_row_sha256"]
        if not isinstance(row_hash, str) or not ROW_SHA_RE.fullmatch(row_hash):
            errors.append(f"{path}.ledger_row_sha256: lowercase SHA-256 required")
        elif row_hash != ledger["row_sha256"]:
            errors.append(f"{path}.ledger_row_sha256: exact ledger-row hash mismatch")

        expected_adjacent = "passed" if ledger["status"] == "closed" else "pending_hosted"
        if record["adjacent_flow_result"] != expected_adjacent:
            errors.append(
                f"{path}.adjacent_flow_result: expected {expected_adjacent!r} "
                f"for ledger status {ledger['status']!r}"
            )

        evidence = record["evidence_paths"]
        if not isinstance(evidence, list) or not evidence or not all(
            _locator_exists(repository_root, locator) for locator in evidence
        ):
            errors.append(f"{path}.evidence_paths: existing current-tree locators required")
        elif COMPLETION_REPORT not in set(evidence):
            errors.append(f"{path}.evidence_paths: completion report is required")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("index", type=Path)
    parser.add_argument("--ledger", type=Path, default=Path("docs/DEFECT_LEDGER.md"))
    args = parser.parse_args()
    root = Path.cwd()
    payload = json.loads(args.index.read_text(encoding="utf-8"))
    errors = validate(payload, ledger_path=args.ledger, repository_root=root)
    if errors:
        for error in errors:
            print(error)
        return 1
    print(
        json.dumps(
            {
                "structurally_valid": True,
                "defects": len(payload["records"]),
                "execution_verified": False,
                "release_ready": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
