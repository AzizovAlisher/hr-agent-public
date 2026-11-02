"""Resource-bounded child process for untrusted CV text extraction.

The durable worker launches this module with a scrubbed environment. Upload
validation and malware scanning have already completed before this parser runs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

MAX_OUTPUT_CHARS = 500_000
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_ADDRESS_SPACE_BYTES = 768 * 1024 * 1024
MAX_CPU_SECONDS = 20
MAX_OPEN_FILES = 64


def _apply_resource_limits() -> None:
    try:
        import resource

        resource.setrlimit(
            resource.RLIMIT_CPU,
            (MAX_CPU_SECONDS, MAX_CPU_SECONDS),
        )
        resource.setrlimit(
            resource.RLIMIT_AS,
            (MAX_ADDRESS_SPACE_BYTES, MAX_ADDRESS_SPACE_BYTES),
        )
        resource.setrlimit(
            resource.RLIMIT_NOFILE,
            (MAX_OPEN_FILES, MAX_OPEN_FILES),
        )
        # The child writes only to a pipe, but retaining a small file-output
        # ceiling protects against a future parser accidentally creating data.
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (4 * 1024 * 1024, 4 * 1024 * 1024),
        )
    except (ImportError, OSError, ValueError):
        # Windows development lacks ``resource``. Production images are Linux
        # and the parent still enforces a wall-clock timeout and output cap.
        pass


def main() -> int:
    if len(sys.argv) != 3:
        return 64
    _apply_resource_limits()
    path = Path(sys.argv[1])
    suffix = sys.argv[2].casefold()
    if not path.is_file() or suffix not in {".pdf", ".docx"}:
        return 65
    try:
        from app.engine.parsing import extract_text_from_docx, extract_text_from_pdf

        text = (
            extract_text_from_docx(path)
            if suffix == ".docx"
            else extract_text_from_pdf(path)
        )
    except Exception:
        return 66
    if len(text) > MAX_OUTPUT_CHARS:
        return 69
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) > MAX_OUTPUT_BYTES:
        return 69
    try:
        os.write(sys.stdout.fileno(), encoded)
    except OSError:
        return 74
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
