"""Regression coverage for capability credentials in HTTP request targets."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app.observability.middleware import ObservabilityMiddleware
from app.observability.telemetry import TelemetryRuntime

API_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = API_ROOT.parents[1]
DOCKERFILE = API_ROOT / "Dockerfile"
SUPPORTED_LAUNCH_FILES = (
    DOCKERFILE,
    REPOSITORY_ROOT / "README.md",
    REPOSITORY_ROOT / "DEMO_SCRIPT_RU.md",
)

CAPABILITY_REQUESTS = {
    "/auth/invitations/invitation-capability-credential": (
        "/auth/invitations/{token}"
    ),
    "/auth/password-reset/reset-capability-credential": (
        "/auth/password-reset/{token}"
    ),
    "/public/referrals/referral-capability-credential": (
        "/public/referrals/{token}"
    ),
    "/public/shortlists/shortlist-capability-credential": (
        "/public/shortlists/{token}"
    ),
    "/public/scheduling/scheduling-capability-credential": (
        "/public/scheduling/{token}"
    ),
    "/public/offers/offer-capability-credential": "/public/offers/{token}",
    "/public/assessments/assessment-capability-credential": (
        "/public/assessments/{token}"
    ),
    "/integrations/oauth/callback?state=oauth-state-capability-credential"
    "&code=oauth-code-capability-credential": "/integrations/oauth/callback",
}


def _unused_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_supported_uvicorn_commands_disable_generic_access_logging():
    commands = [
        (path, line.strip())
        for path in SUPPORTED_LAUNCH_FILES
        for line in path.read_text(encoding="utf-8").splitlines()
        if "uvicorn app.main:app" in line
    ]
    assert commands
    assert all("--no-access-log" in command for _path, command in commands), commands


def test_uvicorn_process_emits_no_capability_request_targets(tmp_path):

    probe_module = tmp_path / "access_log_probe.py"
    probe_module.write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/{path:path}')\n"
        "async def probe(path: str):\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    port = _unused_loopback_port()
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONUNBUFFERED": "1",
        "OPENROUTER_API_KEY": "",
        "VLLM_API_KEY": "",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "access_log_probe:app",
            "--app-dir",
            str(tmp_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output = ""
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
            deadline = time.monotonic() + 5
            while True:
                if process.poll() is not None:
                    pytest.fail("Uvicorn probe exited before accepting requests")
                try:
                    if client.get("/health", timeout=0.2).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if time.monotonic() >= deadline:
                    pytest.fail("Uvicorn probe did not become ready")
                time.sleep(0.05)

            for request_target in CAPABILITY_REQUESTS:
                assert client.get(request_target).status_code == 200
    finally:
        process.terminate()
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate(timeout=5)

    assert "Uvicorn running" in output
    assert "GET /" not in output
    assert "credential" not in output


@pytest.mark.asyncio
async def test_structured_http_telemetry_uses_templates_not_capability_values():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    runtime = TelemetryRuntime(
        tracer=provider.get_tracer("capability-access-log-test"),
        provider=provider,
    )
    mini = FastAPI()
    mini.add_middleware(ObservabilityMiddleware, runtime=runtime)

    async def capability_probe(token: str) -> dict[str, bool]:
        return {"ok": bool(token)}

    async def oauth_probe() -> dict[str, bool]:
        return {"ok": True}

    try:
        for route_template in set(CAPABILITY_REQUESTS.values()):
            endpoint = (
                oauth_probe
                if "oauth/callback" in route_template
                else capability_probe
            )
            mini.add_api_route(route_template, endpoint, methods=["GET"])

        transport = httpx.ASGITransport(app=mini)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            for request_target in CAPABILITY_REQUESTS:
                assert (await client.get(request_target)).status_code == 200

        spans = exporter.get_finished_spans()
        observed_routes = {
            span.attributes["http.route"]
            for span in spans
            if "http.route" in span.attributes
        }
        serialized_telemetry = repr(
            [(span.name, dict(span.attributes), tuple(span.events)) for span in spans]
        )
        assert observed_routes == set(CAPABILITY_REQUESTS.values())
        assert "credential" not in serialized_telemetry
        assert "oauth-state" not in serialized_telemetry
        assert "oauth-code" not in serialized_telemetry
    finally:
        runtime.shutdown()
