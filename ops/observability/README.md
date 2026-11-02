# HR Agent observability runbook

This stack exposes one operator-only Prometheus endpoint and optional OTLP/HTTP
traces. Neither surface contains request bodies, query strings, candidate data,
tenant IDs, object IDs, access tokens, provider payloads, or credential values.

## Required deployment configuration

`/internal/metrics` is disabled by default. Set
`OBSERVABILITY_METRICS_ENABLED=true`; the endpoint then permits only the socket
peer's IP against `OBSERVABILITY_METRICS_ALLOWED_CIDRS` or a constant-time
bearer-token match. Forwarded headers are intentionally ignored. For a
Prometheus deployment on another host or pod network:

1. Generate an independent random token of at least 32 characters and mount it
   into Prometheus at `/run/secrets/hr_agent_metrics_token`.
2. Set the same value as `OBSERVABILITY_METRICS_BEARER_TOKEN` on every API
   replica. Never reuse the JWT, MFA, integration, storage, or rate-limit key.
3. Set `OBSERVABILITY_METRICS_ALLOWED_CIDRS` to an empty value when token-only
   access is desired, or to the exact monitoring network. Do not use `0.0.0.0/0`
   or `::/0`.
4. Keep the metrics service on an internal listener/network. An ingress or BFF
   must not publish `/internal/metrics`.
5. Mount `prometheus.yml` and `hr-agent-alerts.yml` at the paths shown in the
   sample configuration. Mount `alertmanager.yml` in Alertmanager and place the
   pilot incident receiver URL (including any secret query token) in the
   mode-0400 file `/run/secrets/hr_agent_alert_webhook_url`. Prometheus sends
   firing and resolved notifications only through the internal Alertmanager
   target; never put receiver credentials in source-controlled YAML.

Set `OBSERVABILITY_METRICS_ENABLED=false` to remove the endpoint during an
emergency rollback. A disabled endpoint returns 404; an unauthorized endpoint
returns 403 and never falls through to tenant authentication.

## Reference topology

[`compose.reference.yml`](./compose.reference.yml) is a hardened deployment
reference for the monitoring tier. It uses version-and-manifest-digest pinned,
non-root images; read-only root filesystems; dropped capabilities; bounded
memory/PIDs; private service networks; persistent data volumes; and no
published host ports.

```text
API /internal/metrics -> Prometheus -> Alertmanager -> approved incident webhook
       OTLP/HTTPS      -> OTel Collector ---------> approved trace backend
operator auth proxy   -> Perses -----------------> Prometheus
```

The application network must be private and externally created. Attach the API
with DNS alias `api`, and attach an authenticated operator-access proxy to the
monitoring backend network created by Compose. Do not attach Perses,
Prometheus, or Alertmanager to a public ingress and do not publish their ports.
Perses native authentication is intentionally disabled because the reference
requires the deployment's operator proxy to enforce SSO/MFA and authorization;
without that proxy, the dashboard is not safe to expose.

Before rendering or starting the topology, create independent mode-0400 files
outside the repository and set these variables to their absolute paths:

- `HR_AGENT_METRICS_TOKEN_FILE`: the same independent 32+ character token the
  application receives from the deployment secret manager.
- `HR_AGENT_ALERT_WEBHOOK_URL_FILE`: the complete approved receiver URL. Any
  embedded receiver token stays only in this file.
- `HR_AGENT_OTEL_TRACE_EXPORTERS_FILE`: a private copy of
  `otel-trace-exporters.example.yml` containing the trace backend endpoint,
  TLS policy, and any authorization header.
- `HR_AGENT_OTEL_RECEIVER_CERTIFICATE_FILE` and
  `HR_AGENT_OTEL_RECEIVER_PRIVATE_KEY_FILE`: the Collector server certificate
  and key. The certificate must chain to a CA trusted by the API image and
  include `otel-collector` in its SANs.
- `HR_AGENT_OTEL_RECEIVER_BEARER_TOKENS_FILE`: one or more independent OTLP
  bearer tokens, one per line. Inject a matching `Authorization=Bearer ...`
  header into the API from the deployment secret manager.
- `HR_AGENT_PERSES_ENCRYPTION_KEY_FILE`: exactly 32 random printable bytes,
  with no trailing newline, used only by Perses.

Set `HR_AGENT_APPLICATION_OBSERVABILITY_NETWORK` to the private network name,
then validate before deployment:

```sh
docker compose -f ops/observability/compose.reference.yml config --quiet
docker compose -f ops/observability/compose.reference.yml up --detach
```

Configure the API trace endpoint as
`https://otel-collector:4318/v1/traces`. Notification and trace-export networks
provide egress, but the deployment firewall must restrict them to the exact
approved incident and trace destinations. The checked-in Collector deletes
common body, credential, query, database-statement, user, candidate, and tenant
attributes again before batching, providing a second privacy boundary after
application-side sanitization.

The provisioned operator UI is Perses, whose project is Apache-2.0 licensed,
as are Prometheus, Alertmanager, and the OpenTelemetry Collector. Grafana is
intentionally not part of the reference because its AGPL license conflicts
with this repository's current production dependency denylist. Substitution
requires an explicit legal/license-policy review; image updates require the
same digest, vulnerability, provenance, and SBOM review as application images.

## OpenTelemetry traces

Trace export remains a true no-op unless both
`OBSERVABILITY_OTLP_TRACES_ENDPOINT` and a positive
`OBSERVABILITY_TRACE_SAMPLE_RATIO` are set. Recommended pilot settings are a
0.05 sample ratio and an internal OpenTelemetry Collector receiving
`/v1/traces` over TLS. Optional application exporter headers use comma-separated
`name=value` pairs in the secret `OBSERVABILITY_OTLP_HEADERS`. Production HTTP
transport is rejected unless `OBSERVABILITY_OTLP_ALLOW_INSECURE=true` is an
explicit operator decision.

Only W3C `traceparent` is accepted for inbound parent correlation. Baggage is
not extracted. Request spans use the FastAPI route template, never the raw URL.
The server generates a new UUID `X-Request-ID` and ignores any caller-supplied
request ID. When tracing is sampled, `X-Trace-ID` is also returned.

Durable workers and provider adapters use `job_observation` and
`provider_observation` around actual work. UUID job IDs are available in log
and trace correlation only; metric labels are constrained to reviewed queue,
provider, operation, outcome, route, method, and status vocabularies. Unknown
values collapse to `other`, which both prevents cardinality attacks and gives
operators a signal that a new contract needs classification.

The dedicated worker publishes cumulative bounded job/provider counters,
latency buckets, and observed connection states inside its existing database
heartbeat. Each API scrape reads only fresh heartbeats and re-exposes those
aggregates as `hr_worker_*` metrics. No tenant or work identity is persisted in
the heartbeat telemetry. With multiple API replicas, dashboards and alerts use
`max` for identical worker-heartbeat series rather than summing replica copies.

## Verification and triage

From an authorized monitoring peer:

```sh
curl --fail --silent --show-error \
  --header "Authorization: Bearer $OBSERVABILITY_METRICS_BEARER_TOKEN" \
  http://api:8000/internal/metrics
```

Confirm `hr_observability_snapshot_success` and `hr_readiness_overall_ok` are
both `1`. The scrape refreshes the same database, migration, tenant-RLS,
private-storage, abuse-protection, worker-heartbeat, and queue checks used by
the authenticated `/enterprise/operations/readiness` endpoint; telemetry
cannot report a healthier state than readiness. Public `/health/ready` exposes
only the aggregate status and check time.

For an alert:

1. Use the request, trace, or durable-job correlation ID to locate sanitized
   structured logs and traces. Do not search by candidate data.
2. As an operator with `enterprise.manage`, check
   `/enterprise/operations/readiness` for the failing dependency and compare
   queue age/DLQ metrics with worker heartbeat coverage.
3. For provider failures, inspect the provider's normalized connection state,
   retry history, and connector health without copying payloads or credentials
   into the incident record.
4. Pause or replay durable work only through its idempotent operator controls.
   Do not mutate queue tables manually.
5. After remediation, require readiness and the adjacent alert expression to
   remain healthy for at least one full alert window.

The checked-in thresholds implement the private-pilot targets: 500 ms aggregate
API p95, five-minute queue warning, fifteen-minute queue critical, two-minute
worker loss, and repeated provider failure/degradation. Tune only with a linked
incident or capacity review; keep severity and runbook ownership explicit.

Before a release, run the CI gate or reproduce its pinned checks locally. The
rule test file covers all 17 checked-in alerts at the exact threshold/healthy
case, immediately before the `for` duration, and at the firing duration, with
the complete expected labels and annotations. The delivery test intentionally
takes a little over two minutes because it scrapes authenticated synthetic
metrics and waits for the unchanged production rule duration; it then recovers
the gauge and requires a resolved webhook notification. The Collector harness
uses an ephemeral certificate and token to prove that its pinned runtime
rejects unauthenticated OTLP/HTTPS and accepts the matching bearer credential.

```sh
docker run --rm --entrypoint /bin/promtool \
  --volume "$PWD/ops/observability:/rules:ro" \
  prom/prometheus:v3.12.0@sha256:69f5241418838263316593f7274a304b095c40bcf22e57272865da91bd60a8ac \
  check rules /rules/hr-agent-alerts.yml
docker run --rm --entrypoint /bin/promtool \
  --volume "$PWD/ops/observability:/rules:ro" --workdir /rules \
  prom/prometheus:v3.12.0@sha256:69f5241418838263316593f7274a304b095c40bcf22e57272865da91bd60a8ac \
  test rules hr-agent-alerts.test.yml
python3 ops/observability/test_dashboard_provisioning.py
python3 ops/observability/test_otel_collector.py
python3 ops/observability/test_alert_delivery.py
```
