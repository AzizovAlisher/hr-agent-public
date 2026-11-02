import base64
import binascii
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/recruitment_v2"
    )
    # Central audit writes must complete before sensitive response bodies are
    # released. They use a small reserved pool so request-scoped transactions
    # cannot exhaust every connection and then deadlock while waiting for an
    # audit write. The reservation is deliberately bounded per API replica.
    # Candidate/requisition workspaces intentionally fan out many independent
    # sensitive reads. Eight audit slots drain that bounded browser burst even
    # when forced-RLS actor binding is comparatively slow, while the engine's
    # max_overflow=0 keeps the per-replica database budget deterministic.
    audit_database_pool_size: int = Field(default=8, ge=1, le=8)
    audit_database_pool_timeout_seconds: float = Field(
        default=5.0,
        ge=1.0,
        le=30.0,
    )
    app_environment: Literal["development", "test", "production"] = "development"
    # Forced mode requires the d9 RLS/bootstrap migration and a non-owner,
    # NOBYPASSRLS runtime role. Explicit off exists only for local metadata-
    # created tests and single-process development.
    tenant_rls_mode: Literal["forced", "off"] = "off"

    web_origin: str = "http://localhost:3000"
    # Calendar-date features use one explicit business timezone so API filters
    # and the browser agree around midnight.  This becomes organization-level
    # configuration when enterprise settings are introduced.
    business_timezone: str = "Asia/Dushanbe"

    jwt_secret: str = "dev-jwt-secret-change-me"
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    jwt_issuer: str = "hr-agent-api"
    jwt_audience: str = "hr-agent-web"
    # Access tokens are intentionally short lived. Session continuation should
    # eventually use rotating refresh tokens rather than widening this window.
    jwt_expires_minutes: int = Field(default=15, ge=5, le=60)

    auth_login_max_failures: int = Field(default=5, ge=3, le=20)
    auth_login_window_seconds: int = Field(default=900, ge=60, le=3600)
    auth_login_lockout_seconds: int = Field(default=900, ge=60, le=86400)
    auth_refresh_expires_days: int = Field(default=14, ge=1, le=90)
    # A successful rotation may be replayed briefly by another BFF replica
    # handling the same browser cookie. The database row lock and deterministic
    # successor make that retry idempotent; later reuse remains an incident.
    auth_refresh_retry_grace_seconds: int = Field(default=10, ge=1, le=30)
    auth_mfa_challenge_minutes: int = Field(default=5, ge=1, le=15)
    auth_mfa_issuer: str = Field(default="HR Agent", min_length=1, max_length=64)
    # Production must supply an independent value. Development/test derive a
    # purpose-separated key from JWT_SECRET so local setup stays frictionless.
    auth_mfa_encryption_key: str | None = None
    # Explicit local-review escape hatch. It is accepted only for a loopback
    # development origin backed by a database whose name ends in ``_preview``.
    # Production, test suites, ordinary development databases, and every other
    # administrator continue to require MFA.
    auth_local_preview_admin_email: str | None = None

    # Public capability and intake endpoints are protected before routing or
    # multipart parsing. Development/test can use the process-local backend;
    # every production replica must share the configured Valkey instance.
    public_rate_limit_backend: Literal["memory", "valkey"] = "memory"
    public_rate_limit_valkey_url: str | None = None
    public_rate_limit_hmac_secret: SecretStr | None = None
    public_rate_limit_trusted_proxy_cidrs: str = ""
    public_rate_limit_read_requests: int = Field(default=240, ge=10, le=10_000)
    public_rate_limit_read_window_seconds: int = Field(default=60, ge=10, le=3600)
    public_rate_limit_mutation_requests: int = Field(default=60, ge=5, le=1_000)
    public_rate_limit_mutation_window_seconds: int = Field(default=60, ge=10, le=3600)
    public_rate_limit_expensive_requests: int = Field(default=10, ge=1, le=100)
    public_rate_limit_expensive_window_seconds: int = Field(
        default=600, ge=60, le=86_400
    )

    llm_provider: str = "openrouter"
    openrouter_api_key: str | None = None
    openrouter_default_model: str = "google/gemini-2.5-flash"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_default_max_output_tokens: int = Field(default=4096, ge=64, le=32768)
    llm_http_timeout_seconds: float = Field(default=90.0, ge=5.0, le=240.0)
    # OpenRouter's max_price values are USD per million tokens. The gateway
    # uses the same ceilings to reserve a conservative microusd upper bound.
    openrouter_max_prompt_usd_per_million: float = Field(
        default=5.0, ge=0.0, le=1_000.0
    )
    openrouter_max_completion_usd_per_million: float = Field(
        default=20.0, ge=0.0, le=1_000.0
    )
    openrouter_require_zdr: bool = True
    # Comma-separated exact base URLs (scheme, explicit port, and path) that
    # may resolve to private networks for approved on-premise deployments.
    # Host-only entries are intentionally rejected because they authorize an
    # unintended HTTP downgrade and every service port on that host.
    llm_private_base_url_allowlist: str = ""
    # Private providers do not report billable currency. Positive per-mission
    # currency budgets are therefore enabled only when the operator supplies
    # both conservative USD-per-million-token accounting rates.
    llm_private_prompt_usd_per_million: float | None = Field(
        default=None, gt=0.0, le=1_000.0
    )
    llm_private_completion_usd_per_million: float | None = Field(
        default=None, gt=0.0, le=1_000.0
    )

    # Provider secrets use a purpose-specific random 256-bit AES-GCM key. The
    # value must never be derived from or reused as an auth/JWT secret.
    integration_credential_encryption_key: str | None = None
    integration_credential_key_id: str | None = Field(
        default=None, min_length=1, max_length=64
    )
    integration_oauth_redirect_origins: str = ""
    integration_outbound_host_allowlist: str = ""
    integration_http_timeout_seconds: float = Field(default=15.0, ge=2.0, le=60.0)
    hh_user_agent: str = "HR-Agent-Pilot/1.0 (integration-contact-not-configured)"

    # GitHub enrichment. Token is optional — public profile/repo reads work
    # unauthenticated but at 60 req/hr; with a token the limit is 5000 req/hr.
    github_token: str | None = None
    github_api_base: str = "https://api.github.com"

    storage_dir: str = "./storage"
    # Read-only compatibility mount for files created before the private-object
    # registry. Production never falls back to STORAGE_DIR: the worker must be
    # pointed at the explicit legacy volume before it can settle old paths.
    legacy_private_storage_dir: str | None = None
    blob_store_backend: Literal["local", "s3"] = "local"
    s3_endpoint: str | None = None
    s3_bucket: str | None = None
    # Public branding is deliberately stored in a different bucket. The
    # private bucket must never need an anonymous bucket policy.
    s3_public_bucket: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: SecretStr | None = None
    s3_region: str = "us-east-1"
    s3_public_base_url: str | None = None
    s3_timeout_seconds: float = Field(default=30.0, ge=2.0, le=120.0)
    malware_scanner_backend: Literal["signature", "clamav"] = "signature"
    clamav_host: str = "127.0.0.1"
    clamav_port: int = Field(default=3310, ge=1, le=65535)
    clamav_timeout_seconds: float = Field(default=30.0, ge=2.0, le=120.0)
    # The Prometheus release-gate rule is intentionally fixed at the same
    # 48-hour threshold. Keeping this immutable prevents readiness and alerting
    # from silently disagreeing because of an environment override.
    clamav_max_signature_age_hours: Literal[48] = 48
    # This ceiling is deliberately above the 15 MiB document limit so normal
    # multipart framing fits, while still stopping request spooling before an
    # endpoint sees an unbounded body. Endpoints retain their lower limits.
    request_body_max_bytes: int = Field(
        default=16 * 1024 * 1024,
        ge=15 * 1024 * 1024 + 64 * 1024,
        le=32 * 1024 * 1024,
    )
    storage_reconciliation_stale_hours: int = Field(default=24, ge=1, le=168)
    storage_reconciliation_scan_limit: int = Field(default=1000, ge=1, le=5000)

    # Local communications queue. These are operational knobs only; provider
    # credentials are intentionally not accepted through application settings.
    communications_worker_enabled: bool = True
    communications_worker_poll_seconds: float = 5.0
    communications_claim_limit: int = 50
    communications_max_bulk_candidates: int = 100

    # Provider jobs and signed webhook deliveries run only in the dedicated
    # worker process; API replicas enqueue and inspect durable records.
    integration_worker_enabled: bool = True
    integration_worker_poll_seconds: float = Field(default=5.0, ge=0.25, le=60.0)
    integration_worker_claim_limit: int = Field(default=25, ge=1, le=100)

    # Durable local interview reminders. Calendar OAuth credentials are
    # intentionally not part of application settings in this release.
    scheduling_worker_enabled: bool = True
    scheduling_worker_poll_seconds: float = 15.0
    scheduling_claim_limit: int = 50

    # Durable rule evaluation. Actions are allowlisted by schema and execute
    # atomically; this worker never accepts executable code from rule payloads.
    workflow_worker_enabled: bool = True
    workflow_worker_poll_seconds: float = 15.0
    workflow_claim_limit: int = 50

    # AI hiring missions use their own durable step queue. The worker can be
    # disabled without affecting the recruiter-led Human Platform.
    ai_worker_enabled: bool = True
    ai_worker_poll_seconds: float = 2.0
    ai_worker_claim_limit: int = 10

    # CV extraction and LLM scoring are isolated from request-serving API
    # processes and use a longer lease than local notification work.
    candidate_processing_worker_enabled: bool = True
    candidate_processing_worker_poll_seconds: float = Field(
        default=2.0, ge=0.25, le=60.0
    )
    candidate_processing_claim_limit: int = Field(default=10, ge=1, le=100)

    # Production readiness fails closed when the dedicated worker fleet has no
    # recent heartbeat. Local/API-only development may leave this disabled.
    worker_readiness_required: bool = False

    # Operator observability is deliberately separate from tenant APIs. The
    # Prometheus endpoint is reachable only from these exact networks or with
    # the independent bearer token. Trace export is a safe no-op until both an
    # endpoint and a positive sample ratio are configured.
    observability_metrics_enabled: bool = False
    observability_metrics_bearer_token: SecretStr | None = None
    observability_metrics_allowed_cidrs: str = ""
    observability_service_name: str = Field(
        default="hr-agent-api",
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$",
    )
    observability_otlp_traces_endpoint: str | None = None
    observability_otlp_headers: SecretStr | None = None
    observability_otlp_allow_insecure: bool = False
    observability_trace_sample_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    observability_export_timeout_seconds: float = Field(default=5.0, ge=1.0, le=30.0)

    @model_validator(mode="after")
    def reject_insecure_production_defaults(self) -> "Settings":
        private_rates = (
            self.llm_private_prompt_usd_per_million,
            self.llm_private_completion_usd_per_million,
        )
        if (private_rates[0] is None) != (private_rates[1] is None):
            raise ValueError(
                "Private LLM prompt and completion accounting rates must be configured together"
            )

        normalized_model_endpoints: set[str] = set()
        for raw_endpoint in self.llm_private_base_url_allowlist.split(","):
            endpoint = raw_endpoint.strip().rstrip("/")
            if not endpoint:
                continue
            parsed_endpoint = urlsplit(endpoint)
            try:
                endpoint_port = parsed_endpoint.port
                endpoint_host = (
                    parsed_endpoint.hostname.rstrip(".")
                    .encode("idna")
                    .decode("ascii")
                    .lower()
                    if parsed_endpoint.hostname
                    else ""
                )
            except (UnicodeError, ValueError) as exc:
                raise ValueError(
                    "LLM_PRIVATE_BASE_URL_ALLOWLIST contains an invalid endpoint"
                ) from exc
            endpoint_path = parsed_endpoint.path.rstrip("/")
            path_parts = endpoint_path.split("/")
            if (
                parsed_endpoint.scheme not in {"http", "https"}
                or not endpoint_host
                or endpoint_port is None
                or parsed_endpoint.username is not None
                or parsed_endpoint.password is not None
                or parsed_endpoint.query
                or parsed_endpoint.fragment
                or not endpoint_path.startswith("/")
                or endpoint_path == ""
                or "//" in endpoint_path
                or "\\" in endpoint_path
                or "%" in endpoint_path
                or any(part in {"", ".", ".."} for part in path_parts[1:])
                or any(
                    re.fullmatch(r"[A-Za-z0-9._~-]+", part) is None
                    for part in path_parts[1:]
                )
            ):
                raise ValueError(
                    "LLM_PRIVATE_BASE_URL_ALLOWLIST must contain exact HTTP(S) base URLs with an explicit port and safe path"
                )
            rendered_host = (
                f"[{endpoint_host}]" if ":" in endpoint_host else endpoint_host
            )
            normalized_model_endpoints.add(
                f"{parsed_endpoint.scheme}://{rendered_host}:{endpoint_port}{endpoint_path}"
            )
        self.llm_private_base_url_allowlist = ",".join(
            sorted(normalized_model_endpoints)
        )

        try:
            from ipaddress import ip_network

            trusted_networks = [
                ip_network(value.strip(), strict=False)
                for value in self.public_rate_limit_trusted_proxy_cidrs.split(",")
                if value.strip()
            ]
        except ValueError as exc:
            raise ValueError(
                "PUBLIC_RATE_LIMIT_TRUSTED_PROXY_CIDRS contains an invalid network"
            ) from exc
        self.public_rate_limit_trusted_proxy_cidrs = ",".join(
            str(network) for network in trusted_networks
        )
        try:
            metrics_networks = [
                ip_network(value.strip(), strict=False)
                for value in self.observability_metrics_allowed_cidrs.split(",")
                if value.strip()
            ]
        except ValueError as exc:
            raise ValueError(
                "OBSERVABILITY_METRICS_ALLOWED_CIDRS contains an invalid network"
            ) from exc
        internal_boundaries = (
            ip_network("127.0.0.0/8"),
            ip_network("10.0.0.0/8"),
            ip_network("172.16.0.0/12"),
            ip_network("192.168.0.0/16"),
            ip_network("::1/128"),
            ip_network("fc00::/7"),
        )
        if any(
            not any(
                network.version == boundary.version and network.subnet_of(boundary)
                for boundary in internal_boundaries
            )
            for network in metrics_networks
        ):
            raise ValueError(
                "OBSERVABILITY_METRICS_ALLOWED_CIDRS must contain only loopback or private monitoring networks"
            )
        self.observability_metrics_allowed_cidrs = ",".join(
            str(network) for network in metrics_networks
        )
        metrics_token = (
            self.observability_metrics_bearer_token.get_secret_value()
            if self.observability_metrics_bearer_token is not None
            else None
        )
        if metrics_token is not None and len(metrics_token) < 32:
            raise ValueError(
                "OBSERVABILITY_METRICS_BEARER_TOKEN must contain at least 32 characters"
            )
        if metrics_token is not None and metrics_token in {
            self.jwt_secret,
            self.auth_mfa_encryption_key,
            self.integration_credential_encryption_key,
        }:
            raise ValueError(
                "OBSERVABILITY_METRICS_BEARER_TOKEN must be independent from application secrets"
            )
        if (
            self.observability_metrics_enabled
            and not metrics_networks
            and metrics_token is None
        ):
            raise ValueError(
                "Observability metrics require an operator token or an allowed network"
            )
        if self.observability_otlp_headers is not None:
            raw_headers = self.observability_otlp_headers.get_secret_value()
            if "\n" in raw_headers or "\r" in raw_headers:
                raise ValueError("OBSERVABILITY_OTLP_HEADERS cannot contain newlines")
        if self.observability_otlp_traces_endpoint is not None:
            parsed_otlp = urlsplit(self.observability_otlp_traces_endpoint)
            if (
                parsed_otlp.scheme not in {"http", "https"}
                or not parsed_otlp.hostname
                or parsed_otlp.username is not None
                or parsed_otlp.password is not None
                or parsed_otlp.query
                or parsed_otlp.fragment
            ):
                raise ValueError(
                    "OBSERVABILITY_OTLP_TRACES_ENDPOINT must be an HTTP(S) URL without credentials, query, or fragment"
                )
        if self.public_rate_limit_valkey_url is not None:
            parsed_valkey_url = urlsplit(self.public_rate_limit_valkey_url)
            if (
                parsed_valkey_url.scheme not in {"redis", "rediss"}
                or not parsed_valkey_url.hostname
                or parsed_valkey_url.fragment
                or parsed_valkey_url.path not in {"", "/"}
                and not re.fullmatch(r"/[0-9]{1,3}", parsed_valkey_url.path)
            ):
                raise ValueError(
                    "PUBLIC_RATE_LIMIT_VALKEY_URL must be a redis:// or rediss:// URL with an optional numeric database"
                )
        if self.legacy_private_storage_dir is not None:
            legacy_root = Path(self.legacy_private_storage_dir)
            if (
                not legacy_root.is_absolute()
                or legacy_root == Path(legacy_root.anchor)
                or len(legacy_root.parts) < 3
            ):
                raise ValueError(
                    "LEGACY_PRIVATE_STORAGE_DIR must be a bounded absolute directory"
                )
        if self.integration_credential_encryption_key is not None:
            raw_key = self.integration_credential_encryption_key
            try:
                decoded_key = base64.b64decode(
                    raw_key + "=" * (-len(raw_key) % 4),
                    altchars=b"-_",
                    validate=True,
                )
            except (binascii.Error, ValueError) as exc:
                raise ValueError(
                    "INTEGRATION_CREDENTIAL_ENCRYPTION_KEY must be URL-safe base64"
                ) from exc
            if len(decoded_key) != 32:
                raise ValueError(
                    "INTEGRATION_CREDENTIAL_ENCRYPTION_KEY must decode to exactly 32 bytes"
                )
            if raw_key in {self.jwt_secret, self.auth_mfa_encryption_key}:
                raise ValueError(
                    "INTEGRATION_CREDENTIAL_ENCRYPTION_KEY must be independent from auth secrets"
                )
        elif self.integration_credential_key_id is not None:
            raise ValueError(
                "INTEGRATION_CREDENTIAL_KEY_ID requires an integration encryption key"
            )

        configured_origins = {
            value.strip().rstrip("/")
            for value in self.integration_oauth_redirect_origins.split(",")
            if value.strip()
        }
        configured_origins.add(self.web_origin.rstrip("/"))
        for origin in configured_origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError(
                    "INTEGRATION_OAUTH_REDIRECT_ORIGINS must contain exact HTTP(S) origins"
                )
        self.integration_oauth_redirect_origins = ",".join(sorted(configured_origins))

        for host in (
            value.strip().lower().rstrip(".")
            for value in self.integration_outbound_host_allowlist.split(",")
            if value.strip()
        ):
            if (
                "://" in host
                or "/" in host
                or "*" in host
                or not re.fullmatch(
                    r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}",
                    host,
                )
            ):
                raise ValueError(
                    "INTEGRATION_OUTBOUND_HOST_ALLOWLIST must contain exact public DNS hosts"
                )

        for setting_name, configured_url in (
            ("S3_ENDPOINT", self.s3_endpoint),
            ("S3_PUBLIC_BASE_URL", self.s3_public_base_url),
        ):
            if configured_url is None:
                continue
            parsed_storage_url = urlsplit(configured_url)
            if (
                parsed_storage_url.scheme not in {"http", "https"}
                or not parsed_storage_url.netloc
                or parsed_storage_url.query
                or parsed_storage_url.fragment
                or parsed_storage_url.username is not None
                or parsed_storage_url.password is not None
                or ".." in parsed_storage_url.path.split("/")
            ):
                raise ValueError(
                    f"{setting_name} must be an absolute HTTP(S) URL without credentials, query, or fragment"
                )
        for setting_name, bucket_name in (
            ("S3_BUCKET", self.s3_bucket),
            ("S3_PUBLIC_BUCKET", self.s3_public_bucket),
        ):
            if bucket_name is not None and (
                not re.fullmatch(
                    r"(?=.{3,63}$)(?!\d+\.\d+\.\d+\.\d+$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?",
                    bucket_name,
                )
                or any(token in bucket_name for token in ("..", ".-", "-."))
            ):
                raise ValueError(f"{setting_name} must be a DNS-compatible bucket name")
        if (
            self.s3_bucket is not None
            and self.s3_public_bucket is not None
            and self.s3_bucket == self.s3_public_bucket
        ):
            raise ValueError("S3_BUCKET and S3_PUBLIC_BUCKET must be different")
        if not re.fullmatch(r"[a-z0-9-]{1,64}", self.s3_region):
            raise ValueError("S3_REGION is invalid")
        if self.s3_access_key is not None and any(
            ord(character) < 32 for character in self.s3_access_key
        ):
            raise ValueError("S3_ACCESS_KEY contains control characters")
        if self.s3_secret_key is not None and any(
            ord(character) < 32 for character in self.s3_secret_key.get_secret_value()
        ):
            raise ValueError("S3_SECRET_KEY contains control characters")
        if (
            not self.clamav_host
            or self.clamav_host != self.clamav_host.strip()
            or len(self.clamav_host) > 255
            or "://" in self.clamav_host
            or "/" in self.clamav_host
            or any(ord(character) < 32 for character in self.clamav_host)
        ):
            raise ValueError("CLAMAV_HOST must be a bounded host name or IP address")
        if self.blob_store_backend == "s3":
            secret_value = (
                self.s3_secret_key.get_secret_value()
                if self.s3_secret_key is not None
                else ""
            )
            if not all(
                (
                    self.s3_endpoint,
                    self.s3_bucket,
                    self.s3_public_bucket,
                    self.s3_access_key,
                    secret_value,
                )
            ):
                raise ValueError("S3 object storage credentials are incomplete")

        if self.auth_local_preview_admin_email is not None:
            bypass_email = self.auth_local_preview_admin_email.strip().lower()
            parsed_origin = urlsplit(self.web_origin)
            parsed_database = urlsplit(
                self.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
            )
            database_name = parsed_database.path.rsplit("/", 1)[-1]
            if (
                self.app_environment != "development"
                or parsed_origin.scheme != "http"
                or parsed_origin.hostname not in {"127.0.0.1", "localhost", "::1"}
                or not database_name.endswith("_preview")
                or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", bypass_email)
            ):
                raise ValueError(
                    "AUTH_LOCAL_PREVIEW_ADMIN_EMAIL is restricted to a loopback "
                    "development origin and an isolated _preview database"
                )
            self.auth_local_preview_admin_email = bypass_email

        if self.app_environment != "production":
            return self
        problems: list[str] = []
        if self.jwt_secret == "dev-jwt-secret-change-me" or len(self.jwt_secret) < 32:
            problems.append(
                "JWT_SECRET must be a non-default value of at least 32 characters"
            )
        if (
            self.auth_mfa_encryption_key is None
            or len(self.auth_mfa_encryption_key) < 32
            or self.auth_mfa_encryption_key == self.jwt_secret
        ):
            problems.append(
                "AUTH_MFA_ENCRYPTION_KEY must be an independent value of at least 32 characters"
            )
        if self.public_rate_limit_backend != "valkey":
            problems.append("PUBLIC_RATE_LIMIT_BACKEND must be valkey")
        if not self.public_rate_limit_valkey_url:
            problems.append("PUBLIC_RATE_LIMIT_VALKEY_URL must be configured")
        elif not self.public_rate_limit_valkey_url.startswith("rediss://"):
            problems.append(
                "PUBLIC_RATE_LIMIT_VALKEY_URL must use rediss in production"
            )
        public_rate_limit_secret = (
            self.public_rate_limit_hmac_secret.get_secret_value()
            if self.public_rate_limit_hmac_secret is not None
            else None
        )
        if (
            public_rate_limit_secret is None
            or len(public_rate_limit_secret) < 32
            or public_rate_limit_secret
            in {
                self.jwt_secret,
                self.auth_mfa_encryption_key,
                self.integration_credential_encryption_key,
            }
        ):
            problems.append(
                "PUBLIC_RATE_LIMIT_HMAC_SECRET must be an independent value of at least 32 characters"
            )
        if not self.web_origin.startswith("https://"):
            problems.append("WEB_ORIGIN must use HTTPS")
        if "postgres:postgres@" in self.database_url:
            problems.append(
                "DATABASE_URL must not use the development database credential"
            )
        if not self.openrouter_base_url.startswith("https://"):
            problems.append("OPENROUTER_BASE_URL must use HTTPS")
        if not self.worker_readiness_required:
            problems.append("WORKER_READINESS_REQUIRED must be true")
        if (
            self.observability_otlp_traces_endpoint is not None
            and self.observability_otlp_traces_endpoint.startswith("http://")
            and not self.observability_otlp_allow_insecure
        ):
            problems.append(
                "OBSERVABILITY_OTLP_TRACES_ENDPOINT must use HTTPS in production unless insecure transport is explicitly allowed"
            )
        if self.tenant_rls_mode != "forced":
            problems.append("TENANT_RLS_MODE must be forced")
        if self.malware_scanner_backend != "clamav":
            problems.append("MALWARE_SCANNER_BACKEND must be clamav")
        if self.blob_store_backend != "s3":
            problems.append("BLOB_STORE_BACKEND must be s3 in production")
        else:
            if not all(
                (
                    self.s3_endpoint,
                    self.s3_bucket,
                    self.s3_public_bucket,
                    self.s3_access_key,
                    self.s3_secret_key,
                )
            ):
                problems.append("S3 object storage credentials are incomplete")
            elif not str(self.s3_endpoint).startswith("https://"):
                problems.append("S3_ENDPOINT must use HTTPS in production")
            if not self.s3_public_base_url or not self.s3_public_base_url.startswith(
                "https://"
            ):
                problems.append("S3_PUBLIC_BASE_URL must use HTTPS for public branding")
            if (
                self.s3_secret_key is not None
                and self.s3_secret_key.get_secret_value()
                in {
                    self.jwt_secret,
                    self.auth_mfa_encryption_key,
                    self.integration_credential_encryption_key,
                }
            ):
                problems.append(
                    "S3_SECRET_KEY must be independent from application auth secrets"
                )
            if (
                self.s3_secret_key is not None
                and len(self.s3_secret_key.get_secret_value()) < 16
            ):
                problems.append("S3_SECRET_KEY must contain at least 16 characters")
        if self.integration_credential_encryption_key is None:
            problems.append(
                "INTEGRATION_CREDENTIAL_ENCRYPTION_KEY must be a random 32-byte URL-safe base64 value"
            )
        if (
            len(self.hh_user_agent.strip()) < 12
            or "integration-contact-not-configured" in self.hh_user_agent
            or not any(marker in self.hh_user_agent for marker in ("@", "https://"))
        ):
            problems.append("HH_USER_AGENT must identify a real operator contact")
        if problems:
            raise ValueError(
                "Insecure production configuration: " + "; ".join(problems)
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
