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

10. **Observability without a data leak (Phase 23).** Telemetry is a second copy of what the app
    handles, so it is constrained on purpose. *Traces* never carry event properties,
    user ids, request bodies or header values; the worker's own spans hold only an event id, a batch id, a
    batch size and the table name. The auto-instrumented HTTP server spans do record the request URL, so
    tenant UUIDs in the path and any query-string values (e.g. an export's `event_name` filter) reach the
    trace backend -- treat the trace backend as holding tenant identifiers, though not payloads.
    *Metric labels* are route **templates**, outcomes and status codes, never an org, project or user id
    (also what keeps cardinality bounded). *Sentry* is off unless `SENTRY_DSN` is set, and when on it never
    receives request bodies, stack-frame local variables or default PII (`max_request_body_size:
    "never"`, `include_local_variables: False`) -- the SDK's defaults would ship event payloads and
    passwords to a third party the moment something errored, undoing the Phase 22 PII controls.
    *Metrics* are served on a dedicated port per process, never on the public API port.
11. **Release integrity (Phase 23).** Production never auto-deploys; the only path is the manual
    `Deploy production` workflow, which refuses a commit CI hasn't passed, then waits on a required-reviewer
    approval, then releases the API (and so its migrations) before workers. Staging deploys only after CI
    passes. Secrets are generated by Terraform and handed to Render through an environment group -- none
    are committed or pasted. The app connects as the non-superuser `pulse_app` in every environment
    (a test pins this), and Redis is provisioned `noeviction`.

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

- **Metrics endpoints are unauthenticated.** They are safe only because they are never published: the
  compose file gives the API/worker metrics ports no host mapping, and Prometheus scrapes over the internal
  network. Publishing `METRICS_PORT` (or fronting it with a public ingress) would expose route names,
  traffic shape and queue depth to anyone. Nothing in code can enforce that; it is a deployment rule.
- **The local Grafana runs anonymous-Admin** (`GF_AUTH_ANONYMOUS_ORG_ROLE=Admin`, login form disabled).
  Acceptable only because it is bound to `127.0.0.1` in a development-only compose override; it must never
  be reused as a deployed configuration.
- **A client chooses its own trace id.** `/ingest` continues whatever `traceparent` the caller sends, so a
  caller can make its spans share a trace id with another's. Trace ids are correlation handles, not
  credentials, and spans carry no event payloads, so the impact is confusing a trace view, not reading
  customer data -- but note the HTTP spans do include tenant UUIDs (in URLs), so a shared trace id is also
  a way to see two tenants' request paths side by side in the trace backend. Access to that backend is
  therefore an operator privilege, and spans must not gain payload-bearing attributes.
- **Terraform state contains every generated secret in plain text** (database, ClickHouse, S3 keys, JWT and
  PII-hash secrets). Local state files are git-ignored, but a real deployment needs a remote, access-controlled,
  encrypted backend configured before the first `apply` -- not yet set up.
- **The production approval gate is GitHub configuration, not code.** `deploy-prod.yml` names the
  `production` environment, but whether it requires reviewers is a repository setting; if it's never
  configured the "gate" is a no-op. The workflow can't detect that. Likewise `RENDER_API_KEY` is
  account-wide, so a leak of that secret is a leak of both environments.
- **Deployment has never been exercised against real accounts** (`docs/DEPLOYMENT.md`, "Not yet proven").
  The configuration is validated, not proven; expect first-deploy surprises.
- **The image scan is advisory**, for the same reason the dependency scan is: known starlette CVEs with no
  resolvable fix would keep a blocking gate permanently red.
- **Backpressure is a 5xx, by design.** With `noeviction`, a Redis that fills (a worker outage long enough
  to exhaust memory) makes `/ingest` fail rather than silently drop events. The ingest stream is now bounded
  in steady state (acked entries are deleted -- previously it grew forever, found by the Phase 23
  stream-length gauge), but an extended outage can still fill it; the consumer-lag and stream-length panels
  are the early warning.

## Input validation

Swept this phase: every request-body free-text field across auth, orgs, projects, invites, PII rules, and
subject deletion now has an explicit `max_length` (and `min_length` where empty is meaningless) —
previously several had none at all, including `LoginRequest.password`, which meant an unbounded string could
drive a real (if minor) Argon2-hashing DoS on a deliberately unauthenticated endpoint. `IngestEvent`'s
`user_id`/`anonymous_id`/`properties` (a public, write-key-gated endpoint meant for arbitrary customer app
traffic) gained the same bounds. See `git log` for the exact fields changed this phase.
