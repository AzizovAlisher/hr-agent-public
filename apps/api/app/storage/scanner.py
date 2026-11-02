"""Malware scanner boundary with ClamAV and deterministic test backends."""

from __future__ import annotations

import asyncio
import re
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol


class MalwareDetectedError(RuntimeError):
    pass


class MalwareScannerUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ScanResult:
    engine: str
    result: str


@dataclass(frozen=True, slots=True)
class ClamAvVersion:
    engine_version: str
    signature_version: int
    signature_updated_at: datetime


class MalwareScanner(Protocol):
    async def scan(self, path: Path) -> ScanResult: ...


class SignatureScanner:
    """Deterministic local safety net, not a production antivirus engine.

    It recognizes the standardized EICAR test signature and the application's
    explicit test marker. Production configuration requires ClamAV.
    """

    _MARKERS = (
        b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE",
        b"HR_AGENT_TEST_MALWARE",
    )

    async def scan(self, path: Path) -> ScanResult:
        def _contains_marker() -> bool:
            carry = b""
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    sample = carry + chunk
                    if any(marker in sample for marker in self._MARKERS):
                        return True
                    carry = sample[-128:]
            return False

        if await asyncio.to_thread(_contains_marker):
            raise MalwareDetectedError("Upload was rejected by malware scanning")
        return ScanResult(engine="signature", result="clean")


class FakeMalwareScanner:
    """Injectable deterministic scanner for cleanup and failure tests."""

    def __init__(self, *, verdict: str = "clean"):
        if verdict not in {"clean", "infected", "unavailable"}:
            raise ValueError("Invalid fake scanner verdict")
        self.verdict = verdict
        self.scanned_paths: list[Path] = []

    async def scan(self, path: Path) -> ScanResult:
        self.scanned_paths.append(path)
        if self.verdict == "infected":
            raise MalwareDetectedError("Upload was rejected by malware scanning")
        if self.verdict == "unavailable":
            raise MalwareScannerUnavailableError("Malware scanning is unavailable")
        return ScanResult(engine="fake", result="clean")


class ClamAvScanner:
    """ClamAV INSTREAM client; raw file names and tenant data never leave."""

    def __init__(self, *, host: str, port: int, timeout_seconds: float = 30.0):
        self.host = host
        self.port = port
        self.timeout_seconds = timeout_seconds

    async def version(self) -> ClamAvVersion:
        """Return the engine and signature timestamp through the ClamAV protocol."""

        async def _run() -> ClamAvVersion:
            try:
                reader, writer = await asyncio.open_connection(self.host, self.port)
            except OSError as exc:
                raise MalwareScannerUnavailableError(
                    "Malware scanning is unavailable"
                ) from exc
            try:
                writer.write(b"zVERSION\0")
                await writer.drain()
                response = await reader.readuntil(b"\0")
            except (
                OSError,
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
            ) as exc:
                raise MalwareScannerUnavailableError(
                    "Malware scanning is unavailable"
                ) from exc
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
            message = response.rstrip(b"\0").decode("utf-8", errors="replace")
            match = re.fullmatch(
                r"ClamAV (?P<engine>[^/]{1,64})/"
                r"(?P<signature>\d{1,12})/"
                r"(?P<updated>[A-Za-z]{3} [A-Za-z]{3} +\d{1,2} "
                r"\d{2}:\d{2}:\d{2} \d{4})",
                message,
            )
            if match is None:
                raise MalwareScannerUnavailableError(
                    "Malware scanning is unavailable"
                )
            try:
                updated_at = datetime.strptime(
                    " ".join(match.group("updated").split()),
                    "%a %b %d %H:%M:%S %Y",
                ).replace(tzinfo=UTC)
                signature_version = int(match.group("signature"))
            except (ValueError, OverflowError) as exc:
                raise MalwareScannerUnavailableError(
                    "Malware scanning is unavailable"
                ) from exc
            return ClamAvVersion(
                engine_version=match.group("engine"),
                signature_version=signature_version,
                signature_updated_at=updated_at,
            )

        try:
            return await asyncio.wait_for(_run(), timeout=self.timeout_seconds)
        except TimeoutError as exc:
            raise MalwareScannerUnavailableError(
                "Malware scanning is unavailable"
            ) from exc

    async def scan(self, path: Path) -> ScanResult:
        async def _run() -> ScanResult:
            try:
                reader, writer = await asyncio.open_connection(self.host, self.port)
            except OSError as exc:
                raise MalwareScannerUnavailableError(
                    "Malware scanning is unavailable"
                ) from exc
            try:
                writer.write(b"zINSTREAM\0")
                await writer.drain()
                with path.open("rb") as stream:
                    while chunk := await asyncio.to_thread(stream.read, 1024 * 1024):
                        writer.write(struct.pack("!I", len(chunk)))
                        writer.write(chunk)
                        await writer.drain()
                writer.write(struct.pack("!I", 0))
                await writer.drain()
                response = await reader.readuntil(b"\0")
            except (
                OSError,
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
            ) as exc:
                raise MalwareScannerUnavailableError(
                    "Malware scanning is unavailable"
                ) from exc
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
            message = response.rstrip(b"\0").decode("utf-8", errors="replace")
            if message.endswith(" OK"):
                return ScanResult(engine="clamav", result="clean")
            if message.endswith(" FOUND"):
                raise MalwareDetectedError("Upload was rejected by malware scanning")
            raise MalwareScannerUnavailableError("Malware scanning is unavailable")

        try:
            return await asyncio.wait_for(_run(), timeout=self.timeout_seconds)
        except TimeoutError as exc:
            raise MalwareScannerUnavailableError(
                "Malware scanning is unavailable"
            ) from exc
