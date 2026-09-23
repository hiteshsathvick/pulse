# Threat model

Phase 22's DoD deliverable (`SPEC.md` §7: "a written threat model exists"). Reports what's actually in
place and what's actually still open, including things that don't flatter the app, because the point of a
threat model is to surface those before a real deployment does — same spirit as `PERFORMANCE.md`.

## Assets

- **Customer behavioral event data** in ClickHouse (`events`) — the whole point of the product, and the
  thing with the most GDPR/privacy exposure: arbitrary properties a customer's own app sends, which can
  include PII if the customer doesn't configure PII rules.
- **Control-plane data** in Postgres — org/project/membership/user records, API keys (hashed), refresh
  tokens (hashed), invites (hashed), audit logs, billing/subscription state.
- **Credentials in transit/at rest**: user passwords (Argon2-hashed, never stored plain), API keys
  (SHA-256-hashed, shown once at creation), JWT access/refresh tokens, the `pii_hash_secret` (an HMAC key,
  not a password — see below).
- **The ingestion pipeline itself** (Redis stream → worker → ClickHouse/object storage) — availability
  matters here nearly as much as confidentiality: an outage or a poison-event flood shouldn't lose data or
  take the API down.

## Actors / trust levels

| Actor | Reaches | Trust level |
|---|---|---|
| Console user (browser, JWT) | Everything their `Membership.role` allows, scoped to their own org(s) | Authenticated, role-scoped |
| A customer's own end-user app (write API key) | `POST /ingest` only | Unauthenticated from Pulse's perspective — the write key is the only gate, and it can only ingest, never read/query/delete |
| A customer's own backend/server (read API key) | `query`/`export` routes for exactly one project | Server-side secret, never meant to reach a browser |
| An anonymous caller with no credentials | `/health`, `/auth/register`, `/auth/login` | Untrusted by definition |
| An event's own payload content (`properties`, `event_name`, NL-to-query question text) | Nothing directly — always data, never instructions | **Always untrusted**, regardless of which authenticated actor sent it |

The last row is the one this app has been most deliberate about since Phase 18/19: an ingested event's
properties, a natural-language question, and a caller-supplied webhook URL are all just data. Nothing in
this codebase ever interpolates such a value into SQL, a shell command, or a trust decision.

## Trust boundaries and what's already enforced there

1. **Tenant isolation (Postgres).** Row-Level Security on every tenant table (`org_id`), `FORCE`d so even
   the table owner can't bypass it — except the app connects as `pulse_app`, a non-superuser role
   specifically *because* Postgres superusers bypass RLS unconditionally, FORCE or not (a real bug this
   exact gap caused locally during Phase 20/21 work, caught by `test_tenant_isolation.py` and friends, not
   silently missed). `session_scope()` with no `org_id` default-denies every RLS table via a sentinel UUID
   no real row can ever match.
2. **Tenant isolation (ClickHouse).** No RLS equivalent exists in ClickHouse, so it's enforced entirely in
   the query-building layer (`pulse/query/builder.py`, `pulse/api/export.py`, `pulse/retention/service.py`,
   `pulse/services/deletion.py`): `org_id`/`project_id` are always function parameters, injected by the
   server from the authenticated caller's own context, never read off client input — there is structurally
   nothing for a client to smuggle. Every ClickHouse query in this codebase uses bound parameters
   (`{name:Type}`), never string-built SQL. `test_query_engine.py`, `test_export_api.py`,
   `test_retention.py`, and `test_deletion.py` each have an explicit cross-tenant-leak test.
3. **Authentication.** JWT (short-lived access + rotating, hashed refresh tokens) for console users; a
   separate, purpose-scoped API key (read-only or write-only, SHA-256-hashed, revocable) for
   server-to-server traffic. A write key can only ingest; a read key can only query/export; neither can do
   what the other does, checked explicitly (`require_write_key`/`require_read_key`/`resolve_query_scope`).
4. **Authorization.** A four-level role hierarchy (Viewer < Member < Admin < Owner) checked as a FastAPI
   dependency on every mutating route, not inline per-handler. Destructive/compliance actions get a higher
   bar than ordinary config: PII rules and webhook test-send are Admin+; GDPR subject deletion — the single
   most destructive action in the app, an irreversible cross-partition data delete — is Owner-only, above
   every other action.
5. **Untrusted-content boundaries.**
   - *NL-to-query* (`pulse/ai/translator.py`): a question's text can only ever produce a
     `DiscriminatedInsightSpec` (trend/funnel/retention) or a clarify response — never SQL, never a tenant
     field, never a write. `test_nl_injection.py` runs it against real adversarial input (prompt injection,
     SQL-injection-shaped strings, XSS, role-escalation phrasing, cross-org data requests, and — added this
     phase — PII/deletion-flavored attempts) and asserts every response stays inside that shape.
   - *Ingested event content* never reaches a shell, a SQL string, or a trust decision — it's either a
     ClickHouse bound parameter or (Phase 22) a value a PII rule hashes/drops before it's written anywhere.
   - *Outbound webhooks* (alerts, `pulse/api/webhooks.py`'s test-send): HMAC-SHA256-signed
     (`X-Pulse-Signature`) when a secret is configured, so a receiver can verify a payload actually came from
     this app. Retried with backoff on transient failure only (Phase 21) — never on a 4xx, since retrying a
     receiver's own rejection can't help and could be abused to hammer an attacker-supplied URL.
6. **Rate limiting.** Login attempts, ClickHouse queries, and AI/NL requests are all rate-limited per org
   (`pulse/core/rate_limit.py`), independent of each other.
7. **Data minimization at ingest (Phase 22).** PII rules (`pii_rules`, checked in
   `pulse/worker/processing.py::apply_pii_rules`) can drop a property entirely or replace it with a keyed
   HMAC (`pii_hash_secret`) before it's written to ClickHouse *or* observed by the schema registry —
   proactive (works from the very first event carrying a marked key, not just after the registry has already
   seen it once unprotected) and deterministic (a hashed value still supports unique-user-style grouping,
   since the same input always hashes the same, but isn't reversible or rainbow-table-able without the
   secret).
8. **Retention (Phase 22).** Every project has an effective retention window (its own override, or its
   org's default) enforced by a scheduled sweep (`pulse/retention/`) issuing a real `ALTER TABLE ... DELETE`
   mutation — not a promise, a real deletion, verified live against seeded old/new events
   (`test_retention.py`).
9. **Security headers** (`pulse/core/middleware.py::SecurityHeadersMiddleware`, Phase 22):
   `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, applied
   app-wide including `/ingest` (doesn't conflict with its deliberately open CORS, which is about *who* can
   call the API, not how a browser treats the response body).

## Residual risks — open, not hidden

- **GDPR subject deletion is scoped to ClickHouse `events` only** (confirmed with the user first, Phase 22
  design fork). `event_hourly`'s aggregated sketches retain a deleted subject's contribution until an
  operator next runs the existing, global, ingestion-must-be-stopped `python -m pulse.rollups rebuild` — not
  something a live API call can safely trigger inline. The raw batch archive in object storage (Phase 8) is
  batch-shaped (many users per file), not per-subject-editable without rewriting archive files. Both are
  documented gaps, not oversights.
- **PII rules don't protect the raw batch archive either**, for the same batch-shaped reason — a dropped or
  hashed property is kept out of ClickHouse and the schema registry, but the original raw JSON batch (before
  any Phase 22 transform) still lands in object storage as Phase 8 always intended it to (the
  "replay/backfill source of truth"). A customer relying on PII rules for full compliance needs to know this
  boundary exists.
- **`pii_hash_secret` has an insecure default** (`dev-insecure-pii-hash-secret-change-me`, matching
  `jwt_secret`'s own convention) — fine for dev/CI, a real deployment must set a real one via `.env`, same
  operational requirement as every other secret this app has.
- **Known, currently-unfixable dependency vulnerabilities** (Phase 22's `dependency-scan` CI job, advisory
  not blocking — see below for why): `starlette` (FastAPI's core dependency) has several CVEs with no newer
  version resolvable in this environment's package index at the fastapi version this app is pinned to;
  `pytest` (backend and sdk-python) and the `vite`/`esbuild`/`vitest` chain (sdk-js, dev-only tooling) each
  have a fix available but only via a breaking major-version bump that needs its own full regression pass —
  deliberately not done as an unrelated side effect of this phase. Tracked here, not silently ignored.
- **The dependency-scan CI job is advisory, not a blocking gate.** Given the finding above, a blocking gate
  today would leave CI permanently red over things this run can't fix, defeating "CI stays green" as a
  meaningful signal. It still runs and reports every push, so a *new* finding stays visible.
- **No Content-Security-Policy or HSTS.** This is a JSON API with no HTML of its own to scope a CSP
  against; HSTS is a deployment-level concern (only meaningful once real TLS termination sits in front of
  this) that's out of this app's own config, not something code here can decide.
- **CORS on `/ingest` is deliberately wide open** (`allow_origins` from config, `*` methods/headers) — by
  design, since `/ingest` must accept traffic from arbitrary customer domains and nothing in this app relies
  on cookies a cross-origin script could ride on regardless. Documented in `pulse/main.py` itself, repeated
  here because it's a real, intentional trade-off worth a threat model calling out explicitly.
- **A write key, once leaked, can ingest arbitrary events into that one project indefinitely** until
  revoked. No automatic anomaly detection on ingest volume/shape exists to catch a leaked key being abused
  (Phase 19's anomaly detection is for a project's own trend *insights*, not for the ingest path itself).

## Input validation

Swept this phase: every request-body free-text field across auth, orgs, projects, invites, PII rules, and
subject deletion now has an explicit `max_length` (and `min_length` where empty is meaningless) —
previously several had none at all, including `LoginRequest.password`, which meant an unbounded string could
drive a real (if minor) Argon2-hashing DoS on a deliberately unauthenticated endpoint. `IngestEvent`'s
`user_id`/`anonymous_id`/`properties` (a public, write-key-gated endpoint meant for arbitrary customer app
traffic) gained the same bounds. See `git log` for the exact fields changed this phase.
