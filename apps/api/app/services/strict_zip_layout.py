"""Reject unreferenced or metadata-bearing ZIP surfaces in strict OOXML files."""

from __future__ import annotations

import struct
import zipfile
import zlib
from pathlib import Path
from typing import Sequence

_LOCAL_FILE_HEADER = struct.Struct("<IHHHHHIIIHH")
_CENTRAL_DIRECTORY_HEADER = struct.Struct("<IHHHHHHIIIHHHHHII")
_END_OF_CENTRAL_DIRECTORY = struct.Struct("<IHHHHIIH")
_LOCAL_SIGNATURE = 0x04034B50
_CENTRAL_SIGNATURE = 0x02014B50
_END_SIGNATURE = 0x06054B50
_ALLOWED_FLAGS = frozenset({0, 0x0800})


class StrictZipLayoutError(ValueError):
    """The archive contains bytes outside its canonical bounded file graph."""


def _slice(raw: bytes, start: int, length: int) -> bytes:
    end = start + length
    if start < 0 or length < 0 or end > len(raw):
        raise StrictZipLayoutError("zip_layout_invalid")
    return raw[start:end]


def validate_strict_zip_layout(
    path: Path,
    archive: zipfile.ZipFile,
    entries: Sequence[zipfile.ZipInfo],
) -> None:
    """Require a gap-free, comment-free, extra-free non-ZIP64 archive.

    `zipfile` intentionally ignores prefixes, gaps, per-entry comments, extra
    fields, and some trailing records. Those bytes are an erasure blind spot in
    a privacy inventory, so strict OOXML accepts only the canonical local-file,
    central-directory, and EOCD records that correspond to returned entries.
    """

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise StrictZipLayoutError("zip_layout_unavailable") from exc
    if (
        not raw.startswith(b"PK\x03\x04")
        or archive.comment
        or not entries
        or any(
            entry.comment
            or entry.extra
            or entry.flag_bits not in _ALLOWED_FLAGS
            or entry.create_system not in {0, 3}
            for entry in entries
        )
    ):
        raise StrictZipLayoutError("zip_layout_invalid")

    ordered = sorted(entries, key=lambda item: item.header_offset)
    offset = 0
    for entry in ordered:
        if entry.header_offset != offset:
            raise StrictZipLayoutError("zip_layout_invalid")
        header = _LOCAL_FILE_HEADER.unpack(
            _slice(raw, entry.header_offset, _LOCAL_FILE_HEADER.size)
        )
        (
            signature,
            _version,
            flags,
            compression,
            _modified_time,
            _modified_date,
            crc,
            compressed_size,
            file_size,
            filename_length,
            extra_length,
        ) = header
        filename_start = entry.header_offset + _LOCAL_FILE_HEADER.size
        filename = _slice(raw, filename_start, filename_length)
        try:
            expected_filename = entry.filename.encode("ascii")
        except UnicodeEncodeError as exc:
            raise StrictZipLayoutError("zip_layout_invalid") from exc
        if (
            signature != _LOCAL_SIGNATURE
            or flags != entry.flag_bits
            or compression != entry.compress_type
            or crc != entry.CRC
            or compressed_size != entry.compress_size
            or file_size != entry.file_size
            or extra_length != 0
            or filename != expected_filename
        ):
            raise StrictZipLayoutError("zip_layout_invalid")
        data_start = filename_start + filename_length
        compressed = _slice(raw, data_start, entry.compress_size)
        try:
            if entry.compress_type == zipfile.ZIP_STORED:
                decoded = compressed
            elif entry.compress_type == zipfile.ZIP_DEFLATED:
                decompressor = zlib.decompressobj(-15)
                decoded = decompressor.decompress(compressed)
                decoded += decompressor.flush()
                if (
                    not decompressor.eof
                    or decompressor.unused_data
                    or decompressor.unconsumed_tail
                ):
                    raise StrictZipLayoutError("zip_layout_invalid")
            else:
                raise StrictZipLayoutError("zip_layout_invalid")
        except zlib.error as exc:
            raise StrictZipLayoutError("zip_layout_invalid") from exc
        if (
            len(decoded) != entry.file_size
            or zlib.crc32(decoded) & 0xFFFFFFFF != entry.CRC
        ):
            raise StrictZipLayoutError("zip_layout_invalid")
        offset = data_start + entry.compress_size
    if offset != archive.start_dir:
        raise StrictZipLayoutError("zip_layout_invalid")

    central_start = archive.start_dir
    offset = central_start
    for entry in entries:
        header = _CENTRAL_DIRECTORY_HEADER.unpack(
            _slice(raw, offset, _CENTRAL_DIRECTORY_HEADER.size)
        )
        (
            signature,
            _made_by,
            _needed,
            flags,
            compression,
            _modified_time,
            _modified_date,
            crc,
            compressed_size,
            file_size,
            filename_length,
            extra_length,
            comment_length,
            disk_start,
            _internal_attributes,
            _external_attributes,
            local_offset,
        ) = header
        filename_start = offset + _CENTRAL_DIRECTORY_HEADER.size
        filename = _slice(raw, filename_start, filename_length)
        if (
            signature != _CENTRAL_SIGNATURE
            or flags != entry.flag_bits
            or compression != entry.compress_type
            or crc != entry.CRC
            or compressed_size != entry.compress_size
            or file_size != entry.file_size
            or extra_length != 0
            or comment_length != 0
            or disk_start != 0
            or local_offset != entry.header_offset
            or filename != entry.filename.encode("ascii")
        ):
            raise StrictZipLayoutError("zip_layout_invalid")
        offset = filename_start + filename_length

    if offset + _END_OF_CENTRAL_DIRECTORY.size != len(raw):
        raise StrictZipLayoutError("zip_layout_invalid")
    (
        signature,
        disk_number,
        central_disk,
        entries_on_disk,
        total_entries,
        central_size,
        central_offset,
        comment_length,
    ) = _END_OF_CENTRAL_DIRECTORY.unpack(
        _slice(raw, offset, _END_OF_CENTRAL_DIRECTORY.size)
    )
    if (
        signature != _END_SIGNATURE
        or disk_number != 0
        or central_disk != 0
        or entries_on_disk != len(entries)
        or total_entries != len(entries)
        or central_size != offset - central_start
        or central_offset != central_start
        or comment_length != 0
    ):
        raise StrictZipLayoutError("zip_layout_invalid")
