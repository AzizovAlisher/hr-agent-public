#!/usr/bin/env python3
"""Validate the strict, secret-free *pending* external-gate template.

This validator deliberately cannot authorize release. Completed provider,
incident, hosted-artifact, and human-approval evidence is an external signed
attestation bound to the immutable candidate revision.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


PROVIDER_IDS = {
    "google_workspace",
    "microsoft_365",
    "telegram",
    "hh_ru",
}
APPROVAL_ROLES = {"product", "security", "operations", "legal"}
ROOT_FIELDS = {
    "schema_version",
    "candidate",
    "updated_at",
    "release_decision",
    "providers",
    "openrouter_incident",
    "approvals",
    "hosted_evidence",
}
PROVIDER_FIELDS = {"id", "status", "certification"}
INCIDENT_FIELDS = {
    "status",
    "old_key_fingerprint_sha256",
    "new_key_fingerprint_sha256",
    "revoked_at",
    "old_probe_http_status",
    "new_probe_http_status",
    "operator",
    "evidence_reference",
}
APPROVAL_FIELDS = {
    "role",
    "status",
    "reviewer",
    "decided_at",
    "evidence_reference",
}
HOSTED_FIELDS = {
    "status",
    "head_sha",
    "run_ids",
    "checks",
    "artifact_digests_sha256",
}
SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-(?:or-)?[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b(?:ghp|github_pat|xox[baprs])_[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bya29\.[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}", re.IGNORECASE),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def _valid_utc_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)


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


def _scan_secret_values(value: Any, path: str, errors: list[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _scan_secret_values(child, f"{path}.{key}", errors)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_secret_values(child, f"{path}[{index}]", errors)
    elif isinstance(value, str) and any(
        pattern.search(value) for pattern in SECRET_VALUE_PATTERNS
    ):
        errors.append(f"{path}: value resembles a credential and is prohibited")


def validate(payload: Any) -> list[str]:
    errors: list[str] = []
    if not _exact_fields(payload, ROOT_FIELDS, "$", errors):
        return errors
    _scan_secret_values(payload, "$", errors)
    if payload["schema_version"] != 1:
        errors.append("$.schema_version: expected 1")
    if payload["candidate"] != "E18":
        errors.append("$.candidate: expected E18")
    if not _valid_utc_timestamp(payload["updated_at"]):
        errors.append("$.updated_at: UTC timestamp required")
    if payload["release_decision"] != "not_go":
        errors.append("$.release_decision: committed template must remain 'not_go'")

    providers = payload["providers"]
    if not isinstance(providers, list):
        errors.append("$.providers: array required")
    else:
        ids: list[Any] = []
        for index, provider in enumerate(providers):
            path = f"$.providers[{index}]"
            if not _exact_fields(provider, PROVIDER_FIELDS, path, errors):
                continue
            ids.append(provider["id"])
            if provider["status"] not in {
                "awaiting_credentials",
                "scheduled",
                "failed",
            }:
                errors.append(f"{path}.status: pending-template status required")
            if provider["certification"] is not None:
                errors.append(f"{path}.certification: must remain null in source template")
        if len(ids) != len(set(ids)) or set(ids) != PROVIDER_IDS:
            errors.append("$.providers: each mandatory provider required exactly once")

    incident = payload["openrouter_incident"]
    if _exact_fields(incident, INCIDENT_FIELDS, "$.openrouter_incident", errors):
        if incident["status"] != "blocking_rotation":
            errors.append("$.openrouter_incident.status: must remain blocking_rotation")
        if any(incident[field] is not None for field in INCIDENT_FIELDS - {"status"}):
            errors.append("$.openrouter_incident: closure evidence is external, not committed")

    approvals = payload["approvals"]
    if not isinstance(approvals, list):
        errors.append("$.approvals: array required")
    else:
        roles: list[Any] = []
        for index, approval in enumerate(approvals):
            path = f"$.approvals[{index}]"
            if not _exact_fields(approval, APPROVAL_FIELDS, path, errors):
                continue
            roles.append(approval["role"])
            if approval["status"] != "awaiting_review":
                errors.append(f"{path}.status: source template must await review")
            if any(
                approval[field] is not None
                for field in APPROVAL_FIELDS - {"role", "status"}
            ):
                errors.append(f"{path}: decision evidence is external, not committed")
        if len(roles) != len(set(roles)) or set(roles) != APPROVAL_ROLES:
            errors.append("$.approvals: each named approval required exactly once")

    hosted = payload["hosted_evidence"]
    if _exact_fields(hosted, HOSTED_FIELDS, "$.hosted_evidence", errors):
        if hosted["status"] != "pending_exact_revision":
            errors.append("$.hosted_evidence.status: source template must remain pending")
        if hosted["head_sha"] is not None:
            errors.append("$.hosted_evidence.head_sha: completed attestation is external")
        if (
            hosted["run_ids"] != []
            or hosted["checks"] != {}
            or hosted["artifact_digests_sha256"] != {}
        ):
            errors.append("$.hosted_evidence: completed evidence is external, not committed")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("template", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.template.read_text(encoding="utf-8"))
    errors = validate(payload)
    if errors:
        for error in errors:
            print(error)
        return 1
    print(
        json.dumps(
            {"pending_template_valid": True, "release_ready": False},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
