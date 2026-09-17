# Pulse — Engineering Specification (SPEC.md)

> Authoritative engineering detail for **Pulse**, a self-hostable real-time product-analytics / event
> data platform. This is the single source of truth Claude Code reads before each phase. The higher-level
> rationale lives in `PULSE_PROJECT_GUIDE.md`; the agent operating rules live in `CLAUDE.md`. When code and
> this spec disagree, update this spec in the same PR — the spec is not allowed to go stale.

**Status:** living document. **Owner:** Hitesh. **Primary assistant:** Claude Code (VS Code).

---

## 1. Goals & non-goals

### 1.1 Product goals
- Ingest behavioral events from any client (browser, mobile, server) via a small SDK or raw HTTP.
- Store events at volume in a columnar store and query them fast: trends, funnels, retention, breakdowns.
- Let non-technical users build insights and dashboards; let technical users ask in natural language.
- Be genuinely self-hostable and data-ownership-friendly (export raw events, per-project retention, PII controls).

### 1.2 Engineering goals (why this project exists)
- Demonstrate a **data-intensive systems** design end to end: separated write/read paths, a buffered
  ingestion pipeline, a columnar OLAP store, pre-aggregation, and query-cost control.
- Every phase leaves a working, demoable, tested app. No phase is "done" until its DoD and tests pass.

### 1.3 Non-goals (v1)
- Not a warehouse-native tool (querying the user's Snowflake/BigQuery is a post-v1 stretch).
- Not session replay, not experimentation/feature-flags (that's Cortex), not a CDP.
- Not multi-region or horizontally-sharded ClickHouse in v1 — single-cluster, designed to extract later.
- Not exactly-once end-to-end. Target is **at-least-once ingestion + `event_id` idempotency** so the
  observable result is effectively exactly-once at query time.

### 1.4 Non-functional requirements (targets, refined by load tests in Phase 17/23)
- **Ingestion:** sustain ≥ 5,000 events/sec on a modest single node in load tests; `/ingest` p99 < 50 ms
  (it only validates + buffers); zero event loss under worker restart; poison events land in DLQ, never dropped.
- **Query:** dashboard insights (rollup-eligible) p95 < 1 s; ad-hoc raw queries p95 < 5 s over the test
  dataset; every query bounded by scan/row/time caps.
- **Isolation:** no query or ingest path can read or write another org's data — enforced in code and proven
  by tenant-leakage tests.
- **Availability posture:** ingestion degrades gracefully (buffer absorbs bursts; API stays up if
  ClickHouse is briefly down).

---

## 2. Tech stack (pinned intentions; exact versions fixed in Phase 0)

- **Backend:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 (Postgres), Alembic (Postgres migrations),
  an official ClickHouse client (`clickhouse-connect`), Arq or Celery for workers.
- **Event buffer:** Redis Streams (v1) with a consumer-group design that can be swapped for Kafka/Redpanda.
- **Stores:** PostgreSQL 16 (control plane), ClickHouse (event store), Redis (buffer + cache + rate limit),
  S3/MinIO (raw event archive + exports).
- **Frontend:** Next.js (App Router) + TypeScript + Tailwind + TanStack Query; a charting lib (Recharts or
  ECharts) chosen in Phase 15.
- **SDK:** a TypeScript package (browser + Node) published from the monorepo; a thin Python server SDK.
- **AI:** provider abstraction (OpenAI/Anthropic/local) — port from Helix; used for NL-to-query and anomaly explanations.
- **Tooling:** ruff, mypy, pytest, pre-commit; Playwright for E2E; k6 or Locust for load; GitHub Actions CI.
- **Deploy:** Docker + Compose (local); Render (staging/prod) with managed Postgres + Redis, ClickHouse via
  ClickHouse Cloud or self-managed container; IaC (Terraform) in Phase 23.

---

## 3. Architecture invariants (never violate these)

1. **Two stores, two roles.** Postgres = control plane (users, orgs, projects, keys, registry, insights,
   dashboards, alerts, billing). ClickHouse = event plane (behavioral events + rollups). No event-scale
   data in Postgres; no control-plane relational data in ClickHouse.
2. **Write path ≠ read path.** Ingestion never does a synchronous insert into ClickHouse. `/ingest`
   validates lightly and appends to the buffer, then returns `202`. Workers batch-insert into ClickHouse.
3. **Tenant scope is injected, never trusted.** Every ClickHouse query is built by the query engine, which
   always injects `org_id` and `project_id` predicates. The client cannot supply them.
4. **`event_id` is the idempotency key.** Every event carries a client-generated UUID; workers dedup on it.
5. **Bad data is routed, not dropped.** Malformed/schema-violating events go to a dead-letter sink.
6. **Modules own their tables.** No module reaches into another module's tables; cross-module calls go
   through the module's service interface. This is what makes later service extraction a deploy change.

---

## 4. Data model — control plane (PostgreSQL)

All tenant-scoped tables carry `org_id` and are protected by Row-Level Security. Standard mixins: `id`
(UUID), `created_at`, `updated_at`, `deleted_at` (soft delete).

- **User** — `id, email (unique), password_hash (Argon2), name, is_active, ...`
- **Organization** — `id, name, slug, retention_days (default), ...`
- **Membership** — `id, org_id, user_id, role (owner|admin|member|viewer)`
- **Project** — `id, org_id, name, slug, timezone` — the analytics unit and the write-key boundary.
- **ApiKey** — `id, org_id, project_id (nullable), type (write|read), key_hash, scopes[], last_used_at,
  revoked_at`. **Write keys** are public/client-side, append-only to their project. **Read keys** are
  server-side, scoped to query.
- **EventSchema** (registry) — `id, org_id, project_id, event_name, status (active|deprecated|hidden),
  first_seen_at, volume_estimate`.
- **PropertySchema** — `id, event_schema_id (nullable = event-agnostic), key, inferred_type
  (string|number|bool|datetime), is_pii (bool), status`.
- **Insight** — `id, org_id, project_id, name, kind (trend|funnel|retention), spec (JSONB), created_by`.
- **Dashboard** — `id, org_id, project_id, name, layout (JSONB), default_range, shared_scope`.
- **DashboardItem** — `id, dashboard_id, insight_id, position (JSONB)`.
- **Alert** — `id, org_id, project_id, insight_id, rule (JSONB: threshold|anomaly), channels[], enabled`.
- **AuditLog** — `id, org_id, actor_id, action, target, metadata (JSONB), created_at`.
- **Billing** — `Subscription`, `UsageRecord (org_id, period, events_ingested, mtu)` (Stripe-linked).

> As of Phase 4: `User`/`Organization`/`Membership`/`Project` (Phase 2), `RefreshToken` (Phase 3, not
> listed above — see §6.2), `Invite` and `AuditLog` (Phase 4, added ahead of the table above's original
> phase notes — see §4.1). The rest of this list is the full eventual shape; each remaining table is built
> in the phase that needs it (`ApiKey` Phase 5, schema registry Phase 9, `Insight`/`Dashboard` Phase 15/16,
> `Alert` Phase 19, `Billing` Phase 20).

### 4.1 Row-Level Security mechanism (implemented Phase 2, extended Phase 3/4)

Of the four Phase 2 tables, only `Membership` and `Project` carry `org_id` and are RLS-protected;
`User` is a global identity (which orgs it belongs to lives in `Membership`) and `Organization` *is* the
tenant rather than referencing one, so neither is RLS-scoped itself.

**The actual rule, refined by Phase 3/4:** RLS protects tables reached via a *browse-this-org's-data*
pattern (`Membership`, `Project`, and now `AuditLog`) — it's defense-in-depth against an application bug
that forgets to filter by `org_id`. It does **not** protect tables reached via *token redemption*
(`RefreshToken`, `Invite`), even though `Invite` carries `org_id`: accepting an invite (or refreshing a
session) means looking a row up by an opaque secret *before* the caller has any org context to scope a
session by — the token itself is the security boundary there, not RLS. Listing invites for management
(`GET /orgs/{id}/invites`) still explicitly filters by `org_id` in the query, reached only once the
caller's membership in that org has already been confirmed by `get_org_membership` (§6.2-adjacent, see
`pulse/api/dependencies.py`).

**Two GUCs, not one, as of Phase 4.** `app.current_org_id` (Phase 2) scopes the common case. A second,
`app.current_user_id` GUC (migration 0004) exists purely for "which orgs am I in" (`GET /orgs`): normally
`Membership` is visible only within one org's scope, but that query is inherently cross-org from the
user's own perspective. A second, **`FOR SELECT`-only** permissive policy on `memberships` allows
visibility by `user_id` in addition to `org_id` — scoped to `SELECT` specifically so it can never loosen
the org-scoped `WITH CHECK` that governs INSERT/UPDATE/DELETE, which would otherwise let a user self-grant
membership in any org just by setting their own `user_id`. `session_scope()` takes both parameters; an
unset one gets a nil-UUID sentinel exactly like the original `org_id` case, for the same NULL/`''`
ambiguity reason below.

- **Session-scoped GUC, not superuser demotion.** Auth (Phase 3) doesn't exist yet, so tenant scope for a
  given session is set explicitly: `SELECT set_config('app.current_org_id', :org_id, true)` (parameterized,
  transaction-local) at the start of an org-scoped session. Every RLS policy is
  `USING (org_id = current_setting('app.current_org_id', true)::uuid)` with an identical `WITH CHECK` —
  the latter is what stops a session scoped to org A from *writing* a row claiming org B, not just reading
  one. An "unscoped" session sets this GUC to the nil UUID (`00000000-...-000000000000`), which can never
  equal a real `gen_random_uuid()` value — not NULL/unset, because a custom (unregistered) GUC's
  `SET LOCAL` doesn't reliably read back as NULL after the setting transaction commits on a pooled
  connection; it can come back as `''`, which fails the policy's `::uuid` cast outright instead of just
  not matching. The sentinel sidesteps that ambiguity entirely.
- **`FORCE ROW LEVEL SECURITY` is mandatory, not optional.** Postgres does not apply RLS policies to a
  table's owning role by default — only `FORCE` makes it apply even to the owner. Since the app connects
  as the same role that creates the tables, skipping `FORCE` would make every policy a silent no-op for
  every real query the app makes.
- **The app never connects as the Postgres bootstrap superuser.** RLS — even with `FORCE` — is
  unconditionally bypassed for superuser roles, and the official `postgres` image's `POSTGRES_USER` is
  created as exactly that (the initdb bootstrap role), which Postgres also refuses to ever demote
  (`ALTER ROLE ... NOSUPERUSER` on it fails: "the bootstrap user must have the SUPERUSER attribute"). So a
  second, ordinary role (`pulse_app`) is what the app and migrations actually connect as; `alembic/env.py`
  creates it idempotently, using the bootstrap role, before running any migration. `DATABASE_URL` points at
  `pulse_app`; `DATABASE_BOOTSTRAP_URL` (superuser) is used only for that one bootstrap step. A practical
  consequence: `/health`'s Postgres check will report an auth error until `alembic upgrade head` has been
  run at least once against a given database — see `README.md`.

### 4.2 Insight spec shapes (JSONB, versioned)

```jsonc
// trend
{ "kind": "trend", "version": 1,
  "events": ["checkout completed"],
  "measure": "unique_users",            // count | unique_users | property_sum:<key> | property_avg:<key>
  "filters": [{"key":"platform","op":"eq","value":"mobile"}],
  "breakdown": "utm_source",            // optional single dimension
  "range": {"from":"2026-08-01","to":"2026-09-01","tz":"project"},
  "granularity": "day" }                // hour | day | week | month

// funnel
{ "kind": "funnel", "version": 1,
  "steps": [{"event":"signed up"},{"event":"added to cart"},{"event":"checkout completed"}],
  "window": {"value": 7, "unit": "day"},   // conversion window, order enforced
  "filters": [], "breakdown": null,
  "range": {"from":"...","to":"...","tz":"project"} }

// retention
{ "kind": "retention", "version": 1,
  "born_event": "signed up",
  "return_event": "checkout completed",  // may equal born_event for "came back at all"
  "period": "week",                      // day | week
  "periods": 8,
  "range": {"from":"...","to":"...","tz":"project"} }
```

The insight spec is the **only** thing NL-to-query is allowed to produce. It is compiled to SQL by the
query engine, so tenant scoping, caps, and validation are inherited for free.

---

## 5. Data model — event plane (ClickHouse)

### 5.1 Raw events table

```sql
CREATE TABLE events
(
    org_id        UUID,
    project_id    UUID,
    event_id      UUID,                          -- client-generated; idempotency key
    event_name    LowCardinality(String),
    user_id       String,                        -- known/identified id ('' if anonymous)
    anonymous_id  String,                        -- device/session id
    timestamp     DateTime64(3),                 -- client event time (UTC)
    received_at   DateTime64(3),                 -- server ingest time (UTC)
    properties    Map(String, String),           -- open-ended; typed reads via casting
    -- promoted/typed columns (added over time for hot properties):
    -- platform   LowCardinality(String) DEFAULT properties['platform'],
    _ingest_batch UUID                            -- for replay/debug
)
ENGINE = MergeTree
PARTITION BY (org_id, toYYYYMM(timestamp))
ORDER BY (org_id, project_id, event_name, timestamp)
TTL toDateTime(timestamp) + INTERVAL 365 DAY      -- per-org override applied via config/materialized policy
SETTINGS index_granularity = 8192;
```

Design notes (be able to defend each in an interview):
- **`ORDER BY (org_id, project_id, event_name, timestamp)`** matches the dominant filter and makes the
  common query a bounded range scan. This is the single biggest performance lever.
- **`PARTITION BY (org_id, toYYYYMM(...))`** isolates tenants and makes retention/TTL and per-tenant
  deletion cheap (drop parts, not row-by-row).
- **`properties Map(String,String)`** gives schema flexibility with no migration per new property; hot
  properties get **promoted** to typed materialized columns for speed once the registry sees their volume.
- **`LowCardinality`** on `event_name`/promoted enums shrinks storage and speeds group-bys.
- **Dedup:** dedup on `event_id` at the worker (keep a short-TTL seen-set in Redis) and/or use a
  `ReplacingMergeTree(received_at)` variant keyed by `event_id` as defense-in-depth. Document which and why.

### 5.2 Rollups (Phase 17)

```sql
-- daily unique users per event, per project (AggregatingMergeTree via MV)
CREATE MATERIALIZED VIEW mv_event_daily
ENGINE = AggregatingMergeTree
PARTITION BY (org_id, toYYYYMM(day))
ORDER BY (org_id, project_id, event_name, day)
AS SELECT
    org_id, project_id, event_name,
    toDate(timestamp) AS day,
    uniqState(user_id)  AS users_state,
    count()             AS events
FROM events
GROUP BY org_id, project_id, event_name, day;
```

Rollup-eligible insights read from the MV (`uniqMerge(users_state)`); everything else falls back to raw.

### 5.3 Query building (the engine)

- Input: a validated insight spec + resolved `{org_id, project_id, timezone}`.
- Output: parameterized ClickHouse SQL with **org/project predicates always present**, a time-range bound,
  and hard caps (`max_execution_time`, `max_rows_to_read`, `LIMIT`).
- **Trend:** `SELECT toStartOf<granularity>(timestamp, tz), <measure> ... GROUP BY bucket [, breakdown]`.
- **Funnel:** `windowFunnel(<window_seconds>)(timestamp, event_name = s1, ..., event_name = sN)` per user;
  bucket by max level reached → per-step counts, conversion %, drop-off.
- **Retention:** `retention(<born cond>, <return cond @ period 1>, ...)` per user → cohort grid.
- **Uniques:** `uniqExact` for small/verifiable results; `uniq`/`uniqCombined` when volume demands.
- All timezone bucketing uses the project timezone; store UTC, bucket with tz — never bucket on raw UTC.

---

## 6. API contract (v1 surface; full OpenAPI generated by the app)

Auth: control-plane endpoints use JWT (user) or read API keys (server). `/ingest` uses **write keys only**.

```
# Auth
POST   /api/v1/auth/register | login | refresh | logout
GET    /api/v1/auth/me

# Orgs / projects / members / invites / keys
CRUD   /api/v1/orgs, /orgs/{id}/projects, /orgs/{id}/members    # Phase 4
CRUD   /api/v1/orgs/{id}/invites                                # Phase 4
POST   /api/v1/invites/accept                                   # Phase 4
CRUD   /api/v1/projects/{id}/keys                                # Phase 5 -- not built yet

# Ingestion (write key)
POST   /ingest                 # { batch: [ {event_id, event, user_id?, anonymous_id?, timestamp?, properties?} ] } -> 202

# Schema registry
GET    /api/v1/projects/{id}/schema/events
GET    /api/v1/projects/{id}/schema/events/{event}/properties
PATCH  /api/v1/projects/{id}/schema/...          # deprecate/hide, mark PII

# Insights & queries
POST   /api/v1/projects/{id}/query/trend
POST   /api/v1/projects/{id}/query/funnel
POST   /api/v1/projects/{id}/query/retention
POST   /api/v1/projects/{id}/query/nl            # natural language -> spec (returns interpreted spec + result)
CRUD   /api/v1/projects/{id}/insights

# Dashboards & alerts
CRUD   /api/v1/projects/{id}/dashboards, /alerts

# Public/read API, exports, webhooks
GET    /api/v1/export/events                      # streamed CSV/JSON, scoped
POST   /api/v1/webhooks (mgmt)                    # outbound signed webhooks
GET    /health                                    # checks Postgres + ClickHouse + Redis
```

Every response uses a standard envelope and standard error shape (defined Phase 1). Every list endpoint is
paginated. Every mutating action writes an audit log.

### 6.1 Standard error envelope (defined Phase 1)

Every non-2xx response — validation failures, `HTTPException`s raised anywhere in the app, and unhandled
exceptions alike — has this shape:

```jsonc
{
  "error": {
    "code": "validation_error",       // stable slug; see below
    "message": "Request validation failed.",
    "request_id": "5c1c...",          // echoes X-Request-ID; null if somehow unset
    "fields": [ /* present only for validation_error: pydantic's exc.errors(), jsonable-encoded */ ]
  }
}
```

`code` is derived from the HTTP status for the common cases (`bad_request`, `unauthorized`, `forbidden`,
`not_found`, `conflict`, `validation_error`, `rate_limited`), falls back to `http_error` for anything else
raised via `HTTPException`, and is always `internal_error` for the catch-all 500 handler — which never
leaks the real exception message or traceback to the client; those are logged server-side only, keyed by
`request_id`. This is a Phase 1 scope decision: only the **error** shape is standardized here. A generic
envelope wrapping every *successful* response body is not part of any phase's DoD and is not implemented.

### 6.2 Authentication mechanism (implemented Phase 3)

- **Access token** — a stateless JWT (HS256, 15 min default TTL). Verified by signature and expiry only;
  never persisted. Sent as `Authorization: Bearer <token>`; a protected route (e.g. `GET /auth/me`) depends
  on `get_current_user`, which returns `401` uniformly for a missing, malformed, expired, or
  signature-invalid token, and for a token whose user no longer exists or is inactive.
- **Refresh token** — deliberately *not* a JWT: an opaque `secrets.token_urlsafe` string, stored **hashed**
  (SHA-256) in `refresh_tokens` (30 day default TTL), never plaintext. This is what makes rotation and
  revocation real rather than aspirational — a bare JWT can't be revoked before it expires without a
  server-side blocklist, but a DB-backed opaque token can be checked, rotated, and revoked directly.
  - **Rotation:** every `POST /auth/refresh` call immediately marks the presented refresh token revoked and
    issues a brand new access+refresh pair. Reusing an already-rotated refresh token fails — a stolen
    refresh token is only useful once.
  - **Revocation:** `POST /auth/logout` marks the given refresh token revoked. Idempotent: logging out with
    an already-revoked or unknown token is not an error.
- **Rate limiting** — `POST /auth/login` only (the DoD's "brute-force limit"; register/refresh/logout have a
  different abuse profile). A fixed-window counter in Redis, keyed by client IP, default 5 attempts per 5
  minutes, counting every attempt (not just failures) so the endpoint itself is protected, not only
  wrong-password floods. Exceeding it returns `429` with `code: "rate_limited"` (§6.1).
- Register and login are separate calls — register never returns tokens, per the DoD's literal
  "signup → login → protected route" sequence.

---

## 7. Per-phase authoritative detail

> The phase list, objectives, and Definitions of Done are in `PULSE_PROJECT_GUIDE.md` §"PHASE 8". This
> section adds the binding acceptance criteria and the tests each phase must ship. **A phase is complete
> only when every checkbox is true and CI is green.** Do not advance otherwise.

**Phase 0 — Repo & setup.** ☐ `docker compose up` boots api, ingest-worker, postgres, clickhouse, redis,
minio, frontend. ☐ `/health` returns 200 and verifies Postgres **and** ClickHouse connectivity. ☐ CI runs
lint/type/test/build green. ☐ `SPEC.md`, `CLAUDE.md` committed before app code.

**Phase 1 — Backend foundation.** ☐ env-based settings; ☐ structured JSON logs with request IDs; ☐ global
exception handlers + standard error envelope; ☐ Alembic up/down works in CI; ☐ a ClickHouse migration
runner exists and applies versioned DDL. Tests: config load, error handler, migration round-trips (both stores).

**Phase 2 — Control-plane models + RLS.** ☐ User/Org/Membership/Project with mixins + soft delete; ☐ RLS
policies on tenant tables. Tests: **tenant isolation** — a query in org A cannot see org B rows; migrations reversible.

**Phase 3 — Auth.** ☐ Argon2; ☐ JWT access+refresh with rotation + revocation; ☐ auth rate limiting. Tests:
register/login/refresh/logout, token expiry, brute-force limit. DoD: signup → login → protected route.

**Phase 4 — Orgs/projects/membership.** ☐ org + project CRUD; ☐ email-token invites + accept; ☐ switcher.
Tests: invite lifecycle, tenant-context resolution, project scoping.

**Phase 5 — RBAC + API keys.** ☐ permission dependency on every endpoint; ☐ write-key vs read-key model;
☐ key hashing + revocation. Tests: permission matrix; **write keys can only ingest, read keys can only query**.

**Phase 6 — Event model + ClickHouse schema.** ☐ `events` table per §5.1; ☐ fake-event generator; ☐ typed
round-trip. Tests: insert/read round-trip; org/project columns always present; partition/order verified.

**Phase 7 — Ingest API.** ☐ `POST /ingest` batch, write-key auth, light validation, enqueue to Redis
Streams, `202`; ☐ size limits + rate limit; ☐ browser CORS. Tests: batch buffered (nothing hits ClickHouse
synchronously); oversized/malformed rejected; write-key scoping; p99 latency sanity.

**Phase 8 — Ingestion workers.** ☐ consumer-group workers; ☐ batch by size/time; ☐ registry validation +
enrichment; ☐ **`event_id` dedup**; ☐ large batched inserts; ☐ **DLQ**; ☐ raw-batch archive to object
storage. Tests: idempotent re-consume (no double count); poison → DLQ; **worker crash mid-batch → no loss,
no dup**; backpressure behavior.

**Phase 9 — Schema registry.** ☐ auto-register new events/properties with inferred types; ☐ deprecate/hide;
☐ mark PII. Tests: new event auto-registers; type-conflict flagged; unknown events still ingest.

**Phase 10 — SDK.** ☐ TS SDK (`identify`/`track`/`page`) with local buffer, batched flush on
interval/size/`beforeunload`, backoff retry, `event_id`; ☐ thin Python server SDK. Tests: offline buffering,
flush triggers, retry/idempotency, no loss on unload.

**Phase 11 — Query engine + trends.** ☐ spec→SQL builder with **org/project injected**, caps, timeouts; ☐
Redis result cache. Tests: spec→SQL correctness; **tenant-leakage tests**; tz-correct bucketing; cache
hit/miss.

**Phase 12 — Funnels.** ☐ `windowFunnel`-based; ☐ per-step counts + conversion/drop-off; ☐ breakdown.
Tests: ordering enforced; window boundaries; **hand-computed fixture funnel matches exactly**.

**Phase 13 — Retention.** ☐ `retention`-based cohort grids + curve. Tests: cohort assignment; day/week
bucketing; **hand-computed retention fixture matches**.

**Phase 14 — Frontend foundation.** ☐ shell/nav/design system/data layer/auth routing/org+project switcher.
Tests: components + a couple of Playwright flows.

**Phase 15 — Insight builder + charts.** ☐ builder with schema-driven autocomplete; ☐ line/bar/table +
funnel + retention viz; ☐ save insights. Tests: builder emits valid specs; each result type renders; saved
insights reload.

**Phase 16 — Dashboards.** ☐ dashboard CRUD; ☐ arrange saved insights; ☐ per-dashboard range + refresh; ☐
RBAC sharing. Tests: layout persistence; permission-scoped sharing; refresh correctness.

**Phase 17 — Rollups + performance.** ☐ MVs per §5.2; ☐ router prefers rollups; ☐ per-tenant query rate
limits; ☐ load tests. Tests: **rollup vs raw parity**; documented before/after benchmarks meeting §1.4 targets.

**Phase 18 — NL-to-query.** ☐ NL → **insight spec** (never raw SQL) → existing safe builder; ☐ interpreted
spec shown before running; ☐ read-only/scope/cap guardrails; ☐ eval set gates regressions. Tests:
injection attempts can't cross tenant or write; ambiguous → clarify; NL→spec eval thresholds.

**Phase 19 — Anomaly + alerts.** ☐ threshold + statistical anomaly (moving avg + z-score / seasonal
baseline); ☐ alert rules; ☐ email + in-app + outbound webhook. Tests: fires on breach not noise;
seasonality handled; delivery honored.

**Phase 20 — Billing/metering.** ☐ Stripe test mode; ☐ meter events/MTU from **real ingestion counts**; ☐
quotas + soft/hard limits; ☐ invoices. Tests: metering accuracy vs ingested volume; quota enforcement;
webhook handling.

**Phase 21 — Public API/exports/webhooks.** ☐ scoped read API; ☐ streamed CSV/JSON export (results + raw
events); ☐ signed, retried outbound webhooks. Tests: key scoping; export completeness; webhook signing/retry.

**Phase 22 — Security/retention/PII.** ☐ per-project TTL retention; ☐ PII allow/deny + hash/drop at ingest;
☐ user-deletion across partitions; ☐ input-validation sweep, headers, dep scan; ☐ threat model. Tests: TTL
expires data; deletion removes a subject's events; PII fields never reach ClickHouse; injection suite passes.

**Phase 23 — Observability/CI-CD/deploy.** ☐ OTel trace SDK→API→buffer→worker→ClickHouse; ☐ Grafana:
ingestion lag, batch size, events/sec, DLQ rate, query p50/95/99; ☐ Sentry; ☐ full CI; ☐ migrations-on-
deploy (both stores); ☐ staging+prod on Render; ☐ IaC. DoD: merge→staging auto; prod one gated click; one
trace follows an event end to end.

---

## 8. Testing strategy (what "tested" means here)

- **Unit:** query builder (spec→SQL), funnel/retention math against fixtures with **known hand-computed
  answers**, SDK buffering/retry, dedup logic.
- **Integration:** full ingest path (SDK → API → buffer → worker → ClickHouse → query) on ephemeral
  containers; migration round-trips both stores.
- **Isolation:** dedicated **tenant-leakage** suite — for every query type, assert org A can never read org
  B; assert write keys can't query and read keys can't ingest.
- **Reliability:** worker-crash-mid-batch (no loss / no dup), poison→DLQ, buffer-full backpressure.
- **Performance:** k6/Locust baselines for ingest throughput and query latency; recorded before/after Phase 17.
- **E2E:** Playwright for the core UI flows (build a trend, a funnel, a retention report; assemble a dashboard).
- **AI:** a fixed NL→spec eval set with regression thresholds; prompt-injection tests that must fail to escape scope.

---

## 9. Security & data-handling rules

- Tenant scope injected in every ClickHouse query; never accepted from the client.
- Write keys are append-only and project-scoped; read keys are query-scoped; both hashed at rest, revocable.
- PII controls: configurable allow/deny lists per project; denied properties are hashed or dropped **before**
  they reach ClickHouse. No PII in logs.
- Retention: per-project TTL; raw archive in object storage governed by the same retention.
- Right-to-deletion: a documented, tested path to remove a user's events across partitions.
- Standard web hardening: input validation, security headers, dependency scanning, rate limits everywhere,
  signed webhooks, secrets only via env (never committed).

---

## 10. Glossary

- **Event** — a single tracked action (`event_name`) by a user at a time, with properties.
- **MTU** — monthly tracked user (billing unit alongside raw event count).
- **Insight** — a saved query spec (trend / funnel / retention).
- **Rollup** — a pre-aggregated materialized view used to answer common insights quickly.
- **Write key / read key** — public append-only key vs server-side query key.
- **DLQ** — dead-letter sink for events that fail validation.

---

*Change log: keep a running list of spec changes per phase here (date — phase — what changed — why).*

- 2026-09-16 — Phase 1 — Added §6.1 (the standard error envelope's concrete JSON shape and error codes),
  which §6 had referenced as "defined Phase 1" without specifying. Scoped to error responses only, not a
  wrapper for every successful response body — no phase's DoD calls for the latter.
- 2026-09-16 — Phase 2 — Added §4.1 (the RLS mechanism: session-scoped GUC, `FORCE ROW LEVEL SECURITY`,
  and why the app connects as a dedicated `pulse_app` role rather than the Postgres bootstrap superuser
  role RLS would otherwise silently bypass). Added a note to §4 that only `User`/`Organization`/
  `Membership`/`Project` exist so far — the rest of that section's table list is the eventual full shape,
  not what Phase 2 built.
- 2026-09-16 — Phase 3 — Added §6.2 (the concrete auth mechanism: stateless JWT access tokens vs. opaque
  DB-backed hashed refresh tokens, rotation, revocation, and the login-only rate limiter). New
  `refresh_tokens` table (not RLS-protected — no `org_id`, same as `User`).
- 2026-09-17 — Phase 4 — Extended §4.1 with the actual RLS rule (browse-pattern tables vs. token-redemption
  tables, regardless of whether the latter carry `org_id`) and the second, `FOR SELECT`-only
  `app.current_user_id` GUC/policy on `memberships` for "list my orgs". New `Invite` (not RLS-protected,
  per the refined rule) and `AuditLog` (RLS-protected) tables — the latter closes a gap this spec's own §4
  note flagged since Phase 2 ("AuditLog... lands whenever the first real mutating endpoint does"). Updated
  §6's org/project/member/invite endpoint list; `ApiKey`'s endpoints remain Phase 5, not built yet. No
  email provider exists in §2's tech stack, so `POST /orgs/{id}/invites` returns the raw invite token in
  the response body as an explicit stand-in for actually emailing it. Member/org/project destructive
  actions (delete, role change, removal) require an OWNER/ADMIN role via one small helper
  (`require_elevated_role`) — a deliberately minimal exception to "RBAC is Phase 5," since leaving those
  specific actions open to any member felt like an obvious gap not worth waiting on.
