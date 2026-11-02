"""Narrow public-branding redirect for S3-backed deployments."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote
from uuid import UUID

from starlette.responses import PlainTextResponse, RedirectResponse

from app.storage.base import validate_blob_key


_BRANDING_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def _branding_key(path: str) -> str | None:
    """Accept only generated versioned keys plus one-release legacy keys."""

    relative = path.lstrip("/")
    try:
        key = validate_blob_key(f"logos/{relative}")
        parts = PurePosixPath(relative).parts
        if len(parts) == 2:
            UUID(parts[0])
            UUID(PurePosixPath(parts[1]).stem)
            suffix = PurePosixPath(parts[1]).suffix.casefold()
        elif len(parts) == 1:  # Legacy ``logos/<organization-id>.<ext>``.
            UUID(PurePosixPath(parts[0]).stem)
            suffix = PurePosixPath(parts[0]).suffix.casefold()
        else:
            return None
        if suffix not in _BRANDING_SUFFIXES:
            return None
    except (TypeError, ValueError):
        return None
    return key


class PublicBrandingRedirectApp:
    """Redirect the historical API branding path to the public S3/CDN root."""

    def __init__(self, public_base_url: str | None) -> None:
        self.public_base_url = (
            public_base_url.rstrip("/") if public_base_url is not None else None
        )

    async def __call__(self, scope: dict[str, Any], receive, send) -> None:
        if scope.get("type") != "http":
            await PlainTextResponse("Not found", status_code=404)(scope, receive, send)
            return
        if scope.get("method") not in {"GET", "HEAD"}:
            await PlainTextResponse(
                "Method not allowed",
                status_code=405,
                headers={"Allow": "GET, HEAD", "Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        request_path = str(scope.get("path") or "")
        root_path = str(scope.get("root_path") or "")
        if root_path and request_path.startswith(root_path):
            request_path = request_path[len(root_path) :]
        key = _branding_key(request_path)
        if key is None or self.public_base_url is None:
            await PlainTextResponse(
                "Not found",
                status_code=404,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        await RedirectResponse(
            f"{self.public_base_url}/{quote(key, safe='/-_.~')}",
            status_code=307,
            headers={
                "Cache-Control": "public, max-age=300",
                "X-Content-Type-Options": "nosniff",
            },
        )(scope, receive, send)


__all__ = ["PublicBrandingRedirectApp"]
