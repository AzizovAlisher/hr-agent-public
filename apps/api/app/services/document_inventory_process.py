"""Strict bounded privacy inventory for unsubmitted DOCX and PDF objects."""

from __future__ import annotations

import math
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

MAX_ARCHIVE_BYTES = 15 * 1024 * 1024
MAX_EXPANDED_BYTES = 50 * 1024 * 1024
MAX_PART_BYTES = 25 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
MAX_XML_ELEMENTS = 200_000
MAX_INVENTORY_CHARS = 2_000_000
MAX_DOCUMENT_TEXT_CHARS = 500_000
MAX_PDF_PAGES = 250
MAX_PDF_OPERATIONS = 200_000
MAX_PDF_CMAP_MAPPINGS = 65_536
MAX_PDF_CID_MAP_BYTES = 2 * MAX_PDF_CMAP_MAPPINGS
MAX_PDF_FONT_BYTES = 1_000_000
MAX_PDF_FONT_GLYPHS = 8_192
# Privacy inventory is emitted to a bounded parent pipe. Strict AI-upload PDFs
# therefore remain deliberately text-oriented and compact; larger/general PDFs
# still use the existing candidate-document parser but cannot enter the
# unbound AI-attachment inventory without explicit provenance.
MAX_PDF_RAW_BYTES = 1_500_000

_OFFICE_RELATIONSHIP_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_CORE_RELATIONSHIP_NS = (
    "http://schemas.openxmlformats.org/package/2006/relationships/metadata/"
    "core-properties"
)
_ROOT_RELATIONSHIPS = frozenset(
    {
        (f"{_OFFICE_RELATIONSHIP_NS}/officeDocument", "word/document.xml"),
        (_CORE_RELATIONSHIP_NS, "docProps/core.xml"),
        (
            f"{_OFFICE_RELATIONSHIP_NS}/extended-properties",
            "docProps/app.xml",
        ),
    }
)

_DOCX_REQUIRED_PARTS = frozenset(
    {
        "[Content_Types].xml",
        "_rels/.rels",
        "docProps/app.xml",
        "docProps/core.xml",
        "word/_rels/document.xml.rels",
        "word/document.xml",
        "word/fontTable.xml",
        "word/numbering.xml",
        "word/settings.xml",
        "word/styles.xml",
        "word/theme/theme1.xml",
        "word/webSettings.xml",
    }
)
_DOCX_STATIC_OPTIONAL_PARTS = frozenset({"word/stylesWithEffects.xml"})
_DOCX_DYNAMIC_PART = re.compile(
    r"word/(?:header|footer)[1-9][0-9]*\.xml|"
    r"word/(?:comments|footnotes|endnotes)\.xml"
)
_DOCX_CONTENT_TYPES = {
    "/docProps/app.xml": (
        "application/vnd.openxmlformats-officedocument.extended-properties+xml"
    ),
    "/docProps/core.xml": "application/vnd.openxmlformats-package.core-properties+xml",
    "/word/document.xml": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document."
        "main+xml"
    ),
    "/word/fontTable.xml": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml"
    ),
    "/word/numbering.xml": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"
    ),
    "/word/settings.xml": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"
    ),
    "/word/styles.xml": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"
    ),
    "/word/stylesWithEffects.xml": (
        "application/vnd.ms-word.stylesWithEffects+xml"
    ),
    "/word/theme/theme1.xml": (
        "application/vnd.openxmlformats-officedocument.theme+xml"
    ),
    "/word/webSettings.xml": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml."
        "webSettings+xml"
    ),
    "/word/comments.xml": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
    ),
    "/word/footnotes.xml": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"
    ),
    "/word/endnotes.xml": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml"
    ),
}
_DOCX_RELATIONSHIP_TYPES = {
    "word/fontTable.xml": f"{_OFFICE_RELATIONSHIP_NS}/fontTable",
    "word/numbering.xml": f"{_OFFICE_RELATIONSHIP_NS}/numbering",
    "word/settings.xml": f"{_OFFICE_RELATIONSHIP_NS}/settings",
    "word/styles.xml": f"{_OFFICE_RELATIONSHIP_NS}/styles",
    "word/stylesWithEffects.xml": (
        "http://schemas.microsoft.com/office/2007/relationships/stylesWithEffects"
    ),
    "word/theme/theme1.xml": f"{_OFFICE_RELATIONSHIP_NS}/theme",
    "word/webSettings.xml": f"{_OFFICE_RELATIONSHIP_NS}/webSettings",
    "word/comments.xml": f"{_OFFICE_RELATIONSHIP_NS}/comments",
    "word/footnotes.xml": f"{_OFFICE_RELATIONSHIP_NS}/footnotes",
    "word/endnotes.xml": f"{_OFFICE_RELATIONSHIP_NS}/endnotes",
}
_DOCX_REJECTED_ELEMENTS = frozenset(
    {
        "OLEObject",
        "altChunk",
        "background",
        "blip",
        "checkBox",
        "connectString",
        "checkbox",
        "comboBox",
        "control",
        "contentPart",
        "customXml",
        "dataBinding",
        "dataSource",
        "date",
        "docPartList",
        "docPartObj",
        "drawing",
        "dropDownList",
        "embeddedObject",
        "entityPicker",
        "ffData",
        "fldChar",
        "fldSimple",
        "formFieldData",
        "hyperlink",
        "imagedata",
        "instrText",
        "linkToQuery",
        "listItem",
        "mailMerge",
        "movie",
        "object",
        "odso",
        "pict",
        "placeholder",
        "query",
        "repeatingSection",
        "repeatingSectionItem",
        "recipientData",
        "sdt",
        "sdtContent",
        "sdtPr",
        "shape",
        "subDoc",
        "sourceFileName",
        "temporary",
        "textbox",
        "textInput",
        "txbxContent",
    }
)

_PDF_ALLOWED_OPERATORS = frozenset(
    {
        b"\"",
        b"'",
        b"BMC",
        b"BDC",
        b"BT",
        b"BX",
        b"CS",
        b"DP",
        b"EMC",
        b"ET",
        b"EX",
        b"G",
        b"J",
        b"K",
        b"M",
        b"MP",
        b"Q",
        b"RG",
        b"SC",
        b"SCN",
        b"T*",
        b"TD",
        b"TJ",
        b"TL",
        b"Tc",
        b"Td",
        b"Tf",
        b"Tj",
        b"Tm",
        b"Tr",
        b"Ts",
        b"Tw",
        b"Tz",
        b"cm",
        b"d",
        b"g",
        b"gs",
        b"i",
        b"j",
        b"k",
        b"q",
        b"rg",
        b"ri",
        b"sc",
        b"scn",
        b"w",
    }
)
_STANDARD_PDF_FONTS = frozenset(
    {
        "/Courier",
        "/Courier-Bold",
        "/Courier-BoldOblique",
        "/Courier-Oblique",
        "/Helvetica",
        "/Helvetica-Bold",
        "/Helvetica-BoldOblique",
        "/Helvetica-Oblique",
        "/Symbol",
        "/Times-Bold",
        "/Times-BoldItalic",
        "/Times-Italic",
        "/Times-Roman",
        "/ZapfDingbats",
    }
)
_PDF_TRAILER_KEYS = frozenset({"/Size", "/Root", "/Info"})
_PDF_CATALOG_KEYS = frozenset({"/Type", "/Pages", "/Metadata"})
_PDF_INTERACTIVE_CATALOG_KEYS = frozenset(
    {
        "/AA",
        "/AcroForm",
        "/Collection",
        "/EmbeddedFiles",
        "/JavaScript",
        "/Names",
        "/OpenAction",
        "/Outlines",
        "/Perms",
        "/RichMedia",
        "/Threads",
    }
)
_PDF_PAGE_TREE_KEYS = frozenset({"/Type", "/Count", "/Kids", "/Parent"})
_PDF_PAGE_KEYS = frozenset(
    {
        "/ArtBox",
        "/BleedBox",
        "/Contents",
        "/CropBox",
        "/MediaBox",
        "/Parent",
        "/Resources",
        "/Rotate",
        "/TrimBox",
        "/Type",
        "/UserUnit",
    }
)
_PDF_RESOURCE_KEYS = frozenset({"/Font", "/ProcSet"})
_PDF_FONT_KEYS = frozenset({"/BaseFont", "/Encoding", "/Subtype", "/Type"})
_PDF_TYPE0_FONT_KEYS = frozenset(
    {
        "/BaseFont",
        "/DescendantFonts",
        "/Encoding",
        "/Subtype",
        "/ToUnicode",
        "/Type",
    }
)
_PDF_CID_FONT_KEYS = frozenset(
    {
        "/BaseFont",
        "/CIDSystemInfo",
        "/CIDToGIDMap",
        "/DW",
        "/FontDescriptor",
        "/Subtype",
        "/Type",
        "/W",
    }
)
_PDF_CID_SYSTEM_INFO_KEYS = frozenset(
    {"/Ordering", "/Registry", "/Supplement"}
)
_PDF_FONT_DESCRIPTOR_KEYS = frozenset(
    {
        "/Ascent",
        "/CapHeight",
        "/Descent",
        "/Flags",
        "/FontBBox",
        "/FontFile2",
        "/FontName",
        "/ItalicAngle",
        "/MissingWidth",
        "/StemV",
        "/Type",
    }
)
_PDF_FONT_DESCRIPTOR_REQUIRED_KEYS = frozenset(
    {
        "/Ascent",
        "/CapHeight",
        "/Descent",
        "/Flags",
        "/FontBBox",
        "/FontFile2",
        "/FontName",
        "/ItalicAngle",
        "/StemV",
        "/Type",
    }
)
_PDF_STREAM_KEYS = frozenset({"/DecodeParms", "/Filter"})
_PDF_FONT_STREAM_KEYS = frozenset({"/DecodeParms", "/Filter", "/Length1"})
_PDF_STANDARD_PROCSETS = frozenset(
    {"/PDF", "/Text", "/ImageB", "/ImageC", "/ImageI"}
)
_PDF_SUBSET_FONT_NAME = re.compile(r"/[A-Z]{6}\+[A-Za-z0-9_.-]{1,96}")
_PDF_CMAP_TOKEN = re.compile(
    r"<<|>>|\[|\]|/[A-Za-z0-9_.+-]{1,127}|"
    r"<[0-9A-Fa-f]{2,128}>|\([A-Za-z0-9_.+-]{1,64}\)|"
    r"-?[0-9]+|[A-Za-z][A-Za-z0-9]*"
)
_TRUETYPE_REQUIRED_TABLES = frozenset(
    {"head", "hhea", "maxp", "hmtx", "cmap", "loca", "glyf", "name", "post"}
)
_TRUETYPE_ALLOWED_TABLES = frozenset(
    {
        "head",
        "hhea",
        "maxp",
        "OS/2",
        "hmtx",
        "cmap",
        "loca",
        "glyf",
        "name",
        "post",
        "cvt ",
        "fpgm",
        "prep",
        "gasp",
        "kern",
    }
)


class _InventoryError(ValueError):
    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code
        super().__init__(str(exit_code))


class _Collector:
    def __init__(self) -> None:
        self.values: list[str] = []
        self.chars = 0
        self.elements = 0
        self._names: set[str] = set()

    def add(self, value: object | None) -> None:
        if value is None:
            return
        if isinstance(value, bytes):
            converted = value.decode("utf-8", errors="replace")
        else:
            converted = str(value)
        if not converted.strip():
            return
        self.chars += len(converted)
        if self.chars > MAX_INVENTORY_CHARS:
            raise _InventoryError(71)
        self.values.append(converted)

    def add_name(self, value: str) -> None:
        if value not in self._names:
            self._names.add(value)
            self.add(value)

    def collect_xml(self, root: ElementTree.Element) -> None:
        for element in root.iter():
            self.elements += 1
            if self.elements > MAX_XML_ELEMENTS:
                raise _InventoryError(71)
            self.add_name(element.tag)
            for name, value in element.attrib.items():
                self.add_name(name)
                self.add(value)
            self.add(element.text)
            self.add(element.tail)


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def _decode_ooxml_surface(value: bytes) -> str:
    """Decode the complete bounded XML surface, including parser-discarded syntax."""

    try:
        encoding = (
            "utf-16"
            if value.startswith((b"\xff\xfe", b"\xfe\xff"))
            else "utf-8-sig"
        )
        decoded = value.decode(encoding, errors="strict")
    except UnicodeDecodeError as exc:
        raise _InventoryError(68) from exc
    if "\x00" in decoded:
        raise _InventoryError(68)
    return decoded


def _collect_ooxml_lexical_surface(
    collector: _Collector,
    value: bytes,
) -> None:
    """Collect decoded syntax omitted by ElementTree's ordinary root tree."""

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
        raise _InventoryError(68) from exc


def _normalized_member_name(value: str) -> str:
    if (
        not value
        or value.startswith("/")
        or value.endswith("/")
        or "\\" in value
        or "\x00" in value
    ):
        raise _InventoryError(68)
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise _InventoryError(68)
    normalized = path.as_posix()
    if normalized != value:
        raise _InventoryError(68)
    return normalized


def _safe_docx_part(value: str) -> bool:
    return (
        value in _DOCX_REQUIRED_PARTS
        or value in _DOCX_STATIC_OPTIONAL_PARTS
        or _DOCX_DYNAMIC_PART.fullmatch(value) is not None
    )


def _read_docx_package(path: Path) -> dict[str, bytes]:
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise _InventoryError(68)
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > 32:
                raise _InventoryError(68)
            validate_strict_zip_layout(path, archive, entries)
            names: dict[str, str] = {}
            compressed_total = 0
            expanded_total = 0
            for entry in entries:
                if entry.is_dir() or entry.flag_bits & 0x1:
                    raise _InventoryError(68)
                if entry.compress_type not in {
                    zipfile.ZIP_STORED,
                    zipfile.ZIP_DEFLATED,
                }:
                    raise _InventoryError(68)
                normalized = _normalized_member_name(entry.filename)
                collision_key = normalized.casefold()
                if collision_key in names or not _safe_docx_part(normalized):
                    raise _InventoryError(68)
                names[collision_key] = normalized
                if entry.file_size > MAX_PART_BYTES:
                    raise _InventoryError(68)
                if entry.compress_size == 0 and entry.file_size > 0:
                    raise _InventoryError(68)
                if (
                    entry.file_size > 1024 * 1024
                    and entry.compress_size > 0
                    and entry.file_size
                    > entry.compress_size * MAX_COMPRESSION_RATIO
                ):
                    raise _InventoryError(68)
                compressed_total += entry.compress_size
                expanded_total += entry.file_size
                if (
                    compressed_total > MAX_ARCHIVE_BYTES
                    or expanded_total > MAX_EXPANDED_BYTES
                ):
                    raise _InventoryError(68)
            accepted_names = set(names.values())
            if not _DOCX_REQUIRED_PARTS.issubset(accepted_names):
                raise _InventoryError(68)
            return {entry.filename: archive.read(entry) for entry in entries}
    except _InventoryError:
        raise
    except (
        OSError,
        RuntimeError,
        EOFError,
        zipfile.BadZipFile,
        StrictZipLayoutError,
    ) as exc:
        raise _InventoryError(68) from exc


def _parse_xml(value: bytes) -> ElementTree.Element:
    lowered = value.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise _InventoryError(68)
    try:
        return ElementTree.fromstring(value)
    except ElementTree.ParseError as exc:
        raise _InventoryError(68) from exc


def _relationship_target(source: str | None, target: str) -> str:
    if (
        not target
        or "\\" in target
        or "\x00" in target
        or "%" in target
        or "?" in target
        or "#" in target
    ):
        raise _InventoryError(69)
    absolute = target.startswith("/")
    raw_parts = PurePosixPath(target.lstrip("/")).parts
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise _InventoryError(69)
    parts = (
        raw_parts
        if absolute or source is None
        else PurePosixPath(source).parent.parts + raw_parts
    )
    return _normalized_member_name(PurePosixPath(*parts).as_posix())


def _relationships(
    root: ElementTree.Element,
    *,
    source: str | None,
) -> frozenset[tuple[str, str]]:
    if _local_name(root.tag) != "Relationships":
        raise _InventoryError(69)
    found: set[tuple[str, str]] = set()
    ids: set[str] = set()
    targets: set[str] = set()
    for child in root:
        if _local_name(child.tag) != "Relationship":
            raise _InventoryError(69)
        identifier = child.attrib.get("Id", "")
        relationship_type = child.attrib.get("Type", "")
        if child.attrib.get("TargetMode") is not None:
            raise _InventoryError(69)
        identifier_key = identifier.casefold()
        if not identifier or identifier_key in ids:
            raise _InventoryError(69)
        ids.add(identifier_key)
        target = _relationship_target(source, child.attrib.get("Target", ""))
        target_key = target.casefold()
        if target_key in targets:
            raise _InventoryError(69)
        targets.add(target_key)
        found.add((relationship_type, target))
    if len(found) != len(ids):
        raise _InventoryError(69)
    return frozenset(found)


def _validate_docx_content_types(
    root: ElementTree.Element,
    package: dict[str, bytes],
) -> None:
    if _local_name(root.tag) != "Types":
        raise _InventoryError(68)
    defaults: dict[str, str] = {}
    overrides: dict[str, str] = {}
    override_keys: set[str] = set()
    for child in root:
        local = _local_name(child.tag)
        if local == "Default":
            extension = child.attrib.get("Extension", "").casefold()
            if not extension or extension in defaults:
                raise _InventoryError(68)
            defaults[extension] = child.attrib.get("ContentType", "")
        elif local == "Override":
            name = child.attrib.get("PartName", "")
            key = name.casefold()
            if not name.startswith("/") or key in override_keys:
                raise _InventoryError(68)
            override_keys.add(key)
            overrides[name] = child.attrib.get("ContentType", "")
        else:
            raise _InventoryError(68)
    if defaults != {
        "rels": "application/vnd.openxmlformats-package.relationships+xml",
        "xml": "application/xml",
    }:
        raise _InventoryError(68)
    expected: dict[str, str] = {}
    for name in package:
        part_name = f"/{name}"
        if part_name in _DOCX_CONTENT_TYPES:
            expected[part_name] = _DOCX_CONTENT_TYPES[part_name]
        elif re.fullmatch(r"/word/header[1-9][0-9]*\.xml", part_name):
            expected[part_name] = (
                "application/vnd.openxmlformats-officedocument.wordprocessingml."
                "header+xml"
            )
        elif re.fullmatch(r"/word/footer[1-9][0-9]*\.xml", part_name):
            expected[part_name] = (
                "application/vnd.openxmlformats-officedocument.wordprocessingml."
                "footer+xml"
            )
    if overrides != expected:
        raise _InventoryError(68)


def _expected_docx_relationships(package: dict[str, bytes]):
    expected: set[tuple[str, str]] = set()
    for name in package:
        relationship_type = _DOCX_RELATIONSHIP_TYPES.get(name)
        if relationship_type is None:
            if re.fullmatch(r"word/header[1-9][0-9]*\.xml", name):
                relationship_type = f"{_OFFICE_RELATIONSHIP_NS}/header"
            elif re.fullmatch(r"word/footer[1-9][0-9]*\.xml", name):
                relationship_type = f"{_OFFICE_RELATIONSHIP_NS}/footer"
        if relationship_type is not None:
            expected.add((relationship_type, name))
    return frozenset(expected)


def _docx_inventory(path: Path) -> str:
    package = _read_docx_package(path)
    roots = {name: _parse_xml(value) for name, value in package.items()}
    _validate_docx_content_types(roots["[Content_Types].xml"], package)
    if _relationships(roots["_rels/.rels"], source=None) != _ROOT_RELATIONSHIPS:
        raise _InventoryError(69)
    document_relationships = _relationships(
        roots["word/_rels/document.xml.rels"],
        source="word/document.xml",
    )
    if document_relationships != _expected_docx_relationships(package):
        raise _InventoryError(69)
    for root in roots.values():
        if any(
            _local_name(element.tag) in _DOCX_REJECTED_ELEMENTS
            for element in root.iter()
        ):
            raise _InventoryError(70)

    collector = _Collector()
    semantic_parts: list[str] = []
    for name, root in roots.items():
        # The ordinary ElementTree root drops comments, processing
        # instructions, and unused namespace declarations. Collect those
        # decoded lexical values separately without double-counting ordinary
        # element/cell text, which is inventoried below.
        collector.add(name)
        _collect_ooxml_lexical_surface(collector, package[name])
        collector.collect_xml(root)
        if name == "word/document.xml" or _DOCX_DYNAMIC_PART.fullmatch(name):
            for paragraph in (
                element for element in root.iter() if _local_name(element.tag) == "p"
            ):
                text = "".join(
                    element.text or ""
                    for element in paragraph.iter()
                    if _local_name(element.tag) == "t"
                )
                if text:
                    semantic_parts.append(text)
    semantic = "\n".join(semantic_parts)
    if not semantic.strip() or len(semantic) > MAX_DOCUMENT_TEXT_CHARS:
        raise _InventoryError(67)
    collector.add(semantic)
    return "\n".join(collector.values)


def _read_pdf_bytes(path: Path) -> bytes:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _InventoryError(66) from exc
    if (
        not raw.startswith(b"%PDF-")
        or len(raw) > MAX_PDF_RAW_BYTES
        or raw.count(b"startxref") != 1
        or raw.count(b"%%EOF") != 1
    ):
        raise _InventoryError(74)
    eof_end = raw.find(b"%%EOF") + len(b"%%EOF")
    if raw[eof_end:].strip(b"\x00\x09\x0a\x0c\x0d\x20"):
        raise _InventoryError(74)
    return raw


def _pdf_reference_key(value: object) -> tuple[int, int] | None:
    from pypdf.generic import IndirectObject

    if not isinstance(value, IndirectObject):
        return None
    return (value.idnum, value.generation)


def _resolve_pdf_dictionary(value: object, *, exit_code: int):
    from pypdf.generic import DictionaryObject

    try:
        resolved = value.get_object() if hasattr(value, "get_object") else value
    except Exception as exc:
        raise _InventoryError(exit_code) from exc
    if not isinstance(resolved, DictionaryObject):
        raise _InventoryError(exit_code)
    return resolved


def _validate_pdf_info(value: object, collector: _Collector) -> None:
    from pypdf.generic import (
        ArrayObject,
        DictionaryObject,
        IndirectObject,
        StreamObject,
    )

    info = _resolve_pdf_dictionary(value, exit_code=74)
    for key, item in info.items():
        if isinstance(
            item,
            (ArrayObject, DictionaryObject, IndirectObject, StreamObject),
        ):
            raise _InventoryError(74)
        collector.add(key)
        collector.add(item)


def _decode_pdf_stream(
    value: object,
    *,
    allowed_keys: frozenset[str],
    max_bytes: int,
    collector: _Collector | None,
):
    from pypdf.generic import ArrayObject, NameObject, StreamObject

    try:
        stream = value.get_object() if hasattr(value, "get_object") else value
    except Exception as exc:
        raise _InventoryError(74) from exc
    if not isinstance(stream, StreamObject) or set(stream.keys()) - allowed_keys:
        raise _InventoryError(74)
    filter_value = stream.get("/Filter")
    filters = filter_value if isinstance(filter_value, ArrayObject) else [filter_value]
    if any(
        item is not None
        and (
            not isinstance(item, NameObject)
            or str(item) not in {"/FlateDecode", "/ASCII85Decode"}
        )
        for item in filters
    ):
        raise _InventoryError(74)
    if stream.get("/DecodeParms") is not None:
        raise _InventoryError(74)
    try:
        decoded = stream.get_data()
    except Exception as exc:
        raise _InventoryError(74) from exc
    if len(decoded) > max_bytes:
        raise _InventoryError(71)
    if collector is not None:
        collector.add(decoded)
    return stream, decoded


def _validate_pdf_stream(value: object, collector: _Collector):
    stream, _decoded = _decode_pdf_stream(
        value,
        allowed_keys=_PDF_STREAM_KEYS,
        max_bytes=MAX_PART_BYTES,
        collector=collector,
    )
    return stream


def _pdf_number(value: object) -> float:
    from pypdf.generic import FloatObject, NumberObject

    if not isinstance(value, (FloatObject, NumberObject)):
        raise _InventoryError(74)
    converted = float(value)
    if not math.isfinite(converted) or abs(converted) > 10_000_000:
        raise _InventoryError(74)
    return converted


def _validate_pdf_widths(value: object) -> None:
    from pypdf.generic import ArrayObject, NumberObject

    if not isinstance(value, ArrayObject) or not value:
        raise _InventoryError(74)
    index = 0
    covered: set[int] = set()
    while index < len(value):
        first = value[index]
        if not isinstance(first, NumberObject) or not 0 <= int(first) <= 65_535:
            raise _InventoryError(74)
        first_cid = int(first)
        index += 1
        if index >= len(value):
            raise _InventoryError(74)
        widths = value[index]
        index += 1
        if isinstance(widths, ArrayObject):
            if not widths or first_cid + len(widths) > 65_536:
                raise _InventoryError(74)
            cids = range(first_cid, first_cid + len(widths))
            for width in widths:
                _pdf_number(width)
        else:
            if not isinstance(widths, NumberObject) or index >= len(value):
                raise _InventoryError(74)
            last_cid = int(widths)
            width = value[index]
            index += 1
            if not first_cid <= last_cid <= 65_535:
                raise _InventoryError(74)
            _pdf_number(width)
            cids = range(first_cid, last_cid + 1)
        for cid in cids:
            if cid in covered:
                raise _InventoryError(74)
            covered.add(cid)
            if len(covered) > MAX_PDF_CMAP_MAPPINGS:
                raise _InventoryError(71)


def _pdf_cmap_hex(token: str, *, source: bool) -> bytes:
    if not token.startswith("<") or not token.endswith(">"):
        raise _InventoryError(74)
    try:
        decoded = bytes.fromhex(token[1:-1])
    except ValueError as exc:
        raise _InventoryError(74) from exc
    maximum = 4 if source else 64
    if not decoded or len(decoded) > maximum or (not source and len(decoded) % 2):
        raise _InventoryError(74)
    if not source:
        try:
            decoded.decode("utf-16-be", errors="strict")
        except UnicodeDecodeError as exc:
            raise _InventoryError(74) from exc
    return decoded


def _validate_pdf_tounicode(decoded: bytes) -> None:
    try:
        source = decoded.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise _InventoryError(74) from exc
    source = re.sub(r"%[^\r\n]*", "", source)
    compact = re.sub(r"\s+", "", source)
    tokens = _PDF_CMAP_TOKEN.findall(source)
    if not compact or "".join(tokens) != compact:
        raise _InventoryError(74)
    position = 0

    def take(expected: str | None = None) -> str:
        nonlocal position
        if position >= len(tokens):
            raise _InventoryError(74)
        token = tokens[position]
        position += 1
        if expected is not None and token != expected:
            raise _InventoryError(74)
        return token

    def take_count(*, maximum: int) -> int:
        token = take()
        if re.fullmatch(r"[0-9]+", token) is None:
            raise _InventoryError(74)
        count = int(token)
        if not 1 <= count <= maximum:
            raise _InventoryError(74)
        return count

    for expected in ("/CIDInit", "/ProcSet", "findresource", "begin"):
        take(expected)
    take_count(maximum=64)
    take("dict")
    take("begin")
    take("begincmap")
    for expected in (
        "/CIDSystemInfo",
        "<<",
        "/Registry",
        "(Adobe)",
        "/Ordering",
        "(UCS)",
        "/Supplement",
        "0",
        ">>",
        "def",
        "/CMapName",
    ):
        take(expected)
    cmap_name = take()
    if re.fullmatch(r"/[A-Za-z0-9_.+-]{1,127}", cmap_name) is None:
        raise _InventoryError(74)
    for expected in ("def", "/CMapType", "2", "def"):
        take(expected)

    code_spaces: list[tuple[int, int, int]] = []
    mapped: set[tuple[int, int]] = set()

    def source_code(token: str) -> tuple[int, int]:
        raw = _pdf_cmap_hex(token, source=True)
        key = (len(raw), int.from_bytes(raw, "big"))
        if not any(
            length == key[0] and lower <= key[1] <= upper
            for length, lower, upper in code_spaces
        ):
            raise _InventoryError(74)
        return key

    while position < len(tokens) and tokens[position] != "endcmap":
        count = take_count(maximum=100)
        section = take()
        if section == "begincodespacerange":
            for _ in range(count):
                lower_bytes = _pdf_cmap_hex(take(), source=True)
                upper_bytes = _pdf_cmap_hex(take(), source=True)
                lower = int.from_bytes(lower_bytes, "big")
                upper = int.from_bytes(upper_bytes, "big")
                if len(lower_bytes) != len(upper_bytes) or lower > upper:
                    raise _InventoryError(74)
                candidate = (len(lower_bytes), lower, upper)
                if any(
                    length == candidate[0]
                    and not (candidate[2] < old_lower or candidate[1] > old_upper)
                    for length, old_lower, old_upper in code_spaces
                ):
                    raise _InventoryError(74)
                code_spaces.append(candidate)
            take("endcodespacerange")
            continue
        if not code_spaces or section not in {"beginbfchar", "beginbfrange"}:
            raise _InventoryError(74)
        if section == "beginbfchar":
            for _ in range(count):
                key = source_code(take())
                _pdf_cmap_hex(take(), source=False)
                if key in mapped:
                    raise _InventoryError(74)
                mapped.add(key)
            take("endbfchar")
            continue
        for _ in range(count):
            first = source_code(take())
            last = source_code(take())
            if first[0] != last[0] or first[1] > last[1]:
                raise _InventoryError(74)
            mapping_count = last[1] - first[1] + 1
            destination = take()
            if destination == "[":
                for _mapping in range(mapping_count):
                    _pdf_cmap_hex(take(), source=False)
                take("]")
            else:
                start = _pdf_cmap_hex(destination, source=False)
                if len(start) not in {2, 4}:
                    raise _InventoryError(74)
                maximum = (1 << (8 * len(start))) - 1
                start_value = int.from_bytes(start, "big")
                if start_value + mapping_count - 1 > maximum:
                    raise _InventoryError(74)
                for offset in range(mapping_count):
                    try:
                        (start_value + offset).to_bytes(
                            len(start), "big"
                        ).decode("utf-16-be", errors="strict")
                    except UnicodeDecodeError as exc:
                        raise _InventoryError(74) from exc
            for code in range(first[1], last[1] + 1):
                key = (first[0], code)
                if key in mapped:
                    raise _InventoryError(74)
                mapped.add(key)
                if len(mapped) > MAX_PDF_CMAP_MAPPINGS:
                    raise _InventoryError(71)
        take("endbfrange")
    if not code_spaces or not mapped:
        raise _InventoryError(74)
    for expected in (
        "endcmap",
        "CMapName",
        "currentdict",
        "/CMap",
        "defineresource",
        "pop",
        "end",
        "end",
    ):
        take(expected)
    if position != len(tokens):
        raise _InventoryError(74)


def _validate_truetype_font(decoded: bytes, collector: _Collector | None) -> int:
    from fontTools.ttLib import TTFont

    try:
        font = TTFont(
            BytesIO(decoded),
            lazy=False,
            recalcBBoxes=False,
            recalcTimestamp=False,
        )
        if font.sfntVersion != "\x00\x01\x00\x00":
            raise _InventoryError(74)
        tables = set(font.keys()) - {"GlyphOrder"}
        if (
            not _TRUETYPE_REQUIRED_TABLES <= tables
            or tables - _TRUETYPE_ALLOWED_TABLES
            or len(tables) > len(_TRUETYPE_ALLOWED_TABLES)
        ):
            raise _InventoryError(74)
        for table in sorted(tables):
            font[table]
            if collector is not None:
                collector.add_name(table)
        glyph_order = font.getGlyphOrder()
        glyph_count = int(font["maxp"].numGlyphs)
        if (
            not 1 <= glyph_count <= MAX_PDF_FONT_GLYPHS
            or len(glyph_order) != glyph_count
            or len(set(glyph_order)) != glyph_count
            or glyph_order[0] != ".notdef"
            or int(font["head"].unitsPerEm) not in range(16, 16_385)
        ):
            raise _InventoryError(74)
        cmap_entries = 0
        cmap_tables = font["cmap"].tables
        if not cmap_tables:
            raise _InventoryError(74)
        for cmap in cmap_tables:
            if not cmap.isUnicode() or cmap.format not in {4, 12}:
                raise _InventoryError(74)
            cmap_entries += len(cmap.cmap)
            if cmap_entries > MAX_PDF_CMAP_MAPPINGS:
                raise _InventoryError(71)
            if any(
                not 0 <= int(codepoint) <= 0x10FFFF
                or 0xD800 <= int(codepoint) <= 0xDFFF
                or glyph not in glyph_order
                for codepoint, glyph in cmap.cmap.items()
            ):
                raise _InventoryError(74)
        names = font["name"].names
        if not names or len(names) > 256:
            raise _InventoryError(74)
        name_chars = 0
        for name in names:
            converted = name.toUnicode()
            name_chars += len(converted)
            if (
                name_chars > 16_384
                or any(
                    ord(character) < 32 and character not in "\t\r\n"
                    for character in converted
                )
            ):
                raise _InventoryError(74)
            if collector is not None:
                collector.add(converted)
    except _InventoryError:
        raise
    except Exception as exc:
        raise _InventoryError(74) from exc
    finally:
        if "font" in locals():
            font.close()
    return glyph_count


def _validate_pdf_font_descriptor(
    value: object,
    *,
    base_font: str,
    collector: _Collector | None,
) -> int:
    from pypdf.generic import NumberObject

    descriptor = _resolve_pdf_dictionary(value, exit_code=74)
    keys = set(descriptor.keys())
    if (
        not _PDF_FONT_DESCRIPTOR_REQUIRED_KEYS <= keys
        or keys - _PDF_FONT_DESCRIPTOR_KEYS
        or str(descriptor.get("/Type")) != "/FontDescriptor"
        or str(descriptor.get("/FontName")) != base_font
    ):
        raise _InventoryError(74)
    flags = descriptor.get("/Flags")
    if not isinstance(flags, NumberObject) or not 0 <= int(flags) <= 2**31 - 1:
        raise _InventoryError(74)
    _validate_pdf_box(descriptor.get("/FontBBox"))
    for metric in (
        "/Ascent",
        "/CapHeight",
        "/Descent",
        "/ItalicAngle",
        "/MissingWidth",
        "/StemV",
    ):
        if metric in descriptor:
            _pdf_number(descriptor.get(metric))
    font_file = descriptor.raw_get("/FontFile2")
    if _pdf_reference_key(font_file) is None:
        raise _InventoryError(74)
    stream, decoded = _decode_pdf_stream(
        font_file,
        allowed_keys=_PDF_FONT_STREAM_KEYS,
        max_bytes=MAX_PDF_FONT_BYTES,
        collector=collector,
    )
    length = stream.get("/Length1")
    if not isinstance(length, NumberObject) or int(length) != len(decoded):
        raise _InventoryError(74)
    return _validate_truetype_font(decoded, collector)


def _validate_pdf_cid_system_info(
    value: object,
    collector: _Collector | None,
) -> None:
    from pypdf.generic import NumberObject, TextStringObject

    if _pdf_reference_key(value) is None:
        raise _InventoryError(74)
    info = _resolve_pdf_dictionary(value, exit_code=74)
    if set(info.keys()) != _PDF_CID_SYSTEM_INFO_KEYS:
        raise _InventoryError(74)
    registry = info.get("/Registry")
    ordering = info.get("/Ordering")
    supplement = info.get("/Supplement")
    if (
        not isinstance(registry, TextStringObject)
        or str(registry) != "Adobe"
        or not isinstance(ordering, TextStringObject)
        or str(ordering) != "UCS"
        or not isinstance(supplement, NumberObject)
        or int(supplement) != 0
    ):
        raise _InventoryError(74)
    if collector is not None:
        collector.add(registry)
        collector.add(ordering)


def _validate_pdf_unicode_font(
    value: object,
    collector: _Collector | None,
) -> None:
    from pypdf.generic import ArrayObject, NameObject

    if _pdf_reference_key(value) is None:
        raise _InventoryError(74)
    font = _resolve_pdf_dictionary(value, exit_code=74)
    base_font = str(font.get("/BaseFont"))
    descendants = font.get("/DescendantFonts")
    if (
        set(font.keys()) != _PDF_TYPE0_FONT_KEYS
        or str(font.get("/Type")) != "/Font"
        or str(font.get("/Subtype")) != "/Type0"
        or str(font.get("/Encoding")) != "/Identity-H"
        or _PDF_SUBSET_FONT_NAME.fullmatch(base_font) is None
        or not isinstance(descendants, ArrayObject)
        or len(descendants) != 1
        or _pdf_reference_key(descendants[0]) is None
    ):
        raise _InventoryError(74)
    to_unicode = font.raw_get("/ToUnicode")
    if _pdf_reference_key(to_unicode) is None:
        raise _InventoryError(74)
    _stream, cmap = _decode_pdf_stream(
        to_unicode,
        allowed_keys=_PDF_STREAM_KEYS,
        max_bytes=512 * 1024,
        collector=collector,
    )
    _validate_pdf_tounicode(cmap)

    descendant = _resolve_pdf_dictionary(descendants[0], exit_code=74)
    if (
        set(descendant.keys()) != _PDF_CID_FONT_KEYS
        or str(descendant.get("/Type")) != "/Font"
        or str(descendant.get("/Subtype")) != "/CIDFontType2"
        or str(descendant.get("/BaseFont")) != base_font
    ):
        raise _InventoryError(74)
    _validate_pdf_cid_system_info(
        descendant.raw_get("/CIDSystemInfo"),
        collector,
    )
    _pdf_number(descendant.get("/DW"))
    _validate_pdf_widths(descendant.get("/W"))
    descriptor_reference = descendant.raw_get("/FontDescriptor")
    if _pdf_reference_key(descriptor_reference) is None:
        raise _InventoryError(74)
    glyph_count = _validate_pdf_font_descriptor(
        descriptor_reference,
        base_font=base_font,
        collector=collector,
    )
    cid_map_reference = descendant.raw_get("/CIDToGIDMap")
    if isinstance(cid_map_reference, NameObject):
        if str(cid_map_reference) != "/Identity":
            raise _InventoryError(74)
    else:
        if _pdf_reference_key(cid_map_reference) is None:
            raise _InventoryError(74)
        _stream, cid_map = _decode_pdf_stream(
            cid_map_reference,
            allowed_keys=_PDF_STREAM_KEYS,
            max_bytes=MAX_PDF_CID_MAP_BYTES,
            collector=collector,
        )
        if not cid_map or len(cid_map) % 2:
            raise _InventoryError(74)
        if any(
            int.from_bytes(cid_map[index : index + 2], "big") >= glyph_count
            for index in range(0, len(cid_map), 2)
        ):
            raise _InventoryError(74)
    if collector is not None:
        collector.add(base_font)


def _validate_pdf_resources(
    value: object,
    *,
    collector: _Collector | None = None,
    validated_fonts: set[tuple[int, int]] | None = None,
    collected_fonts: set[tuple[int, int]] | None = None,
) -> None:
    from pypdf.generic import ArrayObject, NameObject

    resources = _resolve_pdf_dictionary(value, exit_code=74)
    if set(resources.keys()) - _PDF_RESOURCE_KEYS:
        raise _InventoryError(73)
    procsets = resources.get("/ProcSet")
    if procsets is not None and (
        not isinstance(procsets, ArrayObject)
        or any(
            not isinstance(item, NameObject)
            or str(item) not in _PDF_STANDARD_PROCSETS
            for item in procsets
        )
    ):
        raise _InventoryError(74)
    fonts_value = resources.get("/Font")
    if fonts_value is None:
        return
    fonts = _resolve_pdf_dictionary(fonts_value, exit_code=74)
    for resource_name, font_reference in fonts.items():
        if re.fullmatch(r"/F[1-9][0-9]*", str(resource_name)) is None:
            raise _InventoryError(74)
        font = _resolve_pdf_dictionary(font_reference, exit_code=74)
        if str(font.get("/Subtype")) == "/Type0":
            reference_key = _pdf_reference_key(font_reference)
            if reference_key is None:
                raise _InventoryError(74)
            already_validated = (
                validated_fonts is not None and reference_key in validated_fonts
            )
            needs_collection = (
                collector is not None
                and (
                    collected_fonts is None
                    or reference_key not in collected_fonts
                )
            )
            if not already_validated or needs_collection:
                _validate_pdf_unicode_font(
                    font_reference,
                    collector if needs_collection else None,
                )
            if validated_fonts is not None:
                validated_fonts.add(reference_key)
            if needs_collection and collected_fonts is not None:
                collected_fonts.add(reference_key)
            continue
        if set(font.keys()) != _PDF_FONT_KEYS or (
            str(font.get("/Type")) != "/Font"
            or str(font.get("/Subtype")) != "/Type1"
            or str(font.get("/BaseFont")) not in _STANDARD_PDF_FONTS
            or str(font.get("/Encoding")) != "/WinAnsiEncoding"
        ):
            raise _InventoryError(74)


def _validate_pdf_box(value: object) -> None:
    from pypdf.generic import ArrayObject, FloatObject, NumberObject

    if not isinstance(value, ArrayObject) or len(value) != 4 or any(
        not isinstance(item, (FloatObject, NumberObject)) for item in value
    ):
        raise _InventoryError(74)


def _validate_pdf_page_tree(
    root: object,
    *,
    expected_page_count: int,
    validated_fonts: set[tuple[int, int]],
) -> None:
    from pypdf.generic import ArrayObject, IndirectObject, NumberObject

    seen: set[tuple[int, int]] = set()
    pages_found = 0

    def visit(reference: object, parent_key: tuple[int, int] | None) -> None:
        nonlocal pages_found
        key = _pdf_reference_key(reference)
        if key is None or key in seen:
            raise _InventoryError(74)
        seen.add(key)
        node = _resolve_pdf_dictionary(reference, exit_code=74)
        node_type = str(node.get("/Type"))
        actual_parent = (
            _pdf_reference_key(node.raw_get("/Parent"))
            if "/Parent" in node
            else None
        )
        if actual_parent != parent_key:
            raise _InventoryError(74)
        if node_type == "/Pages":
            if set(node.keys()) - _PDF_PAGE_TREE_KEYS:
                raise _InventoryError(74)
            count = node.get("/Count")
            kids = node.get("/Kids")
            if (
                not isinstance(count, NumberObject)
                or int(count) < 1
                or not isinstance(kids, ArrayObject)
                or not kids
                or any(not isinstance(item, IndirectObject) for item in kids)
            ):
                raise _InventoryError(74)
            before = pages_found
            for child in kids:
                visit(child, key)
            if pages_found - before != int(count):
                raise _InventoryError(74)
            return
        if node_type != "/Page" or set(node.keys()) - _PDF_PAGE_KEYS:
            if set(node.keys()) & {"/AA", "/Annots"}:
                raise _InventoryError(72)
            raise _InventoryError(74)
        for box_name in ("/MediaBox", "/CropBox", "/BleedBox", "/TrimBox", "/ArtBox"):
            if box_name in node:
                _validate_pdf_box(node.get(box_name))
        if "/MediaBox" not in node:
            raise _InventoryError(74)
        if "/Resources" not in node or "/Contents" not in node:
            raise _InventoryError(75)
        _validate_pdf_resources(
            node.raw_get("/Resources"),
            validated_fonts=validated_fonts,
        )
        pages_found += 1

    pages_reference = root.raw_get("/Pages")
    visit(pages_reference, None)
    if pages_found != expected_page_count:
        raise _InventoryError(74)


def _pdf_reachable_references(trailer: object) -> set[tuple[int, int]]:
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject

    found: set[tuple[int, int]] = set()
    visited_direct: set[int] = set()
    nodes = 0

    def visit(value: object) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_XML_ELEMENTS:
            raise _InventoryError(71)
        if isinstance(value, IndirectObject):
            key = (value.idnum, value.generation)
            if key in found:
                return
            found.add(key)
            try:
                visit(value.get_object())
            except _InventoryError:
                raise
            except Exception as exc:
                raise _InventoryError(74) from exc
            return
        if isinstance(value, (DictionaryObject, ArrayObject)):
            identity = id(value)
            if identity in visited_direct:
                return
            visited_direct.add(identity)
            items = value.values() if isinstance(value, DictionaryObject) else value
            for item in items:
                visit(item)

    visit(trailer)
    return found


def _validate_pdf_object_graph(
    reader,
    root: object,
    collector: _Collector,
    *,
    validated_fonts: set[tuple[int, int]],
) -> None:
    from pypdf.generic import IndirectObject, NumberObject, StreamObject

    if set(reader.trailer.keys()) != _PDF_TRAILER_KEYS:
        raise _InventoryError(74)
    root_reference = reader.trailer.raw_get("/Root")
    info_reference = reader.trailer.raw_get("/Info")
    if not isinstance(root_reference, IndirectObject) or not isinstance(
        info_reference,
        IndirectObject,
    ):
        raise _InventoryError(74)
    if set(root.keys()) & _PDF_INTERACTIVE_CATALOG_KEYS:
        raise _InventoryError(72)
    if set(root.keys()) not in ({"/Type", "/Pages"}, _PDF_CATALOG_KEYS):
        raise _InventoryError(74)
    if str(root.get("/Type")) != "/Catalog":
        raise _InventoryError(74)
    _validate_pdf_info(info_reference, collector)
    _validate_pdf_page_tree(
        root,
        expected_page_count=len(reader.pages),
        validated_fonts=validated_fonts,
    )
    metadata_reference = root.raw_get("/Metadata") if "/Metadata" in root else None
    if metadata_reference is not None:
        metadata = _validate_pdf_stream(metadata_reference, collector)
        if not isinstance(metadata, StreamObject):
            raise _InventoryError(74)

    if reader.xref_objStm or any(
        generation not in {0, 65535} for generation in reader.xref
    ):
        raise _InventoryError(74)
    xref_references = {
        (object_id, generation)
        for generation, objects in reader.xref.items()
        if generation != 65535
        for object_id in objects
        if object_id != 0
    }
    if not xref_references:
        raise _InventoryError(74)
    size = reader.trailer.get("/Size")
    if not isinstance(size, NumberObject) or int(size) != max(
        object_id for object_id, _generation in xref_references
    ) + 1:
        raise _InventoryError(74)
    if _pdf_reachable_references(reader.trailer) != xref_references:
        raise _InventoryError(74)


def _collect_pdf_operand(collector: _Collector, value: object) -> None:
    from pypdf.generic import ArrayObject, ByteStringObject, TextStringObject

    if isinstance(value, (TextStringObject, ByteStringObject, str, bytes)):
        collector.add(value)
    elif isinstance(value, (ArrayObject, list, tuple)):
        for item in value:
            _collect_pdf_operand(collector, item)


def _validate_pdf_fonts(
    resources,
    *,
    collector: _Collector,
    validated_fonts: set[tuple[int, int]],
    collected_fonts: set[tuple[int, int]],
) -> None:
    _validate_pdf_resources(
        resources,
        collector=collector,
        validated_fonts=validated_fonts,
        collected_fonts=collected_fonts,
    )


def _pdf_inventory(path: Path) -> str:
    from pypdf import PdfReader
    from pypdf.generic import ArrayObject, ContentStream

    raw = _read_pdf_bytes(path)
    try:
        reader = PdfReader(str(path), strict=True)
    except Exception as exc:
        raise _InventoryError(66) from exc
    if reader.is_encrypted or not 1 <= len(reader.pages) <= MAX_PDF_PAGES:
        raise _InventoryError(72)
    collector = _Collector()
    # Preserve every raw lexical surface (comments and spacing included) while
    # decoded streams and typed objects are validated below. The small strict
    # PDF size limit keeps this complete inventory within the parent pipe cap.
    collector.add(raw.decode("latin-1"))
    root = reader.trailer["/Root"].get_object()
    validated_fonts: set[tuple[int, int]] = set()
    collected_fonts: set[tuple[int, int]] = set()
    _validate_pdf_object_graph(
        reader,
        root,
        collector,
        validated_fonts=validated_fonts,
    )
    metadata_stream = root.get("/Metadata")
    if metadata_stream is not None:
        try:
            raw_metadata = metadata_stream.get_object().get_data()
        except Exception as exc:
            raise _InventoryError(74) from exc
        if len(raw_metadata) > MAX_PART_BYTES:
            raise _InventoryError(71)
        collector.collect_xml(_parse_xml(raw_metadata))

    page_text: list[str] = []
    operations = 0
    for page in reader.pages:
        if any(key in page for key in ("/AA", "/Annots", "/Metadata")):
            raise _InventoryError(72)
        if set(page.keys()) - _PDF_PAGE_KEYS:
            raise _InventoryError(74)
        resources = page.get("/Resources")
        if resources is None:
            raise _InventoryError(75)
        resources = resources.get_object()
        if set(resources.keys()) - _PDF_RESOURCE_KEYS:
            raise _InventoryError(73)
        _validate_pdf_fonts(
            resources,
            collector=collector,
            validated_fonts=validated_fonts,
            collected_fonts=collected_fonts,
        )
        raw_contents = page.raw_get("/Contents")
        content_objects = (
            raw_contents if isinstance(raw_contents, ArrayObject) else [raw_contents]
        )
        if not content_objects:
            raise _InventoryError(75)
        for content_object in content_objects:
            _validate_pdf_stream(content_object, collector)
        contents = page.get_contents()
        if contents is None:
            raise _InventoryError(75)
        try:
            content_stream = ContentStream(contents, reader)
        except Exception as exc:
            raise _InventoryError(74) from exc
        try:
            decoded_content = contents.get_data()
        except Exception as exc:
            raise _InventoryError(74) from exc
        if len(decoded_content) > MAX_PART_BYTES:
            raise _InventoryError(71)
        collector.add(decoded_content)
        for operands, operator in content_stream.operations:
            operations += 1
            if (
                operations > MAX_PDF_OPERATIONS
                or operator not in _PDF_ALLOWED_OPERATORS
            ):
                raise _InventoryError(74)
            collector.add(operator)
            for operand in operands:
                _collect_pdf_operand(collector, operand)
        try:
            extracted = page.extract_text()
        except Exception as exc:
            raise _InventoryError(74) from exc
        if extracted:
            page_text.append(extracted)
    semantic = "\n".join(page_text).strip()
    if not semantic or len(semantic) > MAX_DOCUMENT_TEXT_CHARS:
        raise _InventoryError(75)
    collector.add(semantic)
    return "\n".join(collector.values)


def main() -> int:
    if len(sys.argv) != 3:
        return 64
    _apply_resource_limits()
    path = Path(sys.argv[1])
    suffix = sys.argv[2].casefold()
    if not path.is_file() or suffix not in {".docx", ".pdf"}:
        return 65
    try:
        inventory = _docx_inventory(path) if suffix == ".docx" else _pdf_inventory(path)
    except _InventoryError as exc:
        return exc.exit_code
    except Exception:  # noqa: BLE001 - child must not expose parser details
        return 66
    encoded = inventory.encode("utf-8", errors="replace")
    if len(encoded) > MAX_INVENTORY_CHARS * 4:
        return 71
    try:
        os.write(sys.stdout.fileno(), encoded)
    except OSError:
        return 76
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
