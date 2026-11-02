"""Safe, reusable parsing contract for CSV/XLSX candidate imports."""

from __future__ import annotations

import asyncio
import csv
import io
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from app.schemas.sourcing import SourceCandidateInput
from app.services.bounded_document_parser import (
    BoundedDocumentParseError,
    BoundedDocumentParseTimeout,
    extract_spreadsheet_rows,
)
from app.storage.validation import UploadValidationError, validate_upload_path_bounded

IMPORT_FIELDS = frozenset(
    {
        "external_id",
        "position_id",
        "name",
        "phone",
        "email",
        "cv_text",
        "source_url",
        "utm_campaign",
    }
)
REQUIRED_IMPORT_FIELDS = frozenset({"external_id", "position_id", "name", "cv_text"})
MAX_COLUMNS = 128
MAX_CELL_CHARS = 20_000
MAX_TOTAL_CHARS = 2_000_000


@dataclass(frozen=True, slots=True)
class SourceFileImportError(ValueError):
    status_code: int
    detail: str | dict[str, object]

    def __str__(self) -> str:
        return str(self.detail)


def safe_validation_errors(exc: ValidationError) -> list[dict[str, object]]:
    return [
        {
            "type": item["type"],
            "loc": list(item["loc"]),
            "msg": item["msg"],
        }
        for item in exc.errors(include_input=False, include_url=False)
    ]


def rows_from_values(
    headers: Iterable[object],
    rows: Iterable[Iterable[object]],
    mapping: dict[str, str] | None = None,
    *,
    max_rows: int,
) -> list[SourceCandidateInput]:
    normalized_headers = [
        str(value).strip().lower() if value is not None else "" for value in headers
    ]
    if not normalized_headers or any(not value for value in normalized_headers):
        raise SourceFileImportError(422, "Import headers cannot be empty")
    if len(normalized_headers) > MAX_COLUMNS:
        raise SourceFileImportError(
            413,
            {"code": "spreadsheet_column_limit_exceeded"},
        )
    if any(len(value) > MAX_CELL_CHARS for value in normalized_headers):
        raise SourceFileImportError(
            413,
            {"code": "spreadsheet_cell_limit_exceeded"},
        )
    if len(set(normalized_headers)) != len(normalized_headers):
        raise SourceFileImportError(422, "Import headers must be unique")
    requested_mapping = mapping or {}
    if (
        set(requested_mapping) - IMPORT_FIELDS
        or any(not source for source in requested_mapping.values())
        or len(set(requested_mapping.values())) != len(requested_mapping)
    ):
        raise SourceFileImportError(422, {"code": "mapping_invalid"})
    canonical_to_source = {
        field: requested_mapping.get(field, field) for field in IMPORT_FIELDS
    }
    available_headers = set(normalized_headers)
    if not REQUIRED_IMPORT_FIELDS.issubset(
        {
            field
            for field, source in canonical_to_source.items()
            if source in available_headers
        }
    ):
        raise SourceFileImportError(
            422,
            "Import requires external_id, position_id, name, and cv_text columns",
        )

    parsed: list[SourceCandidateInput] = []
    total_chars = sum(len(value) for value in normalized_headers)
    for number, values in enumerate(rows, start=2):
        values_tuple = tuple(values)
        if len(values_tuple) > MAX_COLUMNS:
            raise SourceFileImportError(
                413,
                {"code": "spreadsheet_column_limit_exceeded", "row": number},
            )
        converted = [
            str(value) if value is not None else None for value in values_tuple
        ]
        if any(len(value) > MAX_CELL_CHARS for value in converted if value is not None):
            raise SourceFileImportError(
                413,
                {"code": "spreadsheet_cell_limit_exceeded", "row": number},
            )
        total_chars += sum(len(value or "") for value in converted)
        if total_chars > MAX_TOTAL_CHARS:
            raise SourceFileImportError(
                413,
                {"code": "spreadsheet_total_text_limit_exceeded"},
            )
        if not any(value is not None and value.strip() for value in converted):
            continue
        if len(parsed) >= max_rows:
            raise SourceFileImportError(
                413,
                {
                    "code": "limit_exceeded",
                    "limit": max_rows,
                    "requested": max_rows + 1,
                },
            )
        source_values = dict(zip(normalized_headers, converted, strict=False))
        raw = {
            canonical: source_values.get(source)
            for canonical, source in canonical_to_source.items()
            if source in source_values
        }
        try:
            parsed.append(SourceCandidateInput.model_validate(raw))
        except ValidationError as exc:
            raise SourceFileImportError(
                422,
                {"row": number, "errors": safe_validation_errors(exc)},
            ) from exc
    if not parsed:
        raise SourceFileImportError(422, "Import file is empty")
    return parsed


def rows_from_csv(
    content: bytes,
    mapping: dict[str, str] | None = None,
    *,
    max_rows: int,
) -> list[SourceCandidateInput]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceFileImportError(422, "CSV must use UTF-8") from exc
    try:
        reader = csv.reader(io.StringIO(text), strict=True)
        headers = next(reader)
        return rows_from_values(headers, reader, mapping, max_rows=max_rows)
    except StopIteration as exc:
        raise SourceFileImportError(422, "CSV is malformed or empty") from exc
    except csv.Error as exc:
        raise SourceFileImportError(422, "CSV is malformed") from exc


async def rows_from_xlsx(
    content: bytes,
    mapping: dict[str, str] | None = None,
    *,
    max_rows: int,
) -> list[SourceCandidateInput]:
    try:
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=True) as temporary:
            temporary.write(content)
            temporary.flush()
            path = Path(temporary.name)
            await validate_upload_path_bounded(
                path,
                filename="candidates.xlsx",
                media_type=(
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                ),
                allowed_suffixes={".xlsx"},
            )
            decoded = await extract_spreadsheet_rows(path, max_rows=max_rows)
    except UploadValidationError as exc:
        raise SourceFileImportError(exc.status_code, exc.detail) from exc
    except BoundedDocumentParseTimeout as exc:
        raise SourceFileImportError(
            422,
            {"code": "spreadsheet_parse_timeout"},
        ) from exc
    except BoundedDocumentParseError as exc:
        code = str(exc)
        limit_codes = {
            "spreadsheet_row_limit_exceeded",
            "spreadsheet_column_limit_exceeded",
            "spreadsheet_cell_limit_exceeded",
            "spreadsheet_total_text_limit_exceeded",
            "spreadsheet_expanded_limit_exceeded",
            "parser_output_limit_exceeded",
        }
        raise SourceFileImportError(
            413 if code in limit_codes else 422,
            {"code": code},
        ) from exc
    if not decoded:
        raise SourceFileImportError(422, "Import file is empty")
    headers = list(decoded[0])
    values = [tuple(row.get(header) for header in headers) for row in decoded]
    return rows_from_values(headers, values, mapping, max_rows=max_rows)


async def parse_source_file(
    content: bytes,
    *,
    format_name: str,
    mapping: dict[str, str] | None,
    max_rows: int,
) -> list[SourceCandidateInput]:
    if format_name == "csv":
        return await asyncio.to_thread(
            rows_from_csv,
            content,
            mapping,
            max_rows=max_rows,
        )
    if format_name == "xlsx":
        return await rows_from_xlsx(content, mapping, max_rows=max_rows)
    raise SourceFileImportError(415, "This endpoint accepts UTF-8 CSV or XLSX")


__all__ = [
    "IMPORT_FIELDS",
    "SourceFileImportError",
    "parse_source_file",
    "rows_from_csv",
    "rows_from_values",
    "rows_from_xlsx",
]
