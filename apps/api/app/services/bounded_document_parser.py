"""Parent-side contract for parsing untrusted PDF and DOCX files.

All complex document parsing happens in a short-lived child process with a
scrubbed environment, operating-system resource limits, a wall-clock timeout,
and a bounded output pipe.  API and worker processes should use this module
instead of importing parser libraries directly.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PARSER_TIMEOUT_SECONDS = 30.0
MAX_PARSED_TEXT_BYTES = 2 * 1024 * 1024
MAX_DOCUMENT_INVENTORY_BYTES = 4 * 1024 * 1024
SUPPORTED_SUFFIXES = frozenset({".pdf", ".docx"})
MAX_SPREADSHEET_ROWS = 500
MAX_SPREADSHEET_OUTPUT_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SpreadsheetInventory:
    """Rows plus bounded non-cell OOXML text used only for privacy inventory."""

    rows: tuple[dict[str, Any], ...]
    metadata_text: str


class BoundedDocumentParseError(ValueError):
    """The child rejected or could not safely parse the document."""


class BoundedDocumentParseTimeout(TimeoutError):
    """The child exceeded the parser wall-clock budget."""


async def _run_child(
    module: str,
    *arguments: str,
    max_output_bytes: int,
) -> tuple[bytes, int]:
    api_root = Path(__file__).resolve().parents[2]
    # Run away from the API root so pydantic-settings cannot discover the
    # application's ``.env`` while importing parser dependencies. These
    # children receive a deliberately scrubbed environment and must remain
    # independent of database, authentication, provider, and tenant config.
    child_working_directory = Path(__file__).resolve().parent
    child_environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(api_root),
        "PYTHONIOENCODING": "utf-8",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        module,
        *arguments,
        cwd=str(child_working_directory),
        env=child_environment,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )

    async def collect_bounded_stdout() -> tuple[bytes, int]:
        if process.stdout is None:  # pragma: no cover - subprocess contract
            raise BoundedDocumentParseError("parser_output_unavailable")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await process.stdout.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_output_bytes:
                process.kill()
                await process.wait()
                raise BoundedDocumentParseError("parser_output_limit_exceeded")
            chunks.append(chunk)
        return b"".join(chunks), await process.wait()

    try:
        stdout, returncode = await asyncio.wait_for(
            collect_bounded_stdout(),
            timeout=PARSER_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        raise BoundedDocumentParseTimeout("document_parse_timeout") from None
    return stdout, int(returncode or 0)


async def extract_document_text(path: Path, suffix: str | None = None) -> str:
    """Extract bounded text without loading a complex parser in this process."""

    normalized_suffix = (suffix or path.suffix).casefold()
    if normalized_suffix not in SUPPORTED_SUFFIXES:
        raise BoundedDocumentParseError("document_type_unsupported")
    stdout, returncode = await _run_child(
        "app.services.document_parser_process",
        str(path),
        normalized_suffix,
        max_output_bytes=MAX_PARSED_TEXT_BYTES,
    )
    if returncode == 69:
        raise BoundedDocumentParseError("document_text_limit_exceeded")
    if returncode != 0:
        raise BoundedDocumentParseError("document_parse_failed")
    return stdout.decode("utf-8", errors="replace").strip()


async def extract_document_inventory(path: Path, suffix: str | None = None) -> str:
    """Inventory only the strict, bounded semantic surface of DOCX/PDF."""

    normalized_suffix = (suffix or path.suffix).casefold()
    if normalized_suffix not in SUPPORTED_SUFFIXES:
        raise BoundedDocumentParseError("document_type_unsupported")
    stdout, returncode = await _run_child(
        "app.services.document_inventory_process",
        str(path),
        normalized_suffix,
        max_output_bytes=MAX_DOCUMENT_INVENTORY_BYTES,
    )
    error_codes = {
        67: "document_inventory_empty",
        68: "docx_package_unsupported",
        69: "docx_relationship_unsupported",
        70: "docx_feature_unsupported",
        71: "document_inventory_limit_exceeded",
        72: "pdf_interactive_unsupported",
        73: "pdf_xobject_unsupported",
        74: "pdf_content_unsupported",
        75: "pdf_inventory_empty",
    }
    if returncode in error_codes:
        raise BoundedDocumentParseError(error_codes[returncode])
    if returncode != 0:
        raise BoundedDocumentParseError("document_inventory_failed")
    return stdout.decode("utf-8", errors="strict").strip()


async def extract_spreadsheet_rows(
    path: Path,
    *,
    max_rows: int = MAX_SPREADSHEET_ROWS,
) -> list[dict[str, Any]]:
    """Read an XLSX workbook in the bounded parser child."""

    if max_rows < 1 or max_rows > 5_000:
        raise ValueError("max_rows must be between 1 and 5000")

    stdout, returncode = await _run_child(
        "app.services.spreadsheet_parser_process",
        str(path),
        str(max_rows),
        "rows",
        max_output_bytes=MAX_SPREADSHEET_OUTPUT_BYTES,
    )
    error_codes = {
        67: "spreadsheet_row_limit_exceeded",
        68: "spreadsheet_column_limit_exceeded",
        69: "spreadsheet_cell_limit_exceeded",
        70: "spreadsheet_total_text_limit_exceeded",
        71: "spreadsheet_header_blank",
        72: "spreadsheet_header_duplicate",
        73: "spreadsheet_expanded_limit_exceeded",
        74: "spreadsheet_sheet_count_unsupported",
        75: "spreadsheet_formula_unsupported",
        76: "spreadsheet_package_unsupported",
        77: "spreadsheet_relationship_unsupported",
        78: "spreadsheet_feature_unsupported",
        79: "spreadsheet_metadata_limit_exceeded",
    }
    if returncode in error_codes:
        raise BoundedDocumentParseError(error_codes[returncode])
    if returncode != 0:
        raise BoundedDocumentParseError("spreadsheet_parse_failed")
    try:
        decoded = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BoundedDocumentParseError("document_parse_failed") from exc
    if not isinstance(decoded, list) or len(decoded) > max_rows:
        raise BoundedDocumentParseError("spreadsheet_parse_failed")
    if not all(isinstance(row, dict) for row in decoded):
        raise BoundedDocumentParseError("spreadsheet_parse_failed")
    return decoded


async def extract_spreadsheet_inventory(
    path: Path,
    *,
    max_rows: int = MAX_SPREADSHEET_ROWS,
) -> SpreadsheetInventory:
    """Return privacy-searchable rows and bounded allowed OOXML metadata."""

    if max_rows < 1 or max_rows > 5_000:
        raise ValueError("max_rows must be between 1 and 5000")
    stdout, returncode = await _run_child(
        "app.services.spreadsheet_parser_process",
        str(path),
        str(max_rows),
        "inventory",
        max_output_bytes=MAX_SPREADSHEET_OUTPUT_BYTES,
    )
    error_codes = {
        67: "spreadsheet_row_limit_exceeded",
        68: "spreadsheet_column_limit_exceeded",
        69: "spreadsheet_cell_limit_exceeded",
        70: "spreadsheet_total_text_limit_exceeded",
        71: "spreadsheet_header_blank",
        72: "spreadsheet_header_duplicate",
        73: "spreadsheet_expanded_limit_exceeded",
        74: "spreadsheet_sheet_count_unsupported",
        75: "spreadsheet_formula_unsupported",
        76: "spreadsheet_package_unsupported",
        77: "spreadsheet_relationship_unsupported",
        78: "spreadsheet_feature_unsupported",
        79: "spreadsheet_metadata_limit_exceeded",
    }
    if returncode in error_codes:
        raise BoundedDocumentParseError(error_codes[returncode])
    if returncode != 0:
        raise BoundedDocumentParseError("spreadsheet_parse_failed")
    try:
        decoded = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BoundedDocumentParseError("document_parse_failed") from exc
    if not isinstance(decoded, dict) or set(decoded) != {"rows", "metadata_text"}:
        raise BoundedDocumentParseError("spreadsheet_parse_failed")
    rows = decoded["rows"]
    metadata_text = decoded["metadata_text"]
    if (
        not isinstance(rows, list)
        or len(rows) > max_rows
        or not all(isinstance(row, dict) for row in rows)
        or not isinstance(metadata_text, str)
    ):
        raise BoundedDocumentParseError("spreadsheet_parse_failed")
    return SpreadsheetInventory(rows=tuple(rows), metadata_text=metadata_text)
