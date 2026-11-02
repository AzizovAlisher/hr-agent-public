#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from validate_external_gate_register import validate


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "docs/evidence/external-gate-register-e18.template.json"


class ExternalGateTemplateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = json.loads(TEMPLATE.read_text(encoding="utf-8"))

    def assert_rejected(self, payload) -> None:
        self.assertNotEqual(validate(payload), [])

    def test_truthful_pending_template_is_valid_and_never_ready(self) -> None:
        self.assertEqual(validate(self.payload), [])
        self.assertEqual(self.payload["release_decision"], "not_go")

    def test_false_go_or_completed_provider_is_rejected(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["release_decision"] = "go"
        self.assert_rejected(payload)
        payload = copy.deepcopy(self.payload)
        payload["providers"][0]["status"] = "certified"
        payload["providers"][0]["certification"] = {"claimed": True}
        self.assert_rejected(payload)

    def test_extra_token_and_authorization_fields_are_rejected(self) -> None:
        for key, value in (
            ("token", "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcd"),
            ("authorization", "Bearer ya29.not-a-real-credential-value"),
        ):
            with self.subTest(key=key):
                payload = copy.deepcopy(self.payload)
                payload["providers"][0][key] = value
                errors = validate(payload)
                self.assertTrue(any("extra=" in error for error in errors))
                self.assertTrue(any("credential" in error for error in errors))

    def test_incident_approval_and_hosted_completion_are_rejected(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["openrouter_incident"]["status"] = "closed"
        self.assert_rejected(payload)
        payload = copy.deepcopy(self.payload)
        payload["approvals"][0]["status"] = "accepted"
        self.assert_rejected(payload)
        payload = copy.deepcopy(self.payload)
        payload["hosted_evidence"]["status"] = "passed"
        self.assert_rejected(payload)

    def test_timestamp_must_be_utc(self) -> None:
        for timestamp in ("2026-08-24T12:00:00", "2026-08-24T15:00:00+03:00"):
            with self.subTest(timestamp=timestamp):
                payload = copy.deepcopy(self.payload)
                payload["updated_at"] = timestamp
                self.assert_rejected(payload)


if __name__ == "__main__":
    unittest.main()
