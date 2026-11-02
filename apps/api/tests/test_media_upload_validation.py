"""Structural recording validation rejects malformed and polyglot containers."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.storage import validation
from app.storage.validation import (
    UploadValidationError,
    validate_upload_bytes,
    validate_upload_path_bounded,
)


def _ogg_crc(content: bytes | bytearray) -> int:
    checksum = 0
    for byte in content:
        checksum ^= byte << 24
        for _bit in range(8):
            checksum = (
                ((checksum << 1) ^ 0x04C11DB7)
                if checksum & 0x80000000
                else checksum << 1
            ) & 0xFFFFFFFF
    return checksum


def _ogg_page(payload: bytes, *, sequence: int, flags: int) -> bytes:
    assert len(payload) < 255
    header = bytearray(
        b"OggS"
        + b"\x00"
        + bytes([flags])
        + (0).to_bytes(8, "little")
        + (7).to_bytes(4, "little")
        + sequence.to_bytes(4, "little")
        + b"\x00\x00\x00\x00"
        + b"\x01"
        + bytes([len(payload)])
    )
    page = header + payload
    page[22:26] = _ogg_crc(page).to_bytes(4, "little")
    return bytes(page)


def minimal_ogg() -> bytes:
    opus_head = b"OpusHead\x01\x01\x00\x00\x80\xbb\x00\x00\x00\x00\x00"
    opus_tags = b"OpusTags\x04\x00\x00\x00test\x00\x00\x00\x00"
    return b"".join(
        (
            _ogg_page(opus_head, sequence=0, flags=0x02),
            _ogg_page(opus_tags, sequence=1, flags=0),
            _ogg_page(b"\xf8\xff\xfe", sequence=2, flags=0x04),
        )
    )


def _ebml_size(value: int) -> bytes:
    for width in range(1, 9):
        if value < (1 << (7 * width)) - 1:
            return (value | (1 << (7 * width))).to_bytes(width, "big")
    raise ValueError("EBML fixture element is too large")


def _ebml(element_id: int, payload: bytes) -> bytes:
    encoded_id = element_id.to_bytes((element_id.bit_length() + 7) // 8, "big")
    return encoded_id + _ebml_size(len(payload)) + payload


def minimal_webm() -> bytes:
    header = _ebml(
        0x1A45DFA3,
        _ebml(0x4286, b"\x01")
        + _ebml(0x42F7, b"\x01")
        + _ebml(0x4282, b"webm")
        + _ebml(0x4287, b"\x04")
        + _ebml(0x4285, b"\x02"),
    )
    info = _ebml(0x1549A966, _ebml(0x2AD7B1, b"\x0fB@"))
    track_entry = _ebml(
        0xAE,
        _ebml(0xD7, b"\x01")
        + _ebml(0x73C5, b"\x01")
        + _ebml(0x83, b"\x02")
        + _ebml(0x86, b"A_OPUS")
        + _ebml(0x63A2, b"OpusHead\x01\x01\x00\x00\x80\xbb\x00\x00\x00\x00\x00")
        + _ebml(0xE1, _ebml(0x9F, b"\x01")),
    )
    tracks = _ebml(0x1654AE6B, track_entry)
    cluster = _ebml(
        0x1F43B675,
        _ebml(0xE7, b"\x00") + _ebml(0xA3, b"\x81\x00\x00\x80\xf8\xff\xfe"),
    )
    return header + _ebml(0x18538067, info + tracks + cluster)


def minimal_mp3(*, frames: int = 2) -> bytes:
    # MPEG-1 Layer III, 128 kbit/s, 44.1 kHz: 417 bytes per frame.
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    return frame * frames


RECORDINGS = {
    ".ogg": ("audio/ogg", minimal_ogg()),
    ".webm": ("video/webm", minimal_webm()),
    ".mp3": ("audio/mpeg", minimal_mp3()),
}


def _validate(suffix: str, content: bytes) -> str:
    media_type, _fixture = RECORDINGS[suffix]
    return validate_upload_bytes(
        filename=f"interview{suffix}",
        media_type=media_type,
        content=content,
        allowed_suffixes={suffix},
    )


@pytest.mark.parametrize("suffix", RECORDINGS)
def test_real_minimal_recording_containers_are_accepted(suffix: str):
    assert _validate(suffix, RECORDINGS[suffix][1]) == suffix


@pytest.mark.parametrize(
    ("suffix", "content"),
    [
        (".ogg", b"OggS\x00"),
        (".webm", b"\x1aE\xdf\xa3"),
        (".mp3", b"ID3\x04\x00\x00\x00\x00\x00\x00"),
        (".mp3", b"\xff\xfb\x90\x00"),
    ],
    ids=("ogg-prefix", "webm-prefix", "id3-only", "mp3-frame-prefix"),
)
def test_header_only_recordings_are_rejected(suffix: str, content: bytes):
    with pytest.raises(UploadValidationError):
        _validate(suffix, content)


@pytest.mark.parametrize("suffix", RECORDINGS)
@pytest.mark.parametrize(
    "payload",
    [b"MZ\x90\x00payload", b"PK\x03\x04archive", b"<!doctype html><html></html>"],
    ids=("pe", "zip", "html"),
)
def test_appended_recording_polyglots_are_rejected(suffix: str, payload: bytes):
    with pytest.raises(UploadValidationError):
        _validate(suffix, RECORDINGS[suffix][1] + payload)


@pytest.mark.parametrize(
    ("suffix", "content"),
    [
        (".ogg", minimal_ogg()[:-1]),
        (".ogg", minimal_ogg()[:-4] + b"bad!"),
        (".webm", minimal_webm()[:-1]),
        (".webm", minimal_webm() + _ebml(0x1941A469, b"attachment")),
        (".mp3", minimal_mp3()[:-1]),
        (".mp3", b"\xff\xfb\x00\x00" + b"\x00" * 413),
    ],
    ids=(
        "ogg-truncated",
        "ogg-bad-crc",
        "webm-truncated",
        "webm-attachments",
        "mp3-truncated",
        "mp3-free-bitrate",
    ),
)
def test_malformed_recording_structures_are_rejected(suffix: str, content: bytes):
    with pytest.raises(UploadValidationError):
        _validate(suffix, content)


@pytest.mark.parametrize(
    ("suffix", "limit_name", "limit", "content"),
    [
        (".ogg", "MAX_MEDIA_ELEMENTS", 2, minimal_ogg()),
        (".webm", "MAX_MEDIA_ELEMENTS", 2, minimal_webm()),
        (".mp3", "MAX_MP3_FRAMES", 1, minimal_mp3(frames=2)),
    ],
)
def test_recording_parser_limits_fail_closed(
    monkeypatch,
    suffix: str,
    limit_name: str,
    limit: int,
    content: bytes,
):
    monkeypatch.setattr(validation, limit_name, limit)
    with pytest.raises(UploadValidationError) as rejected:
        _validate(suffix, content)
    assert rejected.value.status_code == 413


@pytest.mark.parametrize("suffix", RECORDINGS)
async def test_recording_validation_remains_compatible_with_bounded_child(
    tmp_path: Path,
    suffix: str,
):
    media_type, content = RECORDINGS[suffix]
    path = tmp_path / f"interview{suffix}"
    path.write_bytes(content)
    assert (
        await validate_upload_path_bounded(
            path,
            filename=path.name,
            media_type=media_type,
            allowed_suffixes={suffix},
        )
        == suffix
    )
