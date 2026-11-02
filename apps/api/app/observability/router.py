"""Fail-closed operator endpoint for Prometheus exposition."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address, ip_network

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy import select

from app.db import SessionLocal
from app.models.operations import WorkerHeartbeat
from app.observability.metrics import (
    install_worker_telemetry_snapshots,
    mark_snapshot_failed,
    render_metrics,
    update_readiness_metrics,
)
from app.services.operations import readiness_components


def _bearer_token(request: Request) -> str | None:
    scheme, separator, token = request.headers.get("authorization", "").partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        return None
    return token


def _network_authorized(request: Request, configured: str) -> bool:
    if request.client is None:
        return False
    try:
        client = ip_address(request.client.host)
    except ValueError:
        return False
    return any(
        client in ip_network(value, strict=False)
        for value in configured.split(",")
        if value
    )


def _authorized(request: Request, settings) -> bool:
    configured_token = (
        settings.observability_metrics_bearer_token.get_secret_value()
        if settings.observability_metrics_bearer_token is not None
        else None
    )
    supplied_token = _bearer_token(request)
    token_allowed = (
        configured_token is not None
        and supplied_token is not None
        and secrets.compare_digest(configured_token, supplied_token)
    )
    return token_allowed or _network_authorized(
        request,
        settings.observability_metrics_allowed_cidrs,
    )


def build_observability_router(settings) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.get("/internal/metrics")
    async def metrics(request: Request) -> Response:
        if not settings.observability_metrics_enabled:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        if not _authorized(request, settings):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                {
                    "code": "metrics_access_denied",
                    "detail": "Operator metrics access denied",
                },
            )
        try:
            async with SessionLocal() as db:
                components = await readiness_components(db)
                update_readiness_metrics(components)
                freshness_seconds = int(
                    getattr(settings, "worker_freshness_seconds", 90)
                )
                heartbeat_metadata = list(
                    (
                        await db.execute(
                            select(WorkerHeartbeat.metadata_json).where(
                                WorkerHeartbeat.last_seen_at
                                >= datetime.now(UTC)
                                - timedelta(seconds=freshness_seconds)
                            )
                        )
                    ).scalars()
                )
                install_worker_telemetry_snapshots(
                    [
                        metadata.get("telemetry")
                        for metadata in heartbeat_metadata
                        if isinstance(metadata, dict)
                    ]
                )
        except Exception:  # noqa: BLE001 - a failed scrape must remain observable
            # The endpoint remains scrapeable during a dependency outage; the
            # failure is represented as a zero gauge without secret details.
            install_worker_telemetry_snapshots([])
            mark_snapshot_failed()
        return Response(
            content=render_metrics(),
            media_type="text/plain; version=0.0.4",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return router
