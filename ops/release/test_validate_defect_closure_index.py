#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from validate_defect_closure_index import validate


ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "docs/DEFECT_LEDGER.md"
INDEX = ROOT / "docs/evidence/defect-closure-index-e18.json"


class DefectClosureIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = json.loads(INDEX.read_text(encoding="utf-8"))

    def validate(self, payload):
        return validate(
            payload,
            ledger_path=LEDGER,
            repository_root=ROOT,
            verify_commits=False,
        )

    def test_current_index_has_exact_structural_ledger_parity(self) -> None:
        self.assertEqual(self.validate(self.payload), [])

    def test_missing_or_extra_defect_is_rejected(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["records"].pop(next(iter(payload["records"])))
        self.assertTrue(any("ledger parity" in error for error in self.validate(payload)))
        payload = copy.deepcopy(self.payload)
        payload["records"]["DEF-999"] = copy.deepcopy(payload["records"]["SEC-001"])
        self.assertTrue(any("ledger parity" in error for error in self.validate(payload)))

    def test_unknown_context_or_missing_locator_is_rejected(self) -> None:
        payload = copy.deepcopy(self.payload)
        record = next(iter(payload["records"].values()))
        record["context"] = "unknown"
        record["regression_locators"] = []
        errors = self.validate(payload)
        self.assertTrue(any("unknown context" in error for error in errors))
        self.assertTrue(any("current-tree locators" in error for error in errors))

    def test_ledger_fields_and_row_hash_are_bound_exactly(self) -> None:
        mutations = {
            "severity": 4,
            "status": "closed-ish",
            "area": "invented area",
            "independent_retest": "generic passed",
            "ledger_row_sha256": "0" * 64,
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                payload = copy.deepcopy(self.payload)
                payload["records"]["SEC-001"][field] = value
                errors = self.validate(payload)
                self.assertTrue(any(field in error for error in errors), errors)

    def test_closed_and_verification_adjacent_dispositions_are_enforced(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["records"]["SEC-001"]["adjacent_flow_result"] = "pending_hosted"
        self.assertTrue(any("expected 'passed'" in error for error in self.validate(payload)))
        payload = copy.deepcopy(self.payload)
        payload["records"]["DEF-055"]["adjacent_flow_result"] = "passed"
        self.assertTrue(
            any("expected 'pending_hosted'" in error for error in self.validate(payload))
        )

    def test_schema_drift_and_missing_completion_report_are_rejected(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["records"]["SEC-001"]["self_certified"] = True
        self.assertTrue(any("extra=" in error for error in self.validate(payload)))
        payload = copy.deepcopy(self.payload)
        payload["records"]["SEC-001"]["evidence_paths"] = [
            "docs/DEFECT_LEDGER.md"
        ]
        self.assertTrue(any("completion report" in error for error in self.validate(payload)))

    def test_reproduction_is_bound_to_ledger_text_and_current_locators(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["records"]["SEC-001"]["reproduction"] = "SEC-001 generic claim"
        self.assertTrue(
            any("ledger condition/evidence" in error for error in self.validate(payload))
        )
        payload = copy.deepcopy(self.payload)
        payload["records"]["SEC-001"]["reproduction"] = payload["records"][
            "SEC-001"
        ]["reproduction"].replace("apps/api/tests/test_private_object_storage.py, ", "")
        self.assertTrue(
            any("every regression locator" in error for error in self.validate(payload))
        )

    def test_locators_cannot_escape_or_fake_nodes(self) -> None:
        for locator in (
            "/etc/passwd",
            "../../etc/passwd",
            "docs/DEFECT_LEDGER.md::definitely_not_a_test",
            "docs/DEFECT_LEDGER.md#invented-node",
        ):
            with self.subTest(locator=locator):
                payload = copy.deepcopy(self.payload)
                record = payload["records"]["SEC-001"]
                record["regression_locators"] = [locator]
                record["reproduction"] = f"{record['reproduction']} {locator}"
                self.assertTrue(
                    any("current-tree locators" in error for error in self.validate(payload))
                )


if __name__ == "__main__":
    unittest.main()
