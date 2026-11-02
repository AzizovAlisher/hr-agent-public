"""Semantic PDF intake checks that distinguish objects from stream bytes."""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from app.storage.validation import (
    UploadValidationError,
    validate_upload_bytes,
    validate_upload_path_bounded,
)

PdfMutation = Callable[[PdfWriter, DictionaryObject], None]


def _pdf_bytes(mutate: PdfMutation | None = None) -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    if mutate is not None:
        mutate(writer, page)
    writer.write(output)
    return output.getvalue()


def _validate(content: bytes) -> str:
    return validate_upload_bytes(
        filename="resume.pdf",
        media_type="application/pdf",
        content=content,
        allowed_suffixes={".pdf"},
    )


def _local_destination(page: DictionaryObject) -> ArrayObject:
    assert page.indirect_reference is not None
    return ArrayObject([page.indirect_reference, NameObject("/Fit")])


def _add_c2pa_manifest(
    writer: PdfWriter,
    *,
    manifest: bytes | None = None,
    relationship: str = "/C2PA_Manifest",
    extra_spec_key: str | None = None,
) -> None:
    body = b"\x00\x00\x00\x0cjumdc2pa"
    payload = manifest or (8 + len(body)).to_bytes(4, "big") + b"jumb" + body
    stream = DecodedStreamObject()
    stream.set_data(payload)
    embedded = DictionaryObject(
        {NameObject("/F"): writer._add_object(stream)}
    )
    spec = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/FileSpec"),
            NameObject("/F"): TextStringObject("Content Credentials"),
            NameObject("/UF"): TextStringObject("Content Credentials"),
            NameObject("/Desc"): TextStringObject("Content Credentials"),
            NameObject("/Subtype"): TextStringObject("application/c2pa"),
            NameObject("/AFRelationship"): NameObject(relationship),
            NameObject("/EF"): embedded,
        }
    )
    if extra_spec_key is not None:
        spec[NameObject(extra_spec_key)] = TextStringObject("payload")
    spec_reference = writer._add_object(spec)
    writer.root_object[NameObject("/AF")] = ArrayObject([spec_reference])
    writer.root_object[NameObject("/Names")] = DictionaryObject(
        {
            NameObject("/EmbeddedFiles"): DictionaryObject(
                {
                    NameObject("/Names"): ArrayObject(
                        [TextStringObject("Content Credentials"), spec_reference]
                    )
                }
            )
        }
    )


def test_incidental_active_markers_and_binary_magic_inside_font_stream_are_safe():
    markers = b"\x00MZ PK\x03\x04 \x7fELF /AA /JS /OpenAction /EmbeddedFile"

    def add_font_stream(writer: PdfWriter, page: DictionaryObject) -> None:
        font_program = DecodedStreamObject()
        font_program.set_data(markers)
        font_program[NameObject("/Subtype")] = NameObject("/Type1C")
        descriptor = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/FontDescriptor"),
                NameObject("/FontName"): NameObject("/LegacyFont"),
                NameObject("/Flags"): NumberObject(4),
                NameObject("/FontFile3"): writer._add_object(font_program),
            }
        )
        page[NameObject("/LegacyFontDescriptor")] = writer._add_object(descriptor)

    content = _pdf_bytes(add_font_stream)
    assert all(marker in content for marker in (b"MZ", b"/AA", b"/JS"))
    assert _validate(content) == ".pdf"


def test_safe_local_open_destinations_and_user_initiated_web_links_are_allowed():
    def add_destinations(writer: PdfWriter, page: DictionaryObject) -> None:
        destination = _local_destination(page)
        writer.root_object[NameObject("/OpenAction")] = destination
        page[NameObject("/Annots")] = ArrayObject(
            [
                DictionaryObject(
                    {
                        NameObject("/Type"): NameObject("/Annot"),
                        NameObject("/Subtype"): NameObject("/Link"),
                        NameObject("/A"): DictionaryObject(
                            {
                                NameObject("/S"): NameObject("/URI"),
                                NameObject("/URI"): TextStringObject(
                                    "https://example.test/profile"
                                ),
                            }
                        ),
                    }
                )
            ]
        )

    assert _validate(_pdf_bytes(add_destinations)) == ".pdf"


def test_safe_local_goto_open_action_is_allowed():
    def add_goto(writer: PdfWriter, page: DictionaryObject) -> None:
        writer.root_object[NameObject("/OpenAction")] = DictionaryObject(
            {
                NameObject("/S"): NameObject("/GoTo"),
                NameObject("/D"): _local_destination(page),
            }
        )

    assert _validate(_pdf_bytes(add_goto)) == ".pdf"


async def test_passive_c2pa_content_credentials_manifest_is_allowed(
    tmp_path: Path,
):
    def add_manifest(writer: PdfWriter, _page: DictionaryObject) -> None:
        _add_c2pa_manifest(writer)

    content = _pdf_bytes(add_manifest)
    assert _validate(content) == ".pdf"
    path = tmp_path / "Сайидазизхон Абдуллаев - CV.pdf"
    path.write_bytes(content)
    assert (
        await validate_upload_path_bounded(
            path,
            filename=path.name,
            media_type="application/pdf",
            allowed_suffixes={".pdf"},
        )
        == ".pdf"
    )


@pytest.mark.parametrize(
    ("manifest", "relationship", "extra_spec_key"),
    [
        (b"not-a-jumbf-manifest", "/C2PA_Manifest", None),
        (None, "/Data", None),
        (None, "/C2PA_Manifest", "/A"),
    ],
    ids=["invalid-jumbf", "wrong-relationship", "extra-action-surface"],
)
def test_malformed_or_active_c2pa_attachment_shapes_are_rejected(
    manifest: bytes | None,
    relationship: str,
    extra_spec_key: str | None,
):
    def add_manifest(writer: PdfWriter, _page: DictionaryObject) -> None:
        _add_c2pa_manifest(
            writer,
            manifest=manifest,
            relationship=relationship,
            extra_spec_key=extra_spec_key,
        )

    with pytest.raises(UploadValidationError, match="Active or embedded"):
        _validate(_pdf_bytes(add_manifest))


@pytest.mark.parametrize(
    "action_type",
    ["/GoTo3DView", "/JavaScript", "/Launch", "/ResetForm", "/SubmitForm"],
)
def test_executable_or_mutating_actions_are_rejected(action_type: str):
    def add_action(_writer: PdfWriter, page: DictionaryObject) -> None:
        action = DictionaryObject({NameObject("/S"): NameObject(action_type)})
        if action_type == "/JavaScript":
            action[NameObject("/JS")] = TextStringObject("app.alert('x')")
        else:
            action[NameObject("/F")] = TextStringObject("payload.exe")
        page[NameObject("/A")] = action

    with pytest.raises(UploadValidationError, match="Active or embedded"):
        _validate(_pdf_bytes(add_action))


def test_automatic_uri_and_unsafe_uri_schemes_are_rejected():
    def automatic_uri(writer: PdfWriter, _page: DictionaryObject) -> None:
        writer.root_object[NameObject("/OpenAction")] = DictionaryObject(
            {
                NameObject("/S"): NameObject("/URI"),
                NameObject("/URI"): TextStringObject("https://example.test/track"),
            }
        )

    def javascript_link(_writer: PdfWriter, page: DictionaryObject) -> None:
        page[NameObject("/A")] = DictionaryObject(
            {
                NameObject("/S"): NameObject("/URI"),
                NameObject("/URI"): TextStringObject("javascript:alert(1)"),
            }
        )

    def custom_protocol(_writer: PdfWriter, page: DictionaryObject) -> None:
        page[NameObject("/A")] = DictionaryObject(
            {
                NameObject("/S"): NameObject("/URI"),
                NameObject("/URI"): TextStringObject("ms-msdt:/id PCWDiagnostic"),
            }
        )

    def internal_protocol(_writer: PdfWriter, page: DictionaryObject) -> None:
        page[NameObject("/A")] = DictionaryObject(
            {
                NameObject("/S"): NameObject("/URI"),
                NameObject("/URI"): TextStringObject(
                    "internal:diagnostic/candidate-export"
                ),
            }
        )

    for mutation in (
        automatic_uri,
        javascript_link,
        custom_protocol,
        internal_protocol,
    ):
        with pytest.raises(UploadValidationError, match="Active or embedded"):
            _validate(_pdf_bytes(mutation))


@pytest.mark.parametrize("action_key", ["/A", "/Next"])
def test_unknown_viewer_action_types_fail_closed(action_key: str):
    def add_vendor_action(_writer: PdfWriter, page: DictionaryObject) -> None:
        page[NameObject(action_key)] = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Action"),
                NameObject("/S"): NameObject("/VendorExecute"),
            }
        )

    with pytest.raises(UploadValidationError, match="Active or embedded"):
        _validate(_pdf_bytes(add_vendor_action))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda writer, _page: writer.root_object.__setitem__(
            NameObject("/AA"), DictionaryObject()
        ),
        lambda writer, _page: writer.root_object.__setitem__(
            NameObject("/AcroForm"),
            DictionaryObject({NameObject("/XFA"): TextStringObject("payload")}),
        ),
        lambda writer, _page: writer.root_object.__setitem__(
            NameObject("/Names"),
            DictionaryObject({NameObject("/EmbeddedFiles"): DictionaryObject()}),
        ),
        lambda _writer, page: page.__setitem__(
            NameObject("/Annots"),
            ArrayObject(
                [
                    DictionaryObject(
                        {
                            NameObject("/Type"): NameObject("/Annot"),
                            NameObject("/Subtype"): NameObject("/FileAttachment"),
                        }
                    )
                ]
            ),
        ),
        lambda _writer, page: page.__setitem__(
            NameObject("/Annots"),
            ArrayObject(
                [
                    DictionaryObject(
                        {
                            NameObject("/Type"): NameObject("/Annot"),
                            NameObject("/Subtype"): NameObject("/RichMedia"),
                        }
                    )
                ]
            ),
        ),
    ],
    ids=["additional-actions", "xfa", "embedded-file", "file-attachment", "rich-media"],
)
def test_interactive_and_embedded_surfaces_are_rejected(mutation: PdfMutation):
    with pytest.raises(UploadValidationError, match="Active or embedded"):
        _validate(_pdf_bytes(mutation))


@pytest.mark.parametrize(
    "payload",
    [b"PK\x03\x04hidden.zip", b"MZhidden.exe", b"\x7fELFpayload", b"trailing junk"],
    ids=["zip", "pe", "elf", "generic"],
)
def test_appended_payloads_after_eof_are_rejected(payload: bytes):
    with pytest.raises(UploadValidationError):
        _validate(_pdf_bytes() + payload)


def test_fake_eof_cannot_hide_an_appended_executable():
    content = _pdf_bytes()
    xref_offset = int(content.rsplit(b"startxref", 1)[1].split(b"%%EOF", 1)[0])
    forged = (
        content
        + b"MZ-appended-payload\nstartxref\n"
        + str(xref_offset).encode("ascii")
        + b"\n%%EOF\n"
    )
    with pytest.raises(UploadValidationError):
        _validate(forged)


async def test_semantic_validation_remains_compatible_with_bounded_child(
    tmp_path: Path,
):
    path = tmp_path / "resume.pdf"

    def add_marker_stream(writer: PdfWriter, page: DictionaryObject) -> None:
        stream = DecodedStreamObject()
        stream.set_data(b"font data MZ /AA /JS")
        page[NameObject("/LegacyFontData")] = writer._add_object(stream)

    path.write_bytes(_pdf_bytes(add_marker_stream))
    assert (
        await validate_upload_path_bounded(
            path,
            filename="resume.pdf",
            media_type="application/pdf",
            allowed_suffixes={".pdf"},
        )
        == ".pdf"
    )
