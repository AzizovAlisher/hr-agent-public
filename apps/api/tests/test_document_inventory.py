"""Strict privacy inventory contracts for unsubmitted DOCX/PDF attachments."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from xml.etree import ElementTree

import pytest
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fpdf import FPDF
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

from app.services.bounded_document_parser import (
    BoundedDocumentParseError,
    extract_document_inventory,
)

pytestmark = pytest.mark.asyncio


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
            content = incoming.read(name)
            if name == "[Content_Types].xml":
                root = ElementTree.fromstring(content)
                for child in list(root):
                    if child.attrib.get("Extension") == "jpeg" or child.attrib.get(
                        "PartName", ""
                    ).startswith("/customXml/"):
                        root.remove(child)
                content = ElementTree.tostring(
                    root, encoding="utf-8", xml_declaration=True
                )
            elif name == "_rels/.rels":
                root = ElementTree.fromstring(content)
                for child in list(root):
                    if child.attrib.get("Type", "").endswith("/thumbnail"):
                        root.remove(child)
                content = ElementTree.tostring(
                    root, encoding="utf-8", xml_declaration=True
                )
            elif name == "word/_rels/document.xml.rels":
                root = ElementTree.fromstring(content)
                for child in list(root):
                    if child.attrib.get("Type", "").endswith("/customXml"):
                        root.remove(child)
                content = ElementTree.tostring(
                    root, encoding="utf-8", xml_declaration=True
                )
            outgoing.writestr(name, content)
    return output.getvalue()


def _write(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def _rewrite_zip_part(
    content: bytes,
    part_name: str,
    mutate,
) -> bytes:
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
                mutate(part) if info.filename == part_name else part,
            )
    return output.getvalue()


def _docx_with_vml_textbox(marker: str) -> bytes:
    document = Document()
    document.add_paragraph("Generic candidate body")
    content = _sanitized_docx(document)
    word_namespace = (
        "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    )
    vml_namespace = "urn:schemas-microsoft-com:vml"

    def add_textbox(document_xml: bytes) -> bytes:
        root = ElementTree.fromstring(document_xml)
        body = root.find(f"{{{word_namespace}}}body")
        assert body is not None
        paragraph = ElementTree.SubElement(body, f"{{{word_namespace}}}p")
        run = ElementTree.SubElement(paragraph, f"{{{word_namespace}}}r")
        pict = ElementTree.SubElement(run, f"{{{word_namespace}}}pict")
        shape = ElementTree.SubElement(pict, f"{{{vml_namespace}}}shape")
        textbox = ElementTree.SubElement(shape, f"{{{vml_namespace}}}textbox")
        textbox_content = ElementTree.SubElement(
            textbox,
            f"{{{word_namespace}}}txbxContent",
        )
        inner_paragraph = ElementTree.SubElement(
            textbox_content,
            f"{{{word_namespace}}}p",
        )
        inner_run = ElementTree.SubElement(
            inner_paragraph,
            f"{{{word_namespace}}}r",
        )
        text = ElementTree.SubElement(inner_run, f"{{{word_namespace}}}t")
        text.text = marker
        return ElementTree.tostring(
            root,
            encoding="utf-8",
            xml_declaration=True,
        )

    return _rewrite_zip_part(content, "word/document.xml", add_textbox)


def _docx_with_interactive_word_surface(case: str, marker: str) -> bytes:
    document = Document()
    document.add_paragraph("Generic candidate body")
    content = _sanitized_docx(document)
    word_namespace = (
        "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    )

    def add_surface(document_xml: bytes) -> bytes:
        root = ElementTree.fromstring(document_xml)
        body = root.find(f"{{{word_namespace}}}body")
        assert body is not None
        paragraph = ElementTree.SubElement(body, f"{{{word_namespace}}}p")
        if case == "field":
            field = ElementTree.SubElement(
                paragraph,
                f"{{{word_namespace}}}fldSimple",
                {
                    f"{{{word_namespace}}}instr": (
                        f'HYPERLINK "mailto:{marker}"'
                    )
                },
            )
            run_parent = field
        elif case == "content-control":
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
                    f"{{{word_namespace}}}displayText": marker,
                    f"{{{word_namespace}}}value": marker,
                },
            )
            contents = ElementTree.SubElement(
                control,
                f"{{{word_namespace}}}sdtContent",
            )
            run_parent = ElementTree.SubElement(
                contents,
                f"{{{word_namespace}}}p",
            )
        else:
            raise AssertionError(f"Unknown interactive DOCX case: {case}")
        run = ElementTree.SubElement(run_parent, f"{{{word_namespace}}}r")
        text = ElementTree.SubElement(run, f"{{{word_namespace}}}t")
        text.text = marker
        return ElementTree.tostring(
            root,
            encoding="utf-8",
            xml_declaration=True,
        )

    return _rewrite_zip_part(content, "word/document.xml", add_surface)


def _docx_with_mail_merge(marker: str) -> bytes:
    document = Document()
    document.add_paragraph("Generic candidate body")
    content = _sanitized_docx(document)
    word_namespace = (
        "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    )

    def add_mail_merge(settings_xml: bytes) -> bytes:
        root = ElementTree.fromstring(settings_xml)
        merge = ElementTree.SubElement(root, f"{{{word_namespace}}}mailMerge")
        ElementTree.SubElement(
            merge,
            f"{{{word_namespace}}}linkToQuery",
        )
        ElementTree.SubElement(
            merge,
            f"{{{word_namespace}}}dataSource",
            {f"{{{word_namespace}}}val": f"https://example.test/{marker}"},
        )
        ElementTree.SubElement(
            merge,
            f"{{{word_namespace}}}connectString",
            {f"{{{word_namespace}}}val": f"Provider=OLEDB;Data Source={marker}"},
        )
        return ElementTree.tostring(
            root,
            encoding="utf-8",
            xml_declaration=True,
        )

    return _rewrite_zip_part(content, "word/settings.xml", add_mail_merge)


def _docx_with_lexical_surface(case: str, marker: str) -> bytes:
    document = Document()
    document.add_paragraph("Generic candidate body")
    content = _sanitized_docx(document)

    def add_surface(document_xml: bytes) -> bytes:
        encoded_marker = marker.encode()
        declaration_end = document_xml.find(b"?>")
        if declaration_end < 0:
            raise AssertionError("DOCX XML declaration is missing")
        declaration_end += 2
        if case == "comment":
            lexical = b"<!--" + encoded_marker + b"-->"
            return (
                document_xml[:declaration_end]
                + lexical
                + document_xml[declaration_end:]
            )
        if case == "processing-instruction":
            lexical = b"<?candidate " + encoded_marker + b"?>"
            return (
                document_xml[:declaration_end]
                + lexical
                + document_xml[declaration_end:]
            )
        if case in {"namespace", "encoded-namespace"}:
            root_start = document_xml.find(b"<w:document")
            if root_start < 0:
                raise AssertionError("DOCX document root is missing")
            insert_at = root_start + len(b"<w:document")
            namespace_marker = (
                encoded_marker.replace(b"@", b"&#64;")
                if case == "encoded-namespace"
                else encoded_marker
            )
            declaration = (
                b' xmlns:privacy="urn:privacy:' + namespace_marker + b'"'
            )
            return (
                document_xml[:insert_at]
                + declaration
                + document_xml[insert_at:]
            )
        raise AssertionError(f"Unknown lexical DOCX case: {case}")

    return _rewrite_zip_part(content, "word/document.xml", add_surface)


def _text_pdf(
    text: str = "Candidate PDF text",
    *,
    metadata: dict[str, str] | None = None,
    xmp: bytes | None = None,
) -> PdfWriter:
    source = FPDF()
    source.add_page()
    source.set_font("Helvetica", size=12)
    source.text(10, 20, text)
    reader = PdfReader(io.BytesIO(bytes(source.output())))
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    if metadata:
        writer.add_metadata(metadata)
    if xmp is not None:
        writer.xmp_metadata = xmp
    return writer


def _writer_bytes(writer: PdfWriter) -> bytes:
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _unicode_font_bytes(text: str) -> bytes:
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
    output = io.BytesIO()
    builder.save(output)
    return output.getvalue()


def _unicode_text_pdf(
    text: str = "Кандидат: Анна Иванова — Python",
) -> PdfWriter:
    with TemporaryDirectory() as directory:
        font_path = Path(directory) / "pilot-unicode.ttf"
        font_path.write_bytes(_unicode_font_bytes(text))
        source = FPDF()
        source.add_page()
        source.add_font("PilotUnicode", fname=str(font_path))
        source.set_font("PilotUnicode", size=12)
        source.text(10, 20, text)
        reader = PdfReader(io.BytesIO(bytes(source.output())))
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    return writer


def _mutate_unicode_font_graph(writer: PdfWriter, case: str) -> None:
    font = writer.pages[0]["/Resources"]["/Font"]["/F1"].get_object()
    descendant = font["/DescendantFonts"][0].get_object()
    descriptor = descendant["/FontDescriptor"].get_object()
    if case == "vertical-encoding":
        font[NameObject("/Encoding")] = NameObject("/Identity-V")
    elif case == "cmap-program":
        cmap = font["/ToUnicode"].get_object()
        cmap.set_data(
            cmap.get_data().replace(
                b"endcmap",
                b"(candidate@example.test) run\nendcmap",
            )
        )
    elif case == "invalid-font-program":
        font_file = descriptor["/FontFile2"].get_object()
        invalid = b"\x00\x01\x00\x00not-a-valid-truetype-font"
        font_file.set_data(invalid)
        font_file[NameObject("/Length1")] = NumberObject(len(invalid))
    elif case == "invalid-cid-map":
        cid_map = descendant["/CIDToGIDMap"].get_object()
        content = bytearray(cid_map.get_data())
        content[:2] = b"\xff\xff"
        cid_map.set_data(bytes(content))
    elif case == "descriptor-extra":
        descriptor[NameObject("/CandidateMarker")] = TextStringObject(
            "candidate@example.test"
        )
    else:
        raise AssertionError(f"Unknown Unicode font case: {case}")


async def test_docx_inventory_includes_body_properties_headers_and_comments(tmp_path):
    marker = f"docx-{uuid4().hex}@candidate.example"
    document = Document()
    paragraph = document.add_paragraph("Candidate body text")
    document.core_properties.title = marker
    document.sections[0].header.paragraphs[0].text = f"Header {marker}"
    document.sections[0].footer.paragraphs[0].text = f"Footer {marker}"
    document.add_comment(paragraph.runs[0], f"Comment {marker}", author="HR")
    path = _write(tmp_path / "safe.docx", _sanitized_docx(document))

    inventory = await extract_document_inventory(path, ".docx")

    assert "Candidate body text" in inventory
    assert marker in inventory
    assert "Comment" in inventory


@pytest.mark.parametrize(
    "case",
    ["comment", "processing-instruction", "namespace", "encoded-namespace"],
)
async def test_docx_inventory_includes_complete_xml_lexical_surface(
    tmp_path,
    case,
):
    marker = f"lexical-{case}-{uuid4().hex}@candidate.example"
    path = _write(
        tmp_path / f"lexical-{case}.docx",
        _docx_with_lexical_surface(case, marker),
    )

    inventory = await extract_document_inventory(path, ".docx")

    assert marker in inventory


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("custom-xml", "docx_package_unsupported"),
        ("hyperlink", "docx_relationship_unsupported"),
        ("image", "docx_package_unsupported"),
        ("vml-textbox", "docx_feature_unsupported"),
        ("field", "docx_feature_unsupported"),
        ("content-control", "docx_feature_unsupported"),
        ("mail-merge", "docx_feature_unsupported"),
    ],
)
async def test_docx_inventory_rejects_uninventoried_package_surfaces(
    tmp_path,
    case,
    expected_code,
):
    document = Document()
    document.add_paragraph("Generic candidate body")
    if case == "hyperlink":
        document.part.relate_to(
            "mailto:candidate@example.test",
            RELATIONSHIP_TYPE.HYPERLINK,
            is_external=True,
        )
        content = _sanitized_docx(document)
    elif case == "image":
        image = io.BytesIO()
        Image.new("RGB", (20, 20), color="white").save(image, format="PNG")
        image.seek(0)
        document.add_picture(image)
        content = _sanitized_docx(document)
    elif case == "vml-textbox":
        content = _docx_with_vml_textbox("candidate@example.test")
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            assert b"candidate@example.test" in archive.read(
                "word/document.xml"
            )
    elif case in {"field", "content-control"}:
        content = _docx_with_interactive_word_surface(
            case,
            "candidate@example.test",
        )
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            assert b"candidate@example.test" in archive.read(
                "word/document.xml"
            )
    elif case == "mail-merge":
        content = _docx_with_mail_merge("candidate@example.test")
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            assert b"candidate@example.test" in archive.read(
                "word/settings.xml"
            )
    else:
        output = io.BytesIO()
        document.save(output)
        content = output.getvalue()
    path = _write(tmp_path / f"unsafe-{case}.docx", content)

    with pytest.raises(BoundedDocumentParseError, match=expected_code):
        await extract_document_inventory(path, ".docx")


async def test_pdf_inventory_includes_page_info_and_xmp_metadata(tmp_path):
    marker = f"pdf-{uuid4().hex}@candidate.example"
    xmp = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        f'<rdf:Description title="{marker}" />'
        "</rdf:RDF></x:xmpmeta>"
    ).encode()
    writer = _text_pdf(metadata={"/Title": marker}, xmp=xmp)
    path = _write(tmp_path / "safe.pdf", _writer_bytes(writer))

    inventory = await extract_document_inventory(path, ".pdf")

    assert "Candidate PDF text" in inventory
    assert marker in inventory


async def test_pdf_inventory_accepts_embedded_unicode_truetype_and_russian_text(
    tmp_path,
):
    text = "Кандидат: Анна Иванова — Python"
    path = _write(
        tmp_path / "russian-profile.pdf",
        _writer_bytes(_unicode_text_pdf(text)),
    )

    inventory = await extract_document_inventory(path, ".pdf")

    assert text in inventory
    assert "Pilot Unicode" in inventory
    assert "Adobe-Identity-UCS" in inventory


@pytest.mark.parametrize(
    "case",
    [
        "vertical-encoding",
        "cmap-program",
        "invalid-font-program",
        "invalid-cid-map",
        "descriptor-extra",
    ],
)
async def test_pdf_inventory_rejects_unsupported_unicode_font_graph(
    tmp_path,
    case,
):
    writer = _unicode_text_pdf()
    _mutate_unicode_font_graph(writer, case)
    path = _write(
        tmp_path / f"unsafe-unicode-{case}.pdf",
        _writer_bytes(writer),
    )

    with pytest.raises(
        BoundedDocumentParseError,
        match="pdf_content_unsupported",
    ):
        await extract_document_inventory(path, ".pdf")


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("annotation", "pdf_interactive_unsupported"),
        ("acroform", "pdf_interactive_unsupported"),
        ("image", "pdf_xobject_unsupported"),
        ("empty", "pdf_inventory_empty"),
    ],
)
async def test_pdf_inventory_rejects_interactive_or_non_text_content(
    tmp_path,
    case,
    expected_code,
):
    if case == "empty":
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
    elif case == "image":
        image = io.BytesIO()
        Image.new("RGB", (20, 20), color="white").save(image, format="PNG")
        image.seek(0)
        source = FPDF()
        source.add_page()
        source.set_font("Helvetica", size=12)
        source.text(10, 20, "Generic body")
        source.image(image, x=10, y=30, w=20, h=20)
        reader = PdfReader(io.BytesIO(bytes(source.output())))
        writer = PdfWriter()
        writer.add_page(reader.pages[0])
    else:
        writer = _text_pdf()
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
        else:
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
    content = _writer_bytes(writer)
    if case == "annotation":
        reader = PdfReader(io.BytesIO(content))
        annotation = reader.pages[0]["/Annots"][0].get_object()
        assert annotation["/Contents"] == "candidate@example.test"
    elif case == "acroform":
        reader = PdfReader(io.BytesIO(content))
        field = reader.trailer["/Root"]["/AcroForm"]["/Fields"][0].get_object()
        assert field["/V"] == "candidate@example.test"
    path = _write(tmp_path / f"unsafe-{case}.pdf", content)

    with pytest.raises(BoundedDocumentParseError, match=expected_code):
        await extract_document_inventory(path, ".pdf")


@pytest.mark.parametrize(
    "case",
    ["custom-catalog", "unreferenced-object", "trailing-data"],
)
async def test_pdf_inventory_rejects_text_outside_accepted_object_graph(
    tmp_path,
    case,
):
    marker = f"graph-{uuid4().hex}@candidate.example"
    writer = _text_pdf()
    if case == "custom-catalog":
        writer._root_object[NameObject("/CandidateMarker")] = (
            TextStringObject(marker)
        )
    elif case == "unreferenced-object":
        reference = writer._add_object(TextStringObject(marker))
        assert writer.get_object(reference) == marker
    content = _writer_bytes(writer)
    if case == "trailing-data":
        content += f"\n{marker}\n".encode()
    path = _write(tmp_path / f"unsafe-{case}.pdf", content)

    with pytest.raises(
        BoundedDocumentParseError,
        match="pdf_content_unsupported",
    ):
        await extract_document_inventory(path, ".pdf")
