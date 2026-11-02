# Recruitment app — design brief

A B2B recruiting tool. Recruiters create job positions, upload candidate CVs, and an LLM scores each candidate against the position on 4 criteria with Russian-language feedback. They can also chat with an AI scoped to a specific candidate or position.

## Screens

**1. `/login`** — Centered card. Email + password. Nothing else.

**2. `/dashboard`** — Positions list.
- Header: "Positions" + "New position" CTA (opens modal with title / description / requirements-JSON fields).
- Grid of cards. Each card: title, status badge, candidate count, last-updated date.
- Empty state if none.

**3. `/positions/{id}`** — Position detail.
- Header: title, description, status badge, edit affordance.
- Two columns: main (left) + chat sidebar (right, ~360px).
- Main top: "Add a candidate" card with **3 tabs** — PDF/DOCX file drop, paste text, paste HH.ru JSON.
- Main bottom: ranked candidates list. Each row shows overall score (0–100, in a chip), source badge, source ref, and 4-column score breakdown with criterion comments (Russian text).
- Sidebar: chat panel scoped to this position. Streaming responses, message history persists.

**4. `/candidates/{id}`** — Candidate detail.
- Header: candidate ref, source badge, big overall score chip.
- Two columns: main + chat sidebar.
- Main: 2×2 grid of criterion cards (`technical_skills`, `quality_of_projects`, `previous_experience`, `impact`) — each shows large score + Russian comment.
- Below: collapsible resume text. If HH-sourced, also collapsible parsed-JSON block.
- Sidebar: chat panel scoped to this candidate.

## Visual direction
Linear / Vercel / Raycast / Cal.com school. Dense but calm. Confident, not loud.
- Monochrome zinc/neutral base. **One accent: emerald** `oklch(0.65 0.15 162)`.
- Geist font (sans + mono).
- Dark mode default; light mode supported.
- Generous whitespace. No gradients, no glassmorphism, no purple→pink, no emoji in chrome.
- Subtle motion only on list reorders, score reveals, page transitions.

## Data shapes (already implemented in API)
```ts
type Position = { id; title; description: string|null; requirements: object|null; status: "open"|"closed"|"paused"; candidate_count: number; created_at; updated_at };
type RankedCandidate = { candidate_id; source; source_ref: string|null; overall_score: number|null; score_<criterion>: number|null; comment_<criterion>: string|null };
type Candidate = { id; source; source_ref; raw_cv: string|null; parsed_cv: object|null };
type ChatSession = { id; scope: "candidate"|"position"|"free"; candidate_id?; position_id?; title? };
type ChatMessage = { id; role: "user"|"assistant"; content: string; created_at };
// criterion ∈ {technical_skills, quality_of_projects, previous_experience, impact}
```

## Core flows
1. Login → /dashboard.
2. New position → fill form → card appears.
3. Open position → upload CV (file / text / HH JSON) → auto-scored → row appears in ranked list within a few seconds.
4. Click ranked row → /candidates/{id} → breakdown + chat.
5. Chat (either scope) → streaming reply, persists across reloads. SSE: `data: {token}\n\n` frames, ends with `data: [DONE]\n\n`.

## API (FastAPI on :8000, JWT bearer auth)
- `POST /auth/login`, `GET /auth/me`
- `GET/POST /positions`, `GET/PATCH/DELETE /positions/{id}`
- `POST /candidates/upload/file` (multipart), `POST /candidates/upload/text` `{raw_text}`, `POST /candidates/upload/hh` `{hh_json}`
- `GET /candidates/{id}`, `POST /candidates/{id}/score` `{position_id}`
- `POST /chat/sessions` `{scope, candidate_id?, position_id?}`, `GET /chat/sessions/{id}`, `POST /chat/sessions/{id}/messages` `{content}`, `POST /chat/sessions/{id}/messages/stream` `{content}` (SSE)

## Stack
Next 16 (App Router) + Tailwind v4 + shadcn (`@base-ui/react`) + NextAuth v5 + Geist. Server components + server actions where possible; client components only for chat (SSE) and modal/tab interactions.
