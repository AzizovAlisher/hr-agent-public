"""Start the pinned Collector and prove TLS plus bearer-authenticated OTLP."""

from __future__ import annotations

import os
import secrets
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

COLLECTOR_IMAGE = (
    "otel/opentelemetry-collector-contrib:0.159.0@"
    "sha256:1f2c54a30e713fac6b3ae77a1ec84010c2007e29ced8ec666214fc2f6739c1cc"
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_health(port: int, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("OpenTelemetry Collector did not become healthy")
        time.sleep(0.25)


def _post_otlp(port: int, token: str | None) -> int:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"https://127.0.0.1:{port}/v1/traces",
        data=b'{"resourceSpans":[]}',
        headers=headers,
        method="POST",
    )
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(request, timeout=3, context=context) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def main() -> int:
    root = Path(__file__).resolve().parent
    health_port = _free_port()
    otlp_port = _free_port()
    container = f"hr-agent-otel-config-test-{uuid4().hex[:10]}"
    uid = os.getuid()
    gid = os.getgid()
    try:
        with tempfile.TemporaryDirectory(prefix="hr-otel-gate-") as raw_temp:
            temp = Path(raw_temp)
            certificate = temp / "collector.crt"
            private_key = temp / "collector.key"
            bearer_tokens = temp / "bearer-tokens"
            exporter_mapping = temp / "exporters.yml"
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "1",
                    "-subj",
                    "/CN=otel-collector",
                    "-addext",
                    "subjectAltName=DNS:otel-collector,IP:127.0.0.1",
                    "-keyout",
                    str(private_key),
                    "-out",
                    str(certificate),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            token = secrets.token_urlsafe(32)
            bearer_tokens.write_text(token + "\n", encoding="utf-8")
            exporter_mapping.write_text(
                (root / "otel-trace-exporters.example.yml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            private_key.chmod(0o400)
            certificate.chmod(0o400)
            bearer_tokens.chmod(0o400)
            exporter_mapping.chmod(0o400)

            subprocess.run(
                [
                    "docker",
                    "run",
                    "--detach",
                    "--name",
                    container,
                    "--user",
                    f"{uid}:{gid}",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--read-only",
                    "--tmpfs",
                    f"/tmp:rw,noexec,nosuid,size=64m,uid={uid},gid={gid},mode=0700",
                    "--publish",
                    f"127.0.0.1:{health_port}:13133",
                    "--publish",
                    f"127.0.0.1:{otlp_port}:4318",
                    "--volume",
                    f"{root / 'otel-collector.yml'}:/etc/otelcol-contrib/config.yaml:ro",
                    "--volume",
                    f"{exporter_mapping}:/run/secrets/hr_agent_otel_trace_exporters:ro",
                    "--volume",
                    f"{certificate}:/run/secrets/hr_agent_otel_receiver_certificate:ro",
                    "--volume",
                    f"{private_key}:/run/secrets/hr_agent_otel_receiver_private_key:ro",
                    "--volume",
                    f"{bearer_tokens}:/run/secrets/hr_agent_otel_receiver_bearer_tokens:ro",
                    COLLECTOR_IMAGE,
                    "--config=/etc/otelcol-contrib/config.yaml",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            _wait_for_health(health_port)
            unauthorized_status = _post_otlp(otlp_port, None)
            authorized_status = _post_otlp(otlp_port, token)
            if unauthorized_status not in {401, 403}:
                raise RuntimeError(
                    f"unauthenticated OTLP was not rejected: HTTP {unauthorized_status}"
                )
            if authorized_status != 200:
                raise RuntimeError(
                    f"authenticated OTLP was not accepted: HTTP {authorized_status}"
                )
    except BaseException:
        logs = subprocess.run(
            ["docker", "logs", container],
            check=False,
            capture_output=True,
            text=True,
        )
        if logs.stdout or logs.stderr:
            print((logs.stdout + logs.stderr).strip())
        raise
    finally:
        subprocess.run(
            ["docker", "rm", "--force", container],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    print("Pinned Collector TLS and bearer-authenticated OTLP receiver succeeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
