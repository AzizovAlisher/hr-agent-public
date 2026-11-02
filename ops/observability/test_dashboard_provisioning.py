"""Start pinned Perses and prove the reviewed dashboard is provisioned."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4

PERSES_IMAGE = (
    "persesdev/perses:v0.54.0@"
    "sha256:a0e34ddaf9d7599d96036611af205b948c1179646844202d869f3bcf3d5d9e9c"
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _get_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=2) as response:
        return json.loads(response.read())


def _wait_for_health(port: int, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            health = _get_json(f"http://127.0.0.1:{port}/api/v1/health")
            if isinstance(health, dict) and health.get("database") is True:
                return
        except (OSError, ValueError, urllib.error.URLError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("Perses did not become healthy")
        time.sleep(0.25)


def _metadata_name(resource: Any) -> str | None:
    if not isinstance(resource, dict):
        return None
    metadata = resource.get("metadata")
    return metadata.get("name") if isinstance(metadata, dict) else None


def main() -> int:
    root = Path(__file__).resolve().parent
    port = _free_port()
    container = f"hr-agent-perses-config-test-{uuid4().hex[:10]}"
    uid = os.getuid()
    gid = os.getgid()
    try:
        with tempfile.TemporaryDirectory(prefix="hr-perses-gate-") as raw_temp:
            temp = Path(raw_temp)
            database = temp / "database"
            database.mkdir(mode=0o700)
            encryption_key = temp / "encryption-key"
            encryption_key.write_text(secrets.token_hex(16), encoding="utf-8")
            encryption_key.chmod(0o400)
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
                    f"127.0.0.1:{port}:8080",
                    "--volume",
                    f"{database}:/perses",
                    "--volume",
                    f"{root / 'perses.yml'}:/etc/perses/config.yaml:ro",
                    "--volume",
                    f"{root / 'perses-provisioning'}:/etc/perses/provisioning:ro",
                    "--volume",
                    f"{encryption_key}:/run/secrets/hr_agent_perses_encryption_key:ro",
                    PERSES_IMAGE,
                    "--config=/etc/perses/config.yaml",
                    "--log.level=warning",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            _wait_for_health(port)

            project = _get_json(f"http://127.0.0.1:{port}/api/v1/projects/hr-agent")
            datasource = _get_json(
                f"http://127.0.0.1:{port}/api/v1/projects/hr-agent/datasources/prometheus"
            )
            dashboard = _get_json(
                f"http://127.0.0.1:{port}/api/v1/projects/hr-agent/dashboards/operations"
            )
            if _metadata_name(project) != "hr-agent":
                raise RuntimeError("Perses project was not provisioned")
            if _metadata_name(datasource) != "prometheus":
                raise RuntimeError("Perses Prometheus datasource was not provisioned")
            if _metadata_name(dashboard) != "operations":
                raise RuntimeError("Perses operator dashboard was not provisioned")
            spec = dashboard.get("spec") if isinstance(dashboard, dict) else None
            panels = spec.get("panels") if isinstance(spec, dict) else None
            expected_panels = {
                "readiness",
                "serverErrorRate",
                "apiP95",
                "oldestQueueWork",
                "workerCoverage",
                "activeAlerts",
            }
            if not isinstance(panels, dict) or set(panels) != expected_panels:
                raise RuntimeError("Perses dashboard panel contract did not match")
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
    print("Pinned Perses project, datasource, and six-panel dashboard provisioning succeeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
