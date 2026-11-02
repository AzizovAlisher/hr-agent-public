# Pilot Threat Model

## Protected assets

Candidate identity and documents, assessment evidence, scorecards, compensation,
offer terms, OAuth/API/MFA secrets, AI prompts and derived data, audit evidence,
connector payloads, exports, backups, and tenant configuration are protected.

## Trust boundaries

- Anonymous candidate/referral/scheduling/offer/shortlist pages to the BFF/API.
- Authenticated browser to the same-origin BFF and FastAPI policy boundary.
- FastAPI to PostgreSQL, private/public object stores, Valkey, and the job outbox.
- Workers to mail/calendar/sourcing/assessment providers and tenant-approved LLMs.
- Inbound provider webhooks and outbound signed customer webhooks.

## Material threats and required controls

| Threat | Required prevention/detection | Required regression evidence |
|---|---|---|
| cross-tenant IDOR or nested relation injection | tenant-scoped queries, relationship validation, non-enumerating errors, RLS defense-in-depth | two colliding tenants across paths, bodies, files, search, exports, AI, and public links |
| private file disclosure | separate private store, bounded validated upload, authorized download, safe disposition and cache headers | anonymous/wrong-tenant denial plus malformed/polyglot/zip-bomb/orphan tests |
| credential/session theft | short tokens, issuer/audience/jti, MFA, throttling, revocation/versioning, disabled-user checks, no-store | brute force, stale token, role/password/status change, logout, concurrent admin tests |
| public token enumeration/replay | digest-only storage, expiry/revoke/rotate/consume, generic responses, rate/risk controls | malformed/expired/revoked/consumed and concurrent replay tests |
| webhook spoofing/replay | bounded raw-body authentication, versioned envelope, HMAC-SHA256, timestamp window, per-owner event ID plus payload fingerprint, retry log | missing/bad/stale/tampered/oversized, duplicate, concurrent replay, same-ID/different-body, reordered, outage/recovery tests for communication and sourcing callbacks |
| readiness reconnaissance | public status/time-only response with `no-store`; dependency detail requires `enterprise.manage` and shares the same snapshot | concurrent public redaction, restricted/anonymous deny, authorized detail, and strict real-stack assertions |
| SSRF/provider exfiltration | operator allowlist for private endpoints, public HTTPS/global DNS checks, redirect and rebinding validation | loopback/private/link-local/metadata/redirect/DNS-change corpus |
| prompt injection or PII leakage | tenant gateway, context minimization/redaction, structured output, provenance, human approval | injection corpus, PII canaries, routing and irreversible-action tests |
| budget/notification abuse | atomic reservation/settlement, entitlement ledger, recipient caps, idempotency | zero/null/race/exhaustion and amplification tests |
| job loss/duplication | transactional outbox, leases, idempotency, bounded retry/DLQ, heartbeat | API/worker kill, dual worker, replay/cancel tests |
| erasure gaps | inventory-driven traversal and backup-expiry manifest | unique canaries absent from live and derived stores after erasure |
| supply-chain compromise | locked installs, audit/SAST/secret/license/container gates, SBOM, non-root pinned images | clean frozen CI and reachable-advisory review |

Security acceptance is evidence-based: implementation does not close a finding
until a different verification pass reproduces the former defect and passes the
adjacent workflow.
