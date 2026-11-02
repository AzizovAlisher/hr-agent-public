#!/usr/bin/env python3
"""Reject plaintext credentials from tracked release documents.

The release evidence may describe credential-dependent gates, but it must never
contain a usable password, token, API key, or client secret. Diagnostics name
only the field and location so a failed scan cannot repeat the value in CI logs.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Iterable


SUPPORTED_SUFFIXES = {".json", ".md", ".txt", ".yaml", ".yml"}
SENSITIVE_KEY_RE = re.compile(
    r"(?:^|_)(?:password|passwd|secret|token|api_key)(?:$|_)", re.IGNORECASE
)
PASSWORD_TABLE_RE = re.compile(
    r"^\s*\|\s*(?:preview\s+|reviewer\s+|temporary\s+|initial\s+|admin\s+)?"
    r"(?:password|пароль)\s*\|\s*(?P<value>.*?)\s*\|\s*$",
    re.IGNORECASE,
)
ASSIGNMENT_RE = re.compile(
    r"^\s*(?:export\s+)?(?P<key>[A-Za-z][A-Za-z0-9_.-]*)"
    r"\s*[:=]\s*(?P<value>.*?)\s*$"
)
PLACEHOLDER_WORDS = {
    "",
    "***",
    "n/a",
    "none",
    "not committed",
    "null",
    "out of band",
    "out-of-band",
    "pending",
    "redacted",
    "removed",
}


def _normalized(value: str) -> str:
    result = value.strip().strip("`\"'").strip()
    return result


def _is_sensitive_key(key: str) -> bool:
    snake_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    snake_case = re.sub(r"[.\-]+", "_", snake_case)
    return SENSITIVE_KEY_RE.search(snake_case) is not None


def _is_placeholder(value: str) -> bool:
    normalized = _normalized(value)
    lowered = normalized.lower()
    if lowered in PLACEHOLDER_WORDS:
        return True
    if normalized.startswith("<") and normalized.endswith(">"):
        return True
    if normalized.startswith("[") and normalized.endswith("]"):
        return True
    if re.fullmatch(r"\$\{[A-Z][A-Z0-9_]*\}", normalized):
        return True
    return False


def _iter_files(paths: Iterable[Path]) -> Iterable[Path]:
    for path in paths:
        if path.is_dir():
            yield from sorted(
                candidate
                for candidate in path.rglob("*")
                if candidate.is_file() and candidate.suffix.lower() in SUPPORTED_SUFFIXES
            )
        elif path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            yield path


def _scan_json(value: Any, *, path: str, errors: list[str]) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            nested_path = f"{path}.{key}"
            if (
                isinstance(key, str)
                and _is_sensitive_key(key)
                and isinstance(nested, str)
                and not _is_placeholder(nested)
            ):
                errors.append(f"{nested_path}: plaintext credential value is forbidden")
            _scan_json(nested, path=nested_path, errors=errors)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _scan_json(nested, path=f"{path}[{index}]", errors=errors)


def validate_file(path: Path) -> list[str]:
    errors: list[str] = []
    text = path.read_text(encoding="utf-8")
    for line_number, line in enumerate(text.splitlines(), start=1):
        table_match = PASSWORD_TABLE_RE.match(line)
        if table_match and not _is_placeholder(table_match.group("value")):
            errors.append(
                f"{path}:{line_number}: plaintext password table value is forbidden"
            )
        assignment_match = ASSIGNMENT_RE.match(line)
        if (
            assignment_match
            and _is_sensitive_key(assignment_match.group("key"))
            and not _is_placeholder(assignment_match.group("value"))
        ):
            errors.append(
                f"{path}:{line_number}: plaintext credential assignment is forbidden"
            )
    if path.suffix.lower() == ".json":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            errors.append(f"{path}:{exc.lineno}: invalid JSON")
        else:
            _scan_json(payload, path=str(path), errors=errors)
    return errors


def validate_paths(paths: Iterable[Path]) -> list[str]:
    files = list(dict.fromkeys(_iter_files(paths)))
    errors: list[str] = []
    if not files:
        return ["no supported release documents were supplied"]
    for path in files:
        errors.extend(validate_file(path))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    files = list(dict.fromkeys(_iter_files(args.paths)))
    errors = validate_paths(args.paths)
    if errors:
        for error in errors:
            print(error)
        return 1
    print(json.dumps({"credential_values_found": 0, "files_scanned": len(files)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
