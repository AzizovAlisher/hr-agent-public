#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from validate_release_document_credentials import validate_file, validate_paths


ROOT = Path(__file__).resolve().parents[2]


class ReleaseDocumentCredentialTest(unittest.TestCase):
    def test_current_release_documents_are_secret_free(self) -> None:
        self.assertEqual(
            validate_paths(
                [
                    ROOT / "docs/PILOT_COMPLETION_REPORT_2026-08-24.md",
                    ROOT / "docs/evidence",
                ]
            ),
            [],
        )

    def test_password_table_values_are_rejected_without_echoing_value(self) -> None:
        for label in ("Password", "Reviewer password", "Пароль"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "report.md"
                value = "canary-credential-value"
                path.write_text(f"| {label} | `{value}` |\n", encoding="utf-8")
                errors = validate_file(path)
                self.assertNotEqual(errors, [])
                self.assertNotIn(value, "\n".join(errors))

    def test_environment_json_and_yaml_credentials_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = root / "release.txt"
            environment.write_text("PROVIDER_API_TOKEN=canary-value\n", encoding="utf-8")
            payload = root / "release.json"
            payload.write_text(
                json.dumps(
                    {
                        "snake": {"client_secret": "canary-value"},
                        "camel": {"clientSecret": "canary-value"},
                    }
                ),
                encoding="utf-8",
            )
            yaml = root / "release.yml"
            yaml.write_text("client_secret: canary-value\n", encoding="utf-8")
            errors = validate_paths([root])
            self.assertEqual(len(errors), 4)
            self.assertNotIn("canary-value", "\n".join(errors))

    def test_placeholders_and_security_prose_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = root / "release.md"
            document.write_text(
                "Password reset invalidates every session.\n"
                "| Password | `out-of-band` |\n"
                "S3_SECRET_KEY=<independent secret>\n"
                "PROVIDER_TOKEN=${PROVIDER_TOKEN}\n",
                encoding="utf-8",
            )
            payload = root / "release.json"
            payload.write_text(
                json.dumps({"password": "redacted", "api_key": None, "token_count": 2}),
                encoding="utf-8",
            )
            self.assertEqual(validate_paths([root]), [])

    def test_missing_input_is_rejected(self) -> None:
        self.assertNotEqual(validate_paths([Path("does-not-exist")]), [])


if __name__ == "__main__":
    unittest.main()
