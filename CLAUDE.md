# Recruitment App v2 — Build Plan

> **For Claude Code.** This document is the single source of truth for the v2 build. Read it top to bottom before writing any code. If anything here conflicts with a later instruction, ask before proceeding.

---

## 1. Context

We have a working B2B recruitment product in production. It analyzes CVs scraped from Russian job boards (HeadHunter, Avito, others) and uploaded manually, ranks candidates against open positions, and produces structured scores plus comments. One major customer uses it daily. The scoring engine works well — no complaints from users on quality.

**The problem:** v1's architecture didn't anticipate chat or agent workflows. As soon as we tried adding AI chat (so recruiters could ask questions about candidates and get interview prep advice), we got stuck. The data model, the LLM coupling, and the lack of a chat-as-bounded-context all blocked us.

**v2 goal:** rebuild the frontend and the API/orchestration layer with the right seams, keep the existing scoring engine as-is (it works), and ship ranking + chat in a UI that looks like a modern AI startup product (Linear / Vercel / Raycast school).

**Future roadmap (not building now, but architecture must support):** autonomous HR agents that source, screen, interview, score, and onboard candidates end-to-end. The architecture must not block this. Specifically: chat is a bounded context separate from candidates and positions, and the LLM layer is abstracted so any model or provider plugs in.

---

## 2. Non-goals

The following are explicitly **out of scope** for v2:

- Rebuilding the scoring engine (it works, leave it alone)
- Touching the production deployment that the existing customer uses
- Personal-data handling, GDPR tooling, audit logs (no PII collected)
- Billing / subscription layer (B2B contract-based, manual)
- Mobile apps (web only)
- Public signup (users provisioned via admin panel only)
- Email / calendar integration (deferred to v3)
- Agent autonomy features (deferred to v3+)

---

## 3. Hard constraints

- **Timeline:** 2 days. Solo developer.
- **Hosting:** Vercel for web, Railway (or Fly.io) for API. Postgres on Supabase or Neon.
- **LLM provider:** OpenRouter as primary. vLLM-compatible adapter stubbed for future self-hosted use.
- **Languages:** EN + RU in the UI.
- **Auth:** Credentials only, users created by admin.
- **No compliance requirements** (no SOC 2, GDPR, HIPAA work needed).

---

## 4. Stack

| Layer | Choice | Why |
|---|---|---|
| Frontend | Next.js 15 (App Router) + TypeScript | Standard, ships fast on Vercel, RSC where useful |
| Styling | Tailwind + shadcn/ui | Fastest path to a polished modern look, fully customizable |
| Server state | TanStack Query | Cache, retries, optimistic updates without writing it ourselves |
| Backend | FastAPI (Python) | Wraps the existing engine cleanly, async-friendly |
| Database | Postgres (Supabase or Neon) | Free tier, jsonb for parsed CVs, scales fine |
| ORM (web) | Prisma | Type-safe DB calls from Next.js for simple reads |
| ORM (API) | SQLAlchemy + Alembic | Migrations, mature Python tooling |
| Auth | NextAuth (credentials provider) | Sessions handled, no signup flow |
| LLM | OpenRouter via single abstraction | Swap models with a config change |
| i18n | next-intl | App Router native, message extraction works well |
| Web deploy | Vercel | Zero-config Next.js |
| API deploy | Railway | Long-running Python, simple Docker deploys |

**Shared types:** generated from FastAPI's OpenAPI schema into the web app. No hand-written DTOs duplicated on both sides.

---

## 5. Architecture

Four layers, each independently replaceable:

```
┌──────────────────────────────────────────────────┐
│  UI Layer (Next.js on Vercel)                    │
│  Knows nothing about LLMs or scoring.            │
│  Calls API only.                                 │
└────────────────────┬─────────────────────────────┘
                     │ HTTPS + session cookie
┌────────────────────▼─────────────────────────────┐
│  API Layer (FastAPI on Railway)                  │
│  Owns: auth, positions, candidates, scores,      │
│  chat sessions. All new code lives here.         │
└──────┬──────────────────────────────────────┬────┘
       │                                      │
┌──────▼─────────────────┐    ┌───────────────▼───┐
│  Engine Layer          │    │  LLM Layer        │
│  Existing scoring      │    │  Abstraction with │
│  engine, wrapped       │    │  OpenRouter +     │
│  behind clean iface.   │    │  vLLM adapters.   │
│  Untouched internally. │    │                   │
└────────────────────────┘    └───────────────────┘
```

### Key seams

1. **`ScoringEngine.score(candidate, position) -> ScoreResult`** — the only entry point to the existing engine. Internal changes to the engine never bubble up.
2. **`LLMClient.complete(messages, model)` / `stream_complete(...)`** — single interface, multiple adapters. Model selection is config-driven (env var or per-request).
3. **Chat is its own bounded context.** `chat_sessions` reference candidates and/or positions but live separately. This is the seam that broke v1.
4. **All LLM calls are logged** to the `messages` table with `model_used`, `tokens_in`, `tokens_out`. Without this, we can't compare models — which is the whole reason we're moving to OpenRouter.

---

## 6. Data model

```sql
users
  id, email, password_hash, role (admin|recruiter|hiring_manager),
  locale (en|ru), created_at

positions
  id, title, description, requirements (jsonb), status (open|closed|paused),
  created_by (user_id), created_at, updated_at

candidates
  id, source (hh|avito|manual|other), source_ref,
  raw_cv (text), parsed_cv (jsonb), created_at

scores
  id, candidate_id, position_id,
  fit_score, fit_comment,
  skills_score, skills_comment,
  experience_score, experience_comment,
  project_quality_score, project_quality_comment,
  overall_score, overall_recommendation,
  model_used, scored_at
  UNIQUE (candidate_id, position_id)

chat_sessions
  id, user_id, scope (candidate|position|free),
  candidate_id (nullable), position_id (nullable),
  title, created_at, updated_at

messages
  id, session_id, role (user|assistant|system),
  content (text), model_used,
  tokens_in, tokens_out, created_at
```

**Notes:**
- `parsed_cv` is jsonb so the engine's structured output (skills, projects, experience) is queryable later without reparsing.
- `scores` uses a composite unique constraint — one score per candidate-position pair, re-running scoring overwrites.
- `chat_sessions.scope` is the discriminator. A session is either about a specific candidate, a specific position, or freeform.
- Final criteria names will be confirmed in pre-flight (section 0 of the task list). The four columns above are placeholders matching what was described: fit, skills, experience, project quality.

---

## 7. UI direction

**Reference aesthetic:** Linear, Vercel, Raycast, Cal.com. Dense but calm. Confident, not loud.

**Concrete rules:**
- Monochrome base (zinc / neutral grays), one accent color (chosen in pre-flight)
- Geist or Inter for type, settled in pre-flight
- Generous whitespace. Don't fill every pixel.
- Subtle motion only where it earns its place: list reorders, score reveals, page transitions
- Dark mode default, light mode supported
- No gradients-on-everything, no glassmorphism, no purple-to-pink, no emoji in UI chrome

**Three core screens for v2:**

1. **Dashboard** — Positions as cards, sorted by recency or activity. Candidate counts, status badges, quick filters. "New position" CTA top right.

2. **Position detail** — Header (title, description, edit). Ranked candidate table with expandable rows showing the four criteria and comments. Score visualization (ring or bar, not just a number). Manual CV upload drop zone. "Chat about this position" entry point.

3. **Candidate detail** — Header (source badge, overall score, recommendation). Four criteria cards with reasoning. Collapsible parsed CV view. Chat panel scoped to this candidate.

**Admin panel** is separate and intentionally plain. List users, create users, set roles. Functional, not pretty.

---

## 8. Two-day build plan

**Day 1 — foundations + data path working end-to-end**
- Sections 0–6 of the task list
- By end of Day 1: you can log in, create a position, upload a CV, get a score back, and see it in a list

**Day 2 — UI, chat, polish**
- Sections 7–13 of the task list
- By end of Day 2: dashboard, position detail, candidate detail, chat interface, RU translations, deployed

**Cut-list (drop in this order if behind):**
1. Streaming chat → non-streaming
2. Russian translations → ship EN-only
3. Admin UI → manage users via DB directly
4. vLLM adapter → keep as stub
5. Mobile / tablet polish → desktop only

If you're cutting before end of Day 1, the plan failed and we need to talk before Day 2.

---

## 9. Task list

### 0. Pre-flight (do these before starting Day 1)
- [ ] Export engine code and document its interface (inputs, outputs, dependencies)
- [ ] Get OpenRouter API key and load credits
- [ ] Create accounts: Vercel, Railway, Supabase, GitHub
- [ ] Confirm four scoring criteria final names in EN and RU
- [ ] Decide accent color and font (Geist or Inter)

### 1. Infrastructure and scaffolding
- [ ] Create monorepo structure (`apps/web`, `apps/api`, `packages/shared`)
- [ ] Scaffold Next.js 15 app with TypeScript and Tailwind
  - [ ] Install shadcn/ui base components (button, card, dialog, input, table, sheet, dropdown, toast)
  - [ ] Set up dark mode and theme tokens
  - [ ] Configure Geist or Inter font
- [ ] Scaffold FastAPI app
  - [ ] Set up Pydantic models, CORS, health endpoint
  - [ ] Write Dockerfile for Railway
- [ ] Set up Postgres on Supabase or Neon
- [ ] Wire Vercel deploy for web
- [ ] Wire Railway deploy for API
- [ ] Verify end-to-end: web calls API calls DB

### 2. Database and migrations
- [ ] Decide ORM stack (Prisma on web, SQLAlchemy + Alembic on API)
- [ ] Schema: `users` table
- [ ] Schema: `positions` table
- [ ] Schema: `candidates` table with `parsed_cv` jsonb field
- [ ] Schema: `scores` table with four criteria fields and overall comment
- [ ] Schema: `chat_sessions` table
- [ ] Schema: `messages` table with `model_used`, `tokens_in`, `tokens_out`
- [ ] Run migrations and seed test user

### 3. Auth
- [ ] Install and configure NextAuth credentials provider
- [ ] Build login page UI
- [ ] Add session middleware on protected routes
- [ ] API: validate session token on every request
- [ ] Test full login flow with seeded user

### 4. LLM abstraction layer
- [ ] Define `LLMClient` interface (`complete`, `stream_complete`)
- [ ] Implement OpenRouter adapter
- [ ] Stub vLLM OpenAI-compatible adapter
- [ ] Config-driven model selection (env var or per-request)
- [ ] Log every LLM call to `messages` table

### 5. Engine integration
- [ ] Wrap existing engine in `ScoringService` class
- [ ] Endpoint: `POST /score`
- [ ] Endpoint: `POST /candidates/upload`
- [ ] Test scoring end-to-end with real CV

### 6. Core API endpoints
- [ ] `GET /positions` (list with candidate counts)
- [ ] `POST /positions` (create)
- [ ] `GET /positions/:id` (detail with ranked candidates)
- [ ] `PATCH /positions/:id` (edit)
- [ ] `DELETE /positions/:id`
- [ ] `GET /candidates/:id` (full detail with score breakdown)
- [ ] `POST /candidates/:id/score` (trigger scoring against position)

### 7. Dashboard screen
- [ ] Build layout with sidebar nav and main area
- [ ] Position cards with title, count, activity, status
- [ ] Sort and filter controls (recency, status)
- [ ] Empty state
- [ ] New position dialog

### 8. Position detail screen
- [ ] Header with title, description, edit button
- [ ] Candidates table ranked by overall score
  - [ ] Expandable rows with four criteria and comments
  - [ ] Score visualization (bar or ring)
- [ ] Manual CV upload drag-drop zone
- [ ] Chat about this position entry point

### 9. Candidate detail screen
- [ ] Header with source badge, overall score, recommendation
- [ ] Four criteria cards with score and reasoning
- [ ] Parsed CV collapsible view

### 10. Chat interface
- [ ] API: `POST /chat/sessions`
- [ ] API: `POST /chat/sessions/:id/messages`
- [ ] API: `GET /chat/sessions/:id`
- [ ] Build chat UI component (message list, input, send)
- [ ] Implement SSE streaming
- [ ] Write system prompts for candidate-scoped chat
- [ ] Write system prompts for position-scoped chat

### 11. Internationalization
- [ ] Install and configure next-intl with EN and RU
- [ ] Extract all UI strings to translation files
- [ ] Add language switcher in header
- [ ] Translate everything to Russian

### 12. Admin (minimal)
- [ ] Add admin flag check on user
- [ ] Admin page: list users
- [ ] Admin page: create user form

### 13. Polish and deploy
- [ ] Add loading states on every async action
- [ ] Add error boundaries and toast notifications
- [ ] Add empty states for every list
- [ ] Mobile and tablet responsive check
- [ ] Set production env vars on Vercel and Railway
- [ ] Full smoke test with real data
- [ ] Custom domain and SSL

### 14. Cut-list (drop in this order if behind)
- [ ] Streaming chat → non-streaming
- [ ] Russian translations → defer (ship EN-only)
- [ ] Admin UI → defer (manage users via DB)
- [ ] vLLM adapter → keep as stub
- [ ] Mobile responsive polish → defer

---

## 10. Repository layout

```
recruitment-v2/
├── apps/
│   ├── web/                 # Next.js 15
│   │   ├── app/
│   │   │   ├── (auth)/login/
│   │   │   ├── (app)/dashboard/
│   │   │   ├── (app)/positions/[id]/
│   │   │   ├── (app)/candidates/[id]/
│   │   │   ├── (admin)/admin/
│   │   │   └── api/auth/[...nextauth]/
│   │   ├── components/
│   │   │   ├── ui/          # shadcn primitives
│   │   │   ├── chat/
│   │   │   ├── positions/
│   │   │   └── candidates/
│   │   ├── lib/
│   │   │   ├── api-client.ts
│   │   │   └── auth.ts
│   │   ├── messages/        # next-intl: en.json, ru.json
│   │   └── prisma/
│   └── api/                 # FastAPI
│       ├── app/
│       │   ├── routers/
│       │   ├── services/
│       │   │   ├── scoring.py    # wraps existing engine
│       │   │   └── chat.py
│       │   ├── llm/
│       │   │   ├── base.py       # LLMClient interface
│       │   │   ├── openrouter.py
│       │   │   └── vllm.py
│       │   ├── models/      # SQLAlchemy
│       │   ├── schemas/     # Pydantic
│       │   └── main.py
│       ├── alembic/
│       └── Dockerfile
├── packages/
│   └── shared/              # generated types from OpenAPI
└── README.md
```

---

## 11. Environment variables

**Web (`apps/web/.env.local`)**
```
DATABASE_URL=
NEXTAUTH_SECRET=
NEXTAUTH_URL=
API_BASE_URL=
```

**API (`apps/api/.env`)**
```
DATABASE_URL=
OPENROUTER_API_KEY=
OPENROUTER_DEFAULT_MODEL=
VLLM_BASE_URL=         # stub, optional
VLLM_API_KEY=          # stub, optional
JWT_SECRET=            # shared with web for session validation
ENGINE_PATH=           # path or import for existing scoring engine
```

---

## 12. Done definition

v2 ships when all of the following are true:
- A user provisioned via admin can log in
- They can create a position, upload a CV, and see a ranked, scored candidate within 60 seconds
- They can open a candidate, see all four criteria with reasoning, and chat with the AI about that candidate
- They can switch the UI between EN and RU
- It's deployed on a custom domain with SSL
- A real CV from a real position scores correctly end-to-end

If any of these is false at end of Day 2, v2 is not done. No "soft launch" loophole.

---

## 13. Working agreement with Claude Code

- **Read this file before every session.** If something here is wrong or stale, fix the file first, then the code.
- **Pre-flight (section 0) is non-negotiable.** No `git init` until those five items are checked off.
- **Stick to the cut-list order if you fall behind.** Don't invent your own.
- **Log every LLM call from day one.** Adding telemetry later is how you end up unable to evaluate models.
- **Don't touch the production deployment.** This is a parallel build for ourselves.
- **When in doubt, ask before building.** A 60-second clarification beats an hour of rework.