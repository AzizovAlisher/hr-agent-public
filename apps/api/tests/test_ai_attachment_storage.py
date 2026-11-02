"""Registry-backed AI attachment authorization, audit, and erasure."""

from __future__ import annotations

import hashlib
import io
import struct
import zipfile
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4
from xml.etree import ElementTree

import pytest
from docx import Document
from fastapi import HTTPException, UploadFile
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fpdf import FPDF
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.workbook.defined_name import DefinedName
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    TextStringObject,
)
from sqlalchemy import select
from starlette.datastructures import Headers

from app.config import get_settings
from app.db import SessionLocal
from app.models.ai_hiring import AiOrganizationSettings, AiSession
from app.models.enterprise import AuditLog
from app.models.organization import Organization
from app.models.private_object import PrivateObject, PrivateObjectDeletion
from app.models.user import User
from app.services.ai_hiring_attachments import (
    AiAttachmentInventoryError,
    delete_ai_attachment,
    download_ai_attachment,
    extract_unsubmitted_ai_attachment_inventory,
    resolve_attachment_references,
    save_ai_attachment,
)
from app.services.bounded_document_parser import extract_spreadsheet_inventory
from app.services.private_object_worker import process_private_object_deletions_once
from app.storage import LocalBlobStore


pytestmark = pytest.mark.asyncio


async def _actor(db, label: str) -> tuple[Organization, User, AiSession]:
    organization = Organization(
        name=f"AI files {label}", slug=f"ai-files-{label}-{uuid4().hex[:8]}"
    )
    db.add(organization)
    await db.flush()
    user = User(
        organization_id=organization.id,
        email=f"ai-files-{label}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    db.add(user)
    db.add(
        AiOrganizationSettings(
            organization_id=organization.id,
            feature_enabled=True,
        )
    )
    session = AiSession(
        organization_id=organization.id,
        created_by_user_id=user.id,
        status="active",
    )
    db.add(session)
    await db.commit()
    return organization, user, session


def _upload() -> UploadFile:
    return UploadFile(
        filename="candidate.txt",
        file=io.BytesIO(b"Python and PostgreSQL delivery experience."),
        headers=Headers({"content-type": "text/plain"}),
    )


def _bytes_upload(filename: str, content_type: str, content: bytes) -> UploadFile:
    return UploadFile(
        filename=filename,
        file=io.BytesIO(content),
        headers=Headers({"content-type": content_type}),
    )


def _xlsx_upload(row_count: int) -> UploadFile:
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet()
    sheet.append(["name", "cv_text"])
    for index in range(row_count):
        sheet.append([f"Candidate {index}", f"Profile {index}"])
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return _bytes_upload(
        "candidates.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        output.getvalue(),
    )


def _unsupported_xlsx_upload(case: str) -> UploadFile:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["name", "cv_text"])
    if case == "formula":
        sheet.append(["Ada", '="candidate@example.test"'])
    elif case == "hidden-sheet":
        sheet.append(["Ada", "Python"])
        private_sheet = workbook.create_sheet("Candidate private data")
        private_sheet.append(["candidate@example.test"])
        private_sheet.sheet_state = "hidden"
    else:
        sheet.append(["Ada", "Python"])
        if case == "comment":
            sheet["B2"].comment = Comment(
                "candidate@example.test",
                "Candidate",
            )
        elif case == "hyperlink":
            sheet["B2"].hyperlink = "mailto:candidate@example.test"
        elif case == "defined-name":
            workbook.defined_names.add(
                DefinedName("CandidateMarker", attr_text="Sheet!$B$2")
            )
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    if case == "custom-property":
        original = io.BytesIO(output.getvalue())
        output = io.BytesIO()
        with (
            zipfile.ZipFile(original) as incoming,
            zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as outgoing,
        ):
            for info in incoming.infolist():
                outgoing.writestr(info.filename, incoming.read(info))
            outgoing.writestr(
                "docProps/custom.xml",
                (
                    "<properties><marker>candidate@example.test"
                    "</marker></properties>"
                ),
            )
    content = output.getvalue()
    if case.startswith("zip-"):
        content = _zip_with_lexical_surface(
            content,
            case.removeprefix("zip-"),
            "candidate@example.test",
        )
    return _bytes_upload(
        f"unsupported-{case}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        content,
    )


def _docx_bytes() -> bytes:
    document = Document()
    document.add_paragraph("Bounded DOCX attachment")
    source = io.BytesIO()
    document.save(source)
    source.seek(0)
    output = io.BytesIO()
    with (
        zipfile.ZipFile(source) as incoming,
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as outgoing,
    ):
        for info in incoming.infolist():
            name = info.filename
            if name.startswith("customXml/") or name == "docProps/thumbnail.jpeg":
                continue
            content = incoming.read(name)
            if name == "[Content_Types].xml":
                root = ElementTree.fromstring(content)
                for child in list(root):
                    if child.attrib.get("Extension") == "jpeg" or child.attrib.get(
                        "PartName", ""
                    ).startswith("/customXml/"):
                        root.remove(child)
                content = ElementTree.tostring(
                    root,
                    encoding="utf-8",
                    xml_declaration=True,
                )
            elif name == "_rels/.rels":
                root = ElementTree.fromstring(content)
                for child in list(root):
                    if child.attrib.get("Type", "").endswith("/thumbnail"):
                        root.remove(child)
                content = ElementTree.tostring(
                    root,
                    encoding="utf-8",
                    xml_declaration=True,
                )
            elif name == "word/_rels/document.xml.rels":
                root = ElementTree.fromstring(content)
                for child in list(root):
                    if child.attrib.get("Type", "").endswith("/customXml"):
                        root.remove(child)
                content = ElementTree.tostring(
                    root,
                    encoding="utf-8",
                    xml_declaration=True,
                )
            outgoing.writestr(name, content)
    return output.getvalue()


def _rewrite_zip_part(content: bytes, name: str, mutate) -> bytes:
    source = io.BytesIO(content)
    output = io.BytesIO()
    with (
        zipfile.ZipFile(source) as incoming,
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as outgoing,
    ):
        for info in incoming.infolist():
            part = incoming.read(info)
            outgoing.writestr(
                info.filename,
                mutate(part) if info.filename == name else part,
            )
    return output.getvalue()


def _ooxml_with_lexical_surface(
    kind: str,
    case: str,
    marker: str,
) -> bytes:
    if kind == "docx":
        content = _docx_bytes()
        part_name = "word/document.xml"
        root_token = b"<w:document"
    elif kind == "xlsx":
        upload = _xlsx_upload(1)
        content = upload.file.read()
        part_name = "xl/worksheets/sheet1.xml"
        root_token = b"<worksheet"
    else:
        raise AssertionError(f"Unknown OOXML kind: {kind}")

    def add_surface(xml: bytes) -> bytes:
        encoded_marker = marker.encode()
        declaration_end = xml.find(b"?>")
        declaration_end = 0 if declaration_end < 0 else declaration_end + 2
        if case == "comment":
            surface = b"<!--" + encoded_marker + b"-->"
            return xml[:declaration_end] + surface + xml[declaration_end:]
        if case == "processing-instruction":
            surface = b"<?candidate " + encoded_marker + b"?>"
            return xml[:declaration_end] + surface + xml[declaration_end:]
        if case in {"namespace", "encoded-namespace"}:
            root_start = xml.find(root_token)
            if root_start < 0:
                raise AssertionError("OOXML root is missing")
            insert_at = root_start + len(root_token)
            namespace_marker = (
                encoded_marker.replace(b"@", b"&#64;")
                if case == "encoded-namespace"
                else encoded_marker
            )
            surface = b' xmlns:privacy="urn:privacy:' + namespace_marker + b'"'
            return xml[:insert_at] + surface + xml[insert_at:]
        raise AssertionError(f"Unknown OOXML lexical case: {case}")

    return _rewrite_zip_part(content, part_name, add_surface)


def _zip_with_lexical_surface(
    content: bytes,
    case: str,
    marker: str,
) -> bytes:
    marker_bytes = marker.encode()
    if case == "prefix":
        return b"unreferenced-prefix:" + marker_bytes + content
    if case == "unreferenced-local":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            central_offset = archive.start_dir
        filename = b"unreferenced-candidate.txt"
        local_record = struct.pack(
            "<IHHHHHIIIHH",
            0x04034B50,
            20,
            0,
            zipfile.ZIP_STORED,
            0,
            0,
            zlib.crc32(marker_bytes),
            len(marker_bytes),
            len(marker_bytes),
            len(filename),
            0,
        ) + filename + marker_bytes
        mutated = bytearray(
            content[:central_offset] + local_record + content[central_offset:]
        )
        eocd_offset = mutated.rfind(b"PK\x05\x06")
        if eocd_offset < 0:
            raise AssertionError("ZIP EOCD is missing")
        struct.pack_into(
            "<I",
            mutated,
            eocd_offset + 16,
            central_offset + len(local_record),
        )
        return bytes(mutated)
    if case == "deflate-trailer":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            central_offset = archive.start_dir
            target = next(
                entry
                for entry in entries
                if entry.compress_type == zipfile.ZIP_DEFLATED
            )
        local_header = struct.unpack_from(
            "<IHHHHHIIIHH",
            content,
            target.header_offset,
        )
        filename_length, extra_length = local_header[-2:]
        data_start = target.header_offset + 30 + filename_length + extra_length
        insert_at = data_start + target.compress_size
        mutated = bytearray(content)
        mutated[insert_at:insert_at] = marker_bytes
        struct.pack_into(
            "<I",
            mutated,
            target.header_offset + 18,
            target.compress_size + len(marker_bytes),
        )
        cursor = central_offset + len(marker_bytes)
        for entry in entries:
            central_header = struct.unpack_from(
                "<IHHHHHHIIIHHHHHII",
                mutated,
                cursor,
            )
            filename_length = central_header[10]
            extra_length = central_header[11]
            comment_length = central_header[12]
            if entry.filename == target.filename:
                struct.pack_into(
                    "<I",
                    mutated,
                    cursor + 20,
                    target.compress_size + len(marker_bytes),
                )
            if entry.header_offset >= insert_at:
                struct.pack_into(
                    "<I",
                    mutated,
                    cursor + 42,
                    entry.header_offset + len(marker_bytes),
                )
            cursor += 46 + filename_length + extra_length + comment_length
        eocd_offset = mutated.rfind(b"PK\x05\x06")
        if eocd_offset < 0:
            raise AssertionError("ZIP EOCD is missing")
        struct.pack_into(
            "<I",
            mutated,
            eocd_offset + 16,
            central_offset + len(marker_bytes),
        )
        return bytes(mutated)

    source = io.BytesIO(content)
    output = io.BytesIO()
    with (
        zipfile.ZipFile(source) as incoming,
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as outgoing,
    ):
        for index, info in enumerate(incoming.infolist()):
            copied = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            copied.compress_type = info.compress_type
            copied.external_attr = info.external_attr
            copied.create_system = info.create_system
            if index == 0 and case == "entry-comment":
                copied.comment = marker_bytes
            elif index == 0 and case == "entry-extra":
                copied.extra = struct.pack("<HH", 0xCAFE, len(marker_bytes)) + marker_bytes
            outgoing.writestr(copied, incoming.read(info))
        if case == "archive-comment":
            outgoing.comment = marker_bytes
        elif case not in {"entry-comment", "entry-extra"}:
            raise AssertionError(f"Unknown ZIP lexical case: {case}")
    return output.getvalue()


def _unsupported_docx_bytes(case: str) -> bytes:
    if case.startswith("zip-"):
        return _zip_with_lexical_surface(
            _docx_bytes(),
            case.removeprefix("zip-"),
            "candidate@example.test",
        )
    if case == "custom-xml":
        document = Document()
        document.add_paragraph("Generic candidate body")
        output = io.BytesIO()
        document.save(output)
        return output.getvalue()
    if case == "image":
        document = Document()
        document.add_paragraph("Generic candidate body")
        image = io.BytesIO()
        Image.new("RGB", (20, 20), color="white").save(image, format="PNG")
        image.seek(0)
        document.add_picture(image)
        output = io.BytesIO()
        document.save(output)
        return output.getvalue()
    if case == "hyperlink":
        document = Document()
        document.add_paragraph("Generic candidate body")
        document.part.relate_to(
            "mailto:candidate@example.test",
            (
                "http://schemas.openxmlformats.org/officeDocument/2006/"
                "relationships/hyperlink"
            ),
            is_external=True,
        )
        source = io.BytesIO()
        document.save(source)
        return source.getvalue()
    if case == "mail-merge":
        word_namespace = (
            "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        )

        def add_mail_merge(value: bytes) -> bytes:
            root = ElementTree.fromstring(value)
            merge = ElementTree.SubElement(
                root,
                f"{{{word_namespace}}}mailMerge",
            )
            ElementTree.SubElement(
                merge,
                f"{{{word_namespace}}}linkToQuery",
            )
            ElementTree.SubElement(
                merge,
                f"{{{word_namespace}}}dataSource",
                {
                    f"{{{word_namespace}}}val": (
                        "https://example.test/candidate@example.test"
                    )
                },
            )
            ElementTree.SubElement(
                merge,
                f"{{{word_namespace}}}connectString",
                {
                    f"{{{word_namespace}}}val": (
                        "Provider=OLEDB;Data Source=candidate@example.test"
                    )
                },
            )
            return ElementTree.tostring(
                root,
                encoding="utf-8",
                xml_declaration=True,
            )

        return _rewrite_zip_part(
            _docx_bytes(),
            "word/settings.xml",
            add_mail_merge,
        )
    if case not in {"vml-textbox", "field", "content-control"}:
        raise AssertionError(f"Unknown DOCX case: {case}")

    word_namespace = (
        "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    )
    vml_namespace = "urn:schemas-microsoft-com:vml"

    def add_vml_textbox(value: bytes) -> bytes:
        root = ElementTree.fromstring(value)
        body = root.find(f"{{{word_namespace}}}body")
        assert body is not None
        paragraph = ElementTree.SubElement(body, f"{{{word_namespace}}}p")
        if case == "field":
            field = ElementTree.SubElement(
                paragraph,
                f"{{{word_namespace}}}fldSimple",
                {
                    f"{{{word_namespace}}}instr": (
                        'HYPERLINK "mailto:candidate@example.test"'
                    )
                },
            )
            run = ElementTree.SubElement(field, f"{{{word_namespace}}}r")
            text = ElementTree.SubElement(run, f"{{{word_namespace}}}t")
            text.text = "candidate@example.test"
            return ElementTree.tostring(
                root,
                encoding="utf-8",
                xml_declaration=True,
            )
        if case == "content-control":
            control = ElementTree.SubElement(
                paragraph,
                f"{{{word_namespace}}}sdt",
            )
            properties = ElementTree.SubElement(
                control,
                f"{{{word_namespace}}}sdtPr",
            )
            dropdown = ElementTree.SubElement(
                properties,
                f"{{{word_namespace}}}dropDownList",
            )
            ElementTree.SubElement(
                dropdown,
                f"{{{word_namespace}}}listItem",
                {
                    f"{{{word_namespace}}}displayText": "Candidate",
                    f"{{{word_namespace}}}value": "candidate@example.test",
                },
            )
            contents = ElementTree.SubElement(
                control,
                f"{{{word_namespace}}}sdtContent",
            )
            nested_paragraph = ElementTree.SubElement(
                contents,
                f"{{{word_namespace}}}p",
            )
            nested_run = ElementTree.SubElement(
                nested_paragraph,
                f"{{{word_namespace}}}r",
            )
            text = ElementTree.SubElement(
                nested_run,
                f"{{{word_namespace}}}t",
            )
            text.text = "candidate@example.test"
            return ElementTree.tostring(
                root,
                encoding="utf-8",
                xml_declaration=True,
            )
        run = ElementTree.SubElement(paragraph, f"{{{word_namespace}}}r")
        pict = ElementTree.SubElement(run, f"{{{word_namespace}}}pict")
        shape = ElementTree.SubElement(pict, f"{{{vml_namespace}}}shape")
        textbox = ElementTree.SubElement(shape, f"{{{vml_namespace}}}textbox")
        contents = ElementTree.SubElement(
            textbox,
            f"{{{word_namespace}}}txbxContent",
        )
        nested_paragraph = ElementTree.SubElement(
            contents,
            f"{{{word_namespace}}}p",
        )
        nested_run = ElementTree.SubElement(
            nested_paragraph,
            f"{{{word_namespace}}}r",
        )
        text = ElementTree.SubElement(
            nested_run,
            f"{{{word_namespace}}}t",
        )
        text.text = "candidate@example.test"
        return ElementTree.tostring(
            root,
            encoding="utf-8",
            xml_declaration=True,
        )

    return _rewrite_zip_part(
        _docx_bytes(),
        "word/document.xml",
        add_vml_textbox,
    )


def _pdf_bytes() -> bytes:
    source = FPDF()
    source.add_page()
    source.set_font("Helvetica", size=12)
    source.text(10, 20, "Bounded PDF attachment")
    reader = PdfReader(io.BytesIO(bytes(source.output())))
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _unicode_pdf_bytes(case: str | None = None) -> bytes:
    text = "Кандидат: Анна Иванова — Python"
    codepoints = sorted(set(map(ord, text)))
    names = {
        codepoint: "space" if codepoint == 32 else f"uni{codepoint:04X}"
        for codepoint in codepoints
    }
    glyph_order = [".notdef", *dict.fromkeys(names.values())]
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(glyph_order)
    builder.setupCharacterMap(
        {codepoint: names[codepoint] for codepoint in codepoints}
    )
    glyphs = {}
    metrics = {}
    for name in glyph_order:
        pen = TTGlyphPen(None)
        if name not in {".notdef", "space"}:
            pen.moveTo((80, 0))
            pen.lineTo((80, 700))
            pen.lineTo((520, 700))
            pen.lineTo((520, 0))
            pen.closePath()
        glyphs[name] = pen.glyph()
        metrics[name] = (600, 0)
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics(metrics)
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupOS2(
        sTypoAscender=800,
        sTypoDescender=-200,
        usWinAscent=800,
        usWinDescent=200,
    )
    builder.setupNameTable(
        {
            "familyName": "Pilot Unicode",
            "styleName": "Regular",
            "uniqueFontIdentifier": "Pilot Unicode Regular",
            "fullName": "Pilot Unicode Regular",
            "psName": "PilotUnicode-Regular",
            "version": "Version 1.0",
        }
    )
    builder.setupPost()
    builder.setupMaxp()
    with TemporaryDirectory() as directory:
        font_path = Path(directory) / "pilot-unicode.ttf"
        builder.save(font_path)
        source = FPDF()
        source.add_page()
        source.add_font("PilotUnicode", fname=str(font_path))
        source.set_font("PilotUnicode", size=12)
        source.text(10, 20, text)
        reader = PdfReader(io.BytesIO(bytes(source.output())))
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    if case is not None:
        font = writer.pages[0]["/Resources"]["/Font"]["/F1"].get_object()
        descendant = font["/DescendantFonts"][0].get_object()
        if case == "unicode-cmap-program":
            cmap = font["/ToUnicode"].get_object()
            cmap.set_data(
                cmap.get_data().replace(
                    b"endcmap",
                    b"(candidate@example.test) run\nendcmap",
                )
            )
        elif case == "unicode-invalid-font":
            descriptor = descendant["/FontDescriptor"].get_object()
            font_file = descriptor["/FontFile2"].get_object()
            invalid = b"\x00\x01\x00\x00not-a-valid-truetype-font"
            font_file.set_data(invalid)
            font_file[NameObject("/Length1")] = NumberObject(len(invalid))
        else:
            raise AssertionError(f"Unknown Unicode PDF case: {case}")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _unsupported_pdf_bytes(case: str) -> bytes:
    if case in {"unicode-cmap-program", "unicode-invalid-font"}:
        return _unicode_pdf_bytes(case)
    if case == "image":
        image = io.BytesIO()
        Image.new("RGB", (20, 20), color="white").save(image, format="PNG")
        image.seek(0)
        source = FPDF()
        source.add_page()
        source.set_font("Helvetica", size=12)
        source.text(10, 20, "Generic candidate body")
        source.image(image, x=10, y=30, w=20, h=20)
        return bytes(source.output())
    if case == "empty":
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
    else:
        reader = PdfReader(io.BytesIO(_pdf_bytes()))
        writer = PdfWriter()
        writer.add_page(reader.pages[0])
        if case == "annotation":
            writer.add_annotation(
                page_number=0,
                annotation=DictionaryObject(
                    {
                        NameObject("/Type"): NameObject("/Annot"),
                        NameObject("/Subtype"): NameObject("/Text"),
                        NameObject("/Rect"): ArrayObject(
                            [
                                FloatObject(0),
                                FloatObject(0),
                                FloatObject(20),
                                FloatObject(20),
                            ]
                        ),
                        NameObject("/Contents"): TextStringObject(
                            "candidate@example.test"
                        ),
                    }
                ),
            )
        elif case == "acroform":
            field = DictionaryObject(
                {
                    NameObject("/FT"): NameObject("/Tx"),
                    NameObject("/T"): TextStringObject("Candidate email"),
                    NameObject("/V"): TextStringObject(
                        "candidate@example.test"
                    ),
                }
            )
            writer._root_object[NameObject("/AcroForm")] = DictionaryObject(
                {
                    NameObject("/Fields"): ArrayObject(
                        [writer._add_object(field)]
                    )
                }
            )
        elif case == "custom-catalog":
            writer._root_object[NameObject("/CandidateMarker")] = (
                TextStringObject("candidate@example.test")
            )
        elif case == "unreferenced-object":
            writer._add_object(TextStringObject("candidate@example.test"))
        elif case == "trailing-data":
            pass
        else:
            raise AssertionError(f"Unknown PDF case: {case}")
    output = io.BytesIO()
    writer.write(output)
    content = output.getvalue()
    if case == "trailing-data":
        content += b"\ncandidate@example.test\n"
    return content


def _stored_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


async def test_ai_attachment_rejects_xlsx_above_inventory_row_bound_without_orphan(
    db,
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "objects"
    monkeypatch.setattr(get_settings(), "storage_dir", str(root))
    organization, admin, session = await _actor(db, "xlsx-over-bound")
    organization_id = organization.id
    store = LocalBlobStore(root)

    with pytest.raises(HTTPException) as rejected:
        await save_ai_attachment(
            db,
            admin,
            session.id,
            upload=_xlsx_upload(501),
            store=store,
        )

    assert rejected.value.status_code == 413
    assert rejected.value.detail == (
        "AI attachment inventory is limited to 500 spreadsheet rows."
    )
    assert (
        await db.execute(
            select(PrivateObject).where(
                PrivateObject.organization_id == organization_id,
                PrivateObject.resource_type == "ai_attachment",
            )
        )
    ).scalars().all() == []
    assert _stored_files(root) == []


async def test_ai_attachment_accepts_xlsx_at_exact_inventory_row_bound(
    db,
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "objects"
    monkeypatch.setattr(get_settings(), "storage_dir", str(root))
    _organization, admin, session = await _actor(db, "xlsx-at-bound")
    store = LocalBlobStore(root)

    result = await save_ai_attachment(
        db,
        admin,
        session.id,
        upload=_xlsx_upload(500),
        store=store,
    )

    record = await db.get(PrivateObject, UUID(result["id"]))
    assert record is not None
    assert record.state == "ready"
    assert await store.stat(record.object_key) is not None


@pytest.mark.parametrize(
    "case",
    [
        "formula",
        "hidden-sheet",
        "comment",
        "hyperlink",
        "defined-name",
        "custom-property",
        "zip-archive-comment",
        "zip-entry-comment",
        "zip-entry-extra",
        "zip-unreferenced-local",
        "zip-deflate-trailer",
    ],
)
async def test_ai_attachment_rejects_unsupported_xlsx_without_orphan(
    db,
    tmp_path: Path,
    monkeypatch,
    case: str,
):
    root = tmp_path / "objects"
    monkeypatch.setattr(get_settings(), "storage_dir", str(root))
    organization, admin, session = await _actor(db, f"unsupported-{case}")
    organization_id = organization.id
    store = LocalBlobStore(root)

    with pytest.raises(HTTPException) as rejected:
        await save_ai_attachment(
            db,
            admin,
            session.id,
            upload=_unsupported_xlsx_upload(case),
            store=store,
        )

    assert rejected.value.status_code == 422
    assert rejected.value.detail == (
        "Attachment contains unsupported embedded or interactive content; "
        "upload a text-only PDF/DOCX, TXT/CSV, or single-sheet XLSX."
    )
    assert (
        await db.execute(
            select(PrivateObject).where(
                PrivateObject.organization_id == organization_id,
                PrivateObject.resource_type == "ai_attachment",
            )
        )
    ).scalars().all() == []
    assert _stored_files(root) == []


@pytest.mark.parametrize(
    ("kind", "case", "filename", "content_type"),
    [
        (
            "docx",
            "custom-xml",
            "custom-data.docx",
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
        ),
        (
            "docx",
            "hyperlink",
            "mailto.docx",
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
        ),
        (
            "docx",
            "image",
            "embedded-image.docx",
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
        ),
        (
            "docx",
            "vml-textbox",
            "vml-textbox.docx",
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
        ),
        (
            "docx",
            "field",
            "word-field.docx",
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
        ),
        (
            "docx",
            "content-control",
            "content-control.docx",
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
        ),
        (
            "docx",
            "mail-merge",
            "mail-merge.docx",
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
        ),
        (
            "docx",
            "zip-archive-comment",
            "archive-comment.docx",
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
        ),
        (
            "docx",
            "zip-entry-comment",
            "entry-comment.docx",
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
        ),
        (
            "docx",
            "zip-entry-extra",
            "entry-extra.docx",
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
        ),
        (
            "docx",
            "zip-unreferenced-local",
            "unreferenced-local.docx",
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
        ),
        (
            "docx",
            "zip-deflate-trailer",
            "deflate-trailer.docx",
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
        ),
        ("pdf", "annotation", "annotation.pdf", "application/pdf"),
        ("pdf", "acroform", "form.pdf", "application/pdf"),
        ("pdf", "image", "image.pdf", "application/pdf"),
        ("pdf", "empty", "empty.pdf", "application/pdf"),
        (
            "pdf",
            "custom-catalog",
            "custom-catalog.pdf",
            "application/pdf",
        ),
        (
            "pdf",
            "unreferenced-object",
            "unreferenced.pdf",
            "application/pdf",
        ),
        ("pdf", "trailing-data", "trailing.pdf", "application/pdf"),
        (
            "pdf",
            "unicode-cmap-program",
            "unicode-cmap-program.pdf",
            "application/pdf",
        ),
        (
            "pdf",
            "unicode-invalid-font",
            "unicode-invalid-font.pdf",
            "application/pdf",
        ),
    ],
)
async def test_ai_attachment_rejects_uninventoried_document_surfaces_without_orphan(
    db,
    tmp_path: Path,
    monkeypatch,
    kind: str,
    case: str,
    filename: str,
    content_type: str,
):
    root = tmp_path / "objects"
    monkeypatch.setattr(get_settings(), "storage_dir", str(root))
    organization, admin, session = await _actor(
        db,
        f"unsupported-{kind}-{case}",
    )
    organization_id = organization.id
    store = LocalBlobStore(root)
    content = (
        _unsupported_docx_bytes(case)
        if kind == "docx"
        else _unsupported_pdf_bytes(case)
    )

    with pytest.raises(HTTPException) as rejected:
        await save_ai_attachment(
            db,
            admin,
            session.id,
            upload=_bytes_upload(filename, content_type, content),
            store=store,
        )

    assert rejected.value.status_code == 422
    assert rejected.value.detail in {
        (
            "Attachment contains unsupported embedded or interactive content; "
            "upload a text-only PDF/DOCX, TXT/CSV, or single-sheet XLSX."
        ),
        "Active or embedded PDF content is not allowed.",
    }
    assert (
        await db.execute(
            select(PrivateObject).where(
                PrivateObject.organization_id == organization_id,
                PrivateObject.resource_type == "ai_attachment",
            )
        )
    ).scalars().all() == []
    assert _stored_files(root) == []


@pytest.mark.parametrize(
    ("filename", "content_type", "content"),
    [
        ("candidate.txt", "text/plain", "UTF-8 profile Ж".encode()),
        ("candidate.csv", "text/csv", b"name,cv_text\nAda,Python\n"),
        (
            "candidate.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            _docx_bytes(),
        ),
        ("candidate.pdf", "application/pdf", _pdf_bytes()),
        (
            "candidate-ru.pdf",
            "application/pdf",
            _unicode_pdf_bytes(),
        ),
    ],
    ids=("txt", "csv", "docx", "pdf", "pdf-unicode-ru"),
)
async def test_ai_attachment_inventory_accepts_adjacent_parseable_formats(
    db,
    tmp_path: Path,
    monkeypatch,
    filename: str,
    content_type: str,
    content: bytes,
):
    root = tmp_path / "objects"
    monkeypatch.setattr(get_settings(), "storage_dir", str(root))
    _organization, admin, session = await _actor(db, f"parseable-{filename}")
    store = LocalBlobStore(root)

    result = await save_ai_attachment(
        db,
        admin,
        session.id,
        upload=_bytes_upload(filename, content_type, content),
        store=store,
    )

    record = await db.get(PrivateObject, UUID(result["id"]))
    assert record is not None
    assert record.state == "ready"
    assert await store.stat(record.object_key) is not None


@pytest.mark.parametrize(
    ("kind", "case"),
    [
        ("docx", "comment"),
        ("docx", "processing-instruction"),
        ("docx", "namespace"),
        ("docx", "encoded-namespace"),
        ("xlsx", "comment"),
        ("xlsx", "processing-instruction"),
        ("xlsx", "namespace"),
        ("xlsx", "encoded-namespace"),
    ],
)
async def test_ai_attachment_inventory_covers_complete_ooxml_lexical_surface(
    db,
    tmp_path: Path,
    monkeypatch,
    kind: str,
    case: str,
):
    root = tmp_path / "objects"
    monkeypatch.setattr(get_settings(), "storage_dir", str(root))
    _organization, admin, session = await _actor(
        db,
        f"lexical-{kind}-{case}",
    )
    marker = f"lexical-{kind}-{case}-{uuid4().hex}@candidate.example"
    content = _ooxml_with_lexical_surface(kind, case, marker)
    filename = f"lexical-{case}.{kind}"
    content_type = (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document"
        if kind == "docx"
        else (
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        )
    )
    store = LocalBlobStore(root)

    result = await save_ai_attachment(
        db,
        admin,
        session.id,
        upload=_bytes_upload(filename, content_type, content),
        store=store,
    )
    record = await db.get(PrivateObject, UUID(result["id"]))
    assert record is not None

    inventory = await extract_unsubmitted_ai_attachment_inventory(
        store,
        object_key=record.object_key,
        size_bytes=record.size_bytes,
        sha256=record.sha256,
    )

    assert marker in inventory


async def test_ai_attachment_rejects_invalid_utf8_without_orphan(
    db,
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "objects"
    monkeypatch.setattr(get_settings(), "storage_dir", str(root))
    organization, admin, session = await _actor(db, "invalid-utf8")
    organization_id = organization.id
    store = LocalBlobStore(root)

    with pytest.raises(HTTPException) as rejected:
        await save_ai_attachment(
            db,
            admin,
            session.id,
            upload=_bytes_upload("candidate.txt", "text/plain", b"\xff\xfe\xfa"),
            store=store,
        )

    assert rejected.value.status_code == 422
    assert rejected.value.detail == "Text uploads must use UTF-8 encoding."
    assert (
        await db.execute(
            select(PrivateObject).where(
                PrivateObject.organization_id == organization_id,
                PrivateObject.resource_type == "ai_attachment",
            )
        )
    ).scalars().all() == []
    assert _stored_files(root) == []


async def test_ai_attachment_inventory_rechecks_integrity_after_child_parse(
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "objects"
    store = LocalBlobStore(root)
    source = tmp_path / "candidate.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["name", "cv_text"])
    sheet.append(["Ada", "Python"])
    workbook.save(source)
    workbook.close()
    original = source.read_bytes()
    object_key = f"private/{uuid4()}/ai/{uuid4()}.xlsx"
    await store.put_file(
        object_key,
        source,
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )

    async def parse_then_mutate(path: Path, *, max_rows: int):
        result = await extract_spreadsheet_inventory(path, max_rows=max_rows)
        path.write_bytes(path.read_bytes() + b"changed-after-parse")
        return result

    monkeypatch.setattr(
        "app.services.ai_hiring_attachments.extract_spreadsheet_inventory",
        parse_then_mutate,
    )

    with pytest.raises(AiAttachmentInventoryError) as rejected:
        await extract_unsubmitted_ai_attachment_inventory(
            store,
            object_key=object_key,
            size_bytes=len(original),
            sha256=hashlib.sha256(original).hexdigest(),
        )

    assert rejected.value.code == "integrity_failed"


async def test_ai_attachment_is_private_audited_and_durably_deleted(
    db,
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "objects"
    monkeypatch.setattr(get_settings(), "storage_dir", str(root))
    organization, admin, session = await _actor(db, "owner")
    _other_org, outsider, _other_session = await _actor(db, "outsider")
    store = LocalBlobStore(root)

    result = await save_ai_attachment(
        db,
        admin,
        session.id,
        upload=_upload(),
        store=store,
    )
    assert "key" not in result
    assert "path" not in result
    attachment_id = UUID(result["id"])
    record = await db.get(PrivateObject, attachment_id)
    assert record is not None
    assert record.resource_type == "ai_attachment"
    assert record.resource_id == session.id
    assert record.category == "ai"
    assert record.retention_until is not None
    assert await store.stat(record.object_key) is not None

    with pytest.raises(HTTPException) as malformed:
        await resolve_attachment_references(
            db,
            user=admin,
            session_id=session.id,
            references=[
                {
                    "id": "not-a-uuid",
                    "upload_token": result["upload_token"],
                }
            ],
        )
    assert malformed.value.status_code == 422

    response = await download_ai_attachment(
        db,
        admin,
        session_id=session.id,
        attachment_id=record.id,
        store=store,
    )
    assert b"".join([chunk async for chunk in response.body_iterator]) == (
        b"Python and PostgreSQL delivery experience."
    )
    assert response.headers["Cache-Control"].startswith("private, no-store")
    audit = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.organization_id == organization.id,
                AuditLog.action == "download_ai_attachment",
            )
        )
    ).scalar_one()
    assert audit.resource_id == record.id
    assert audit.metadata_json["sensitive_read"] is True

    with pytest.raises(HTTPException) as denied:
        await download_ai_attachment(
            db,
            outsider,
            session_id=session.id,
            attachment_id=record.id,
            store=store,
        )
    assert denied.value.status_code == 404

    await delete_ai_attachment(
        db,
        admin,
        session_id=session.id,
        attachment_id=record.id,
    )
    await db.refresh(record)
    assert record.state == "delete_pending"
    deletion = (
        await db.execute(
            select(PrivateObjectDeletion).where(
                PrivateObjectDeletion.private_object_id == record.id
            )
        )
    ).scalar_one()
    assert deletion.reason == "resource_deleted"

    counts = await process_private_object_deletions_once(
        SessionLocal,
        worker_id="ai-attachment-delete",
        mode="off",
        store=store,
    )
    assert counts["deleted"] == 1
    assert await store.stat(record.object_key) is None
