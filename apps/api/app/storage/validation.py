"""Content validation before untrusted objects can leave quarantine."""

from __future__ import annotations

import json
import re
import warnings
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from xml.etree import ElementTree

import pypdf
from PIL import Image

MAX_ARCHIVE_ENTRIES = 2_000
MAX_ARCHIVE_EXPANDED_BYTES = 75 * 1024 * 1024
MAX_ARCHIVE_ENTRY_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_EXPANSION_RATIO = 100
MAX_PDF_PAGES = 250
MAX_PDF_GRAPH_OBJECTS = 100_000
MAX_C2PA_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_MEDIA_ELEMENTS = 100_000
MAX_MEDIA_METADATA_BYTES = 4 * 1024 * 1024
MAX_MP3_FRAMES = 100_000

_GENERIC_MEDIA_TYPES = {"", "application/octet-stream", "binary/octet-stream"}
MEDIA_TYPES_BY_SUFFIX: dict[str, set[str]] = {
    ".pdf": {"application/pdf"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    },
    ".txt": {"text/plain"},
    ".md": {"text/markdown", "text/plain"},
    ".csv": {"text/csv", "application/csv", "text/plain"},
    ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    ".png": {"image/png"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".webp": {"image/webp"},
    ".mp3": {"audio/mpeg", "audio/mp3"},
    ".m4a": {"audio/mp4", "audio/x-m4a"},
    ".mp4": {"video/mp4", "application/mp4"},
    ".ogg": {"audio/ogg", "video/ogg", "application/ogg"},
    ".wav": {"audio/wav", "audio/x-wav", "audio/vnd.wave"},
    ".webm": {"video/webm", "audio/webm"},
}
CANONICAL_MEDIA_TYPE_BY_SUFFIX: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".webm": "video/webm",
}

_FORBIDDEN_ARCHIVE_SUFFIXES = {
    ".7z",
    ".bat",
    ".cmd",
    ".com",
    ".dll",
    ".exe",
    ".hta",
    ".htm",
    ".html",
    ".jar",
    ".js",
    ".lnk",
    ".msi",
    ".ps1",
    ".rar",
    ".scr",
    ".svg",
    ".vbs",
    ".zip",
}
_PDF_WHITESPACE = b"\x00\x09\x0a\x0c\x0d\x20"
_PDF_REVISION_END = re.compile(
    rb"startxref[\x00\x09\x0a\x0c\x0d\x20]+"
    rb"([0-9]+)[\x00\x09\x0a\x0c\x0d\x20]+%%EOF"
)
_PDF_INDIRECT_OBJECT = re.compile(
    rb"[0-9]+[\x00\x09\x0a\x0c\x0d\x20]+"
    rb"[0-9]+[\x00\x09\x0a\x0c\x0d\x20]+obj\b"
)
_PDF_FORBIDDEN_KEYS = frozenset(
    {
        "/AA",
        "/AF",
        "/Collection",
        "/EF",
        "/EmbeddedFiles",
        "/JavaScript",
        "/JS",
        "/RichMedia",
        "/RichMediaContent",
        "/RichMediaSettings",
        "/XFA",
    }
)
_PDF_ACTION_TYPES = frozenset(
    {
        "/GoTo",
        "/GoTo3DView",
        "/GoToE",
        "/GoToR",
        "/Hide",
        "/ImportData",
        "/JavaScript",
        "/Launch",
        "/Movie",
        "/Named",
        "/Rendition",
        "/ResetForm",
        "/RichMediaExecute",
        "/SetOCGState",
        "/Sound",
        "/SubmitForm",
        "/Thread",
        "/Trans",
        "/URI",
    }
)
_PDF_FORBIDDEN_ANNOTATION_SUBTYPES = frozenset(
    {"/3D", "/FileAttachment", "/Movie", "/RichMedia", "/Screen", "/Sound"}
)
PdfReferenceIdentity = tuple[str, int, int] | tuple[str, int]


@dataclass(frozen=True, slots=True)
class UploadValidationError(ValueError):
    status_code: int
    detail: str

    def __str__(self) -> str:
        return self.detail


def _reject(status_code: int, detail: str) -> None:
    raise UploadValidationError(status_code, detail)


def _safe_suffix(
    *,
    filename: str,
    media_type: str,
    allowed_suffixes: set[str],
) -> tuple[str, str]:
    suffix = Path(filename or "").suffix.casefold()
    normalized_media_type = (media_type or "").split(";", 1)[0].strip().casefold()
    normalized_allowed = {item.casefold() for item in allowed_suffixes}
    # SVG is executable XML/HTML in browser contexts and is never accepted by
    # the private-document or public-branding intake boundary.
    normalized_allowed.discard(".svg")
    if suffix not in normalized_allowed:
        _reject(415, "Unsupported upload format.")
    expected = MEDIA_TYPES_BY_SUFFIX.get(suffix, set())
    if (
        normalized_media_type not in _GENERIC_MEDIA_TYPES
        and normalized_media_type not in expected
    ):
        _reject(415, "File extension and content type do not match.")
    return suffix, normalized_media_type


def canonical_media_type(suffix: str) -> str:
    try:
        return CANONICAL_MEDIA_TYPE_BY_SUFFIX[suffix.casefold()]
    except KeyError as exc:
        raise ValueError("Unsupported upload format") from exc


def _looks_like_active_markup(content: bytes) -> bool:
    prefix = content[:8192].lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
    return (
        prefix.startswith((b"<svg", b"<!doctype html", b"<html"))
        or (prefix.startswith(b"<?xml") and b"<svg" in prefix)
        or b"<script" in prefix
    )


def _pdf_skip_space_and_comments(content: bytes) -> bytes:
    """Return a PDF revision prefix after insignificant space/comments."""

    offset = 0
    while offset < len(content):
        while offset < len(content) and content[offset] in _PDF_WHITESPACE:
            offset += 1
        if offset >= len(content) or content[offset] != ord("%"):
            break
        line_end = offset + 1
        while line_end < len(content) and content[line_end] not in b"\r\n":
            line_end += 1
        offset = line_end
    return content[offset:]


def _validate_pdf_revision_boundaries(content: bytes) -> int:
    """Reject data outside complete PDF revisions without scanning streams.

    Marker searches over the whole file confuse compressed images and fonts
    with executable payloads. Revision boundaries are structural: the final
    ``%%EOF`` must be the logical end of the file and each preceding revision
    must be followed by another PDF object/xref section, never arbitrary bytes.
    """

    logical = content.rstrip(_PDF_WHITESPACE)
    revisions = list(_PDF_REVISION_END.finditer(logical))
    if not revisions:
        # Let strict pypdf parsing produce the established malformed-document
        # response while still checking the structural invariant afterwards.
        return 0
    if revisions[-1].end() != len(logical):
        trailing = logical[revisions[-1].end() :]
        if any(magic in trailing for magic in (b"MZ", b"PK\x03\x04", b"\x7fELF")):
            _reject(422, "Polyglot PDF content is not allowed.")
        _reject(422, "Active or embedded PDF content is not allowed.")

    is_linearized = b"/Linearized" in logical[:4096]
    for index, revision in enumerate(revisions):
        xref_offset = int(revision.group(1))
        if xref_offset >= revision.start():
            _reject(422, "PDF contains an unsafe revision boundary.")
        target = logical[xref_offset : xref_offset + 96]
        if not (
            target.startswith(b"xref")
            or _PDF_INDIRECT_OBJECT.match(target)
            or (index == 0 and xref_offset == 0 and is_linearized)
        ):
            _reject(422, "PDF contains an unsafe cross-reference boundary.")
        if index + 1 < len(revisions):
            next_revision = _pdf_skip_space_and_comments(
                logical[revision.end() : revisions[index + 1].start()]
            )
            if not (
                next_revision.startswith(b"xref")
                or _PDF_INDIRECT_OBJECT.match(next_revision)
            ):
                if any(
                    magic in next_revision
                    for magic in (b"MZ", b"PK\x03\x04", b"\x7fELF")
                ):
                    _reject(422, "Polyglot PDF content is not allowed.")
                _reject(422, "Active or embedded PDF content is not allowed.")
    return len(revisions)


def _resolve_pdf_object(value: object) -> object | None:
    """Dereference a PDF object; broken inert references are not executable."""

    from pypdf.generic import IndirectObject

    try:
        while isinstance(value, IndirectObject):
            value = value.get_object()
    except Exception:  # noqa: BLE001 - malformed unreachable legacy reference
        return None
    return value


def _validate_pdf_destination(value: object) -> None:
    from pypdf.generic import (
        ArrayObject,
        ByteStringObject,
        NameObject,
        TextStringObject,
    )

    destination = _resolve_pdf_object(value)
    if isinstance(destination, (NameObject, TextStringObject, ByteStringObject)):
        if (
            len(
                bytes(destination)
                if isinstance(destination, ByteStringObject)
                else str(destination)
            )
            > 4096
        ):
            _reject(422, "Active or embedded PDF content is not allowed.")
        return
    if not isinstance(destination, ArrayObject) or not 1 <= len(destination) <= 16:
        _reject(422, "Active or embedded PDF content is not allowed.")
    # An explicit destination may contain a page reference and fit/coordinate
    # scalars, but never a dictionary or stream capable of carrying an action.
    from pypdf.generic import DictionaryObject, StreamObject

    for index, item in enumerate(destination):
        resolved = _resolve_pdf_object(item)
        if isinstance(resolved, (DictionaryObject, StreamObject)):
            # The first element normally resolves to the target page dictionary.
            if index == 0:
                continue
            _reject(422, "Active or embedded PDF content is not allowed.")


def _validate_pdf_uri(value: object, *, remote_destination: bool = False) -> None:
    resolved = _resolve_pdf_object(value)
    if resolved is None:
        _reject(422, "Active or embedded PDF content is not allowed.")
    target = str(resolved).strip()
    if not target or len(target) > 4096 or any(ord(char) < 0x20 for char in target):
        _reject(422, "Active or embedded PDF content is not allowed.")
    parsed = urlsplit(target if "://" in target else f"https://{target}")
    scheme = urlsplit(target).scheme.casefold()
    if scheme not in {"", "http", "https", "mailto", "tel"}:
        _reject(422, "Active or embedded PDF content is not allowed.")
    if remote_destination and (
        parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname
    ):
        _reject(422, "Active or embedded PDF content is not allowed.")


def _validate_pdf_action(action: object, *, automatic: bool = False) -> None:
    from pypdf.generic import DictionaryObject

    resolved = _resolve_pdf_object(action)
    if not isinstance(resolved, DictionaryObject):
        _reject(422, "Active or embedded PDF content is not allowed.")
    action_type = str(resolved.get("/S", ""))
    if action_type == "/GoTo":
        if "/D" not in resolved:
            _reject(422, "Active or embedded PDF content is not allowed.")
        _validate_pdf_destination(resolved.raw_get("/D"))
    elif action_type == "/URI" and not automatic:
        if "/URI" not in resolved:
            _reject(422, "Active or embedded PDF content is not allowed.")
        _validate_pdf_uri(resolved.raw_get("/URI"))
    elif action_type == "/GoToR" and not automatic:
        # Some office exporters encode ordinary HTTPS resume links as remote
        # destinations. Permit only an explicit web host, never a local path.
        if "/F" not in resolved or "/D" not in resolved:
            _reject(422, "Active or embedded PDF content is not allowed.")
        _validate_pdf_uri(resolved.raw_get("/F"), remote_destination=True)
        _validate_pdf_destination(resolved.raw_get("/D"))
    else:
        _reject(422, "Active or embedded PDF content is not allowed.")


def _validate_pdf_action_reference(value: object) -> None:
    """Fail closed for action dictionaries reached through /A or /Next."""

    from pypdf.generic import ArrayObject, DictionaryObject

    resolved = _resolve_pdf_object(value)
    candidates = resolved if isinstance(resolved, ArrayObject) else [resolved]
    for candidate in candidates:
        candidate = _resolve_pdf_object(candidate)
        if not isinstance(candidate, DictionaryObject):
            continue
        if "/S" in candidate or str(candidate.get("/Type", "")) == "/Action":
            _validate_pdf_action(candidate)


def _pdf_reference_identity(value: object) -> PdfReferenceIdentity:
    """Return a stable identity for direct or indirect PDF objects."""

    from pypdf.generic import IndirectObject

    if isinstance(value, IndirectObject):
        return ("indirect", value.idnum, value.generation)
    resolved = _resolve_pdf_object(value)
    return ("direct", id(resolved))


def _validate_c2pa_file_spec(
    value: object,
) -> tuple[PdfReferenceIdentity, set[int]]:
    """Validate one passive Content Credentials attachment.

    C2PA-aware PDF exporters attach a JUMBF provenance manifest through the
    catalog's associated-files entry. The manifest is inert metadata, but the
    surrounding PDF surface is also used for arbitrary attachments. Accept
    only the exact single-purpose C2PA shape and keep all other embedded files
    fail-closed.
    """

    from pypdf.generic import DictionaryObject, StreamObject

    identity = _pdf_reference_identity(value)
    spec = _resolve_pdf_object(value)
    required_keys = {
        "/AFRelationship",
        "/Desc",
        "/EF",
        "/F",
        "/Subtype",
        "/Type",
        "/UF",
    }
    if not isinstance(spec, DictionaryObject) or {
        str(key) for key in spec
    } != required_keys:
        _reject(422, "Active or embedded PDF content is not allowed.")
    if (
        str(spec.get("/Type", "")) != "/FileSpec"
        or str(spec.get("/Subtype", "")) != "application/c2pa"
        or str(spec.get("/AFRelationship", "")) != "/C2PA_Manifest"
        or any(
            str(spec.get(key, "")) != "Content Credentials"
            for key in ("/F", "/UF", "/Desc")
        )
    ):
        _reject(422, "Active or embedded PDF content is not allowed.")

    embedded = _resolve_pdf_object(spec.raw_get("/EF"))
    if not isinstance(embedded, DictionaryObject) or {
        str(key) for key in embedded
    } != {"/F"}:
        _reject(422, "Active or embedded PDF content is not allowed.")
    stream = _resolve_pdf_object(embedded.raw_get("/F"))
    if not isinstance(stream, StreamObject):
        _reject(422, "Active or embedded PDF content is not allowed.")
    stream_keys = {str(key) for key in stream}
    if not stream_keys <= {
        "/DecodeParms",
        "/DP",
        "/F",
        "/Filter",
        "/Length",
        "/Subtype",
        "/Type",
    }:
        _reject(422, "Active or embedded PDF content is not allowed.")
    if "/Type" in stream and str(stream.get("/Type", "")) != "/EmbeddedFile":
        _reject(422, "Active or embedded PDF content is not allowed.")
    if "/Subtype" in stream and str(stream.get("/Subtype", "")) not in {
        "/application#2Fc2pa",
        "application/c2pa",
    }:
        _reject(422, "Active or embedded PDF content is not allowed.")
    try:
        manifest = stream.get_data()
    except Exception as exc:
        raise UploadValidationError(
            422, "Active or embedded PDF content is not allowed."
        ) from exc
    if (
        not 16 <= len(manifest) <= MAX_C2PA_MANIFEST_BYTES
        or manifest[4:8] != b"jumb"
        or int.from_bytes(manifest[:4], "big") != len(manifest)
        or b"c2pa" not in manifest[:256].lower()
    ):
        _reject(422, "Active or embedded PDF content is not allowed.")
    return identity, {id(spec), id(stream)}


def _validated_c2pa_exceptions(root: object) -> tuple[dict[int, set[str]], set[int]]:
    """Return narrowly validated graph exceptions for one C2PA manifest."""

    from pypdf.generic import ArrayObject, DictionaryObject

    catalog = _resolve_pdf_object(root)
    if not isinstance(catalog, DictionaryObject) or "/AF" not in catalog:
        return {}, set()
    associated = _resolve_pdf_object(catalog.raw_get("/AF"))
    if not isinstance(associated, ArrayObject) or len(associated) != 1:
        _reject(422, "Active or embedded PDF content is not allowed.")
    spec_identity, passive_object_ids = _validate_c2pa_file_spec(associated[0])
    spec = _resolve_pdf_object(associated[0])

    if "/Names" not in catalog:
        _reject(422, "Active or embedded PDF content is not allowed.")
    names = _resolve_pdf_object(catalog.raw_get("/Names"))
    if not isinstance(names, DictionaryObject) or "/EmbeddedFiles" not in names:
        _reject(422, "Active or embedded PDF content is not allowed.")
    embedded_files = _resolve_pdf_object(names.raw_get("/EmbeddedFiles"))
    if not isinstance(embedded_files, DictionaryObject) or {
        str(key) for key in embedded_files
    } != {"/Names"}:
        _reject(422, "Active or embedded PDF content is not allowed.")
    entries = _resolve_pdf_object(embedded_files.raw_get("/Names"))
    if (
        not isinstance(entries, ArrayObject)
        or len(entries) != 2
        or str(entries[0]) != "Content Credentials"
        or _pdf_reference_identity(entries[1]) != spec_identity
    ):
        _reject(422, "Active or embedded PDF content is not allowed.")

    return (
        {
            id(catalog): {"/AF"},
            id(names): {"/EmbeddedFiles"},
            id(spec): {"/EF"},
        },
        passive_object_ids,
    )


def _validate_pdf_object_graph(reader: pypdf.PdfReader) -> None:
    """Inspect reachable PDF dictionaries while deliberately skipping streams."""

    from pypdf.generic import (
        ArrayObject,
        DictionaryObject,
        IndirectObject,
    )

    try:
        root = reader.trailer.raw_get("/Root")
    except Exception as exc:
        raise UploadValidationError(
            422, "PDF document could not be parsed safely."
        ) from exc

    allowed_c2pa_keys, passive_c2pa_object_ids = _validated_c2pa_exceptions(root)
    stack: list[object] = [root]
    indirect_seen: set[tuple[int, int]] = set()
    direct_seen: set[int] = set()
    visited = 0
    while stack:
        value = stack.pop()
        if isinstance(value, IndirectObject):
            reference = (value.idnum, value.generation)
            if reference in indirect_seen:
                continue
            indirect_seen.add(reference)
            resolved = _resolve_pdf_object(value)
            if resolved is None:
                continue
            value = resolved
        if isinstance(value, (DictionaryObject, ArrayObject)):
            identity = id(value)
            if identity in direct_seen:
                continue
            direct_seen.add(identity)
        visited += 1
        if visited > MAX_PDF_GRAPH_OBJECTS:
            _reject(413, "PDF object graph exceeds the processing limit.")
        if isinstance(value, ArrayObject):
            stack.extend(value)
            continue
        if not isinstance(value, DictionaryObject):
            continue

        keys = {str(key) for key in value}
        forbidden_keys = (keys & _PDF_FORBIDDEN_KEYS) - allowed_c2pa_keys.get(
            id(value), set()
        )
        if forbidden_keys:
            _reject(422, "Active or embedded PDF content is not allowed.")
        object_type = str(value.get("/Type", ""))
        subtype = str(value.get("/Subtype", ""))
        if (
            object_type in {"/EmbeddedFile", "/Filespec", "/FileSpec"}
            and id(value) not in passive_c2pa_object_ids
        ) or subtype in _PDF_FORBIDDEN_ANNOTATION_SUBTYPES:
            _reject(422, "Active or embedded PDF content is not allowed.")

        action_type = str(value.get("/S", ""))
        if action_type in _PDF_ACTION_TYPES or object_type == "/Action":
            _validate_pdf_action(value)
        for action_key in ("/A", "/Next"):
            if action_key in value:
                _validate_pdf_action_reference(value.raw_get(action_key))
        if "/OpenAction" in value:
            open_action = _resolve_pdf_object(value.raw_get("/OpenAction"))
            if isinstance(open_action, DictionaryObject):
                _validate_pdf_action(open_action, automatic=True)
            else:
                _validate_pdf_destination(open_action)

        # Stream bytes are opaque parser input. Their dictionary is still
        # inspected above, but incidental names/magic inside encoded font or
        # image data must not be interpreted as PDF actions.
        stack.extend(dict.values(value))


def _validate_pdf(path: Path, content: bytes) -> None:
    if not content.startswith(b"%PDF-"):
        _reject(422, "File content does not match the declared PDF format.")
    revision_count = _validate_pdf_revision_boundaries(content)
    try:
        reader = pypdf.PdfReader(str(path), strict=True)
        if reader.is_encrypted:
            _reject(422, "Encrypted PDF files are not supported.")
        if revision_count == 0:
            _reject(422, "PDF document could not be parsed safely.")
        if (
            revision_count > 1
            and "/Prev" not in reader.trailer
            and (revision_count != 2 or b"/Linearized" not in content[:4096])
        ):
            # Multiple complete revisions without a predecessor chain are
            # valid only for the special two-section linearized layout.
            _reject(422, "PDF contains an unsafe revision chain.")
        if len(reader.pages) > MAX_PDF_PAGES:
            _reject(413, "PDF contains too many pages.")
        # Force page-tree traversal now, inside the bounded parser boundary.
        for page in reader.pages:
            _ = page.mediabox
        _validate_pdf_object_graph(reader)
    except UploadValidationError:
        raise
    except Exception as exc:
        raise UploadValidationError(
            422, "PDF document could not be parsed safely."
        ) from exc


def _zip_member_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return mode == 0o120000


def _validate_ooxml(path: Path, content: bytes, *, suffix: str) -> None:
    try:
        # Python's ZIP reader intentionally tolerates prepended/appended bytes.
        # Office uploads do not need self-extracting or concatenated archives,
        # so require one canonical end-of-central-directory record ending at
        # EOF before any decompression or XML parsing.
        eocd_signature = b"PK\x05\x06"
        eocd = content.rfind(eocd_signature)
        if eocd < 0 or content.count(eocd_signature) != 1 or eocd + 22 > len(content):
            _reject(422, "Office document has an unsafe archive structure.")
        comment_length = int.from_bytes(content[eocd + 20 : eocd + 22], "little")
        if eocd + 22 + comment_length != len(content):
            _reject(422, "Office document contains trailing polyglot data.")
        if b"PK\x06\x06" in content or b"PK\x06\x07" in content:
            _reject(422, "Office document uses an unsupported ZIP64 structure.")
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > MAX_ARCHIVE_ENTRIES:
                _reject(413, "Office document contains too many archive entries.")
            expanded = 0
            for entry in entries:
                normalized = entry.filename.replace("\\", "/")
                parts = PurePosixPath(normalized).parts
                if (
                    normalized.startswith("/")
                    or ".." in parts
                    or _zip_member_is_symlink(entry)
                    or entry.flag_bits & 0x1
                ):
                    _reject(422, "Office document contains an unsafe archive entry.")
                if entry.file_size > MAX_ARCHIVE_ENTRY_BYTES:
                    _reject(413, "Office document contains an oversized archive entry.")
                if entry.compress_size == 0 and entry.file_size > 0:
                    _reject(413, "Office document has an unsafe compression ratio.")
                if (
                    entry.compress_size > 0
                    and entry.file_size > 1024 * 1024
                    and entry.file_size
                    > entry.compress_size * MAX_ARCHIVE_EXPANSION_RATIO
                ):
                    _reject(413, "Office document has an unsafe compression ratio.")
                lowered = normalized.casefold()
                if (
                    Path(lowered).suffix in _FORBIDDEN_ARCHIVE_SUFFIXES
                    or lowered.endswith("vbaproject.bin")
                    or "/embeddings/" in f"/{lowered}"
                    or "/activex/" in f"/{lowered}"
                ):
                    _reject(422, "Office document contains active or embedded content.")
                expanded += entry.file_size
                if expanded > MAX_ARCHIVE_EXPANDED_BYTES:
                    _reject(413, "Office document expands beyond the processing limit.")
            names = {entry.filename.replace("\\", "/") for entry in entries}
            required = (
                {"[Content_Types].xml", "word/document.xml"}
                if suffix == ".docx"
                else {"[Content_Types].xml", "xl/workbook.xml"}
            )
            if not required.issubset(names):
                _reject(
                    422, f"File content does not match the declared {suffix} format."
                )
            # Read required XML within the already bounded member limits so CRC
            # and decompressor errors are surfaced before a downstream parser.
            for name in required:
                archive.read(name)
            for entry in entries:
                lowered_name = entry.filename.casefold()
                if not lowered_name.endswith((".xml", ".rels")):
                    continue
                xml = archive.read(entry)
                lowered_xml = xml.lower()
                if b"<!doctype" in lowered_xml or b"<!entity" in lowered_xml:
                    _reject(422, "Office document contains unsafe XML declarations.")
                try:
                    root = ElementTree.fromstring(xml)
                except ElementTree.ParseError as exc:
                    raise UploadValidationError(
                        422,
                        "Office document contains malformed XML.",
                    ) from exc
                for element in root.iter():
                    local_tag = element.tag.rsplit("}", 1)[-1].casefold()
                    if (
                        local_tag == "instrtext"
                        and "dde" in (element.text or "").casefold()
                    ):
                        _reject(422, "Office document contains an active field.")
                    attributes = {
                        name.rsplit("}", 1)[-1].casefold(): value
                        for name, value in element.attrib.items()
                    }
                    if attributes.get("targetmode", "").casefold() != "external":
                        continue
                    relation_type = attributes.get("type", "").casefold()
                    target = attributes.get("target", "")
                    parsed_target = urlsplit(target)
                    if not (
                        relation_type.endswith("/hyperlink")
                        and parsed_target.scheme.casefold()
                        in {"http", "https", "mailto"}
                        and "\x00" not in target
                    ):
                        _reject(
                            422,
                            "Office document contains an unsafe external relationship.",
                        )
    except UploadValidationError:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError, EOFError) as exc:
        raise UploadValidationError(
            422, "Office document is not a safe OOXML archive."
        ) from exc


def _validate_image(path: Path, content: bytes, *, suffix: str) -> None:
    signatures = {
        ".png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        ".jpg": content.startswith(b"\xff\xd8\xff"),
        ".jpeg": content.startswith(b"\xff\xd8\xff"),
        ".webp": len(content) >= 12
        and content.startswith(b"RIFF")
        and content[8:12] == b"WEBP",
    }
    if not signatures.get(suffix, False):
        _reject(422, f"File content does not match the declared {suffix} format.")
    if suffix == ".png":
        offset = 8
        found_end = False
        while offset + 12 <= len(content):
            chunk_size = int.from_bytes(content[offset : offset + 4], "big")
            chunk_end = offset + 12 + chunk_size
            if chunk_end > len(content):
                break
            chunk_type = content[offset + 4 : offset + 8]
            offset = chunk_end
            if chunk_type == b"IEND":
                found_end = True
                break
        if not found_end or offset != len(content):
            _reject(422, "Image contains trailing or malformed polyglot data.")
    elif suffix in {".jpg", ".jpeg"}:
        if not content.endswith(b"\xff\xd9"):
            _reject(422, "Image contains trailing or malformed polyglot data.")
    elif suffix == ".webp":
        declared_size = int.from_bytes(content[4:8], "little") + 8
        if declared_size != len(content):
            _reject(422, "Image contains trailing or malformed polyglot data.")
    lowered = content.lower()
    if any(
        marker in lowered
        for marker in (b"<script", b"<svg", b"<html", b"<!doctype html")
    ):
        _reject(422, "Active markup is not allowed in image files.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    _reject(413, "Image dimensions exceed the processing limit.")
                expected = "JPEG" if suffix in {".jpg", ".jpeg"} else suffix[1:].upper()
                if image.format != expected:
                    _reject(422, "Image content does not match its file extension.")
                image.verify()
    except UploadValidationError:
        raise
    except (
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise UploadValidationError(422, "Image could not be parsed safely.") from exc


def _validate_iso_media(content: bytes, *, suffix: str) -> None:
    """Walk bounded top-level ISO BMFF boxes and reject trailing polyglots."""

    offset = 0
    box_count = 0
    found_ftyp = False
    while offset < len(content):
        if len(content) - offset < 8:
            _reject(422, "Recording has a malformed media container.")
        size = int.from_bytes(content[offset : offset + 4], "big")
        box_type = content[offset + 4 : offset + 8]
        header_size = 8
        if size == 1:
            if len(content) - offset < 16:
                _reject(422, "Recording has a malformed media container.")
            size = int.from_bytes(content[offset + 8 : offset + 16], "big")
            header_size = 16
        elif size == 0:
            size = len(content) - offset
        if size < header_size or offset + size > len(content):
            _reject(422, "Recording has a malformed media container.")
        if box_count == 0 and box_type != b"ftyp":
            _reject(422, f"File content does not match the declared {suffix} format.")
        if box_type == b"ftyp":
            found_ftyp = True
        offset += size
        box_count += 1
        if box_count > 10_000:
            _reject(413, "Recording contains too many media boxes.")
    if not found_ftyp or offset != len(content):
        _reject(422, "Recording has a malformed media container.")


def _build_ogg_crc_table() -> tuple[int, ...]:
    table: list[int] = []
    for seed in range(256):
        value = seed << 24
        for _bit in range(8):
            value = (
                ((value << 1) ^ 0x04C11DB7) if value & 0x80000000 else value << 1
            ) & 0xFFFFFFFF
        table.append(value)
    return tuple(table)


_OGG_CRC_TABLE = _build_ogg_crc_table()


def _ogg_crc(content: bytes | bytearray) -> int:
    checksum = 0
    for byte in content:
        checksum = ((checksum << 8) & 0xFFFFFFFF) ^ _OGG_CRC_TABLE[
            ((checksum >> 24) & 0xFF) ^ byte
        ]
    return checksum


def _validate_ogg(content: bytes) -> None:
    """Walk complete Ogg pages, including lacing, sequence, and CRC checks."""

    offset = 0
    page_count = 0
    payload_bytes = 0
    completed_packets = 0
    # serial -> (last sequence, unfinished packet, end-of-stream)
    streams: dict[int, tuple[int, bool, bool]] = {}
    while offset < len(content):
        if len(content) - offset < 27 or content[offset : offset + 4] != b"OggS":
            _reject(422, "Recording has a malformed Ogg container.")
        if content[offset + 4] != 0:
            _reject(422, "Recording has an unsupported Ogg bitstream version.")
        header_type = content[offset + 5]
        if header_type & ~0x07:
            _reject(422, "Recording has a malformed Ogg page header.")
        segment_count = content[offset + 26]
        if segment_count == 0:
            _reject(422, "Recording has an empty Ogg page.")
        segment_table_end = offset + 27 + segment_count
        if segment_table_end > len(content):
            _reject(422, "Recording has a truncated Ogg segment table.")
        lacing = content[offset + 27 : segment_table_end]
        body_size = sum(lacing)
        page_end = segment_table_end + body_size
        if page_end > len(content):
            _reject(422, "Recording has a truncated Ogg page.")

        expected_checksum = int.from_bytes(content[offset + 22 : offset + 26], "little")
        page = bytearray(content[offset:page_end])
        page[22:26] = b"\x00\x00\x00\x00"
        if _ogg_crc(page) != expected_checksum:
            _reject(422, "Recording has an invalid Ogg page checksum.")

        serial = int.from_bytes(content[offset + 14 : offset + 18], "little")
        sequence = int.from_bytes(content[offset + 18 : offset + 22], "little")
        continued = bool(header_type & 0x01)
        beginning = bool(header_type & 0x02)
        ending = bool(header_type & 0x04)
        previous = streams.get(serial)
        if previous is None:
            if sequence != 0 or continued or not beginning:
                _reject(422, "Recording has an invalid initial Ogg page.")
        else:
            previous_sequence, unfinished_packet, ended = previous
            if (
                ended
                or beginning
                or sequence != previous_sequence + 1
                or continued != unfinished_packet
            ):
                _reject(422, "Recording has an invalid Ogg page sequence.")

        unfinished_packet = lacing[-1] == 255
        if ending and unfinished_packet:
            _reject(422, "Recording ends inside an Ogg packet.")
        streams[serial] = (sequence, unfinished_packet, ending)
        payload_bytes += body_size
        completed_packets += sum(value < 255 for value in lacing)
        offset = page_end
        page_count += 1
        if page_count > MAX_MEDIA_ELEMENTS:
            _reject(413, "Recording contains too many Ogg pages.")

    if (
        not streams
        or payload_bytes == 0
        or completed_packets == 0
        or any(
            unfinished or not ended for _sequence, unfinished, ended in streams.values()
        )
    ):
        _reject(422, "Recording has an incomplete Ogg container.")


def _read_ebml_element(
    content: bytes,
    offset: int,
    end: int,
    *,
    counter: list[int],
    allow_unknown_size: bool = False,
) -> tuple[int, int, int, bool]:
    """Return (id, body start, body end, unknown-size) within one boundary."""

    if offset >= end:
        _reject(422, "Recording has a truncated WebM element.")
    first = content[offset]
    marker = 0x80
    id_width = 1
    while id_width <= 4 and not first & marker:
        marker >>= 1
        id_width += 1
    if id_width > 4 or offset + id_width > end:
        _reject(422, "Recording has an invalid WebM element identifier.")
    element_id = int.from_bytes(content[offset : offset + id_width], "big")

    size_offset = offset + id_width
    if size_offset >= end:
        _reject(422, "Recording has a truncated WebM element size.")
    size_first = content[size_offset]
    size_marker = 0x80
    size_width = 1
    while size_width <= 8 and not size_first & size_marker:
        size_marker >>= 1
        size_width += 1
    if size_width > 8 or size_offset + size_width > end:
        _reject(422, "Recording has an invalid WebM element size.")
    size = size_first & (size_marker - 1)
    for byte in content[size_offset + 1 : size_offset + size_width]:
        size = (size << 8) | byte
    unknown_size = size == (1 << (7 * size_width)) - 1
    if unknown_size and not allow_unknown_size:
        _reject(422, "Recording has an unsupported unknown-size WebM element.")
    body_start = size_offset + size_width
    body_end = end if unknown_size else body_start + size
    if body_end > end:
        _reject(422, "Recording has a truncated WebM element body.")
    counter[0] += 1
    if counter[0] > MAX_MEDIA_ELEMENTS:
        _reject(413, "Recording contains too many WebM elements.")
    return element_id, body_start, body_end, unknown_size


_WEBM_SEGMENT_CHILDREN = frozenset(
    {
        0x114D9B74,  # SeekHead
        0x1549A966,  # Info
        0x1654AE6B,  # Tracks
        0x1F43B675,  # Cluster
        0x1C53BB6B,  # Cues
        0x1043A770,  # Chapters
        0x1254C367,  # Tags
        0xEC,  # Void
        0xBF,  # CRC-32
    }
)
_WEBM_CLUSTER_CHILDREN = frozenset(
    {
        0xE7,  # Timestamp
        0xA3,  # SimpleBlock
        0xA0,  # BlockGroup
        0xA7,  # Position
        0xAB,  # PrevSize
        0x5854,  # SilentTracks
        0xEC,
        0xBF,
    }
)
_WEBM_TRACK_ENTRY_CHILDREN = frozenset(
    {
        0xD7,  # TrackNumber
        0x73C5,  # TrackUID
        0x83,  # TrackType
        0xB9,  # FlagEnabled
        0x88,  # FlagDefault
        0x55AA,  # FlagForced
        0x9C,  # FlagLacing
        0x6DE7,  # MinCache
        0x6DF8,  # MaxCache
        0x23E383,  # DefaultDuration
        0x234E7A,  # DefaultDecodedFieldDuration
        0x23314F,  # TrackTimestampScale
        0x537F,  # TrackOffset
        0x55EE,  # MaxBlockAdditionID
        0x536E,  # Name
        0x22B59C,  # Language
        0x22B59D,  # LanguageBCP47
        0x86,  # CodecID
        0x63A2,  # CodecPrivate
        0x258688,  # CodecName
        0x7446,  # AttachmentLink
        0x3A9697,  # CodecSettings
        0x3B4040,  # CodecInfoURL
        0x26B240,  # CodecDownloadURL
        0xAA,  # CodecDecodeAll
        0x6FAB,  # TrackOverlay
        0x56AA,  # CodecDelay
        0x56BB,  # SeekPreRoll
        0x6624,  # TrackTranslate
        0xE0,  # Video
        0xE1,  # Audio
        0xEC,
        0xBF,
    }
)
_WEBM_CODECS = frozenset({b"V_VP8", b"V_VP9", b"V_AV1", b"A_VORBIS", b"A_OPUS"})


def _read_webm_uint(content: bytes, start: int, end: int) -> int:
    if not 1 <= end - start <= 8:
        _reject(422, "Recording has an invalid WebM integer.")
    return int.from_bytes(content[start:end], "big")


def _validate_webm_block(content: bytes, start: int, end: int) -> None:
    if end - start < 4:
        _reject(422, "Recording has a truncated WebM block.")
    first = content[start]
    marker = 0x80
    width = 1
    while width <= 8 and not first & marker:
        marker >>= 1
        width += 1
    if width > 8 or start + width + 3 > end:
        _reject(422, "Recording has an invalid WebM block track number.")
    track_number = first & (marker - 1)
    for byte in content[start + 1 : start + width]:
        track_number = (track_number << 8) | byte
    if track_number == 0:
        _reject(422, "Recording has an invalid WebM block track number.")


def _validate_webm_block_group(
    content: bytes,
    start: int,
    end: int,
    *,
    counter: list[int],
) -> None:
    allowed = frozenset(
        {
            0xA1,  # Block
            0xA6,  # BlockAdditions
            0x9B,  # BlockDuration
            0xFA,  # ReferencePriority
            0xFB,  # ReferenceBlock
            0xA4,  # CodecState
            0x75A2,  # DiscardPadding
            0x8E,  # Slices
            0xC8,  # ReferenceFrame
            0xEC,
            0xBF,
        }
    )
    cursor = start
    found_block = False
    while cursor < end:
        element_id, body_start, body_end, _unknown = _read_ebml_element(
            content, cursor, end, counter=counter
        )
        if element_id not in allowed:
            _reject(422, "Recording has an unsupported WebM block element.")
        if element_id == 0xA1:
            _validate_webm_block(content, body_start, body_end)
            found_block = True
        cursor = body_end
    if not found_block:
        _reject(422, "Recording has an empty WebM block group.")


def _validate_webm_tracks(
    content: bytes,
    start: int,
    end: int,
    *,
    counter: list[int],
) -> None:
    cursor = start
    track_count = 0
    while cursor < end:
        element_id, body_start, body_end, _unknown = _read_ebml_element(
            content, cursor, end, counter=counter
        )
        if element_id in {0xEC, 0xBF}:
            cursor = body_end
            continue
        if element_id != 0xAE:  # TrackEntry
            _reject(422, "Recording has an unsupported WebM tracks element.")
        fields: dict[int, tuple[int, int]] = {}
        entry_cursor = body_start
        while entry_cursor < body_end:
            child_id, child_start, child_end, _child_unknown = _read_ebml_element(
                content, entry_cursor, body_end, counter=counter
            )
            if child_id not in _WEBM_TRACK_ENTRY_CHILDREN:
                _reject(422, "Recording has an unsupported WebM track element.")
            if child_id in {0xD7, 0x83, 0x86}:
                if child_id in fields:
                    _reject(422, "Recording has duplicate WebM track fields.")
                fields[child_id] = (child_start, child_end)
            entry_cursor = child_end
        if set(fields) != {0xD7, 0x83, 0x86}:
            _reject(422, "Recording has an incomplete WebM track entry.")
        if _read_webm_uint(content, *fields[0xD7]) == 0:
            _reject(422, "Recording has an invalid WebM track number.")
        if _read_webm_uint(content, *fields[0x83]) not in {1, 2}:
            _reject(422, "Recording has an unsupported WebM track type.")
        codec_start, codec_end = fields[0x86]
        if content[codec_start:codec_end] not in _WEBM_CODECS:
            _reject(422, "Recording has an unsupported WebM codec.")
        track_count += 1
        cursor = body_end
    if track_count == 0:
        _reject(422, "Recording has no WebM tracks.")


def _validate_webm_cluster(
    content: bytes,
    start: int,
    end: int,
    *,
    counter: list[int],
    unknown_size: bool,
) -> tuple[int, bool]:
    cursor = start
    found_timestamp = False
    found_block = False
    while cursor < end:
        element_id, body_start, body_end, child_unknown = _read_ebml_element(
            content,
            cursor,
            end,
            counter=counter,
            allow_unknown_size=True,
        )
        if unknown_size and element_id in (
            _WEBM_SEGMENT_CHILDREN - _WEBM_CLUSTER_CHILDREN
        ):
            # Unknown-size master elements end when a sibling at the parent
            # level begins. Let the segment walker consume that sibling once.
            counter[0] -= 1
            break
        if child_unknown or element_id not in _WEBM_CLUSTER_CHILDREN:
            _reject(422, "Recording has an unsupported WebM cluster element.")
        if element_id == 0xE7:
            _read_webm_uint(content, body_start, body_end)
            found_timestamp = True
        elif element_id == 0xA3:
            _validate_webm_block(content, body_start, body_end)
            found_block = True
        elif element_id == 0xA0:
            _validate_webm_block_group(content, body_start, body_end, counter=counter)
            found_block = True
        cursor = body_end
    if not found_timestamp or not found_block:
        _reject(422, "Recording has an incomplete WebM cluster.")
    return cursor, found_block


def _validate_webm(content: bytes) -> None:
    counter = [0]
    element_id, header_start, header_end, header_unknown = _read_ebml_element(
        content, 0, len(content), counter=counter
    )
    if element_id != 0x1A45DFA3 or header_unknown:
        _reject(422, "File content does not match the declared .webm format.")
    header_children = frozenset(
        {0x4286, 0x42F7, 0x42F2, 0x42F3, 0x4282, 0x4287, 0x4285, 0xEC, 0xBF}
    )
    cursor = header_start
    document_type: bytes | None = None
    while cursor < header_end:
        child_id, child_start, child_end, _unknown = _read_ebml_element(
            content, cursor, header_end, counter=counter
        )
        if child_id not in header_children:
            _reject(422, "Recording has an unsupported WebM header element.")
        if child_id == 0x4282:
            if document_type is not None:
                _reject(422, "Recording has duplicate WebM document types.")
            document_type = content[child_start:child_end].lower()
        cursor = child_end
    if document_type != b"webm":
        _reject(422, "Recording is not a WebM document.")

    segment_id, segment_start, segment_end, _segment_unknown = _read_ebml_element(
        content,
        header_end,
        len(content),
        counter=counter,
        allow_unknown_size=True,
    )
    if segment_id != 0x18538067 or segment_end != len(content):
        _reject(422, "Recording has a malformed WebM segment.")
    cursor = segment_start
    found_info = False
    found_tracks = False
    found_block = False
    while cursor < segment_end:
        child_id, child_start, child_end, child_unknown = _read_ebml_element(
            content,
            cursor,
            segment_end,
            counter=counter,
            allow_unknown_size=True,
        )
        if child_id not in _WEBM_SEGMENT_CHILDREN:
            _reject(422, "Recording has an unsupported WebM segment element.")
        if child_unknown and child_id != 0x1F43B675:
            _reject(422, "Recording has an unsupported unknown-size WebM element.")
        if child_id == 0x1549A966:
            if found_info:
                _reject(422, "Recording has duplicate WebM info elements.")
            found_info = True
        elif child_id == 0x1654AE6B:
            if found_tracks:
                _reject(422, "Recording has duplicate WebM track elements.")
            _validate_webm_tracks(content, child_start, child_end, counter=counter)
            found_tracks = True
        elif child_id == 0x1F43B675:
            cursor, cluster_has_block = _validate_webm_cluster(
                content,
                child_start,
                child_end,
                counter=counter,
                unknown_size=child_unknown,
            )
            found_block = found_block or cluster_has_block
            continue
        cursor = child_end
    if not found_info or not found_tracks or not found_block:
        _reject(422, "Recording has an incomplete WebM segment.")


_MPEG1_BITRATES = {
    3: (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    2: (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    1: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
}
_MPEG2_BITRATES = {
    3: (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    1: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}


def _validate_mp3(content: bytes) -> None:
    offset = 0
    if content.startswith(b"ID3"):
        if len(content) < 10 or content[3] not in {2, 3, 4} or content[4] == 0xFF:
            _reject(422, "Recording has a malformed ID3 header.")
        allowed_flags = {2: 0xC0, 3: 0xE0, 4: 0xF0}[content[3]]
        if content[5] & ~allowed_flags or any(byte & 0x80 for byte in content[6:10]):
            _reject(422, "Recording has a malformed ID3 header.")
        metadata_size = 0
        for byte in content[6:10]:
            metadata_size = (metadata_size << 7) | byte
        if metadata_size > MAX_MEDIA_METADATA_BYTES:
            _reject(413, "Recording metadata exceeds the processing limit.")
        has_footer = content[3] == 4 and bool(content[5] & 0x10)
        offset = 10 + metadata_size + (10 if has_footer else 0)
        if offset > len(content):
            _reject(422, "Recording has a truncated ID3 tag.")
        if has_footer:
            footer = content[offset - 10 : offset]
            if footer[:3] != b"3DI" or footer[3:10] != content[3:10]:
                _reject(422, "Recording has a malformed ID3 footer.")

    frame_count = 0
    stream_parameters: tuple[int, int, int] | None = None
    while offset < len(content):
        if len(content) - offset == 128 and content[offset : offset + 3] == b"TAG":
            offset = len(content)
            break
        if len(content) - offset < 4:
            _reject(422, "Recording has a truncated MP3 frame.")
        header = int.from_bytes(content[offset : offset + 4], "big")
        version = (header >> 19) & 0x03
        layer = (header >> 17) & 0x03
        bitrate_index = (header >> 12) & 0x0F
        sample_index = (header >> 10) & 0x03
        padding = (header >> 9) & 0x01
        emphasis = header & 0x03
        if (
            (header >> 21) & 0x7FF != 0x7FF
            or version == 1
            or layer == 0
            or bitrate_index in {0, 15}
            or sample_index == 3
            or emphasis == 2
        ):
            _reject(422, "Recording has an invalid MP3 frame header.")
        sample_rates = {
            3: (44_100, 48_000, 32_000),
            2: (22_050, 24_000, 16_000),
            0: (11_025, 12_000, 8_000),
        }
        sample_rate = sample_rates[version][sample_index]
        bitrate_table = _MPEG1_BITRATES if version == 3 else _MPEG2_BITRATES
        bitrate = bitrate_table[layer][bitrate_index] * 1_000
        if layer == 3:
            frame_size = ((12 * bitrate) // sample_rate + padding) * 4
        elif layer == 1 and version != 3:
            frame_size = (72 * bitrate) // sample_rate + padding
        else:
            frame_size = (144 * bitrate) // sample_rate + padding
        if frame_size < 4 or offset + frame_size > len(content):
            _reject(422, "Recording has a truncated MP3 frame.")
        parameters = (version, layer, sample_rate)
        if stream_parameters is not None and parameters != stream_parameters:
            _reject(422, "Recording changes MP3 stream parameters unexpectedly.")
        stream_parameters = parameters
        offset += frame_size
        frame_count += 1
        if frame_count > MAX_MP3_FRAMES:
            _reject(413, "Recording contains too many MP3 frames.")
    if frame_count == 0 or offset != len(content):
        _reject(422, "Recording has no complete MP3 audio frames.")


def _validate_recording(content: bytes, *, suffix: str) -> None:
    if suffix == ".wav":
        valid = (
            len(content) >= 12
            and content.startswith(b"RIFF")
            and content[8:12] == b"WAVE"
            and int.from_bytes(content[4:8], "little") + 8 == len(content)
        )
    elif suffix == ".ogg":
        _validate_ogg(content)
        return
    elif suffix == ".webm":
        _validate_webm(content)
        return
    elif suffix == ".mp3":
        _validate_mp3(content)
        return
    elif suffix in {".mp4", ".m4a"}:
        _validate_iso_media(content, suffix=suffix)
        return
    else:  # pragma: no cover - defensive reviewed-suffix boundary
        valid = False
    if not valid:
        _reject(422, f"File content does not match the declared {suffix} format.")


def validate_upload_path(
    path: Path,
    *,
    filename: str,
    media_type: str,
    allowed_suffixes: set[str],
) -> str:
    """Validate extension, MIME, magic, polyglots, and bounded parsers."""

    try:
        content = path.read_bytes()
    except OSError as exc:
        raise UploadValidationError(422, "Uploaded file is unavailable.") from exc
    if not content:
        _reject(422, "Uploaded file is empty.")
    suffix, _normalized_media_type = _safe_suffix(
        filename=filename,
        media_type=media_type,
        allowed_suffixes=allowed_suffixes,
    )
    if _looks_like_active_markup(content) and suffix not in {".txt", ".md", ".csv"}:
        _reject(422, "Active SVG or HTML content is not allowed.")
    if suffix == ".pdf":
        _validate_pdf(path, content)
    elif suffix in {".docx", ".xlsx"}:
        if not content.startswith(b"PK\x03\x04"):
            _reject(422, f"File content does not match the declared {suffix} format.")
        _validate_ooxml(path, content, suffix=suffix)
    elif suffix in {".txt", ".md", ".csv"}:
        if b"\x00" in content:
            _reject(422, "Text uploads may not contain NUL bytes.")
        try:
            content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise UploadValidationError(
                422, "Text uploads must use UTF-8 encoding."
            ) from exc
        if _looks_like_active_markup(content):
            _reject(422, "Active SVG or HTML documents are not accepted as text.")
    elif suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        _validate_image(path, content, suffix=suffix)
    elif suffix in {".mp3", ".m4a", ".mp4", ".ogg", ".wav", ".webm"}:
        _validate_recording(content, suffix=suffix)
    else:  # Defensive: allowed suffixes must also have a reviewed parser.
        _reject(415, "Unsupported upload format.")
    return suffix


async def validate_upload_path_bounded(
    path: Path,
    *,
    filename: str,
    media_type: str,
    allowed_suffixes: set[str],
) -> str:
    """Validate an untrusted file in an isolated, resource-bounded child."""

    from app.services.bounded_document_parser import (
        BoundedDocumentParseError,
        BoundedDocumentParseTimeout,
        _run_child,
    )

    try:
        stdout, returncode = await _run_child(
            "app.services.upload_validation_process",
            str(path),
            filename,
            media_type,
            json.dumps(sorted(allowed_suffixes)),
            max_output_bytes=64 * 1024,
        )
    except BoundedDocumentParseTimeout as exc:
        raise UploadValidationError(
            422,
            "Uploaded file exceeded the safe validation time limit.",
        ) from exc
    except BoundedDocumentParseError as exc:
        raise UploadValidationError(
            422,
            "Uploaded file exceeded the safe validation output limit.",
        ) from exc
    if returncode != 0:
        raise UploadValidationError(
            422,
            "Uploaded file could not be validated safely.",
        )
    try:
        payload = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UploadValidationError(
            422,
            "Uploaded file could not be validated safely.",
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        raise UploadValidationError(
            422,
            "Uploaded file could not be validated safely.",
        )
    if payload["ok"] is False:
        status_code = payload.get("status_code")
        detail = payload.get("detail")
        if status_code not in {413, 415, 422} or not isinstance(detail, str):
            raise UploadValidationError(
                422,
                "Uploaded file could not be validated safely.",
            )
        raise UploadValidationError(status_code, detail[:500])
    suffix = payload.get("suffix")
    normalized_allowed = {item.casefold() for item in allowed_suffixes}
    if not isinstance(suffix, str) or suffix not in normalized_allowed:
        raise UploadValidationError(
            422,
            "Uploaded file could not be validated safely.",
        )
    return suffix


def validate_upload_bytes(
    *,
    filename: str,
    media_type: str,
    content: bytes,
    allowed_suffixes: set[str],
) -> str:
    """Compatibility wrapper used by non-persisted document extraction."""

    import tempfile

    suffix_hint = Path(filename or "upload").suffix
    with tempfile.NamedTemporaryFile(suffix=suffix_hint) as temporary:
        temporary.write(content)
        temporary.flush()
        return validate_upload_path(
            Path(temporary.name),
            filename=filename,
            media_type=media_type,
            allowed_suffixes=allowed_suffixes,
        )
