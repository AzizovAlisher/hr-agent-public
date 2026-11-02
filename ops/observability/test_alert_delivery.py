"""Exercise synthetic metrics through Prometheus, Alertmanager, and a webhook."""

from __future__ import annotations

import json
import os
import queue
import re
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

PROMETHEUS_IMAGE = (
    "prom/prometheus:v3.12.0@"
    "sha256:69f5241418838263316593f7274a304b095c40bcf22e57272865da91bd60a8ac"
)
ALERTMANAGER_IMAGE = (
    "prom/alertmanager:v0.32.1@"
    "sha256:51a825c2a40acc3e338fdd00d622e01ec090f72be2b3ea46be0839cd47a4d286"
)
ALERT_NAME = "HRAgentDependencySnapshotFailed"
EXPECTED_SUMMARY = "HR Agent could not refresh dependency telemetry"
EXPECTED_DESCRIPTION = (
    "The metrics endpoint is available, but its dependency snapshot failed closed."
)
EXPECTED_ALERT_COUNT = 17


class SyntheticMetrics:
    def __init__(self, bearer_token: str) -> None:
        self._bearer_token = bearer_token
        self._snapshot_success = 0
        self._lock = threading.Lock()
        self.authenticated_scrapes = 0

    def recover(self) -> None:
        with self._lock:
            self._snapshot_success = 1

    def render(self, authorization: str | None) -> bytes | None:
        if authorization != f"Bearer {self._bearer_token}":
            return None
        with self._lock:
            self.authenticated_scrapes += 1
            snapshot_success = self._snapshot_success
        return (
            "# TYPE hr_observability_snapshot_success gauge\n"
            f"hr_observability_snapshot_success {snapshot_success}\n"
            "# TYPE hr_readiness_overall_ok gauge\n"
            "hr_readiness_overall_ok 1\n"
            "# TYPE hr_malware_signature_age_seconds gauge\n"
            "hr_malware_signature_age_seconds 0\n"
            "# TYPE hr_worker_missing_queues gauge\n"
            "hr_worker_missing_queues 0\n"
            "# TYPE hr_worker_fresh_replicas gauge\n"
            "hr_worker_fresh_replicas 1\n"
        ).encode()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _request(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=2) as response:
        return response.read()


def _wait_ready(url: str, component: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            _request(url)
            return
        except (OSError, urllib.error.URLError):
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{component} did not become ready") from None
            time.sleep(0.25)


def _run_container(arguments: list[str]) -> None:
    subprocess.run(
        ["docker", "run", "--detach", *arguments],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def _container_logs(*containers: str) -> str:
    output: list[str] = []
    for container in containers:
        result = subprocess.run(
            ["docker", "logs", container],
            check=False,
            capture_output=True,
            text=True,
        )
        logs = (result.stdout + result.stderr).strip()
        if logs:
            output.append(f"{container}:\n{logs}")
    return "\n\n".join(output)


def _write(path: Path, contents: str, mode: int = 0o644) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(mode)


def _assert_rule_test_coverage(root: Path) -> None:
    rules = (root / "hr-agent-alerts.yml").read_text(encoding="utf-8")
    tests = (root / "hr-agent-alerts.test.yml").read_text(encoding="utf-8")
    rule_names = re.findall(r"^\s*- alert:\s+(\S+)\s*$", rules, re.MULTILINE)
    test_names = re.findall(r"^\s*alertname:\s+(\S+)\s*$", tests, re.MULTILINE)
    if len(rule_names) != EXPECTED_ALERT_COUNT or len(set(rule_names)) != len(rule_names):
        raise RuntimeError(
            f"expected {EXPECTED_ALERT_COUNT} unique checked-in alerts, found {len(rule_names)}"
        )
    missing = sorted(set(rule_names) - set(test_names))
    unexpected = sorted(set(test_names) - set(rule_names))
    under_tested = sorted(name for name in rule_names if test_names.count(name) < 2)
    if missing or unexpected or under_tested:
        raise RuntimeError(
            "alert test coverage mismatch: "
            f"missing={missing}, unexpected={unexpected}, fewer_than_two_assertions={under_tested}"
        )


def _matching_alert(delivery: Any, status: str) -> dict[str, Any] | None:
    if not isinstance(delivery, dict) or delivery.get("status") != status:
        return None
    alerts = delivery.get("alerts")
    if not isinstance(alerts, list):
        return None
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        labels = alert.get("labels")
        annotations = alert.get("annotations")
        if (
            alert.get("status") == status
            and isinstance(labels, dict)
            and labels.get("alertname") == ALERT_NAME
            and labels.get("severity") == "critical"
            and isinstance(annotations, dict)
            and annotations.get("summary") == EXPECTED_SUMMARY
            and annotations.get("description") == EXPECTED_DESCRIPTION
        ):
            return alert
    return None


def _await_delivery(
    deliveries: queue.Queue[dict[str, Any]], status: str, timeout: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    unmatched: list[dict[str, Any]] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                f"webhook did not receive a valid {status} notification for {ALERT_NAME}; "
                f"unmatched={json.dumps(unmatched[-3:], sort_keys=True)}"
            )
        try:
            delivery = deliveries.get(timeout=remaining)
        except queue.Empty as exc:
            raise RuntimeError(
                f"webhook did not receive a valid {status} notification for {ALERT_NAME}; "
                f"unmatched={json.dumps(unmatched[-3:], sort_keys=True)}"
            ) from exc
        alert = _matching_alert(delivery, status)
        if alert is not None:
            return alert
        unmatched.append(delivery)


def _wait_for_synthetic_target(port: int, timeout: float = 15) -> None:
    query = urllib.parse.quote('up{job="hr-agent-api"}')
    deadline = time.monotonic() + timeout
    last_payload: Any = None
    while True:
        try:
            last_payload = json.loads(
                _request(f"http://127.0.0.1:{port}/api/v1/query?query={query}")
            )
            results = (
                last_payload.get("data", {}).get("result", [])
                if isinstance(last_payload, dict)
                else []
            )
            if results and results[0].get("value", [None, None])[1] == "1":
                return
        except (OSError, ValueError, urllib.error.URLError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError(f"synthetic Prometheus target was not healthy: {last_payload}")
        time.sleep(0.25)


def main() -> int:
    root = Path(__file__).resolve().parent
    _assert_rule_test_coverage(root)

    bearer_token = secrets.token_urlsafe(32)
    webhook_path = f"/alerts/{secrets.token_urlsafe(24)}"
    metrics = SyntheticMetrics(bearer_token)
    deliveries: queue.Queue[dict[str, Any]] = queue.Queue()

    class MetricsReceiver(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/internal/metrics":
                self.send_response(404)
                self.end_headers()
                return
            body = metrics.render(self.headers.get("Authorization"))
            if body is None:
                self.send_response(403)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    class WebhookReceiver(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != webhook_path:
                self.send_response(404)
                self.end_headers()
                return
            try:
                declared_size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                declared_size = 0
            if declared_size <= 0 or declared_size > 1_000_000:
                self.send_response(413)
                self.end_headers()
                return
            try:
                payload = json.loads(self.rfile.read(declared_size))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_response(400)
                self.end_headers()
                return
            if isinstance(payload, dict):
                deliveries.put(payload)
            self.send_response(204)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    metrics_port = _free_port()
    webhook_port = _free_port()
    prometheus_port = _free_port()
    alertmanager_port = _free_port()
    metrics_server = ThreadingHTTPServer(("0.0.0.0", metrics_port), MetricsReceiver)
    webhook_server = ThreadingHTTPServer(("0.0.0.0", webhook_port), WebhookReceiver)
    server_threads = [
        threading.Thread(target=metrics_server.serve_forever, daemon=True),
        threading.Thread(target=webhook_server.serve_forever, daemon=True),
    ]
    for thread in server_threads:
        thread.start()

    suffix = uuid4().hex[:10]
    network = f"hr-agent-observability-test-{suffix}"
    prometheus_container = f"hr-agent-prometheus-test-{suffix}"
    alertmanager_container = f"hr-agent-alertmanager-test-{suffix}"
    uid = os.getuid()
    gid = os.getgid()
    error: BaseException | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="hr-observability-gate-") as raw_temp:
            temp = Path(raw_temp)
            prometheus_config = temp / "prometheus.yml"
            metrics_secret = temp / "metrics-token"
            webhook_secret = temp / "webhook-url"
            _write(
                prometheus_config,
                f"""global:
  scrape_interval: 1s
  evaluation_interval: 1s

rule_files:
  - /etc/prometheus/rules/hr-agent-alerts.yml

alerting:
  alertmanagers:
    - static_configs:
        - targets: [alertmanager:9093]

scrape_configs:
  - job_name: hr-agent-api
    metrics_path: /internal/metrics
    bearer_token_file: /run/secrets/hr_agent_metrics_token
    static_configs:
      - targets: [host.docker.internal:{metrics_port}]
        labels:
          service: hr-agent-api
          environment: synthetic-release-gate
""",
            )
            _write(metrics_secret, bearer_token + "\n", mode=0o400)
            _write(
                webhook_secret,
                f"http://host.docker.internal:{webhook_port}{webhook_path}\n",
                mode=0o400,
            )

            subprocess.run(["docker", "network", "create", network], check=True, stdout=subprocess.DEVNULL)
            common_security = [
                "--network",
                network,
                "--add-host",
                "host.docker.internal:host-gateway",
                "--user",
                f"{uid}:{gid}",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--read-only",
                "--tmpfs",
                f"/tmp:rw,noexec,nosuid,size=256m,uid={uid},gid={gid},mode=0700",
            ]
            _run_container(
                [
                    "--name",
                    alertmanager_container,
                    *common_security,
                    "--network-alias",
                    "alertmanager",
                    "--publish",
                    f"127.0.0.1:{alertmanager_port}:9093",
                    "--volume",
                    f"{root / 'alertmanager.yml'}:/etc/alertmanager/alertmanager.yml:ro",
                    "--volume",
                    f"{webhook_secret}:/run/secrets/hr_agent_alert_webhook_url:ro",
                    ALERTMANAGER_IMAGE,
                    "--config.file=/etc/alertmanager/alertmanager.yml",
                    "--storage.path=/tmp/alertmanager",
                ]
            )
            _wait_ready(
                f"http://127.0.0.1:{alertmanager_port}/-/ready", "Alertmanager"
            )
            _run_container(
                [
                    "--name",
                    prometheus_container,
                    *common_security,
                    "--publish",
                    f"127.0.0.1:{prometheus_port}:9090",
                    "--volume",
                    f"{prometheus_config}:/etc/prometheus/prometheus.yml:ro",
                    "--volume",
                    f"{root / 'hr-agent-alerts.yml'}:/etc/prometheus/rules/hr-agent-alerts.yml:ro",
                    "--volume",
                    f"{metrics_secret}:/run/secrets/hr_agent_metrics_token:ro",
                    PROMETHEUS_IMAGE,
                    "--config.file=/etc/prometheus/prometheus.yml",
                    "--storage.tsdb.path=/tmp/prometheus",
                ]
            )
            _wait_ready(f"http://127.0.0.1:{prometheus_port}/-/ready", "Prometheus")
            _wait_for_synthetic_target(prometheus_port)

            _await_delivery(deliveries, "firing", timeout=155)

            metrics.recover()
            resolved = _await_delivery(deliveries, "resolved", timeout=45)
            ends_at = resolved.get("endsAt")
            if not isinstance(ends_at, str) or not ends_at or ends_at.startswith("0001-"):
                raise RuntimeError("resolved webhook did not contain a real endsAt timestamp")
            if metrics.authenticated_scrapes < 2:
                raise RuntimeError("Prometheus did not perform repeated authenticated scrapes")
    except BaseException as exc:  # preserve diagnostics before the containers disappear
        error = exc
        logs = _container_logs(prometheus_container, alertmanager_container)
        if logs:
            print(logs)
    finally:
        for server in (metrics_server, webhook_server):
            server.shutdown()
            server.server_close()
        subprocess.run(
            ["docker", "rm", "--force", prometheus_container, alertmanager_container],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["docker", "network", "rm", network],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    if error is not None:
        raise error
    print(
        "Synthetic metrics -> Prometheus production rule -> Alertmanager -> "
        f"webhook firing/resolved succeeded ({metrics.authenticated_scrapes} authenticated scrapes)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
