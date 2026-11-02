# External Release-Gate Evidence

The committed pending template is
`docs/evidence/external-gate-register-e18.template.json`. After the exact
candidate revision finishes CI, copy it into the controlled release-evidence
workspace, complete it through the provider/operator/reviewer process below,
independently verify every referenced receipt and artifact, then sign and retain
it as an attestation of that immutable source revision. Do not commit the
completed register back onto the candidate: doing so would create a new Git SHA
and invalidate its own exact-revision evidence. The private-pilot candidate
remains `NOT GO` until that external attestation is independently accepted. The register records
evidence references and SHA-256 fingerprints only. Never
place provider credentials, access/refresh tokens, passwords, private keys, or
raw incident values in Git, CI logs, screenshots, or evidence artifacts.

## Validation

```text
python3 ops/release/test_validate_external_gate_register.py
python3 ops/release/validate_external_gate_register.py \
  docs/evidence/external-gate-register-e18.template.json
```

The committed validator is deliberately limited: it proves only that the source
template has the exact pending schema, contains no recognized credential value,
cannot claim `GO`, and cannot embed completed evidence. It is not a release
authority and refuses all completed states. A completed external attestation
requires receipt/artifact resolution, signature and digest verification, exact
GitHub revision/run reconciliation, and named human review outside this source
template.

## Provider certification

Use pilot-owned sandbox/test accounts for Google Workspace, Microsoft 365,
Telegram, and hh.ru. For each provider, record a non-secret connection
reference, named verifier, UTC timestamp, immutable evidence reference, and
every provider-specific check required by the release checklist. This includes
connection, refresh where applicable, revoke/disconnect, mandatory
email/calendar/message/publication/import behavior, timezone/conflict or
webhook/provenance behavior where applicable, retry/rate-limit handling,
outage, and recovery. Every mandatory check must be `true` before status becomes
`certified`.

Somon.tj remains tracked-link and validated import only and must not be scraped.
Its launch behavior is covered by local contracts rather than a credentialed
provider certification.

## OpenRouter credential incident

An authorized operator must revoke the development credential and create a new
one outside Codex. Record only SHA-256 fingerprints, never either credential.
Closure requires an old-credential authentication probe returning 401 or 403, a
new-credential probe returning 200, the revocation time, named operator, and an
immutable sanitized incident reference. Tenant opt-in and cross-border notices
remain separate runtime requirements.

## Named acceptance

Product, security, operations, and legal reviewers independently inspect the
preserved-data preview and evidence. Each accepted record requires the reviewer's
name, UTC decision time, and an immutable evidence reference. Engineering cannot
author these decisions on a reviewer's behalf.

## Hosted artifacts

The exact candidate revision must pass API, web, Linux WebKit, recovery,
performance, supply-chain, SBOM verification, and keyless signature verification.
Record the full Git SHA, GitHub run IDs, and SHA-256 digests for every required
API/web image, SBOM, signature bundle, and recovery/performance evidence
artifact. A passing run for an earlier tree is not sufficient.
