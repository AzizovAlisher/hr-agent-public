# Recruitment App v2

B2B hiring and assessment platform for a private RU/EN pilot. The current
release contract and target behavior are defined in
[`docs/PILOT_RELEASE_CONTRACT.md`](./docs/PILOT_RELEASE_CONTRACT.md) and
[`docs/CANONICAL_FEATURE_MATRIX.md`](./docs/CANONICAL_FEATURE_MATRIX.md).

## Layout

```
hr_agent/
├── docs/               # Product contracts, security policy, and runbooks
├── apps/
│   ├── web/            # Next.js; generated API types live in lib/generated/
│   └── api/            # FastAPI (Python 3.12, SQLAlchemy, Alembic)
├── ops/                # Pilot operations and recovery tooling
└── v1/                 # Read-only reference (production v1 code, gitignored)
```

## Local development

Prerequisites: `pnpm`, `uv`, `docker` (for Postgres if not on Supabase yet).

```bash
# API
cd apps/api && uv sync && uv run uvicorn app.main:app --reload --port 8000 --no-access-log

# Durable work (another terminal; required for candidate processing, delivery,
# integrations, reminders, workflows, private-object deletion, and AI missions)
cd apps/api && uv run python -m app.scripts.run_workers

# Web (in another terminal)
cd apps/web && pnpm install && pnpm dev
```

Web runs on `http://localhost:3000`, API on `http://localhost:8000`. Configure
`apps/web/.env.local` with `API_BASE_URL=http://localhost:8000` and the API with
`DATABASE_URL`. Managed OpenRouter is optional and remains unavailable until
the operator configures credentials and the tenant explicitly accepts managed,
potentially cross-border processing.

For an owner-operated local preview, an existing MFA-enrolled administrator can
use password-only sign-in by setting `AUTH_LOCAL_PREVIEW_ADMIN_EMAIL` to that
administrator's exact email. This escape hatch is accepted only in development,
from a loopback browser, with a loopback HTTP web origin, and when the database
name ends in `_preview`; startup fails if any boundary is violated. Production,
test, remote, non-admin, and non-preview sign-ins continue to require the normal
MFA flow.

### Full local pilot topology

The reference Compose stack runs the web BFF, API, a separate durable worker,
PostgreSQL with distinct non-superuser API/worker logins, Valkey, two MinIO
buckets, and ClamAV. It migrates before either runtime starts, keeps private
objects in a bucket with no anonymous policy, and exposes only the public
bucket's generated `logos/` prefix.

```bash
cp .env.compose.example .env
# Fill every value. Use independent random values; database/MinIO passwords
# should be 32+ hexadecimal characters so they remain URL-safe.
docker compose -f docker-compose.dev.yml config --quiet
docker compose -f docker-compose.dev.yml up --build
```

Generate the integration encryption key with
`openssl rand -base64 32 | tr '+/' '-_'`. Generate the other secrets with
`openssl rand -hex 32`. This Compose file uses development-mode HTTP on the
local container network; it is not a substitute for the TLS, encrypted Valkey,
external secrets, backup, and ingress controls required by the production
runbooks.

## Human Platform and AI Hiring Assistant

The recruiter-led Human Platform remains the system of record. The separate
AI Hiring Assistant lives at [`/ai`](http://localhost:3000/ai) and owns its
sessions, hiring missions, immutable plan versions, evidence-backed
assessments, approval bundles, run telemetry, and mission events. Both products
share login, organization permissions, candidates, and vacancies.

The AI workspace is disabled by default for existing organizations. An admin
can enable it in **AI Settings**; `app.scripts.seed_demo` enables it for the
demo organization. All durable queues run only in the dedicated worker process,
so API and worker replicas can scale and fail independently:

```bash
cd apps/api
uv run uvicorn app.main:app --port 8000 --no-access-log
uv run python -m app.scripts.run_workers
```

`GET /health/live` checks the HTTP process. `GET /health/ready` also verifies
PostgreSQL, the migration marker, writable private storage, and reports the
most recent dedicated-worker heartbeat. Contract entitlements and append-only
usage are available to authenticated tenants at `/enterprise/entitlements`;
commercial limits remain operator-managed for the private pilot.

Release gates:

```bash
cd apps/api
uv run alembic upgrade head
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/recruitment_v2_test uv run pytest

cd ../web
pnpm check:api-contracts
pnpm typecheck
pnpm build
```

`pnpm generate:api` refreshes `apps/web/lib/generated/api.ts` from
`apps/api/openapi.json`. `pnpm check:api-contracts` fails when that artifact is
stale or when a new handwritten DTO appears at a reviewed frontend API boundary.

The dedicated 10,000-candidate p95 gate and the encrypted PostgreSQL plus
private-object backup/restore drill are documented in
[`docs/RECOVERY_RUNBOOK.md`](./docs/RECOVERY_RUNBOOK.md). The authenticated
`/enterprise/backup` download is only a non-restorable core-data portability
export; it is not the disaster-recovery mechanism.

## Company careers-site API

Published positions are available through the versioned endpoint
`GET /public/v1/careers/{organization_slug}/positions`. The dependency-free
widget renders a searchable mini careers app and submits candidates through
`POST /public/v1/careers/{organization_slug}/positions/{position_id}/apply`.
Accepted candidates immediately appear in the selected Recruiter vacancy at
the `new` stage while AI scoring continues in the background. The Russian
integration guide and a standalone website example are in
[`docs/CAREERS_API_INTEGRATION_RU.md`](./docs/CAREERS_API_INTEGRATION_RU.md).

## v1 vs v2

v1 is a Django + Vue + Celery monolith deployed in production for one customer. v2 rebuilds the frontend and the API/orchestration layer with a clean LLM abstraction and chat as a bounded context. v1's scoring engine prompts and JSON schema are reused verbatim in `apps/api/app/services/scoring.py` (option D in the build plan).

Do not modify `v1/`. It's a reference snapshot only.
