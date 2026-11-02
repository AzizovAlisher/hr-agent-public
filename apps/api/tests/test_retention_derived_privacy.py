"""Candidate erasure removes denormalized chat, AI, workflow, and source copies."""

from __future__ import annotations

import hashlib
import io
import struct
import zipfile
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4
from xml.etree import ElementTree

import pytest
from docx import Document
from fastapi import HTTPException, UploadFile
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
    TextStringObject,
)
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from starlette.datastructures import Headers

from app.config import get_settings
from app.models.ai_hiring import (
    AiActionBundle,
    AiAgentRun,
    AiMessage,
    AiOrganizationSettings,
    AiSession,
    HiringMission,
    MissionPlanVersion,
    MissionStep,
    MissionStepAttempt,
)
from app.models.application import Application, ApplicationStageEvent
from app.models.candidate import Candidate
from app.models.candidate_activity import CandidateActivity
from app.models.candidate_note import CandidateTag
from app.models.candidate_saved_view import CandidateSavedView
from app.models.chat_session import ChatSession
from app.models.communication import (
    CommunicationBatch,
    CommunicationMessage,
    CommunicationTemplate,
    CommunicationThread,
)
from app.models.enterprise import AuditLog, EnterprisePolicy
from app.models.message import Message
from app.models.notification import Notification
from app.models.organization import Organization
from app.models.private_object import PrivateObject, PrivateObjectDeletion
from app.models.position import Position
from app.models.requisition import CandidateOffer, OfferApproval, OfferTemplate
from app.models.sourcing import SourceCandidateReceipt, SourceImportRun
from app.models.task_submission import TaskSubmission
from app.models.user import User
from app.models.workflow import WorkflowRule, WorkflowRun
from app.routers.enterprise import run_retention
from app.schemas.candidate_search import CandidateSearchFilters
from app.schemas.enterprise import RetentionRunRequest
from app.services.ai_hiring_attachments import (
    import_ai_attachments,
    resolve_attachment_references,
    save_ai_attachment,
)
from app.services.privacy import (
    build_candidate_privacy_context,
    contains_candidate_marker,
)
from app.storage import (
    DeletionSettlement,
    LocalBlobStore,
    ScanResult,
    enqueue_private_object_deletion,
    private_object_key,
    register_ready_private_object,
    settle_private_object_deletion,
)
from app.storage.intake import StoredUpload

pytestmark = pytest.mark.asyncio


def _upload(canary: str, *, filename: str = "candidate.txt") -> UploadFile:
    return UploadFile(
        filename=filename,
        file=io.BytesIO(f"{canary} senior Python candidate".encode()),
        headers=Headers({"content-type": "text/plain"}),
    )


def _unsupported_xlsx_upload(case: str, canary: str) -> UploadFile:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["name", "cv_text"])
    if case == "formula":
        sheet.append(["Ada", f'="{canary}"'])
    elif case == "hidden-sheet":
        sheet.append(["Ada", "Unrelated active sheet"])
        private_sheet = workbook.create_sheet("Candidate private data")
        private_sheet.append([canary])
        private_sheet.sheet_state = "hidden"
    else:
        sheet.append(["Ada", "Unrelated active sheet"])
        if case == "comment":
            sheet["B2"].comment = Comment(canary, "Candidate")
        elif case == "hyperlink":
            sheet["B2"].hyperlink = f"mailto:{canary}"
        elif case == "defined-name":
            workbook.defined_names.add(
                DefinedName("CandidateMarker", attr_text="Sheet!$B$2")
            )
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    if case == "custom-property":
        source = io.BytesIO(output.getvalue())
        output = io.BytesIO()
        with (
            zipfile.ZipFile(source) as incoming,
            zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as outgoing,
        ):
            for info in incoming.infolist():
                outgoing.writestr(info.filename, incoming.read(info))
            outgoing.writestr(
                "docProps/custom.xml",
                f"<properties><marker>{canary}</marker></properties>",
            )
    content = output.getvalue()
    if case.startswith("zip-"):
        content = _zip_with_lexical_surface(
            content,
            case.removeprefix("zip-"),
            canary,
        )
    return UploadFile(
        filename=f"legacy-{case}.xlsx",
        file=io.BytesIO(content),
        headers=Headers(
            {
                "content-type": (
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                )
            }
        ),
    )


def _sanitized_docx(document: Document) -> bytes:
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
            content = incoming.read(info)
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


def _ooxml_with_lexical_surface(
    kind: str,
    case: str,
    marker: str,
) -> bytes:
    if kind == "docx":
        document = Document()
        document.add_paragraph("Generic candidate body")
        content = _sanitized_docx(document)
        part_name = "word/document.xml"
        root_token = b"<w:document"
    elif kind == "xlsx":
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["name", "cv_text"])
        sheet.append(["Ada", "Generic candidate body"])
        output = io.BytesIO()
        workbook.save(output)
        workbook.close()
        content = output.getvalue()
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


def _legacy_docx_bytes(case: str, canary: str) -> bytes:
    document = Document()
    document.add_paragraph("Generic candidate body")
    if case.startswith("zip-"):
        return _zip_with_lexical_surface(
            _sanitized_docx(document),
            case.removeprefix("zip-"),
            canary,
        )
    if case == "image":
        image = io.BytesIO()
        Image.new("RGB", (20, 20), color="white").save(image, format="PNG")
        image.seek(0)
        document.add_picture(image)
        output = io.BytesIO()
        document.save(output)
        return output.getvalue()
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
                {f"{{{word_namespace}}}val": f"https://example.test/{canary}"},
            )
            ElementTree.SubElement(
                merge,
                f"{{{word_namespace}}}connectString",
                {
                    f"{{{word_namespace}}}val": (
                        f"Provider=OLEDB;Data Source={canary}"
                    )
                },
            )
            return ElementTree.tostring(
                root,
                encoding="utf-8",
                xml_declaration=True,
            )

        return _rewrite_zip_part(
            _sanitized_docx(document),
            "word/settings.xml",
            add_mail_merge,
        )
    if case not in {"vml-textbox", "field", "content-control"}:
        raise AssertionError(f"Unknown legacy DOCX case: {case}")

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
                    f"{{{word_namespace}}}instr": f'HYPERLINK "mailto:{canary}"'
                },
            )
            run = ElementTree.SubElement(field, f"{{{word_namespace}}}r")
            text_node = ElementTree.SubElement(run, f"{{{word_namespace}}}t")
            text_node.text = canary
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
                    f"{{{word_namespace}}}value": canary,
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
            text_node = ElementTree.SubElement(
                nested_run,
                f"{{{word_namespace}}}t",
            )
            text_node.text = canary
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
        text_node = ElementTree.SubElement(
            nested_run,
            f"{{{word_namespace}}}t",
        )
        text_node.text = canary
        return ElementTree.tostring(
            root,
            encoding="utf-8",
            xml_declaration=True,
        )

    return _rewrite_zip_part(
        _sanitized_docx(document),
        "word/document.xml",
        add_vml_textbox,
    )


def _base_pdf_writer() -> PdfWriter:
    source = FPDF()
    source.add_page()
    source.set_font("Helvetica", size=12)
    source.text(10, 20, "Generic candidate body")
    reader = PdfReader(io.BytesIO(bytes(source.output())))
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    return writer


def _pdf_bytes(writer: PdfWriter) -> bytes:
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _legacy_pdf_bytes(case: str, canary: str) -> bytes:
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
    writer = _base_pdf_writer()
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
                    NameObject("/Contents"): TextStringObject(canary),
                }
            ),
        )
    elif case == "acroform":
        field = DictionaryObject(
            {
                NameObject("/FT"): NameObject("/Tx"),
                NameObject("/T"): TextStringObject("Candidate email"),
                NameObject("/V"): TextStringObject(canary),
            }
        )
        writer._root_object[NameObject("/AcroForm")] = DictionaryObject(
            {
                NameObject("/Fields"): ArrayObject(
                    [writer._add_object(field)]
                )
            }
        )
    else:
        raise AssertionError(f"Unknown legacy PDF case: {case}")
    return _pdf_bytes(writer)


def _privacy_metadata_upload(kind: str, canary: str) -> UploadFile:
    if kind == "xlsx-core":
        workbook = Workbook()
        workbook.properties.title = canary
        sheet = workbook.active
        sheet.append(["name", "cv_text"])
        sheet.append(["Ada", "Generic candidate body"])
        output = io.BytesIO()
        workbook.save(output)
        workbook.close()
        filename = "metadata.xlsx"
        media_type = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        content = output.getvalue()
    elif kind == "docx-core":
        document = Document()
        document.add_paragraph("Generic candidate body")
        document.core_properties.title = canary
        filename = "metadata.docx"
        media_type = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml."
            "document"
        )
        content = _sanitized_docx(document)
    elif kind.startswith("docx-lexical-"):
        lexical_case = kind.removeprefix("docx-lexical-")
        filename = f"lexical-{lexical_case}.docx"
        media_type = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml."
            "document"
        )
        content = _ooxml_with_lexical_surface("docx", lexical_case, canary)
    elif kind.startswith("xlsx-lexical-"):
        lexical_case = kind.removeprefix("xlsx-lexical-")
        filename = f"lexical-{lexical_case}.xlsx"
        media_type = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        content = _ooxml_with_lexical_surface("xlsx", lexical_case, canary)
    elif kind == "pdf-metadata":
        writer = _base_pdf_writer()
        writer.add_metadata({"/Title": canary})
        writer.xmp_metadata = (
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            f'<rdf:Description title="{canary}" />'
            "</rdf:RDF></x:xmpmeta>"
        ).encode()
        filename = "metadata.pdf"
        media_type = "application/pdf"
        content = _pdf_bytes(writer)
    else:
        raise AssertionError(f"Unknown metadata case: {kind}")
    return UploadFile(
        filename=filename,
        file=io.BytesIO(content),
        headers=Headers({"content-type": media_type}),
    )


def _legacy_attachment_bytes(
    case: str,
    canary: str,
) -> tuple[str, str, bytes]:
    if case.startswith("xlsx-"):
        upload = _unsupported_xlsx_upload(case.removeprefix("xlsx-"), canary)
        return (
            upload.filename or "legacy.xlsx",
            (
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            upload.file.read(),
        )
    if case.startswith("docx-"):
        docx_case = case.removeprefix("docx-")
        return (
            f"legacy-{docx_case}.docx",
            (
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            _legacy_docx_bytes(docx_case, canary),
        )
    if case.startswith("pdf-"):
        pdf_case = case.removeprefix("pdf-")
        return (
            f"legacy-{pdf_case}.pdf",
            "application/pdf",
            _legacy_pdf_bytes(pdf_case, canary),
        )
    raise AssertionError(f"Unknown legacy attachment case: {case}")


async def test_retention_erases_denormalized_candidate_canaries(
    db,
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "objects"
    monkeypatch.setattr(get_settings(), "storage_dir", str(root))
    organization = Organization(
        name="Derived privacy", slug=f"derived-privacy-{uuid4().hex[:8]}"
    )
    db.add(organization)
    await db.flush()
    admin = User(
        organization_id=organization.id,
        email=f"derived-privacy-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    db.add(admin)
    db.add(
        EnterprisePolicy(
            organization_id=organization.id,
            candidate_retention_days=365,
            deleted_retention_days=30,
            audit_retention_days=90,
        )
    )
    db.add(
        AiOrganizationSettings(
            organization_id=organization.id,
            feature_enabled=True,
        )
    )
    ai_session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        status="active",
    )
    db.add(ai_session)
    await db.commit()

    canary = f"PRIVACY-CANARY-{uuid4()}"
    unique_email = f"ann-{uuid4().hex}@candidate.example"
    neighboring_email = f"jo{unique_email}"
    unique_phone = "+992900001234"
    opaque_canary = f"CvToken{uuid4().hex}Z9Q7"
    common_role = "Senior Python Engineer"
    common_name = "Alex Johnson"
    third_party_email = f"referee-{uuid4().hex[:10]}@example.test"
    shared_requisition_id = uuid4()
    exact_document = (
        'Candidate narrative with a "quoted passage" and a newline.\n'
        "This deliberately contains ordinary prose rather than an opaque "
        "identifier, so the privacy inventory must match the full original "
        "document across JSON escaping boundaries. "
        "The remainder provides enough context to exceed the conservative "
        "whole-document threshold without promoting common fragments. "
        "Final paragraph confirms the exact candidate-authored narrative."
    )
    assert len(exact_document) >= 256
    store = LocalBlobStore(root)
    upload = await save_ai_attachment(
        db,
        admin,
        ai_session.id,
        upload=_upload(canary, filename=f"{canary}.txt"),
        store=store,
    )
    resolved = await resolve_attachment_references(
        db,
        user=admin,
        session_id=ai_session.id,
        references=[{"id": upload["id"], "upload_token": upload["upload_token"]}],
    )
    imported = await import_ai_attachments(db, admin, attachments=resolved, store=store)
    candidate_id = UUID(imported["imported_candidate_ids"][0])
    candidate = await db.get(Candidate, candidate_id)
    assert candidate is not None
    candidate.source_ref = common_role
    candidate.github_login = "alex"
    candidate.email_normalized = unique_email
    candidate.phone_normalized = unique_phone
    candidate.raw_cv = exact_document
    candidate.parsed_cv = {
        "identity_canary": canary,
        "opaque_canary": opaque_canary,
        "email": unique_email,
        "referee_email": third_party_email,
        "shared_requisition_id": str(shared_requisition_id),
        "candidate_name": common_name,
        "job_title": common_role,
        "github_login": "alex",
    }
    unrelated_candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"unrelated-candidate-{uuid4()}",
        raw_cv="Unrelated candidate profile",
    )
    db.add(unrelated_candidate)
    await db.flush()

    other_organization = Organization(
        name="Other tenant privacy",
        slug=f"other-tenant-privacy-{uuid4().hex[:8]}",
    )
    db.add(other_organization)
    await db.flush()
    other_admin = User(
        organization_id=other_organization.id,
        email=f"other-tenant-privacy-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    db.add(other_admin)
    other_candidate = Candidate(
        organization_id=other_organization.id,
        source="manual",
        source_ref=f"other-candidate-{uuid4()}",
        raw_cv="Other tenant candidate profile",
    )
    db.add(other_candidate)
    db.add(
        AiOrganizationSettings(
            organization_id=other_organization.id,
            feature_enabled=True,
        )
    )
    await db.flush()

    ai_title_session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        title=canary,
        status="active",
    )
    mission_title_session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        title="Mission title root",
        status="active",
    )
    mission_request_session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        title="Mission request root",
        status="active",
    )
    mission_brief_session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        title="Mission brief root",
        status="active",
    )
    mission_rubric_session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        title="Mission rubric root",
        status="active",
    )
    mission_assumptions_session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        title="Mission assumptions root",
        status="active",
    )
    mission_blockers_session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        title="Mission blockers root",
        status="active",
    )
    unrelated_ai_session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        title="Unrelated workforce planning",
        status="active",
    )
    common_role_ai_session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        title=common_role,
        status="active",
    )
    common_name_ai_session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        title=common_name,
        status="active",
    )
    email_ai_session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        title=unique_email,
        status="active",
    )
    opaque_ai_session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        title=opaque_canary,
        status="active",
    )
    generic_attachment_session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        title="Generic attachment candidate inventory",
        status="active",
    )
    unrelated_attachment_session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        title="Unrelated generic attachment",
        status="active",
    )
    pending_attachment_session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        title="Pending attachment session",
        status="active",
    )
    large_plain_attachment_session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        title="Accepted large plain attachment",
        status="active",
    )
    matching_bundle_session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        title="Bundle idempotency root",
        status="active",
    )
    discovery_step_session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        title="Discovery step result",
        status="active",
    )
    discovery_attempt_session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        title="Discovery attempt result",
        status="active",
    )
    discovery_run_session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        title="Discovery agent result",
        status="active",
    )
    other_tenant_ai_session = AiSession(
        organization_id=other_organization.id,
        created_by_user_id=other_admin.id,
        title=canary,
        status="active",
    )
    db.add_all(
        [
            ai_title_session,
            mission_title_session,
            mission_request_session,
            mission_brief_session,
            mission_rubric_session,
            mission_assumptions_session,
            mission_blockers_session,
            unrelated_ai_session,
            common_role_ai_session,
            common_name_ai_session,
            email_ai_session,
            opaque_ai_session,
            generic_attachment_session,
            unrelated_attachment_session,
            pending_attachment_session,
            large_plain_attachment_session,
            matching_bundle_session,
            discovery_step_session,
            discovery_attempt_session,
            discovery_run_session,
            other_tenant_ai_session,
        ]
    )
    bulk_unrelated_sessions = [
        AiSession(
            organization_id=organization.id,
            created_by_user_id=admin.id,
            title=f"Unrelated attachment inventory {index}",
            status="active",
        )
        for index in range(21)
    ]
    db.add_all(bulk_unrelated_sessions)
    await db.flush()

    def mission(
        session: AiSession,
        *,
        creator: User,
        **overrides,
    ) -> HiringMission:
        values = {
            "title": "General hiring mission",
            "original_request": "Prepare a general workforce plan.",
            "brief": {"goal": "Workforce planning"},
            "rubric": {"criteria": ["Role alignment"]},
            "assumptions": ["Hiring approval is available"],
            "blockers": ["None"],
        }
        values.update(overrides)
        return HiringMission(
            organization_id=session.organization_id,
            session_id=session.id,
            created_by_user_id=creator.id,
            status="draft",
            **values,
        )

    mission_roots = [
        mission(mission_title_session, creator=admin, title=canary),
        mission(
            mission_request_session,
            creator=admin,
            original_request=canary,
        ),
        mission(mission_brief_session, creator=admin, brief={"candidate": canary}),
        mission(
            mission_rubric_session,
            creator=admin,
            rubric={"candidate_evidence": canary},
        ),
        mission(
            mission_assumptions_session,
            creator=admin,
            assumptions=[canary],
        ),
        mission(mission_blockers_session, creator=admin, blockers=[canary]),
    ]
    unrelated_mission = mission(unrelated_ai_session, creator=admin)
    matching_bundle_mission = mission(matching_bundle_session, creator=admin)
    other_tenant_mission = mission(
        other_tenant_ai_session,
        creator=other_admin,
        original_request=canary,
        brief={"candidate": canary},
    )
    db.add_all(
        [
            *mission_roots,
            unrelated_mission,
            matching_bundle_mission,
            other_tenant_mission,
        ]
    )
    await db.flush()

    bundle_plans = [
        MissionPlanVersion(
            organization_id=mission_item.organization_id,
            mission_id=mission_item.id,
            version=1,
            status="active",
            brief_snapshot={},
            rubric_snapshot={},
            plan={},
            input_hash=hash_character * 64,
        )
        for mission_item, hash_character in (
            (matching_bundle_mission, "f"),
            (unrelated_mission, "a"),
            (other_tenant_mission, "b"),
        )
    ]
    db.add_all(bundle_plans)
    await db.flush()

    def action_bundle(
        mission_item: HiringMission,
        plan: MissionPlanVersion,
        *,
        approval_key: str,
    ) -> AiActionBundle:
        return AiActionBundle(
            organization_id=mission_item.organization_id,
            mission_id=mission_item.id,
            plan_version_id=plan.id,
            plan_version=1,
            action_type="draft_message",
            summary="Prepare a general recruiting message",
            preview={},
            action_payload={},
            required_permission="communications.write",
            status="pending",
            approval_idempotency_key=approval_key,
            execution_result={},
        )

    matching_bundle = action_bundle(
        matching_bundle_mission,
        bundle_plans[0],
        approval_key=f"candidate-bundle:{unique_email}",
    )
    unrelated_bundle = action_bundle(
        unrelated_mission,
        bundle_plans[1],
        approval_key=f"common-role:{common_role}",
    )
    other_tenant_bundle = action_bundle(
        other_tenant_mission,
        bundle_plans[2],
        approval_key=f"candidate-bundle:{unique_email}",
    )
    db.add_all([matching_bundle, unrelated_bundle, other_tenant_bundle])
    await db.flush()

    discovery_missions = [
        mission(discovery_step_session, creator=admin),
        mission(discovery_attempt_session, creator=admin),
        mission(discovery_run_session, creator=admin),
    ]
    db.add_all(discovery_missions)
    await db.flush()
    discovery_plans = [
        MissionPlanVersion(
            organization_id=organization.id,
            mission_id=item.id,
            version=1,
            status="active",
            brief_snapshot={},
            rubric_snapshot={},
            plan={},
            input_hash=f"{index:x}" * 64,
        )
        for index, item in enumerate(discovery_missions, start=1)
    ]
    db.add_all(discovery_plans)
    await db.flush()
    discovery_steps = [
        MissionStep(
            organization_id=organization.id,
            mission_id=mission_item.id,
            plan_version_id=plan.id,
            key="discover_candidates",
            step_type="candidate_discovery",
            ordinal=0,
            dependencies=[],
            status="completed",
            input_data={},
            safe_result=(
                {"candidate_id": str(candidate_id)} if index == 0 else {}
            ),
            idempotency_key=f"privacy-discovery:{uuid4()}",
            max_attempts=3,
            available_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
        for index, (mission_item, plan) in enumerate(
            zip(discovery_missions, discovery_plans, strict=True)
        )
    ]
    db.add_all(discovery_steps)
    await db.flush()
    discovery_attempt = MissionStepAttempt(
        organization_id=organization.id,
        step_id=discovery_steps[1].id,
        attempt_number=1,
        status="completed",
        worker_id="privacy-discovery-worker",
        input_hash="a" * 64,
        safe_result={"candidate_email": unique_email},
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    discovery_run = AiAgentRun(
        organization_id=organization.id,
        session_id=discovery_run_session.id,
        mission_id=discovery_missions[2].id,
        step_id=discovery_steps[2].id,
        initiated_by_user_id=admin.id,
        agent_type="candidate_discovery",
        status="completed",
        input_hash="b" * 64,
        safe_input_metadata={},
        safe_result={"candidate_ref": opaque_canary},
        validation_outcome="valid",
        completed_at=datetime.now(UTC),
    )
    db.add_all([discovery_attempt, discovery_run])
    await db.flush()

    root_session_attachment = await save_ai_attachment(
        db,
        admin,
        ai_title_session.id,
        upload=_upload(
            f"{canary} root-only AI session",
            filename="root-session.txt",
        ),
        store=store,
    )
    generic_candidate_attachment = await save_ai_attachment(
        db,
        admin,
        generic_attachment_session.id,
        upload=_upload(
            f"Candidate identity: {unique_email}",
            filename="notes.txt",
        ),
        store=store,
    )
    unrelated_attachment = await save_ai_attachment(
        db,
        admin,
        unrelated_attachment_session.id,
        upload=_upload(
            f"General role discussion for {common_name}, {common_role}",
            filename="general-notes.txt",
        ),
        store=store,
    )
    other_tenant_attachment = await save_ai_attachment(
        db,
        other_admin,
        other_tenant_ai_session.id,
        upload=_upload(
            f"Candidate identity: {unique_email}",
            filename="notes.txt",
        ),
        store=store,
    )
    pending_candidate_attachment = await save_ai_attachment(
        db,
        admin,
        pending_attachment_session.id,
        upload=_upload(
            "Pending attachment with generic body",
            filename=f"{unique_email}.txt",
        ),
        store=store,
    )
    pending_deletion = await enqueue_private_object_deletion(
        db,
        organization_id=organization.id,
        private_object_id=UUID(pending_candidate_attachment["id"]),
        reason="retention_expired",
    )
    assert pending_deletion is not None
    bulk_unrelated_attachments = []
    for index, session in enumerate(bulk_unrelated_sessions):
        bulk_unrelated_attachments.append(
            await save_ai_attachment(
                db,
                admin,
                session.id,
                upload=_upload(
                    f"General workforce capacity note {index} for {common_role}",
                    filename=f"capacity-{index}.txt",
                ),
                store=store,
            )
        )
    large_plain_attachment = await save_ai_attachment(
        db,
        admin,
        large_plain_attachment_session.id,
        upload=_upload(
            "x" * (2 * 1024 * 1024 + 1),
            filename="accepted-large-notes.txt",
        ),
        store=store,
    )
    await db.commit()

    chat = ChatSession(
        organization_id=organization.id,
        user_id=admin.id,
        scope="candidate",
        candidate_id=candidate_id,
        title=canary,
    )
    matching_free_chat = ChatSession(
        organization_id=organization.id,
        user_id=admin.id,
        scope="free",
        title="General recruiting discussion",
    )
    unrelated_free_chat = ChatSession(
        organization_id=organization.id,
        user_id=admin.id,
        scope="free",
        title="Unrelated product discussion",
    )
    common_role_chat = ChatSession(
        organization_id=organization.id,
        user_id=admin.id,
        scope="free",
        title="Common role discussion",
    )
    common_name_chat = ChatSession(
        organization_id=organization.id,
        user_id=admin.id,
        scope="free",
        title="Common name discussion",
    )
    github_login_chat = ChatSession(
        organization_id=organization.id,
        user_id=admin.id,
        scope="free",
        title="Shared GitHub login discussion",
    )
    third_party_email_chat = ChatSession(
        organization_id=organization.id,
        user_id=admin.id,
        scope="free",
        title="Shared referee discussion",
    )
    shared_requisition_chat = ChatSession(
        organization_id=organization.id,
        user_id=admin.id,
        scope="free",
        title="Shared requisition discussion",
    )
    neighboring_email_chat = ChatSession(
        organization_id=organization.id,
        user_id=admin.id,
        scope="free",
        title="Distinct neighboring email",
    )
    exact_document_chat = ChatSession(
        organization_id=organization.id,
        user_id=admin.id,
        scope="free",
        title="Exact multiline candidate document",
    )
    email_matching_chat = ChatSession(
        organization_id=organization.id,
        user_id=admin.id,
        scope="free",
        title="Strong email identifier",
    )
    opaque_matching_chat = ChatSession(
        organization_id=organization.id,
        user_id=admin.id,
        scope="free",
        title="Strong opaque identifier",
    )
    other_tenant_matching_chat = ChatSession(
        organization_id=other_organization.id,
        user_id=other_admin.id,
        scope="free",
        title="Other tenant discussion",
    )
    db.add_all(
        [
            chat,
            matching_free_chat,
            unrelated_free_chat,
            common_role_chat,
            common_name_chat,
            github_login_chat,
            third_party_email_chat,
            shared_requisition_chat,
            neighboring_email_chat,
            exact_document_chat,
            email_matching_chat,
            opaque_matching_chat,
            other_tenant_matching_chat,
        ]
    )
    await db.flush()
    chat_message = Message(
        organization_id=organization.id,
        session_id=chat.id,
        role="assistant",
        content=f"Candidate evidence {canary}",
        purpose="chat",
    )
    matching_free_message = Message(
        organization_id=organization.id,
        session_id=matching_free_chat.id,
        role="user",
        content=f"Please review this exact CV identity marker: {canary}",
        purpose="chat",
    )
    unrelated_free_message = Message(
        organization_id=organization.id,
        session_id=unrelated_free_chat.id,
        role="user",
        content="Discuss dashboard layout and release milestones.",
        purpose="chat",
    )
    common_role_message = Message(
        organization_id=organization.id,
        session_id=common_role_chat.id,
        role="user",
        content=f"Discuss the {common_role} opening.",
        purpose="chat",
    )
    common_name_message = Message(
        organization_id=organization.id,
        session_id=common_name_chat.id,
        role="user",
        content=f"A different person is also named {common_name}.",
        purpose="chat",
    )
    github_login_message = Message(
        organization_id=organization.id,
        session_id=github_login_chat.id,
        role="user",
        content="A different engineer uses the GitHub login alex.",
        purpose="chat",
    )
    third_party_email_message = Message(
        organization_id=organization.id,
        session_id=third_party_email_chat.id,
        role="user",
        content=f"A different candidate also lists referee {third_party_email}.",
        purpose="chat",
    )
    shared_requisition_message = Message(
        organization_id=organization.id,
        session_id=shared_requisition_chat.id,
        role="user",
        content=f"Unrelated applicant for requisition {shared_requisition_id}.",
        purpose="chat",
    )
    neighboring_email_message = Message(
        organization_id=organization.id,
        session_id=neighboring_email_chat.id,
        role="user",
        content=f"Distinct canonical contact {neighboring_email}.",
        purpose="chat",
    )
    exact_document_message = Message(
        organization_id=organization.id,
        session_id=exact_document_chat.id,
        role="user",
        content=exact_document,
        purpose="chat",
    )
    email_matching_message = Message(
        organization_id=organization.id,
        session_id=email_matching_chat.id,
        role="user",
        content=f"Candidate contact is {unique_email}.",
        purpose="chat",
    )
    opaque_matching_message = Message(
        organization_id=organization.id,
        session_id=opaque_matching_chat.id,
        role="user",
        content=f"Candidate import token is {opaque_canary}.",
        purpose="chat",
    )
    other_tenant_matching_message = Message(
        organization_id=other_organization.id,
        session_id=other_tenant_matching_chat.id,
        role="user",
        content=f"A separate tenant retained this text: {canary}",
        purpose="chat",
    )
    matching_name_view = CandidateSavedView(
        organization_id=organization.id,
        user_id=admin.id,
        name=canary,
        filters=CandidateSearchFilters().model_dump(mode="json"),
    )
    matching_filter_view = CandidateSavedView(
        organization_id=organization.id,
        user_id=admin.id,
        name="Candidate identity search",
        filters=CandidateSearchFilters(q=canary).model_dump(mode="json"),
    )
    unrelated_view = CandidateSavedView(
        organization_id=organization.id,
        user_id=admin.id,
        name="Release operations",
        filters=CandidateSearchFilters(q="dashboard rollout").model_dump(mode="json"),
    )
    common_role_view = CandidateSavedView(
        organization_id=organization.id,
        user_id=admin.id,
        name="Common role filter",
        filters=CandidateSearchFilters(q=common_role).model_dump(mode="json"),
    )
    common_name_view = CandidateSavedView(
        organization_id=organization.id,
        user_id=admin.id,
        name=common_name,
        filters=CandidateSearchFilters(q="different candidate").model_dump(mode="json"),
    )
    other_tenant_matching_view = CandidateSavedView(
        organization_id=other_organization.id,
        user_id=other_admin.id,
        name="Other tenant candidate search",
        filters=CandidateSearchFilters(q=canary).model_dump(mode="json"),
    )
    matching_workflow_rules = [
        WorkflowRule(
            organization_id=organization.id,
            created_by_user_id=admin.id,
            name=f"Rule {unique_email}",
            trigger="candidate_added",
            conditions={},
            actions=[],
            is_active=True,
        ),
        WorkflowRule(
            organization_id=organization.id,
            created_by_user_id=admin.id,
            name="Candidate condition rule",
            trigger="candidate_added",
            conditions={"candidate_email": unique_email},
            actions=[],
            is_active=True,
        ),
        WorkflowRule(
            organization_id=organization.id,
            created_by_user_id=admin.id,
            name="Candidate prompt rule",
            trigger="candidate_added",
            conditions={},
            actions=[{"type": "ai_prompt", "prompt": unique_email}],
            is_active=True,
        ),
    ]
    unrelated_workflow_rule = WorkflowRule(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        name=common_name,
        trigger="candidate_added",
        conditions={"role": common_role},
        actions=[{"type": "ai_prompt", "prompt": common_role}],
        is_active=True,
    )
    other_tenant_workflow_rule = WorkflowRule(
        organization_id=other_organization.id,
        created_by_user_id=other_admin.id,
        name=f"Rule {unique_email}",
        trigger="candidate_added",
        conditions={"candidate_email": unique_email},
        actions=[{"type": "ai_prompt", "prompt": unique_email}],
        is_active=True,
    )

    matching_communication_templates = [
        CommunicationTemplate(
            organization_id=organization.id,
            created_by_user_id=admin.id,
            name=f"Template {unique_email}",
            channel="email",
            language="en",
            subject_template="General subject",
            body_template="General body",
            is_active=True,
        ),
        CommunicationTemplate(
            organization_id=organization.id,
            created_by_user_id=admin.id,
            name="Candidate subject template",
            channel="email",
            language="en",
            subject_template=unique_email,
            body_template="General body",
            is_active=True,
        ),
        CommunicationTemplate(
            organization_id=organization.id,
            created_by_user_id=admin.id,
            name="Candidate body template",
            channel="email",
            language="en",
            subject_template="General subject",
            body_template=unique_email,
            is_active=True,
        ),
    ]
    unrelated_communication_template = CommunicationTemplate(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        name=f"General {common_name}",
        channel="email",
        language="en",
        subject_template=common_role,
        body_template=f"A general message for {common_name}",
        is_active=True,
    )
    other_tenant_communication_template = CommunicationTemplate(
        organization_id=other_organization.id,
        created_by_user_id=other_admin.id,
        name=f"Template {unique_email}",
        channel="email",
        language="en",
        subject_template=unique_email,
        body_template=unique_email,
        is_active=True,
    )

    matching_offer_templates = [
        OfferTemplate(
            organization_id=organization.id,
            created_by_user_id=admin.id,
            name=f"Offer {unique_email}",
            subject_template="General offer",
            body_template="General offer body",
            is_active=True,
        ),
        OfferTemplate(
            organization_id=organization.id,
            created_by_user_id=admin.id,
            name="Candidate offer subject",
            subject_template=unique_email,
            body_template="General offer body",
            is_active=True,
        ),
        OfferTemplate(
            organization_id=organization.id,
            created_by_user_id=admin.id,
            name="Candidate offer body",
            subject_template="General offer",
            body_template=unique_email,
            is_active=True,
        ),
    ]
    unrelated_offer_template = OfferTemplate(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        name=f"General offer {common_name}",
        subject_template=common_role,
        body_template=f"A general offer for {common_name}",
        is_active=True,
    )
    other_tenant_offer_template = OfferTemplate(
        organization_id=other_organization.id,
        created_by_user_id=other_admin.id,
        name=f"Offer {unique_email}",
        subject_template=unique_email,
        body_template=unique_email,
        is_active=True,
    )
    db.add_all(
        [
            *matching_workflow_rules,
            unrelated_workflow_rule,
            other_tenant_workflow_rule,
            *matching_communication_templates,
            unrelated_communication_template,
            other_tenant_communication_template,
            *matching_offer_templates,
            unrelated_offer_template,
            other_tenant_offer_template,
        ]
    )
    await db.flush()

    unrelated_position = Position(
        organization_id=organization.id,
        title="Unrelated retained position",
        status="open",
        created_by=admin.id,
    )
    other_tenant_position = Position(
        organization_id=other_organization.id,
        title="Other tenant retained position",
        status="open",
        created_by=other_admin.id,
    )
    db.add_all([unrelated_position, other_tenant_position])
    await db.flush()
    unrelated_application = Application(
        organization_id=organization.id,
        candidate_id=unrelated_candidate.id,
        position_id=unrelated_position.id,
        stage="screening",
        source="manual",
        created_by_user_id=admin.id,
    )
    other_tenant_application = Application(
        organization_id=other_organization.id,
        candidate_id=other_candidate.id,
        position_id=other_tenant_position.id,
        stage="screening",
        source="manual",
        created_by_user_id=other_admin.id,
    )
    db.add_all([unrelated_application, other_tenant_application])
    await db.flush()

    matching_stage_event = ApplicationStageEvent(
        organization_id=organization.id,
        application_id=unrelated_application.id,
        from_stage="new",
        to_stage="screening",
        actor_kind="automation",
        reason=f"Rule materialization for {unique_email}",
        details={"candidate_email": unique_email},
    )
    unrelated_stage_event = ApplicationStageEvent(
        organization_id=organization.id,
        application_id=unrelated_application.id,
        from_stage="new",
        to_stage="screening",
        actor_kind="automation",
        reason=common_role,
        details={"candidate_name": common_name},
    )
    other_tenant_stage_event = ApplicationStageEvent(
        organization_id=other_organization.id,
        application_id=other_tenant_application.id,
        from_stage="new",
        to_stage="screening",
        actor_kind="automation",
        reason=f"Rule materialization for {unique_email}",
        details={"candidate_email": unique_email},
    )
    matching_tag = CandidateTag(
        organization_id=organization.id,
        candidate_id=unrelated_candidate.id,
        label=unique_phone,
        created_by=admin.id,
    )
    unrelated_tag = CandidateTag(
        organization_id=organization.id,
        candidate_id=unrelated_candidate.id,
        label=common_role,
        created_by=admin.id,
    )
    other_tenant_tag = CandidateTag(
        organization_id=other_organization.id,
        candidate_id=other_candidate.id,
        label=unique_phone,
        created_by=other_admin.id,
    )
    matching_activity = CandidateActivity(
        organization_id=organization.id,
        candidate_id=unrelated_candidate.id,
        actor_kind="system",
        event_type="workflow_tag_added",
        subject_type="workflow_rule",
        payload={"rendered_rule_value": unique_email},
    )
    unrelated_activity = CandidateActivity(
        organization_id=organization.id,
        candidate_id=unrelated_candidate.id,
        actor_kind="system",
        event_type="workflow_tag_added",
        subject_type="workflow_rule",
        payload={"role": common_role},
    )
    other_tenant_activity = CandidateActivity(
        organization_id=other_organization.id,
        candidate_id=other_candidate.id,
        actor_kind="system",
        event_type="workflow_tag_added",
        subject_type="workflow_rule",
        payload={"rendered_rule_value": unique_email},
    )
    matching_task = TaskSubmission(
        organization_id=organization.id,
        candidate_id=unrelated_candidate.id,
        created_by=admin.id,
        title=f"Task for {unique_email}",
        prompt=f"Complete this task for {unique_email}",
        submission_type="text",
        status="sent",
    )
    unrelated_task = TaskSubmission(
        organization_id=organization.id,
        candidate_id=unrelated_candidate.id,
        created_by=admin.id,
        title=common_role,
        prompt=f"General task for {common_name}",
        submission_type="text",
        status="sent",
    )
    other_tenant_task = TaskSubmission(
        organization_id=other_organization.id,
        candidate_id=other_candidate.id,
        created_by=other_admin.id,
        title=f"Task for {unique_email}",
        prompt=f"Complete this task for {unique_email}",
        submission_type="text",
        status="sent",
    )
    unrelated_thread = CommunicationThread(
        organization_id=organization.id,
        candidate_id=unrelated_candidate.id,
        channel="email",
        status="open",
    )
    other_tenant_thread = CommunicationThread(
        organization_id=other_organization.id,
        candidate_id=other_candidate.id,
        channel="email",
        status="open",
    )
    db.add_all(
        [
            matching_stage_event,
            unrelated_stage_event,
            other_tenant_stage_event,
            matching_tag,
            unrelated_tag,
            other_tenant_tag,
            matching_activity,
            unrelated_activity,
            other_tenant_activity,
            matching_task,
            unrelated_task,
            other_tenant_task,
            unrelated_thread,
            other_tenant_thread,
        ]
    )
    await db.flush()

    matching_communication_message = CommunicationMessage(
        organization_id=organization.id,
        thread_id=unrelated_thread.id,
        candidate_id=unrelated_candidate.id,
        direction="outbound",
        channel="email",
        status="sent",
        subject=f"Rendered subject {unique_email}",
        body=f"Rendered body {unique_email}",
        idempotency_key=f"message:{unique_email}",
        provider_message_id=unique_email,
        webhook_event_id=f"event:{unique_email}",
        webhook_payload_sha256="1" * 64,
    )
    unrelated_communication_message = CommunicationMessage(
        organization_id=organization.id,
        thread_id=unrelated_thread.id,
        candidate_id=unrelated_candidate.id,
        direction="outbound",
        channel="email",
        status="sent",
        subject=common_role,
        body=f"General message for {common_name}",
        idempotency_key=f"message:{common_role}",
        provider_message_id=common_name,
    )
    other_tenant_communication_message = CommunicationMessage(
        organization_id=other_organization.id,
        thread_id=other_tenant_thread.id,
        candidate_id=other_candidate.id,
        direction="outbound",
        channel="email",
        status="sent",
        subject=f"Rendered subject {unique_email}",
        body=f"Rendered body {unique_email}",
        idempotency_key=f"message:{unique_email}",
        provider_message_id=unique_email,
        webhook_event_id=f"event:{unique_email}",
        webhook_payload_sha256="1" * 64,
    )
    matching_candidate_offer = CandidateOffer(
        organization_id=organization.id,
        candidate_id=unrelated_candidate.id,
        created_by_user_id=admin.id,
        position_title_snapshot="Retained position",
        template_name_snapshot=f"Offer {unique_email}",
        subject_template_snapshot=f"Template {unique_email}",
        body_template_snapshot=f"Template body {unique_email}",
        subject=f"Rendered offer {unique_email}",
        body=f"Rendered offer body {unique_email}",
        salary_currency="TJS",
        expires_at=datetime.now(UTC) + timedelta(days=10),
        required_approvals=1,
        approval_cycle=1,
        status="accepted",
        responded_at=datetime.now(UTC),
        signature_name=unique_email,
    )
    unrelated_candidate_offer = CandidateOffer(
        organization_id=organization.id,
        candidate_id=unrelated_candidate.id,
        created_by_user_id=admin.id,
        position_title_snapshot="Retained position",
        template_name_snapshot=common_name,
        subject_template_snapshot=common_role,
        body_template_snapshot=f"General offer for {common_name}",
        subject=common_role,
        body=f"General offer for {common_name}",
        salary_currency="TJS",
        expires_at=datetime.now(UTC) + timedelta(days=10),
        required_approvals=1,
        status="draft",
    )
    other_tenant_candidate_offer = CandidateOffer(
        organization_id=other_organization.id,
        candidate_id=other_candidate.id,
        created_by_user_id=other_admin.id,
        position_title_snapshot="Other tenant position",
        template_name_snapshot=f"Offer {unique_email}",
        subject_template_snapshot=f"Template {unique_email}",
        body_template_snapshot=f"Template body {unique_email}",
        subject=f"Rendered offer {unique_email}",
        body=f"Rendered offer body {unique_email}",
        salary_currency="TJS",
        expires_at=datetime.now(UTC) + timedelta(days=10),
        required_approvals=1,
        status="draft",
        signature_name=unique_email,
    )
    db.add_all(
        [
            matching_communication_message,
            unrelated_communication_message,
            other_tenant_communication_message,
            matching_candidate_offer,
            unrelated_candidate_offer,
            other_tenant_candidate_offer,
        ]
    )
    await db.flush()
    matching_offer_approval = OfferApproval(
        organization_id=organization.id,
        offer_id=matching_candidate_offer.id,
        user_id=admin.id,
        user_email_snapshot=admin.email,
        offer_version_snapshot=1,
        approval_cycle=1,
        offer_snapshot={
            "candidate_id": str(unrelated_candidate.id),
            "subject": f"Rendered approval {unique_email}",
            "body": f"Rendered approval body {unique_email}",
            "salary_currency": "TJS",
            "version": 1,
        },
        vote="approve",
        comment=f"Approved rendered copy for {unique_email}",
    )
    unrelated_offer_approval = OfferApproval(
        organization_id=organization.id,
        offer_id=unrelated_candidate_offer.id,
        user_id=admin.id,
        user_email_snapshot=admin.email,
        offer_version_snapshot=1,
        approval_cycle=1,
        offer_snapshot={
            "candidate_id": str(unrelated_candidate.id),
            "subject": common_role,
            "body": f"General offer for {common_name}",
            "salary_currency": "TJS",
            "version": 1,
        },
        vote="approve",
        comment=f"Approved general offer for {common_name}",
    )
    other_tenant_offer_approval = OfferApproval(
        organization_id=other_organization.id,
        offer_id=other_tenant_candidate_offer.id,
        user_id=other_admin.id,
        user_email_snapshot=other_admin.email,
        offer_version_snapshot=1,
        approval_cycle=1,
        offer_snapshot={
            "candidate_id": str(other_candidate.id),
            "subject": f"Rendered approval {unique_email}",
            "body": f"Rendered approval body {unique_email}",
            "salary_currency": "TJS",
            "version": 1,
        },
        vote="approve",
        comment=f"Approved rendered copy for {unique_email}",
    )
    db.add_all(
        [
            matching_offer_approval,
            unrelated_offer_approval,
            other_tenant_offer_approval,
        ]
    )
    await db.flush()

    workflow = WorkflowRun(
        organization_id=organization.id,
        # Historical workflow evidence keeps the originating rule identity
        # even when the live rule has already been removed.
        rule_id_snapshot=uuid4(),
        candidate_id=candidate_id,
        rule_name_snapshot="Privacy fixture",
        trigger_snapshot="candidate_added",
        dedupe_key=f"privacy:{candidate_id}",
        status="completed",
        action_results=[{"evidence": canary}],
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    matching_derived_workflow_run = WorkflowRun(
        organization_id=organization.id,
        rule_id=matching_workflow_rules[2].id,
        rule_id_snapshot=matching_workflow_rules[2].id,
        candidate_id=unrelated_candidate.id,
        rule_name_snapshot="Candidate prompt rule",
        trigger_snapshot="candidate_added",
        conditions_snapshot={},
        actions_snapshot=[{"type": "ai_prompt", "prompt": unique_email}],
        dedupe_key=f"derived-privacy:{uuid4()}",
        status="completed",
        action_results=[{"draft": f"Contact {unique_email}"}],
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    unrelated_workflow_run = WorkflowRun(
        organization_id=organization.id,
        rule_id=unrelated_workflow_rule.id,
        rule_id_snapshot=unrelated_workflow_rule.id,
        candidate_id=unrelated_candidate.id,
        rule_name_snapshot=common_name,
        trigger_snapshot="candidate_added",
        conditions_snapshot={"role": common_role},
        actions_snapshot=[{"type": "ai_prompt", "prompt": common_role}],
        dedupe_key=f"unrelated-privacy:{uuid4()}",
        status="completed",
        action_results=[{"role": common_role}],
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    other_tenant_workflow_run = WorkflowRun(
        organization_id=other_organization.id,
        rule_id=other_tenant_workflow_rule.id,
        rule_id_snapshot=other_tenant_workflow_rule.id,
        candidate_id=None,
        rule_name_snapshot=f"Rule {unique_email}",
        trigger_snapshot="candidate_added",
        conditions_snapshot={"candidate_email": unique_email},
        actions_snapshot=[{"type": "ai_prompt", "prompt": unique_email}],
        dedupe_key=f"other-tenant-privacy:{uuid4()}",
        status="completed",
        action_results=[{"draft": unique_email}],
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    receipt = SourceCandidateReceipt(
        organization_id=organization.id,
        candidate_id=candidate_id,
        external_id=f"external-{canary}",
        provider_snapshot="hh",
        source_url=f"https://example.test/{canary}",
        received_at=datetime.now(UTC),
    )
    notification = Notification(
        organization_id=organization.id,
        user_id=admin.id,
        type="candidate_applied",
        payload={"candidate_id": str(candidate_id), "applicant_name": canary},
    )
    batch = CommunicationBatch(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        channel="email",
        status="completed",
        idempotency_key=f"privacy-{uuid4()}",
        total_count=0,
        requested_count=1,
        skipped_count=1,
        skip_summary=[{"candidate_id": str(candidate_id), "status": "opted_out"}],
    )
    candidate_bearing_batch = CommunicationBatch(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        channel="email",
        status="completed",
        idempotency_key=f"candidate-batch:{canary}",
        total_count=2,
        requested_count=2,
        skipped_count=1,
        skip_summary=[{"reason": "candidate_private", "evidence": canary}],
        sent_count=1,
    )
    unrelated_batch = CommunicationBatch(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        channel="email",
        status="completed",
        idempotency_key=f"unrelated-batch:{uuid4()}",
        total_count=1,
        requested_count=1,
        skipped_count=0,
        skip_summary=[],
        sent_count=1,
    )
    other_tenant_batch = CommunicationBatch(
        organization_id=other_organization.id,
        created_by_user_id=other_admin.id,
        channel="email",
        status="completed",
        idempotency_key=f"candidate-batch:{canary}",
        total_count=1,
        requested_count=1,
        skipped_count=1,
        skip_summary=[{"reason": "candidate_private", "evidence": canary}],
    )
    matching_source_run = SourceImportRun(
        organization_id=organization.id,
        integration_id=None,
        started_by_user_id=admin.id,
        provider_snapshot="csv",
        idempotency_key=f"candidate-import:{unique_email}",
        request_fingerprint="c" * 64,
        status="completed",
        requested_count=5,
        created_count=3,
        duplicate_count=1,
        failed_count=1,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    unrelated_source_run = SourceImportRun(
        organization_id=organization.id,
        integration_id=None,
        started_by_user_id=admin.id,
        provider_snapshot="csv",
        idempotency_key=f"common-role:{common_role}",
        request_fingerprint="d" * 64,
        status="completed",
        requested_count=2,
        created_count=2,
        duplicate_count=0,
        failed_count=0,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    other_tenant_source_run = SourceImportRun(
        organization_id=other_organization.id,
        integration_id=None,
        started_by_user_id=other_admin.id,
        provider_snapshot="csv",
        idempotency_key=f"candidate-import:{unique_email}",
        request_fingerprint="e" * 64,
        status="completed",
        requested_count=1,
        created_count=1,
        duplicate_count=0,
        failed_count=0,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    ai_message = AiMessage(
        organization_id=organization.id,
        session_id=ai_session.id,
        actor_user_id=admin.id,
        role="assistant",
        content=f"Imported candidate {canary} ({candidate_id})",
        structured_content={"candidate_ids": [str(candidate_id)]},
        attachments=[],
        redaction_metadata={"raw_prompt_logged": False},
        sequence=1,
        status="completed",
        completed_at=datetime.now(UTC),
    )
    candidate_audit = AuditLog(
        organization_id=organization.id,
        actor_user_id=admin.id,
        actor_email_snapshot=admin.email,
        action="privacy_fixture",
        resource_type="candidate",
        resource_id=candidate_id,
        path_template="/privacy/fixture",
        method="GET",
        status_code=200,
        request_id=uuid4(),
        metadata_json={"canary": canary},
        occurred_at=datetime.now(UTC),
    )
    db.add_all(
        [
            chat_message,
            matching_free_message,
            unrelated_free_message,
            common_role_message,
            common_name_message,
            github_login_message,
            third_party_email_message,
            shared_requisition_message,
            neighboring_email_message,
            exact_document_message,
            email_matching_message,
            opaque_matching_message,
            other_tenant_matching_message,
            matching_name_view,
            matching_filter_view,
            unrelated_view,
            common_role_view,
            common_name_view,
            other_tenant_matching_view,
            *matching_workflow_rules,
            unrelated_workflow_rule,
            other_tenant_workflow_rule,
            *matching_communication_templates,
            unrelated_communication_template,
            other_tenant_communication_template,
            *matching_offer_templates,
            unrelated_offer_template,
            other_tenant_offer_template,
            workflow,
            matching_derived_workflow_run,
            unrelated_workflow_run,
            other_tenant_workflow_run,
            receipt,
            notification,
            batch,
            candidate_bearing_batch,
            unrelated_batch,
            other_tenant_batch,
            matching_source_run,
            unrelated_source_run,
            other_tenant_source_run,
            ai_message,
            candidate_audit,
        ]
    )
    candidate.deleted_at = datetime.now(UTC) - timedelta(days=60)
    await db.commit()
    identifiers = {
        "chat": chat.id,
        "chat_message": chat_message.id,
        "matching_free_chat": matching_free_chat.id,
        "matching_free_message": matching_free_message.id,
        "unrelated_free_chat": unrelated_free_chat.id,
        "unrelated_free_message": unrelated_free_message.id,
        "common_role_chat": common_role_chat.id,
        "common_role_message": common_role_message.id,
        "common_name_chat": common_name_chat.id,
        "common_name_message": common_name_message.id,
        "github_login_chat": github_login_chat.id,
        "github_login_message": github_login_message.id,
        "third_party_email_chat": third_party_email_chat.id,
        "third_party_email_message": third_party_email_message.id,
        "shared_requisition_chat": shared_requisition_chat.id,
        "shared_requisition_message": shared_requisition_message.id,
        "neighboring_email_chat": neighboring_email_chat.id,
        "neighboring_email_message": neighboring_email_message.id,
        "exact_document_chat": exact_document_chat.id,
        "exact_document_message": exact_document_message.id,
        "email_matching_chat": email_matching_chat.id,
        "email_matching_message": email_matching_message.id,
        "opaque_matching_chat": opaque_matching_chat.id,
        "opaque_matching_message": opaque_matching_message.id,
        "other_tenant_matching_chat": other_tenant_matching_chat.id,
        "other_tenant_matching_message": other_tenant_matching_message.id,
        "matching_name_view": matching_name_view.id,
        "matching_filter_view": matching_filter_view.id,
        "unrelated_view": unrelated_view.id,
        "common_role_view": common_role_view.id,
        "common_name_view": common_name_view.id,
        "other_tenant_matching_view": other_tenant_matching_view.id,
        "matching_workflow_rules": tuple(item.id for item in matching_workflow_rules),
        "unrelated_workflow_rule": unrelated_workflow_rule.id,
        "other_tenant_workflow_rule": other_tenant_workflow_rule.id,
        "matching_communication_templates": tuple(
            item.id for item in matching_communication_templates
        ),
        "unrelated_communication_template": unrelated_communication_template.id,
        "other_tenant_communication_template": (
            other_tenant_communication_template.id
        ),
        "matching_offer_templates": tuple(item.id for item in matching_offer_templates),
        "unrelated_offer_template": unrelated_offer_template.id,
        "other_tenant_offer_template": other_tenant_offer_template.id,
        "unrelated_candidate": unrelated_candidate.id,
        "other_candidate": other_candidate.id,
        "unrelated_application": unrelated_application.id,
        "other_tenant_application": other_tenant_application.id,
        "matching_stage_event": matching_stage_event.id,
        "unrelated_stage_event": unrelated_stage_event.id,
        "other_tenant_stage_event": other_tenant_stage_event.id,
        "matching_tag": matching_tag.id,
        "unrelated_tag": unrelated_tag.id,
        "other_tenant_tag": other_tenant_tag.id,
        "matching_activity": matching_activity.id,
        "unrelated_activity": unrelated_activity.id,
        "other_tenant_activity": other_tenant_activity.id,
        "matching_task": matching_task.id,
        "unrelated_task": unrelated_task.id,
        "other_tenant_task": other_tenant_task.id,
        "matching_communication_message": matching_communication_message.id,
        "unrelated_communication_message": unrelated_communication_message.id,
        "other_tenant_communication_message": (
            other_tenant_communication_message.id
        ),
        "matching_candidate_offer": matching_candidate_offer.id,
        "unrelated_candidate_offer": unrelated_candidate_offer.id,
        "other_tenant_candidate_offer": other_tenant_candidate_offer.id,
        "matching_offer_approval": matching_offer_approval.id,
        "matching_offer_approval_updated_at": matching_offer_approval.updated_at,
        "unrelated_offer_approval": unrelated_offer_approval.id,
        "other_tenant_offer_approval": other_tenant_offer_approval.id,
        "matching_derived_workflow_run": matching_derived_workflow_run.id,
        "unrelated_workflow_run": unrelated_workflow_run.id,
        "other_tenant_workflow_run": other_tenant_workflow_run.id,
        "ai_title_session": ai_title_session.id,
        "mission_root_sessions": tuple(
            item.id
            for item in (
                mission_title_session,
                mission_request_session,
                mission_brief_session,
                mission_rubric_session,
                mission_assumptions_session,
                mission_blockers_session,
            )
        ),
        "mission_roots": tuple(item.id for item in mission_roots),
        "unrelated_ai_session": unrelated_ai_session.id,
        "unrelated_mission": unrelated_mission.id,
        "common_role_ai_session": common_role_ai_session.id,
        "common_name_ai_session": common_name_ai_session.id,
        "email_ai_session": email_ai_session.id,
        "opaque_ai_session": opaque_ai_session.id,
        "generic_attachment_session": generic_attachment_session.id,
        "unrelated_attachment_session": unrelated_attachment_session.id,
        "pending_attachment_session": pending_attachment_session.id,
        "large_plain_attachment_session": large_plain_attachment_session.id,
        "matching_bundle_session": matching_bundle_session.id,
        "matching_bundle_mission": matching_bundle_mission.id,
        "matching_bundle": matching_bundle.id,
        "unrelated_bundle": unrelated_bundle.id,
        "other_tenant_bundle": other_tenant_bundle.id,
        "discovery_sessions": tuple(
            item.id
            for item in (
                discovery_step_session,
                discovery_attempt_session,
                discovery_run_session,
            )
        ),
        "discovery_missions": tuple(item.id for item in discovery_missions),
        "other_tenant_ai_session": other_tenant_ai_session.id,
        "other_tenant_mission": other_tenant_mission.id,
        "root_session_attachment": UUID(root_session_attachment["id"]),
        "generic_candidate_attachment": UUID(generic_candidate_attachment["id"]),
        "unrelated_attachment": UUID(unrelated_attachment["id"]),
        "other_tenant_attachment": UUID(other_tenant_attachment["id"]),
        "pending_candidate_attachment": UUID(pending_candidate_attachment["id"]),
        "pending_deletion": pending_deletion.id,
        "large_plain_attachment": UUID(large_plain_attachment["id"]),
        "bulk_unrelated_sessions": tuple(item.id for item in bulk_unrelated_sessions),
        "bulk_unrelated_attachments": tuple(
            UUID(item["id"]) for item in bulk_unrelated_attachments
        ),
        "workflow": workflow.id,
        "receipt": receipt.id,
        "notification": notification.id,
        "batch": batch.id,
        "candidate_bearing_batch": candidate_bearing_batch.id,
        "unrelated_batch": unrelated_batch.id,
        "unrelated_batch_key": unrelated_batch.idempotency_key,
        "other_tenant_batch": other_tenant_batch.id,
        "other_tenant_batch_key": other_tenant_batch.idempotency_key,
        "matching_source_run": matching_source_run.id,
        "unrelated_source_run": unrelated_source_run.id,
        "unrelated_source_run_key": unrelated_source_run.idempotency_key,
        "other_tenant_source_run": other_tenant_source_run.id,
        "other_tenant_source_run_key": other_tenant_source_run.idempotency_key,
        "ai_session": ai_session.id,
        "ai_message": ai_message.id,
        "audit": candidate_audit.id,
    }

    privacy_context = await build_candidate_privacy_context(
        db,
        organization_id=organization.id,
        candidate_ids={candidate_id},
    )
    assert contains_candidate_marker(unique_email, privacy_context.markers)
    assert not contains_candidate_marker(neighboring_email, privacy_context.markers)
    assert not contains_candidate_marker(third_party_email, privacy_context.markers)
    assert not contains_candidate_marker(
        str(shared_requisition_id), privacy_context.markers
    )

    result = await run_retention(
        RetentionRunRequest(
            confirmation="PURGE",
            include_active_candidates=False,
        ),
        db=db,
        user=admin,
    )
    assert result.deleted_candidates == 1
    # SET LOCAL is cleared at commit (an unset custom GUC reads as ""). It
    # must never remain truthy after the narrowly bracketed stage redaction.
    assert await db.scalar(
        text("SELECT current_setting('app.allow_erasure', true)")
    ) not in {"on", "true", "1"}
    # Bulk privacy deletes and database cascades intentionally bypass ORM
    # relationship loading; clear fixture identity-map copies before proving
    # absence from the committed database.
    db.expunge_all()
    assert await db.get(ChatSession, identifiers["chat"]) is None
    assert await db.get(Message, identifiers["chat_message"]) is None
    assert await db.get(ChatSession, identifiers["matching_free_chat"]) is None
    assert await db.get(Message, identifiers["matching_free_message"]) is None
    assert await db.get(ChatSession, identifiers["unrelated_free_chat"]) is not None
    assert await db.get(Message, identifiers["unrelated_free_message"]) is not None
    assert await db.get(ChatSession, identifiers["common_role_chat"]) is not None
    assert await db.get(Message, identifiers["common_role_message"]) is not None
    assert await db.get(ChatSession, identifiers["common_name_chat"]) is not None
    assert await db.get(Message, identifiers["common_name_message"]) is not None
    assert await db.get(ChatSession, identifiers["github_login_chat"]) is not None
    assert await db.get(Message, identifiers["github_login_message"]) is not None
    assert (
        await db.get(ChatSession, identifiers["third_party_email_chat"]) is not None
    )
    assert (
        await db.get(Message, identifiers["third_party_email_message"]) is not None
    )
    assert (
        await db.get(ChatSession, identifiers["shared_requisition_chat"]) is not None
    )
    assert (
        await db.get(Message, identifiers["shared_requisition_message"]) is not None
    )
    assert (
        await db.get(ChatSession, identifiers["neighboring_email_chat"]) is not None
    )
    assert (
        await db.get(Message, identifiers["neighboring_email_message"]) is not None
    )
    assert await db.get(ChatSession, identifiers["exact_document_chat"]) is None
    assert await db.get(Message, identifiers["exact_document_message"]) is None
    assert await db.get(ChatSession, identifiers["email_matching_chat"]) is None
    assert await db.get(Message, identifiers["email_matching_message"]) is None
    assert await db.get(ChatSession, identifiers["opaque_matching_chat"]) is None
    assert await db.get(Message, identifiers["opaque_matching_message"]) is None
    assert (
        await db.get(ChatSession, identifiers["other_tenant_matching_chat"])
        is not None
    )
    assert (
        await db.get(Message, identifiers["other_tenant_matching_message"])
        is not None
    )
    assert await db.get(CandidateSavedView, identifiers["matching_name_view"]) is None
    assert (
        await db.get(CandidateSavedView, identifiers["matching_filter_view"])
        is None
    )
    assert await db.get(CandidateSavedView, identifiers["unrelated_view"]) is not None
    assert await db.get(CandidateSavedView, identifiers["common_role_view"]) is not None
    assert await db.get(CandidateSavedView, identifiers["common_name_view"]) is not None
    assert (
        await db.get(CandidateSavedView, identifiers["other_tenant_matching_view"])
        is not None
    )
    for rule_id in identifiers["matching_workflow_rules"]:
        assert await db.get(WorkflowRule, rule_id) is None
    assert (
        await db.get(WorkflowRule, identifiers["unrelated_workflow_rule"])
        is not None
    )
    assert (
        await db.get(WorkflowRule, identifiers["other_tenant_workflow_rule"])
        is not None
    )
    for template_id in identifiers["matching_communication_templates"]:
        assert await db.get(CommunicationTemplate, template_id) is None
    assert (
        await db.get(
            CommunicationTemplate,
            identifiers["unrelated_communication_template"],
        )
        is not None
    )
    assert (
        await db.get(
            CommunicationTemplate,
            identifiers["other_tenant_communication_template"],
        )
        is not None
    )
    for template_id in identifiers["matching_offer_templates"]:
        assert await db.get(OfferTemplate, template_id) is None
    assert (
        await db.get(OfferTemplate, identifiers["unrelated_offer_template"])
        is not None
    )
    assert (
        await db.get(OfferTemplate, identifiers["other_tenant_offer_template"])
        is not None
    )
    assert await db.get(Candidate, identifiers["unrelated_candidate"]) is not None
    assert await db.get(Candidate, identifiers["other_candidate"]) is not None
    assert await db.get(Application, identifiers["unrelated_application"]) is not None
    assert (
        await db.get(Application, identifiers["other_tenant_application"]) is not None
    )

    sanitized_stage_event = await db.get(
        ApplicationStageEvent, identifiers["matching_stage_event"]
    )
    assert sanitized_stage_event is not None
    assert sanitized_stage_event.reason is None
    assert sanitized_stage_event.details == {"privacy_erased": True}
    savepoint = await db.begin_nested()
    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(
            update(ApplicationStageEvent)
            .where(ApplicationStageEvent.id == identifiers["matching_stage_event"])
            .values(reason="arbitrary post-erasure tamper")
        )
    await savepoint.rollback()
    retained_stage_event = await db.get(
        ApplicationStageEvent, identifiers["unrelated_stage_event"]
    )
    assert retained_stage_event is not None
    assert retained_stage_event.reason == common_role
    assert retained_stage_event.details == {"candidate_name": common_name}
    retained_other_stage_event = await db.get(
        ApplicationStageEvent, identifiers["other_tenant_stage_event"]
    )
    assert retained_other_stage_event is not None
    assert unique_email in (retained_other_stage_event.reason or "")
    assert retained_other_stage_event.details == {"candidate_email": unique_email}

    sanitized_tag = await db.get(CandidateTag, identifiers["matching_tag"])
    assert sanitized_tag is not None
    assert sanitized_tag.label.startswith("privacy-erased-")
    retained_tag = await db.get(CandidateTag, identifiers["unrelated_tag"])
    assert retained_tag is not None
    assert retained_tag.label == common_role
    retained_other_tag = await db.get(CandidateTag, identifiers["other_tenant_tag"])
    assert retained_other_tag is not None
    assert retained_other_tag.label == unique_phone

    sanitized_activity = await db.get(
        CandidateActivity, identifiers["matching_activity"]
    )
    assert sanitized_activity is not None
    assert sanitized_activity.payload == {"privacy_erased": True}
    retained_activity = await db.get(
        CandidateActivity, identifiers["unrelated_activity"]
    )
    assert retained_activity is not None
    assert retained_activity.payload == {"role": common_role}
    retained_other_activity = await db.get(
        CandidateActivity, identifiers["other_tenant_activity"]
    )
    assert retained_other_activity is not None
    assert retained_other_activity.payload == {"rendered_rule_value": unique_email}

    sanitized_task = await db.get(TaskSubmission, identifiers["matching_task"])
    assert sanitized_task is not None
    assert sanitized_task.title == "privacy-erased"
    assert sanitized_task.prompt is None
    retained_task = await db.get(TaskSubmission, identifiers["unrelated_task"])
    assert retained_task is not None
    assert retained_task.title == common_role
    assert retained_task.prompt == f"General task for {common_name}"
    retained_other_task = await db.get(
        TaskSubmission, identifiers["other_tenant_task"]
    )
    assert retained_other_task is not None
    assert unique_email in retained_other_task.title

    sanitized_message = await db.get(
        CommunicationMessage, identifiers["matching_communication_message"]
    )
    assert sanitized_message is not None
    assert sanitized_message.subject == "[privacy erased]"
    assert sanitized_message.body == "[privacy erased]"
    assert sanitized_message.idempotency_key == (
        f"privacy-erased:{identifiers['matching_communication_message']}"
    )
    assert sanitized_message.provider_message_id is None
    assert sanitized_message.webhook_event_id is None
    assert sanitized_message.webhook_payload_sha256 is None
    retained_message = await db.get(
        CommunicationMessage, identifiers["unrelated_communication_message"]
    )
    assert retained_message is not None
    assert retained_message.subject == common_role
    assert retained_message.idempotency_key == f"message:{common_role}"
    retained_other_message = await db.get(
        CommunicationMessage,
        identifiers["other_tenant_communication_message"],
    )
    assert retained_other_message is not None
    assert unique_email in (retained_other_message.subject or "")
    assert retained_other_message.idempotency_key == f"message:{unique_email}"

    sanitized_offer = await db.get(
        CandidateOffer, identifiers["matching_candidate_offer"]
    )
    assert sanitized_offer is not None
    assert sanitized_offer.status == "accepted"
    assert sanitized_offer.template_name_snapshot is None
    assert sanitized_offer.subject_template_snapshot == "[privacy erased]"
    assert sanitized_offer.body_template_snapshot == "[privacy erased]"
    assert sanitized_offer.subject == "[privacy erased]"
    assert sanitized_offer.body == "[privacy erased]"
    assert sanitized_offer.signature_name is None
    retained_offer = await db.get(
        CandidateOffer, identifiers["unrelated_candidate_offer"]
    )
    assert retained_offer is not None
    assert retained_offer.subject == common_role
    retained_other_offer = await db.get(
        CandidateOffer, identifiers["other_tenant_candidate_offer"]
    )
    assert retained_other_offer is not None
    assert unique_email in retained_other_offer.subject

    sanitized_approval = await db.get(
        OfferApproval, identifiers["matching_offer_approval"]
    )
    assert sanitized_approval is not None
    assert sanitized_approval.vote == "approve"
    assert sanitized_approval.comment is None
    assert sanitized_approval.updated_at == identifiers[
        "matching_offer_approval_updated_at"
    ]
    assert sanitized_approval.offer_snapshot == {
        "candidate_id": str(identifiers["unrelated_candidate"]),
        "subject": "[privacy erased]",
        "body": "[privacy erased]",
        "salary_currency": "TJS",
        "version": 1,
    }
    approval_savepoint = await db.begin_nested()
    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(
            update(OfferApproval)
            .where(OfferApproval.id == identifiers["matching_offer_approval"])
            .values(comment="arbitrary post-erasure tamper")
        )
    await approval_savepoint.rollback()
    retained_approval = await db.get(
        OfferApproval, identifiers["unrelated_offer_approval"]
    )
    assert retained_approval is not None
    assert retained_approval.offer_snapshot["subject"] == common_role
    assert retained_approval.comment == f"Approved general offer for {common_name}"
    retained_other_approval = await db.get(
        OfferApproval, identifiers["other_tenant_offer_approval"]
    )
    assert retained_other_approval is not None
    assert unique_email in retained_other_approval.offer_snapshot["subject"]
    assert unique_email in (retained_other_approval.comment or "")

    sanitized_run = await db.get(
        WorkflowRun, identifiers["matching_derived_workflow_run"]
    )
    assert sanitized_run is not None
    assert sanitized_run.status == "completed"
    assert sanitized_run.actions_snapshot == [{"privacy_erased": True}]
    assert sanitized_run.action_results == [{"privacy_erased": True}]
    retained_run = await db.get(
        WorkflowRun, identifiers["unrelated_workflow_run"]
    )
    assert retained_run is not None
    assert retained_run.actions_snapshot == [
        {"type": "ai_prompt", "prompt": common_role}
    ]
    retained_other_run = await db.get(
        WorkflowRun, identifiers["other_tenant_workflow_run"]
    )
    assert retained_other_run is not None
    assert retained_other_run.actions_snapshot == [
        {"type": "ai_prompt", "prompt": unique_email}
    ]
    assert await db.get(AiSession, identifiers["ai_title_session"]) is None
    for session_id in identifiers["mission_root_sessions"]:
        assert await db.get(AiSession, session_id) is None
    for mission_id in identifiers["mission_roots"]:
        assert await db.get(HiringMission, mission_id) is None
    assert await db.get(AiSession, identifiers["unrelated_ai_session"]) is not None
    assert await db.get(HiringMission, identifiers["unrelated_mission"]) is not None
    assert await db.get(AiSession, identifiers["common_role_ai_session"]) is not None
    assert await db.get(AiSession, identifiers["common_name_ai_session"]) is not None
    assert await db.get(AiSession, identifiers["email_ai_session"]) is None
    assert await db.get(AiSession, identifiers["opaque_ai_session"]) is None
    assert await db.get(AiSession, identifiers["generic_attachment_session"]) is None
    assert (
        await db.get(AiSession, identifiers["unrelated_attachment_session"])
        is not None
    )
    assert await db.get(AiSession, identifiers["pending_attachment_session"]) is None
    assert (
        await db.get(AiSession, identifiers["large_plain_attachment_session"])
        is not None
    )
    assert await db.get(AiSession, identifiers["matching_bundle_session"]) is None
    assert await db.get(HiringMission, identifiers["matching_bundle_mission"]) is None
    assert await db.get(AiActionBundle, identifiers["matching_bundle"]) is None
    assert await db.get(AiActionBundle, identifiers["unrelated_bundle"]) is not None
    assert (
        await db.get(AiActionBundle, identifiers["other_tenant_bundle"])
        is not None
    )
    for session_id in identifiers["discovery_sessions"]:
        assert await db.get(AiSession, session_id) is None
    for mission_id in identifiers["discovery_missions"]:
        assert await db.get(HiringMission, mission_id) is None
    assert (
        await db.get(AiSession, identifiers["other_tenant_ai_session"])
        is not None
    )
    assert (
        await db.get(HiringMission, identifiers["other_tenant_mission"])
        is not None
    )
    assert await db.get(WorkflowRun, identifiers["workflow"]) is None
    assert await db.get(SourceCandidateReceipt, identifiers["receipt"]) is None
    assert await db.get(Notification, identifiers["notification"]) is None
    assert await db.get(AiSession, identifiers["ai_session"]) is None
    assert await db.get(AiMessage, identifiers["ai_message"]) is None
    assert await db.get(AuditLog, identifiers["audit"]) is None
    sanitized_batch = await db.get(CommunicationBatch, identifiers["batch"])
    assert sanitized_batch is not None
    assert sanitized_batch.skip_summary == []
    assert sanitized_batch.requested_count == 0
    assert sanitized_batch.skipped_count == 0
    sanitized_candidate_batch = await db.get(
        CommunicationBatch, identifiers["candidate_bearing_batch"]
    )
    assert sanitized_candidate_batch is not None
    assert sanitized_candidate_batch.idempotency_key == (
        f"privacy-erased:{identifiers['candidate_bearing_batch']}"
    )
    assert sanitized_candidate_batch.skip_summary == []
    assert sanitized_candidate_batch.total_count == 2
    assert sanitized_candidate_batch.requested_count == 1
    assert sanitized_candidate_batch.skipped_count == 0
    assert sanitized_candidate_batch.sent_count == 1
    retained_unrelated_batch = await db.get(
        CommunicationBatch, identifiers["unrelated_batch"]
    )
    assert retained_unrelated_batch is not None
    assert retained_unrelated_batch.idempotency_key == identifiers["unrelated_batch_key"]
    assert retained_unrelated_batch.requested_count == 1
    retained_other_tenant_batch = await db.get(
        CommunicationBatch, identifiers["other_tenant_batch"]
    )
    assert retained_other_tenant_batch is not None
    assert (
        retained_other_tenant_batch.idempotency_key
        == identifiers["other_tenant_batch_key"]
    )
    assert retained_other_tenant_batch.skip_summary == [
        {"reason": "candidate_private", "evidence": canary}
    ]
    assert retained_other_tenant_batch.requested_count == 1
    assert retained_other_tenant_batch.skipped_count == 1
    sanitized_source_run = await db.get(
        SourceImportRun, identifiers["matching_source_run"]
    )
    assert sanitized_source_run is not None
    assert sanitized_source_run.idempotency_key == (
        f"privacy-erased:{identifiers['matching_source_run']}"
    )
    assert sanitized_source_run.requested_count == 5
    assert sanitized_source_run.created_count == 3
    assert sanitized_source_run.duplicate_count == 1
    assert sanitized_source_run.failed_count == 1
    retained_source_run = await db.get(
        SourceImportRun, identifiers["unrelated_source_run"]
    )
    assert retained_source_run is not None
    assert retained_source_run.idempotency_key == identifiers["unrelated_source_run_key"]
    retained_other_source_run = await db.get(
        SourceImportRun, identifiers["other_tenant_source_run"]
    )
    assert retained_other_source_run is not None
    assert (
        retained_other_source_run.idempotency_key
        == identifiers["other_tenant_source_run_key"]
    )
    candidate_object = await db.get(PrivateObject, UUID(upload["id"]))
    assert candidate_object is not None
    assert candidate_object.state == "delete_pending"
    assert canary not in candidate_object.original_filename
    assert candidate_object.original_filename == "erased"
    assert candidate_object.media_type == "application/octet-stream"
    assert candidate_object.scanner_engine is None
    assert candidate_object.scanner_version is None
    assert candidate_object.scanner_verdict is None
    candidate_deletion = (
        await db.execute(
            select(PrivateObjectDeletion).where(
                PrivateObjectDeletion.private_object_id == UUID(upload["id"])
            )
        )
    ).scalar_one()
    assert candidate_deletion.reason == "candidate_erasure"
    assert candidate_deletion.object_key_snapshot == candidate_object.object_key
    assert candidate_deletion.sha256_snapshot == candidate_object.sha256
    assert (
        await db.execute(
            select(PrivateObjectDeletion).where(
                PrivateObjectDeletion.private_object_id
                == identifiers["root_session_attachment"]
            )
        )
    ).scalar_one().reason == "candidate_erasure"
    assert (
        await db.execute(
            select(PrivateObjectDeletion).where(
                PrivateObjectDeletion.private_object_id
                == identifiers["generic_candidate_attachment"]
            )
        )
    ).scalar_one().reason == "candidate_erasure"
    generic_candidate_object = await db.get(
        PrivateObject, identifiers["generic_candidate_attachment"]
    )
    assert generic_candidate_object is not None
    assert generic_candidate_object.state == "delete_pending"
    assert generic_candidate_object.original_filename == "erased"
    retained_attachment = await db.get(
        PrivateObject, identifiers["unrelated_attachment"]
    )
    assert retained_attachment is not None
    assert retained_attachment.state == "ready"
    assert retained_attachment.original_filename == "general-notes.txt"
    retained_other_tenant_attachment = await db.get(
        PrivateObject, identifiers["other_tenant_attachment"]
    )
    assert retained_other_tenant_attachment is not None
    assert retained_other_tenant_attachment.state == "ready"
    assert retained_other_tenant_attachment.original_filename == "notes.txt"
    retained_large_plain_attachment = await db.get(
        PrivateObject, identifiers["large_plain_attachment"]
    )
    assert retained_large_plain_attachment is not None
    assert retained_large_plain_attachment.state == "ready"
    assert retained_large_plain_attachment.size_bytes > 2 * 1024 * 1024
    for session_id in identifiers["bulk_unrelated_sessions"]:
        assert await db.get(AiSession, session_id) is not None
    for attachment_id in identifiers["bulk_unrelated_attachments"]:
        bulk_attachment = await db.get(PrivateObject, attachment_id)
        assert bulk_attachment is not None
        assert bulk_attachment.state == "ready"
    pending_object = await db.get(
        PrivateObject, identifiers["pending_candidate_attachment"]
    )
    assert pending_object is not None
    assert pending_object.state == "delete_pending"
    assert pending_object.original_filename == "erased"
    assert pending_object.media_type == "application/octet-stream"
    retained_pending_deletion = await db.get(
        PrivateObjectDeletion, identifiers["pending_deletion"]
    )
    assert retained_pending_deletion is not None
    assert retained_pending_deletion.reason == "retention_expired"
    assert retained_pending_deletion.object_key_snapshot == pending_object.object_key
    assert retained_pending_deletion.sha256_snapshot == pending_object.sha256

    claim_token = uuid4()
    candidate_deletion.claimed_at = datetime.now(UTC)
    candidate_deletion.claim_token = claim_token
    candidate_deletion.attempt_count = 1
    await db.commit()
    outcome = await settle_private_object_deletion(
        db,
        deletion_id=candidate_deletion.id,
        organization_id=organization.id,
        claim_token=claim_token,
        store=store,
    )
    assert outcome == DeletionSettlement.DELETED
    await db.commit()
    settled_object = await db.get(PrivateObject, UUID(upload["id"]))
    assert settled_object is not None
    assert settled_object.state == "deleted"

    pending_claim_token = uuid4()
    retained_pending_deletion.claimed_at = datetime.now(UTC)
    retained_pending_deletion.claim_token = pending_claim_token
    retained_pending_deletion.attempt_count = 1
    await db.commit()
    pending_outcome = await settle_private_object_deletion(
        db,
        deletion_id=retained_pending_deletion.id,
        organization_id=organization.id,
        claim_token=pending_claim_token,
        store=store,
    )
    assert pending_outcome == DeletionSettlement.DELETED
    await db.commit()
    settled_pending_object = await db.get(
        PrivateObject, identifiers["pending_candidate_attachment"]
    )
    assert settled_pending_object is not None
    assert settled_pending_object.state == "deleted"


async def test_retention_fails_closed_when_unsubmitted_ai_object_is_inaccessible(
    db,
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "inaccessible-objects"
    monkeypatch.setattr(get_settings(), "storage_dir", str(root))
    organization = Organization(
        name="Inaccessible privacy inventory",
        slug=f"inaccessible-privacy-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    admin = User(
        organization_id=organization.id,
        email=f"inaccessible-privacy-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"inaccessible-candidate-{uuid4()}",
        raw_cv="Candidate whose purge must remain atomic",
        email_normalized=f"inaccessible-{uuid4().hex}@candidate.example",
        deleted_at=datetime.now(UTC) - timedelta(days=60),
    )
    db.add_all(
        [
            admin,
            candidate,
            EnterprisePolicy(
                organization_id=organization.id,
                candidate_retention_days=365,
                deleted_retention_days=30,
                audit_retention_days=90,
            ),
            AiOrganizationSettings(
                organization_id=organization.id,
                feature_enabled=True,
            ),
        ]
    )
    await db.flush()
    session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        title="Unsubmitted generic attachment",
        status="active",
    )
    db.add(session)
    await db.flush()
    store = LocalBlobStore(root)
    upload = await save_ai_attachment(
        db,
        admin,
        session.id,
        upload=_upload(
            "Unsubmitted object whose inventory is no longer available",
            filename="generic-notes.txt",
        ),
        store=store,
    )
    private_object = await db.get(PrivateObject, UUID(upload["id"]))
    assert private_object is not None
    object_key = private_object.object_key
    candidate_id = candidate.id
    private_object_id = private_object.id
    await db.commit()
    await store.delete(object_key)

    with pytest.raises(HTTPException) as exc_info:
        await run_retention(
            RetentionRunRequest(
                confirmation="PURGE",
                include_active_candidates=False,
            ),
            db=db,
            user=admin,
        )
    assert exc_info.value.status_code == 409
    retained_candidate = (
        await db.execute(
            select(Candidate)
            .where(Candidate.id == candidate_id)
            .execution_options(include_deleted=True)
        )
    ).scalar_one()
    assert retained_candidate.deleted_at is not None
    retained_object = await db.get(PrivateObject, private_object_id)
    assert retained_object is not None
    assert retained_object.state == "ready"
    assert retained_object.original_filename == "generic-notes.txt"


@pytest.mark.parametrize(
    "kind",
    [
        "xlsx-core",
        "docx-core",
        "pdf-metadata",
        "xlsx-lexical-comment",
        "xlsx-lexical-processing-instruction",
        "xlsx-lexical-namespace",
        "xlsx-lexical-encoded-namespace",
        "docx-lexical-comment",
        "docx-lexical-processing-instruction",
        "docx-lexical-namespace",
        "docx-lexical-encoded-namespace",
    ],
)
async def test_retention_discovers_candidate_identifier_in_allowed_file_metadata(
    db,
    tmp_path: Path,
    monkeypatch,
    kind: str,
):
    root = tmp_path / f"metadata-{kind}-objects"
    monkeypatch.setattr(get_settings(), "storage_dir", str(root))
    organization = Organization(
        name=f"Metadata privacy {kind}",
        slug=f"metadata-privacy-{kind}-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    admin = User(
        organization_id=organization.id,
        email=f"metadata-{kind}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    candidate_email = f"metadata-{kind}-{uuid4().hex}@candidate.example"
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"metadata-candidate-{uuid4()}",
        raw_cv="Candidate whose identifier is in allowed file metadata",
        email_normalized=candidate_email,
        deleted_at=datetime.now(UTC) - timedelta(days=60),
    )
    db.add_all(
        [
            admin,
            candidate,
            EnterprisePolicy(
                organization_id=organization.id,
                candidate_retention_days=365,
                deleted_retention_days=30,
                audit_retention_days=90,
            ),
            AiOrganizationSettings(
                organization_id=organization.id,
                feature_enabled=True,
            ),
        ]
    )
    await db.flush()
    session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        title="Generic unsubmitted metadata attachment",
        status="active",
    )
    db.add(session)
    await db.flush()
    candidate_id = candidate.id
    session_id = session.id
    store = LocalBlobStore(root)
    result = await save_ai_attachment(
        db,
        admin,
        session_id,
        upload=_privacy_metadata_upload(kind, candidate_email),
        store=store,
    )
    private_object_id = UUID(result["id"])

    await run_retention(
        RetentionRunRequest(
            confirmation="PURGE",
            include_active_candidates=False,
        ),
        db=db,
        user=admin,
    )

    assert (
        await db.execute(
            select(Candidate)
            .where(Candidate.id == candidate_id)
            .execution_options(include_deleted=True)
        )
    ).scalar_one_or_none() is None
    assert await db.get(AiSession, session_id) is None
    erased_object = await db.get(PrivateObject, private_object_id)
    assert erased_object is not None
    assert erased_object.state == "delete_pending"
    assert erased_object.original_filename == "erased"


@pytest.mark.parametrize(
    "case",
    [
        "xlsx-formula",
        "xlsx-hidden-sheet",
        "xlsx-comment",
        "xlsx-hyperlink",
        "docx-vml-textbox",
        "docx-image",
        "docx-field",
        "docx-content-control",
        "docx-mail-merge",
        "xlsx-zip-archive-comment",
        "xlsx-zip-entry-comment",
        "xlsx-zip-entry-extra",
        "xlsx-zip-prefix",
        "xlsx-zip-unreferenced-local",
        "xlsx-zip-deflate-trailer",
        "docx-zip-archive-comment",
        "docx-zip-entry-comment",
        "docx-zip-entry-extra",
        "docx-zip-prefix",
        "docx-zip-unreferenced-local",
        "docx-zip-deflate-trailer",
        "pdf-annotation",
        "pdf-acroform",
        "pdf-image",
    ],
)
async def test_retention_fails_closed_for_legacy_unsupported_ai_attachment(
    db,
    tmp_path: Path,
    monkeypatch,
    case: str,
):
    root = tmp_path / f"legacy-{case}-objects"
    monkeypatch.setattr(get_settings(), "storage_dir", str(root))
    organization = Organization(
        name=f"Legacy attachment privacy {case}",
        slug=f"legacy-attachment-privacy-{case}-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    admin = User(
        organization_id=organization.id,
        email=f"legacy-attachment-{case}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    candidate_email = f"legacy-{case}-{uuid4().hex}@candidate.example"
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"legacy-attachment-candidate-{uuid4()}",
        raw_cv="Candidate whose unsupported attachment must block purge",
        email_normalized=candidate_email,
        deleted_at=datetime.now(UTC) - timedelta(days=60),
    )
    db.add_all(
        [
            admin,
            candidate,
            EnterprisePolicy(
                organization_id=organization.id,
                candidate_retention_days=365,
                deleted_retention_days=30,
                audit_retention_days=90,
            ),
            AiOrganizationSettings(
                organization_id=organization.id,
                feature_enabled=True,
            ),
        ]
    )
    await db.flush()
    session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        title=f"Legacy unsupported {case}",
        status="active",
    )
    db.add(session)
    await db.flush()
    store = LocalBlobStore(root)

    # Model an already-registered object accepted before the strict inventory
    # contract shipped. New uploads cannot reach this state, while retention
    # must revalidate old rows and fail atomically on unsupported content.
    filename, media_type, content = _legacy_attachment_bytes(
        case,
        candidate_email,
    )
    suffix = Path(filename).suffix.casefold()
    source = tmp_path / filename
    source.write_bytes(content)
    object_key = private_object_key(
        organization.id,
        "ai",
        uuid4(),
        suffix,
    )
    await store.put_file(
        object_key,
        source,
        content_type=media_type,
    )
    stored = StoredUpload(
        key=object_key,
        original_filename=filename,
        suffix=suffix,
        media_type=media_type,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        scan=ScanResult(engine="legacy", result="clean"),
    )
    private_object = await register_ready_private_object(
        db,
        organization_id=organization.id,
        resource_type="ai_attachment",
        resource_id=session.id,
        stored=stored,
        retention_until=datetime.now(UTC) + timedelta(hours=24),
    )
    candidate_id = candidate.id
    private_object_id = private_object.id
    await db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await run_retention(
            RetentionRunRequest(
                confirmation="PURGE",
                include_active_candidates=False,
            ),
            db=db,
            user=admin,
        )
    assert exc_info.value.status_code == 409
    retained_candidate = (
        await db.execute(
            select(Candidate)
            .where(Candidate.id == candidate_id)
            .execution_options(include_deleted=True)
        )
    ).scalar_one()
    assert retained_candidate.deleted_at is not None
    retained_object = await db.get(PrivateObject, private_object_id)
    assert retained_object is not None
    assert retained_object.state == "ready"
    assert retained_object.original_filename == filename
