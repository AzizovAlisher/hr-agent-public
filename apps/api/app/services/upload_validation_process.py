"""Resource-bounded child for structural validation of untrusted uploads."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from app.services.document_parser_process import _apply_resource_limits
from app.storage.validation import UploadValidationError, validate_upload_path


def _write(payload: dict[str, object]) -> int:
    encoded = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    try:
        os.write(sys.stdout.fileno(), encoded)
    except OSError:
        return 74
    return 0


def main() -> int:
    if len(sys.argv) != 5:
        return 64
    _apply_resource_limits()
    path = Path(sys.argv[1])
    filename = sys.argv[2]
    media_type = sys.argv[3]
    try:
        raw_allowed = json.loads(sys.argv[4])
    except json.JSONDecodeError:
        return 64
    if (
        not isinstance(raw_allowed, list)
        or not raw_allowed
        or len(raw_allowed) > 32
        or not all(isinstance(item, str) for item in raw_allowed)
    ):
        return 64
    try:
        suffix = validate_upload_path(
            path,
            filename=filename,
            media_type=media_type,
            allowed_suffixes=set(raw_allowed),
        )
    except UploadValidationError as exc:
        return _write(
            {
                "ok": False,
                "status_code": exc.status_code,
                "detail": exc.detail,
            }
        )
    except Exception:  # noqa: BLE001 - never expose child parser internals
        return _write(
            {
                "ok": False,
                "status_code": 422,
                "detail": "Uploaded file could not be validated safely.",
            }
        )
    return _write({"ok": True, "suffix": suffix})


if __name__ == "__main__":
    raise SystemExit(main())
