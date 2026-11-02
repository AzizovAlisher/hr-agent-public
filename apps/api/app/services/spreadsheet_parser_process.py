"""Resource-bounded XLSX reader used for untrusted candidate imports."""

from __future__ import annotations

import json
import os
import re
import sys
import zipfile
from html import unescape
from io import BytesIO
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from app.services.document_parser_process import _apply_resource_limits
from app.services.strict_zip_layout import (
    StrictZipLayoutError,
    validate_strict_zip_layout,
)

MAX_ROWS = 5_000
MAX_COLUMNS = 128
MAX_CELL_CHARS = 20_000
MAX_TOTAL_CHARS = 2_000_000
MAX_ARCHIVE_BYTES = 15 * 1024 * 1024
MAX_EXPANDED_BYTES = 50 * 1024 * 1024
MAX_PART_BYTES = 25 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
MAX_XML_ELEMENTS = 200_000

_REQUIRED_PARTS = frozenset(
    {
        "[Content_Types].xml",
        "_rels/.rels",
        "docProps/app.xml",
        "docProps/core.xml",
        "xl/_rels/workbook.xml.rels",
        "xl/styles.xml",
        "xl/theme/theme1.xml",
        "xl/workbook.xml",
        "xl/worksheets/sheet1.xml",
    }
)
_OPTIONAL_PARTS = frozenset({"xl/sharedStrings.xml"})

_OFFICE_RELATIONSHIP_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_CORE_RELATIONSHIP_NS = (
    "http://schemas.openxmlformats.org/package/2006/relationships/metadata/"
    "core-properties"
)
_ROOT_RELATIONSHIPS = frozenset(
    {
        (f"{_OFFICE_RELATIONSHIP_NS}/officeDocument", "xl/workbook.xml"),
        (_CORE_RELATIONSHIP_NS, "docProps/core.xml"),
        (
            f"{_OFFICE_RELATIONSHIP_NS}/extended-properties",
            "docProps/app.xml",
        ),
    }
)
_WORKBOOK_RELATIONSHIPS = frozenset(
    {
        (
            f"{_OFFICE_RELATIONSHIP_NS}/worksheet",
            "xl/worksheets/sheet1.xml",
        ),
        (f"{_OFFICE_RELATIONSHIP_NS}/styles", "xl/styles.xml"),
        (f"{_OFFICE_RELATIONSHIP_NS}/theme", "xl/theme/theme1.xml"),
    }
)
_SHARED_STRING_RELATIONSHIP = (
    f"{_OFFICE_RELATIONSHIP_NS}/sharedStrings",
    "xl/sharedStrings.xml",
)

_CONTENT_TYPE_DEFAULTS = {
    "rels": "application/vnd.openxmlformats-package.relationships+xml",
    "xml": "application/xml",
}
_CONTENT_TYPE_OVERRIDES = {
    "/docProps/app.xml": (
        "application/vnd.openxmlformats-officedocument.extended-properties+xml"
    ),
    "/docProps/core.xml": "application/vnd.openxmlformats-package.core-properties+xml",
    "/xl/styles.xml": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"
    ),
    "/xl/theme/theme1.xml": (
        "application/vnd.openxmlformats-officedocument.theme+xml"
    ),
    "/xl/workbook.xml": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
    ),
    "/xl/worksheets/sheet1.xml": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
    ),
}
_SHARED_STRING_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"
)

_WORKBOOK_REJECTED_ELEMENTS = frozenset(
    {
        "customWorkbookViews",
        "definedName",
        "externalReferences",
        "extLst",
        "oleSize",
        "pivotCaches",
        "webPublishing",
    }
)
_WORKSHEET_REJECTED_ELEMENTS = frozenset(
    {
        "autoFilter",
        "cellWatches",
        "conditionalFormatting",
        "controls",
        "customProperties",
        "dataValidation",
        "dataValidations",
        "drawing",
        "evenFooter",
        "evenHeader",
        "extLst",
        "firstFooter",
        "firstHeader",
        "headerFooter",
        "hyperlink",
        "hyperlinks",
        "legacyDrawing",
        "legacyDrawingHF",
        "mergeCell",
        "mergeCells",
        "oddFooter",
        "oddHeader",
        "oleObjects",
        "picture",
        "protectedRange",
        "protectedRanges",
        "scenarios",
        "sheetProtection",
        "sortState",
        "tablePart",
        "tableParts",
        "webPublishItems",
    }
)
_SHARED_STRING_REJECTED_ELEMENTS = frozenset(
    {"phoneticPr", "r", "rPh", "rPr"}
)


class _SpreadsheetPolicyError(ValueError):
    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code
        super().__init__(str(exit_code))


class _MetadataCollector:
    def __init__(self) -> None:
        self.values: list[str] = []
        self.chars = 0
        self.elements = 0
        self._structural_names: set[str] = set()

    def add(self, value: object | None) -> None:
        if value is None:
            return
        converted = str(value)
        if not converted.strip():
            return
        self.chars += len(converted)
        if self.chars > MAX_TOTAL_CHARS:
            raise _SpreadsheetPolicyError(79)
        self.values.append(converted)

    def add_structural_name(self, value: str) -> None:
        if value not in self._structural_names:
            self._structural_names.add(value)
            self.add(value)

    def collect_xml(
        self,
        root: ElementTree.Element,
        *,
        skip_text_elements: frozenset[str] = frozenset(),
    ) -> None:
        for element in root.iter():
            self.elements += 1
            if self.elements > MAX_XML_ELEMENTS:
                raise _SpreadsheetPolicyError(79)
            local_name = _local_name(element.tag)
            self.add_structural_name(element.tag)
            for name, value in element.attrib.items():
                self.add_structural_name(name)
                self.add(value)
            if local_name not in skip_text_elements:
                self.add(element.text)
            self.add(element.tail)


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def _decode_ooxml_surface(value: bytes) -> str:
    """Decode XML syntax that ElementTree omits from its semantic tree."""

    try:
        encoding = (
            "utf-16"
            if value.startswith((b"\xff\xfe", b"\xfe\xff"))
            else "utf-8-sig"
        )
        decoded = value.decode(encoding, errors="strict")
    except UnicodeDecodeError as exc:
        raise _SpreadsheetPolicyError(76) from exc
    if "\x00" in decoded:
        raise _SpreadsheetPolicyError(76)
    return decoded


def _collect_ooxml_lexical_surface(
    collector: _MetadataCollector,
    value: bytes,
) -> None:
    """Collect decoded comments, PIs, and namespace URIs omitted by the tree."""

    _decode_ooxml_surface(value)
    try:
        for event, item in ElementTree.iterparse(
            BytesIO(value),
            events=("start-ns", "comment", "pi"),
        ):
            if event == "start-ns":
                prefix, uri = item
                collector.add(prefix)
                collector.add(uri)
            else:
                collector.add(item.text)
                collector.add(unescape(item.text or ""))
    except ElementTree.ParseError as exc:
        raise _SpreadsheetPolicyError(76) from exc


def _cell(value: object) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    converted = str(value)
    return converted, len(converted) > MAX_CELL_CHARS


def _normalized_member_name(value: str) -> str:
    if (
        not value
        or value.startswith("/")
        or value.endswith("/")
        or "\\" in value
        or "\x00" in value
    ):
        raise _SpreadsheetPolicyError(76)
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise _SpreadsheetPolicyError(76)
    normalized = path.as_posix()
    if normalized != value:
        raise _SpreadsheetPolicyError(76)
    return normalized


def _read_package(path: Path) -> dict[str, bytes]:
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise _SpreadsheetPolicyError(76)
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > 4_096:
                raise _SpreadsheetPolicyError(76)
            validate_strict_zip_layout(path, archive, entries)
            normalized_names: dict[str, str] = {}
            compressed_total = 0
            expanded_total = 0
            for entry in entries:
                if entry.is_dir() or entry.flag_bits & 0x1:
                    raise _SpreadsheetPolicyError(76)
                if entry.compress_type not in {
                    zipfile.ZIP_STORED,
                    zipfile.ZIP_DEFLATED,
                }:
                    raise _SpreadsheetPolicyError(76)
                normalized = _normalized_member_name(entry.filename)
                collision_key = normalized.casefold()
                if collision_key in normalized_names:
                    raise _SpreadsheetPolicyError(76)
                normalized_names[collision_key] = normalized
                if entry.file_size > MAX_PART_BYTES:
                    raise _SpreadsheetPolicyError(76)
                if entry.compress_size == 0 and entry.file_size > 0:
                    raise _SpreadsheetPolicyError(76)
                if (
                    entry.file_size > 1024 * 1024
                    and entry.compress_size > 0
                    and entry.file_size
                    > entry.compress_size * MAX_COMPRESSION_RATIO
                ):
                    raise _SpreadsheetPolicyError(76)
                compressed_total += entry.compress_size
                expanded_total += entry.file_size
                if expanded_total > MAX_EXPANDED_BYTES:
                    raise _SpreadsheetPolicyError(73)
                if compressed_total > MAX_ARCHIVE_BYTES:
                    raise _SpreadsheetPolicyError(76)
            if len(entries) > len(_REQUIRED_PARTS | _OPTIONAL_PARTS):
                raise _SpreadsheetPolicyError(76)
            names = set(normalized_names.values())
            if any(
                re.fullmatch(r"xl/worksheets/sheet[1-9][0-9]*\.xml", name)
                and name != "xl/worksheets/sheet1.xml"
                for name in names
            ):
                raise _SpreadsheetPolicyError(74)
            if not _REQUIRED_PARTS.issubset(names) or not names.issubset(
                _REQUIRED_PARTS | _OPTIONAL_PARTS
            ):
                raise _SpreadsheetPolicyError(76)
            return {entry.filename: archive.read(entry) for entry in entries}
    except _SpreadsheetPolicyError:
        raise
    except (
        OSError,
        RuntimeError,
        EOFError,
        zipfile.BadZipFile,
        StrictZipLayoutError,
    ) as exc:
        raise _SpreadsheetPolicyError(76) from exc


def _parse_xml(value: bytes) -> ElementTree.Element:
    lowered = value.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise _SpreadsheetPolicyError(76)
    try:
        return ElementTree.fromstring(value)
    except ElementTree.ParseError as exc:
        raise _SpreadsheetPolicyError(76) from exc


def _relationship_target(source: str | None, target: str) -> str:
    if (
        not target
        or "\\" in target
        or "\x00" in target
        or "%" in target
        or "?" in target
        or "#" in target
    ):
        raise _SpreadsheetPolicyError(77)
    absolute = target.startswith("/")
    raw_parts = PurePosixPath(target.lstrip("/")).parts
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise _SpreadsheetPolicyError(77)
    if absolute or source is None:
        parts = raw_parts
    else:
        parts = PurePosixPath(source).parent.parts + raw_parts
    return _normalized_member_name(PurePosixPath(*parts).as_posix())


def _validate_relationships(
    root: ElementTree.Element,
    *,
    source: str | None,
    expected: frozenset[tuple[str, str]],
) -> None:
    if _local_name(root.tag) != "Relationships":
        raise _SpreadsheetPolicyError(77)
    found: set[tuple[str, str]] = set()
    identifiers: set[str] = set()
    targets: set[str] = set()
    for child in root:
        if _local_name(child.tag) != "Relationship":
            raise _SpreadsheetPolicyError(77)
        identifier = child.attrib.get("Id", "")
        relationship_type = child.attrib.get("Type", "")
        target = child.attrib.get("Target", "")
        if child.attrib.get("TargetMode") is not None:
            raise _SpreadsheetPolicyError(77)
        identifier_key = identifier.casefold()
        if not identifier or identifier_key in identifiers:
            raise _SpreadsheetPolicyError(77)
        identifiers.add(identifier_key)
        resolved_target = _relationship_target(source, target)
        target_key = resolved_target.casefold()
        if target_key in targets:
            raise _SpreadsheetPolicyError(77)
        targets.add(target_key)
        found.add((relationship_type, resolved_target))
    if found != expected or len(found) != len(identifiers):
        raise _SpreadsheetPolicyError(77)


def _validate_content_types(
    root: ElementTree.Element,
    *,
    has_shared_strings: bool,
) -> None:
    if _local_name(root.tag) != "Types":
        raise _SpreadsheetPolicyError(76)
    defaults: dict[str, str] = {}
    overrides: dict[str, str] = {}
    override_keys: set[str] = set()
    for child in root:
        local_name = _local_name(child.tag)
        if local_name == "Default":
            extension = child.attrib.get("Extension", "").casefold()
            if not extension or extension in defaults:
                raise _SpreadsheetPolicyError(76)
            defaults[extension] = child.attrib.get("ContentType", "")
        elif local_name == "Override":
            part_name = child.attrib.get("PartName", "")
            collision_key = part_name.casefold()
            if not part_name.startswith("/") or collision_key in override_keys:
                raise _SpreadsheetPolicyError(76)
            override_keys.add(collision_key)
            overrides[part_name] = child.attrib.get("ContentType", "")
        else:
            raise _SpreadsheetPolicyError(76)
    expected_overrides = dict(_CONTENT_TYPE_OVERRIDES)
    if has_shared_strings:
        expected_overrides["/xl/sharedStrings.xml"] = _SHARED_STRING_CONTENT_TYPE
    if defaults != _CONTENT_TYPE_DEFAULTS or overrides != expected_overrides:
        raise _SpreadsheetPolicyError(76)


def _validate_features(roots: dict[str, ElementTree.Element]) -> None:
    workbook_root = roots["xl/workbook.xml"]
    worksheet_root = roots["xl/worksheets/sheet1.xml"]
    if _local_name(workbook_root.tag) != "workbook":
        raise _SpreadsheetPolicyError(78)
    if _local_name(worksheet_root.tag) != "worksheet":
        raise _SpreadsheetPolicyError(78)
    sheets = [
        element
        for element in workbook_root.iter()
        if _local_name(element.tag) == "sheet"
    ]
    if len(sheets) != 1:
        raise _SpreadsheetPolicyError(74)
    if any(
        _local_name(element.tag) in _WORKBOOK_REJECTED_ELEMENTS
        for element in workbook_root.iter()
    ):
        raise _SpreadsheetPolicyError(78)
    worksheet_elements = {
        _local_name(element.tag) for element in worksheet_root.iter()
    }
    if "f" in worksheet_elements:
        raise _SpreadsheetPolicyError(75)
    if worksheet_elements & _WORKSHEET_REJECTED_ELEMENTS:
        raise _SpreadsheetPolicyError(78)
    shared_root = roots.get("xl/sharedStrings.xml")
    if shared_root is not None:
        if _local_name(shared_root.tag) != "sst":
            raise _SpreadsheetPolicyError(78)
        if any(
            _local_name(element.tag) in _SHARED_STRING_REJECTED_ELEMENTS
            for element in shared_root.iter()
        ):
            raise _SpreadsheetPolicyError(78)


def _validated_roots(package: dict[str, bytes]) -> dict[str, ElementTree.Element]:
    roots = {name: _parse_xml(value) for name, value in package.items()}
    has_shared_strings = "xl/sharedStrings.xml" in package
    _validate_content_types(
        roots["[Content_Types].xml"], has_shared_strings=has_shared_strings
    )
    _validate_relationships(
        roots["_rels/.rels"], source=None, expected=_ROOT_RELATIONSHIPS
    )
    workbook_relationships = set(_WORKBOOK_RELATIONSHIPS)
    if has_shared_strings:
        workbook_relationships.add(_SHARED_STRING_RELATIONSHIP)
    _validate_relationships(
        roots["xl/_rels/workbook.xml.rels"],
        source="xl/workbook.xml",
        expected=frozenset(workbook_relationships),
    )
    _validate_features(roots)
    return roots


def _metadata_inventory(
    package: dict[str, bytes],
    roots: dict[str, ElementTree.Element],
) -> _MetadataCollector:
    collector = _MetadataCollector()
    for name, root in roots.items():
        # ElementTree's ordinary root omits comments, processing instructions,
        # and unused namespace declarations. Collect those decoded values
        # separately; ordinary cells/elements are inventoried once below.
        collector.add(name)
        _collect_ooxml_lexical_surface(collector, package[name])
        if name == "xl/worksheets/sheet1.xml":
            collector.collect_xml(
                root, skip_text_elements=frozenset({"f", "t", "v"})
            )
        elif name == "xl/sharedStrings.xml":
            collector.collect_xml(root, skip_text_elements=frozenset({"t"}))
        else:
            collector.collect_xml(root)
    return collector


def _read_rows(path: Path, *, max_rows: int, metadata_chars: int):
    from openpyxl import load_workbook

    workbook = load_workbook(
        path,
        read_only=True,
        data_only=False,
        keep_links=False,
    )
    try:
        if len(workbook.sheetnames) != 1:
            raise _SpreadsheetPolicyError(74)
        sheet = workbook.active
        values = iter(sheet.iter_rows(values_only=False))
        first_cells = next(values, None)
        if first_cells is not None and any(
            cell.data_type == "f"
            or getattr(cell, "comment", None)
            or getattr(cell, "hyperlink", None)
            for cell in first_cells
        ):
            raise _SpreadsheetPolicyError(75)
        first = (
            None if first_cells is None else tuple(cell.value for cell in first_cells)
        )
        if first is None:
            return []
        if len(first) > MAX_COLUMNS:
            raise _SpreadsheetPolicyError(68)
        headers = [str(value or "").strip() for value in first]
        if any(len(value) > MAX_CELL_CHARS for value in headers):
            raise _SpreadsheetPolicyError(69)
        if any(not value for value in headers):
            raise _SpreadsheetPolicyError(71)
        normalized_headers = [value.casefold() for value in headers]
        if len(set(normalized_headers)) != len(normalized_headers):
            raise _SpreadsheetPolicyError(72)
        rows = []
        total_chars = metadata_chars + sum(len(value) for value in headers)
        if total_chars > MAX_TOTAL_CHARS:
            raise _SpreadsheetPolicyError(70)
        for row_cells in values:
            if len(rows) >= max_rows:
                raise _SpreadsheetPolicyError(67)
            if any(
                cell.data_type == "f"
                or getattr(cell, "comment", None)
                or getattr(cell, "hyperlink", None)
                for cell in row_cells
            ):
                raise _SpreadsheetPolicyError(75)
            row = tuple(cell.value for cell in row_cells)
            if len(row) > MAX_COLUMNS:
                raise _SpreadsheetPolicyError(68)
            cells = [_cell(value) for value in row]
            if any(exceeded for _value, exceeded in cells):
                raise _SpreadsheetPolicyError(69)
            converted = [value for value, _exceeded in cells]
            total_chars += sum(len(value or "") for value in converted)
            if total_chars > MAX_TOTAL_CHARS:
                raise _SpreadsheetPolicyError(70)
            rows.append(dict(zip(headers, converted, strict=False)))
        return rows
    finally:
        workbook.close()


def main() -> int:
    if len(sys.argv) not in {3, 4}:
        return 64
    _apply_resource_limits()
    path = Path(sys.argv[1])
    try:
        max_rows = int(sys.argv[2])
    except ValueError:
        return 64
    mode = sys.argv[3] if len(sys.argv) == 4 else "rows"
    if max_rows < 1 or max_rows > MAX_ROWS or mode not in {"inventory", "rows"}:
        return 64
    if not path.is_file() or path.suffix.casefold() != ".xlsx":
        return 65
    try:
        package = _read_package(path)
        roots = _validated_roots(package)
        metadata = _metadata_inventory(package, roots)
        rows = _read_rows(path, max_rows=max_rows, metadata_chars=metadata.chars)
        payload: object = (
            {"rows": rows, "metadata_text": "\n".join(metadata.values)}
            if mode == "inventory"
            else rows
        )
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except _SpreadsheetPolicyError as exc:
        return exc.exit_code
    except Exception:  # noqa: BLE001 - child must never expose parser internals
        return 66
    try:
        os.write(sys.stdout.fileno(), encoded)
    except OSError:
        return 80
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
