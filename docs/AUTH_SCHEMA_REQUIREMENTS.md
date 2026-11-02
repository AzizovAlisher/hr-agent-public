# Authentication schema requirements

Wave 2 intentionally does not include an Alembic revision. The consolidated
product migration must add the following schema before the hardened API is
deployed.

## `users` additions

| Column | PostgreSQL type | Null/default | Constraints/meaning |
| --- | --- | --- | --- |
| `is_active` | `boolean` | not null, server default `true` | Disabled users cannot log in or use an existing token. |
| `token_version` | `integer` | not null, server default `0` | Check `token_version >= 0`; increment to revoke every prior session. |
| `mfa_enabled` | `boolean` | not null, server default `false` | Check that enabled MFA has an encrypted active secret. |
| `mfa_secret_ciphertext` | `varchar(2048)` | nullable | Versioned AES-GCM ciphertext; never return in a DTO. |
| `mfa_pending_secret_ciphertext` | `varchar(2048)` | nullable | Unconfirmed enrollment secret; clear on confirmation/restart. |
| `mfa_last_used_step` | `bigint` | nullable | Check null or non-negative; prevents TOTP replay. |

Required named checks:

- `ck_user_token_version`: `token_version >= 0`
- `ck_user_mfa_last_used_step`: `mfa_last_used_step IS NULL OR mfa_last_used_step >= 0`
- `ck_user_enabled_mfa_has_secret`: `mfa_enabled = false OR mfa_secret_ciphertext IS NOT NULL`

## `auth_login_throttles`

- `id uuid` primary key
- `scope varchar(16)` not null; check `scope IN ('account', 'network')`
- `identifier_digest varchar(64)` not null
- `failed_attempts integer` not null, default `0`; check non-negative
- `window_started_at timestamptz` not null
- `blocked_until timestamptz` nullable
- `created_at timestamptz` and `updated_at timestamptz` not null
- unique (`scope`, `identifier_digest`) named
  `uq_auth_throttle_scope_digest`
- index `blocked_until` for expiry/maintenance

Only keyed digests are persisted; raw email and network identifiers must not be
stored in this table. A maintenance job must remove rows whose `updated_at` is
older than the configured window plus lockout duration so distributed unknown-
account traffic cannot grow this table indefinitely.

## `revoked_access_tokens`

- `id uuid` primary key
- `organization_id uuid` not null; backfill through `users`; include in forced RLS
- `user_id uuid` not null, foreign key `users.id` with `ON DELETE CASCADE`, indexed
- composite foreign key (`organization_id`, `user_id`) to
  `users (organization_id, id)` and unique (`organization_id`, `id`)
- `jti_digest varchar(64)` not null and unique
- `expires_at timestamptz` not null, indexed
- `revoked_at timestamptz` not null, default current time

An operational cleanup job should delete rows after `expires_at`.

## `auth_refresh_sessions`

- `id uuid` primary key; this opaque family ID is the access-token `sid`
- `organization_id uuid` not null; backfill through `users`; include in forced RLS
- `user_id uuid` not null, foreign key `users.id` with `ON DELETE CASCADE`, indexed
- composite foreign key (`organization_id`, `user_id`) to
  `users (organization_id, id)` and unique (`organization_id`, `id`)
- `token_version integer` not null; check non-negative
- `expires_at timestamptz` not null, indexed
- `last_used_at timestamptz` nullable
- `revoked_at timestamptz` nullable
- `revoke_reason varchar(64)` nullable
- `created_at timestamptz` not null, default current time
- composite index (`user_id`, `revoked_at`) named
  `ix_auth_refresh_session_user_active`
- check `ck_auth_refresh_session_token_version`: `token_version >= 0`

## `auth_refresh_tokens`

- `id uuid` primary key
- `organization_id uuid` not null; backfill through the refresh session; include
  in forced RLS; unique (`organization_id`, `id`)
- `session_id uuid` not null, foreign key `auth_refresh_sessions.id` with
  `ON DELETE CASCADE`, indexed
- composite foreign key (`organization_id`, `session_id`) to
  `auth_refresh_sessions (organization_id, id)`
- `token_digest varchar(64)` not null and unique
- `expires_at timestamptz` not null, indexed
- `consumed_at timestamptz` nullable
- `replaced_by_token_id uuid` nullable, self foreign key with `ON DELETE SET NULL`
- composite self foreign key (`organization_id`, `replaced_by_token_id`) to
  `auth_refresh_tokens (organization_id, id)` with `ON DELETE SET NULL`
- `created_at timestamptz` not null, default current time

Refresh tokens have at least 384 bits of randomness and only a keyed digest is
stored. Rotation consumes the presented row and creates its successor in one
transaction. The successor is a domain-separated keyed derivation of the
presented opaque token, so separate API replicas can return the same value
without persisting recoverable token material. A consumed token may return that
same unconsumed successor only during the bounded
`AUTH_REFRESH_RETRY_GRACE_SECONDS` window (1–30 seconds). Reuse after that window,
or after the successor has itself been consumed, revokes all user sessions and
increments `users.token_version`. An operational cleanup job should delete
expired or revoked refresh families after the incident-retention window.

## `user_mfa_recovery_codes`

- `id uuid` primary key
- `organization_id uuid` not null; backfill through `users`; include in forced RLS
- `user_id uuid` not null, foreign key `users.id` with `ON DELETE CASCADE`, indexed
- composite foreign key (`organization_id`, `user_id`) to
  `users (organization_id, id)` and unique (`organization_id`, `id`)
- `code_digest varchar(64)` not null
- `used_at timestamptz` nullable
- `created_at timestamptz` not null, default current time
- unique (`user_id`, `code_digest`) named `uq_user_mfa_recovery_code`

Only keyed digests are stored. Plain recovery codes are returned once at MFA
confirmation and cannot be reconstructed.

## Rollout requirements

1. Add and backfill the `users` columns before adding the non-null/check
   constraints; existing rows become active, version `0`, and unenrolled.
2. Create all five auth tables and indexes before starting the new API.
3. Configure an independent `AUTH_MFA_ENCRYPTION_KEY` of at least 32 characters
   on every API instance. Rotation requires decrypt/re-encrypt support and must
   not be performed by merely replacing the value.
4. Expect every legacy JWT to be invalid because it lacks the new access token
   type, version, and MFA claims. Existing administrators must complete MFA
   enrollment at their next password login.
5. Deploy the web MFA challenge/enrollment flow and refresh handling together
   with this backend. Access tokens default to 15 minutes; refresh families
   default to a non-sliding 14-day lifetime (`AUTH_REFRESH_EXPIRES_DAYS`). The
   same-origin BFF must store the refresh token in an HttpOnly, Secure,
   SameSite cookie and replace it atomically after every `/auth/refresh` call.
6. Set `AUTH_REFRESH_RETRY_GRACE_SECONDS` to the smallest value that covers the
   production BFF's cross-replica request overlap (10 seconds by default and no
   more than 30). Alert on consumed-token retries outside that grace window.
