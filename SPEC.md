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
- **ApiKey** — `id, org_id, project_id, key_prefix, type (write|read), key_hash, created_by, last_used_at,
  revoked_at`. **Write keys** are public/client-side, append-only to their project. **Read keys** are
  server-side, scoped to query. As built in Phase 5: `project_id` is required, not nullable — every key
  belongs to exactly one project; an org-wide read key spanning multiple projects has no consumer yet and
  is deferred. `scopes[]` is likewise deferred until a concrete consumer (Phase 7/11) needs finer-grained
  scoping than the type split already provides.
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

> As of Phase 9: `User`/`Organization`/`Membership`/`Project` (Phase 2), `RefreshToken` (Phase 3, not
> listed above — see §6.2), `Invite` and `AuditLog` (Phase 4), `ApiKey` (Phase 5, ahead of this table's
> original phase note — see §4.1), `EventSchema`/`PropertySchema` (Phase 9, see §6.6). The rest of this
> list is the full eventual shape; each remaining table is built in the phase that needs it (`Insight`/
> `Dashboard` Phase 15/16, `Alert` Phase 19, `Billing` Phase 20).

### 4.1 Row-Level Security mechanism (implemented Phase 2, extended Phase 3/4/5)

Of the four Phase 2 tables, only `Membership` and `Project` carry `org_id` and are RLS-protected;
`User` is a global identity (which orgs it belongs to lives in `Membership`) and `Organization` *is* the
tenant rather than referencing one, so neither is RLS-scoped itself.

**The actual rule, refined by Phase 3/4/5:** RLS protects tables reached via a *browse-this-org's-data*
pattern (`Membership`, `Project`, `AuditLog`) — it's defense-in-depth against an application bug that
forgets to filter by `org_id`. It does **not** protect tables reached via *token redemption*
(`RefreshToken`, `Invite`, and now `ApiKey`), even though `Invite`/`ApiKey` carry `org_id`: accepting an
invite, refreshing a session, or validating an API key on an ingest/query request (Phase 7/11) all mean
looking a row up by an opaque secret *before* the caller has any org context to scope a session by — the
token itself is the security boundary there, not RLS. `ApiKey` is the clearest case of this rule, since it
also has a real management/browse need (`GET .../keys`) and still isn't RLS-protected: redemption happens
on every future ingest/query request and correctness there matters most, so management queries just filter
by `org_id`/`project_id` explicitly instead, reached only once `require_role` has confirmed the caller's
membership.

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

### 5.1 Raw events table (implemented Phase 6)

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
ENGINE = ReplacingMergeTree(received_at)
PARTITION BY (org_id, toYYYYMM(timestamp))
ORDER BY (org_id, project_id, event_name, timestamp, event_id)
TTL toDateTime(timestamp) + INTERVAL 365 DAY      -- per-org override applied via config/materialized policy
SETTINGS index_granularity = 8192;
```

Design notes (be able to defend each in an interview):
- **`ORDER BY (org_id, project_id, event_name, timestamp, event_id)`** matches the dominant filter and
  makes the common query a bounded range scan. This is the single biggest performance lever.
- **`PARTITION BY (org_id, toYYYYMM(...))`** isolates tenants and makes retention/TTL and per-tenant
  deletion cheap (drop parts, not row-by-row).
- **`properties Map(String,String)`** gives schema flexibility with no migration per new property; hot
  properties get **promoted** to typed materialized columns for speed once the registry sees their volume.
- **`LowCardinality`** on `event_name`/promoted enums shrinks storage and speeds group-bys.
- **Dedup — resolved:** `ReplacingMergeTree(received_at)`, with `event_id` appended to `ORDER BY`. This is
  more subtle than "keyed by `event_id`" sounds: `ReplacingMergeTree` doesn't dedupe by an arbitrary key —
  it collapses rows sharing the *entire sorting key* during background merges. The dominant-filter `ORDER
  BY` above has no `event_id` in it at all, so without appending it, two *different* events that happen to
  share `(org_id, project_id, event_name, timestamp)` — a real possibility at any volume, e.g. two users
  triggering the same named event in the same millisecond — would be silently collapsed into one, dropping
  real data. Appending `event_id` as the trailing column fixes this: a true duplicate (an ingest worker's
  retried batch) has an identical `event_id` and therefore an identical full sort key, so it still
  collapses; two distinct events no longer can, no matter what else they share. The trailing position
  doesn't hurt the dominant range-scan query pattern, since only the *leading* `ORDER BY` columns matter
  for the sparse primary index.
  This is a **background, eventual** safety net, not the real guarantee — merges run lazily, so a query
  immediately after insert can still see duplicate rows (`OPTIMIZE ... FINAL` forces one, but is expensive
  and not something a normal query path should ever call). The actual, synchronous idempotency guarantee
  is Phase 8's ingestion-worker-level dedup (a short-TTL seen-set in Redis, checked *before* the insert
  happens) — this table's `ReplacingMergeTree` engine is a backstop for whatever slips past that, not a
  substitute for it.

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
- **Retention:** a per-user cohort period (first `born_event` bucket) joined against a per-user activity-
  period set (every `return_event` bucket, widened past the range end) → cohort grid, computed in
  Python -- not ClickHouse's `retention()` aggregate function, which can't express a per-user-relative
  period offset. See §6.10 for why.
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
CRUD   /api/v1/orgs/{org_id}/projects/{project_id}/keys          # Phase 5 -- nested under orgs, not
                                                                  # the bare /projects/{id}/keys shown
                                                                  # above; see the Phase 5 changelog entry

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

### 6.3 RBAC and API keys (implemented Phase 5)

**Role matrix.** `Owner > Admin > Member > Viewer` (`pulse/api/dependencies.py`'s `_ROLE_RANK`), enforced
via `Depends(require_role(minimum))` directly in route signatures — declarative, not an imperative check
buried in a handler body, per the DoD's "permission dependency on every endpoint." Replaces Phase 4's
single inline elevated-or-not check with a graduated matrix:

| Action | Minimum role |
|---|---|
| View org/projects/members | Viewer |
| Create/update project | Member |
| Delete project | Admin |
| Update org settings | Admin |
| Invite/revoke invite, list invites | Admin |
| Change member role, remove member | Admin |
| Create/revoke API keys | Admin |
| Delete org | Owner |

Plus one guard beyond the table: granting the `owner` role — whether via `PATCH .../members/{id}` or by
inviting someone directly as owner — itself requires the actor to already be an owner, closing an
admin-self-escalation path (an admin promoting an accomplice, or a second account, to owner).

**API keys.** Write keys (`pulse_write_<secret>`) and read keys (`pulse_read_<secret>`) are project-scoped,
hashed at rest (SHA-256, like refresh tokens), shown in full exactly once at creation; only a short,
non-secret `key_prefix` is retrievable afterward for display in a key list. `require_write_key`/
`require_read_key` (`pulse/api/dependencies.py`) resolve a key from an `X-API-Key` header and reject a
revoked key or the wrong type outright — built now, ahead of there being a real `/ingest` (Phase 7) or
query endpoint (Phase 11) to attach them to, so the DoD's "write keys can only ingest, read keys can only
query" is real and tested (`resolve_api_key` type-checks explicitly) rather than deferred untested to
whichever phase happens to need it.

### 6.4 Ingest API mechanism (implemented Phase 7)

`POST /ingest` validates lightly and buffers -- it never inserts into ClickHouse synchronously (§3.2).
`require_write_key` (Phase 5) resolves the caller's `org_id`/`project_id`; the request body carries no
tenant field at all, so there's nothing client-supplied to distrust.

- **Buffer topology: one global Redis Stream, not one per project.** All events, across every org and
  project, land in a single stream key (`pulse:ingest:events` by default). This keeps Phase 8's
  consumer-group workers simple as the number of projects grows -- no fan-out across N streams -- while
  tenant scope still lives in each entry's own `org_id`/`project_id` fields. One `XADD` per *event*, not
  per HTTP batch (pipelined so a whole HTTP batch is still one Redis round trip), so a worker's
  `XREADGROUP` reads a batch of individually-ack-able entries directly, matching
  `PULSE_PROJECT_GUIDE.md` §6.2's sequence diagram ("consume batch (consumer group)").
- **Validation is deliberately shallow.** `IngestEvent` (`pulse/ingest/schemas.py`) checks only structural
  well-formedness: `event_id` is a UUID, `event` is a non-empty string, and at least one of
  `user_id`/`anonymous_id` is present. It is **not** the `events` table's own column shape -- schema-registry
  validation and enrichment (geo/device/server timestamp) are Phase 8/9's job, applied once an event leaves
  the buffer. A structurally invalid event fails the **whole batch** (`422`), not a partial accept/drop --
  per-event triage into good/bad belongs to Phase 8's DLQ, once enrichment exists to triage against.
- **Two independent size guards.** A batch-count cap (`ingest_max_batch_size`, default 500) enforced in the
  router against the parsed body, and a body-byte cap (`ingest_max_body_bytes`, default 512,000) enforced
  against the `Content-Length` header before that -- either alone would miss a batch that's small in count
  but huge in property payloads, or vice versa. The `Content-Length` check is a `Depends`, not custom ASGI
  middleware: FastAPI already reads the full body before resolving dependencies, so this doesn't prevent the
  initial buffering into memory, but it does stop a huge payload from reaching JSON parsing, validation, or
  Redis, and (more importantly) it does so through the same registered exception-handler pipeline as every
  other error in this app -- a hand-rolled ASGI middleware raising `HTTPException` would bypass that
  pipeline entirely (Starlette's `ExceptionMiddleware` sits *inside* user middleware, not outside it), so it
  was rejected as strictly worse for a marginal gain.
- **Rate limiting is keyed by write-key id, not client IP** (`check_ingest_rate_limit`,
  `pulse/core/rate_limit.py`), reusing Phase 3's fixed-window-counter-in-Redis pattern. Ingestion traffic
  for many customers can share an IP (CDNs, corporate NAT); the resource actually being protected is a
  single project's buffer capacity, which the write key identifies precisely.
- **CORS is applied app-wide** (`CORSMiddleware`, `allow_origins` from `ingest_cors_allow_origins`,
  default `["*"]`), not scoped to `/ingest` alone, even though `/ingest` (meant to be called from arbitrary
  customer domains, authenticated by a bearer-style write key) is the only real reason it exists. Starlette's
  `CORSMiddleware` has no per-route scoping, and every other endpoint in this app authenticates via a JWT
  bearer token or an API key -- never a cookie -- so there is no ambient credential a cross-origin script
  could ride on regardless of which endpoint a wide-open policy exposes. Hand-rolling a path-scoped CORS
  middleware to avoid that was rejected as reinventing preflight handling for no real security gain.

### 6.5 Ingestion worker mechanism (implemented Phase 8)

The only thing that ever writes to ClickHouse. Consumes `pulse:ingest:events` via a Redis Streams
consumer group (`ingest-workers`), one fixed consumer name (`worker-1`) so a crashed-and-restarted worker
naturally reclaims its own still-pending entries -- true multi-consumer takeover (`XCLAIM`) is deferred
until something actually runs more than one worker replica.

- **Two deferred/scoped-down pieces of the DoD's "registry validation + enrichment" bullet.** "Registry
  validation" is a no-op: Phase 9's schema registry doesn't exist yet, so there is nothing to validate
  against -- unknown events pass through unchanged, matching Phase 9's own future DoD wording ("unknown
  events still ingest"). "Enrichment" is scoped to what Phase 7 actually captured: assigning `_ingest_batch`
  per worker-processed batch (the column the `events` table already reserves "for replay/debug", and that
  Phase 6's own fixture generator flagged as "like a real ingest worker's batch (Phase 8) would produce").
  Geo/device-from-headers enrichment (mentioned in `PULSE_PROJECT_GUIDE.md`) is out of scope here -- Phase
  7's `/ingest` never captured `User-Agent`/client IP into the stream entry in the first place, so there's
  nothing to enrich from yet.
- **One `XREADGROUP` call gives both the size and time trigger.** `BLOCK` (`worker_block_ms`, default 5000)
  + `COUNT` (`worker_batch_size`, default 500) on a single call means a batch is whatever arrived within the
  block window, up to the count cap -- no separate manual timer/accumulator loop needed. Each read cycle
  first reclaims this consumer's own pending (unacked) entries via `id="0"`, and only reads new entries
  (`id=">"`) once that backlog is empty -- the crash-recovery path.
- **Correctness ordering is the crux of "no loss, no dup" under a crash.** Per event: parse (a parse
  failure -> `PoisonEvent` -> DLQ, no further processing) -> a read-only dedup **check** against a Redis
  seen-set to decide skip-vs-insert -> after the whole batch is classified, one bulk `insert_events` call
  for the survivors -> archive the *entire* raw batch (good + poison) to object storage -> **mark** the
  freshly-inserted event ids seen (TTL `worker_dedup_ttl_seconds`, default 24h) -> ack every entry. Marking
  "seen" happens strictly *after* the insert, not before: if the worker dies in between, the redelivered
  event is re-inserted rather than silently lost -- Phase 6's `ReplacingMergeTree(received_at)` is already
  documented as exactly this backstop ("a background, eventual safety net... not a substitute for it"). If
  the ClickHouse insert itself raises, the exception propagates out of `process_batch` *before* marking-seen
  or acking; the caller (the main loop) logs it and leaves the batch pending for the next cycle -- that is
  the backpressure behavior, and it requires no explicit retry/backoff logic of its own.
- **The DLQ is a second Redis Stream** (`pulse:ingest:dlq`, `worker_dlq_stream_key`), not a new store --
  reuses the same infrastructure and primitives as the main buffer. Each entry carries the original raw
  fields plus `error` and `failed_at`. Poison entries are archived (as part of the raw batch) and acked
  like any other entry -- DLQ'd is a terminal, handled outcome, not a reason to retry forever.
- **Property values are stringified for ClickHouse's `Map(String, String)` column** using the exact
  convention Phase 6's fixture generator already established: numbers via plain `str()`, booleans as
  lowercase `"true"`/`"false"` (Python's own `str(True) == "True"` would silently break a later
  `= 'true'`-style query), and a `null`-valued property is dropped from the map entirely (the column has no
  null representation, so an absent key is the natural encoding).
- **New dependency: `minio`** (the official sync S3/MinIO client -- no async SDK exists), wrapped in
  `asyncio.to_thread` for both `ensure_bucket()` (idempotent, called on worker startup, mirroring
  `alembic/env.py`'s `_ensure_app_role_exists`) and the per-batch archive write, so a slow object-storage
  call doesn't stall the Streams consume loop. Archive object key:
  `raw/{received_at:%Y/%m/%d}/{ingest_batch}.json`, one object per worker-processed batch (not per event),
  containing the *entire* raw batch as read off the stream.

### 6.6 Schema registry mechanism (implemented Phase 9)

Pure governance metadata layered on top of ingestion -- never a gate on it. `EventSchema`/`PropertySchema`
are RLS-protected on `org_id` like `Project`/`AuditLog` (a browse-this-project's-taxonomy pattern, not
token redemption); `PropertySchema.org_id` is denormalized (also derivable via `event_schema_id`) purely so
RLS can scope the table directly, matching every other RLS-protected table in this codebase.

- **Registration is best-effort and fully decoupled from ingestion correctness.** The worker calls it only
  *after* a successful ClickHouse insert, wrapped in its own `try`/`except` (`register_events` in
  `pulse/worker/processing.py`) -- any failure (Postgres down, a stale `org_id`/`project_id` whose control-
  plane rows no longer exist) is logged and swallowed, never raised, never costs the archive write or the
  ack. This was verified against a *real* failure during Phase 9's live check, not just in tests: a backlog
  of events from earlier phases' testing (whose orgs/projects had since been dropped by an intervening
  `alembic downgrade base`) hit a genuine `ForeignKeyViolationError` registering against `event_schemas` --
  and every one of those events still landed in ClickHouse correctly, because the two are decoupled.
- **A process-lifetime in-memory cache** (`(project_id, event_name) -> event_schema_id`,
  `(event_schema_id, key) -> (property_schema_id, inferred_type)`) avoids a Postgres round trip for every
  event in a batch of up to 500 -- only a not-yet-seen pair touches the database. Unique constraints on
  `(project_id, event_name)` and `(event_schema_id, key)` are the real safety net; there's no cross-process
  race to worry about, since Phase 8 already committed to a single worker process. `volume_estimate` is
  bumped once per distinct event name *per batch* (grouped), not once per event.
- **Type inference runs on the pre-stringification values.** `ParsedEvent` gained a `raw_properties` field
  (Phase 8's `properties` field had already flattened everything to strings for ClickHouse's
  `Map(String, String)` column, which would make every property look like a "string" to the registry).
  Inference order matters: `bool` is checked *before* `(int, float)`, since `bool` is a subclass of `int` in
  Python and would otherwise be misclassified as `number`. A string is tried against
  `datetime.fromisoformat()` before falling back to `string`.
- **Type conflicts are flagged, never enforced or silently overwritten.** `inferred_type` is set once at
  first sighting and never auto-changed by a later observation; a conflicting type instead sets
  `type_conflict_detected_at` (once -- both in Postgres and via a local `_conflict_flagged` set, so a
  batch with hundreds of the same conflicting property doesn't re-issue the same `UPDATE` hundreds of
  times). This is a deliberate, minimal contract: Phase 9 surfaces the conflict for a human to look at, not
  a type-coercion or rejection system.
- **`PropertySchema.event_schema_id` stays nullable per its original shape in §4 ("nullable = event-
  agnostic"), but Phase 9 does not populate that path.** Every property this phase registers is tied to a
  specific event; a project-wide property catalog entry independent of any one event name is a real future
  feature, not something this phase's DoD asked for.
- **Routes nest under `/orgs/{org_id}/projects/{project_id}/schema/...`**, not the bare
  `/api/v1/projects/{id}/schema/...` this section originally sketched -- the same correction Phase 5 already
  made for API keys, applied consistently: resolving which org a bare `project_id` belongs to, before
  there's org context to check membership, is the same problem every other project sub-resource's URL
  shape already solves. `GET` (list events, list a event's properties) requires Viewer+; `PATCH` (deprecate/
  hide an event, mark a property PII or change its status) requires Admin+, matching the API-keys precedent
  for settings-shaped mutations. Both `PATCH` routes write an audit log entry in the same transaction as the
  mutation (`pulse/registry/service.py`, not the router -- matching `pulse/services/api_keys.py`'s
  convention of keeping the mutation and its audit record atomic).

### 6.7 Ingestion SDK mechanism (implemented Phase 10)

`packages/sdk-js` (`@pulse/sdk-js`) and `packages/sdk-python` (`pulse-sdk`) are both standalone
packages -- no npm workspace links `sdk-js` to `frontend/`, and `sdk-python` is not a dependency of
`backend/`. Neither is published; both are built/installed locally.

- **A real technical conflict, resolved:** `/ingest` auth is the `X-API-Key` header, but
  `navigator.sendBeacon` -- named in `PULSE_PROJECT_GUIDE.md` -- cannot send custom headers at all.
  The unload-time flush uses `fetch(url, { keepalive: true })` instead, which survives page teardown
  the same way `sendBeacon` does but does support headers, so the write-key auth model stays
  identical across every call site. Trade-off: `keepalive` requests share a small (~64KB) in-flight
  body budget per origin, which is why flush batches are kept bounded.
- **The real "no loss" guarantee is `localStorage`, not the unload-time network call.** Every
  `track`/`identify`/`page` call is durably queued to `localStorage` *before* any network attempt.
  The `pagehide`/`beforeunload`-triggered `fetch` is still only best-effort -- if it's cut short by
  the unload itself, the event is simply still on disk, and the next page load's flush (interval or
  size trigger) picks it up. This was verified live, not just in a mocked test: tracking an event,
  then navigating away without ever clicking "flush," produced a real `keepalive` `POST /ingest` that
  the running worker processed on its very next cycle.
- **Browser is this phase's real target; Node is architected for, not shipped.** The DoD's four named
  test categories and its one acceptance line ("dropping the SDK into a demo page") are both
  browser-shaped. `LocalBuffer`/`sendBatch` are written against small, swappable interfaces so a
  future Node entry point (in-memory buffer instead of `localStorage`, no `window` event listeners) is
  a real possibility, but only the browser build is written, tested, and demoed here.
- **`event_id` is never regenerated across retry attempts** -- a failed flush retries the exact same
  batch with exponential backoff (1s, capped at 30s); the worker's Phase-8 dedup makes a
  fails-then-succeeds retry idempotent by design, not by any special-casing in the SDK itself.
- **Bounded local buffer** (default 1000 events, oldest evicted first with a console warning) -- not
  in the DoD's literal wording, but necessary so a prolonged API outage can't grow `localStorage`
  without bound.
- **`identify(userId, traits?)`** persists `userId` for every subsequent `track`/`page` call and emits
  a `track("$identify", traits)` event -- the common SDK convention, needing no new server-side
  concept. **`page(name?, props?)`** is sugar for `track("page viewed", { name, ...props })`, reusing
  the exact event name Phase 6's own fixture generator already uses.
- **The Python SDK is genuinely thin: zero runtime dependencies.** `urllib.request` + `json` from the
  stdlib, not `httpx` -- in-memory buffer only, no local persistence, no retry/backoff. A Python
  server process restarting is a different failure mode than a browser tab closing, and this is meant
  to drop into an arbitrary third-party server without adding a dependency footprint.
- **Fixed a pre-existing CI gap found while touching this file:** the `test` job's service containers
  never included MinIO, so Phase 8/9's object-storage-archiving tests had been silently unexercised in
  CI since they were written. GitHub Actions `services:` containers cannot override a command, and the
  official `minio/minio` image needs `server /data` passed explicitly -- so MinIO is started as a
  plain `docker run` step instead of a `services:` entry. New `test-sdk-js` and `test-sdk-python` CI
  jobs (typecheck/test/build for one, ruff/mypy/pytest for the other).

### 6.8 Query engine mechanism (implemented Phase 11)

The read path's only entry point into ClickHouse: a validated `TrendSpec` compiles to parameterized SQL
with tenant scope, caps, and a cache layer wrapped around it -- never hand-built SQL at a call site.

- **`org_id`/`project_id` are function parameters to `build_trend_query`, not fields on the spec at
  all.** `TrendSpec` (`pulse/query/spec.py`) has no tenant field for a client to populate or a bug to
  forget to filter by -- SPEC.md #3's "tenant scope is injected, never trusted" enforced by the type
  itself, not just by convention at the call site.
- **Every client-controlled value is a named ClickHouse parameter, never string-interpolated** -- event
  names, filter keys/values, the breakdown key, the resolved timezone, even the measure's property key.
  `pulse/query/builder.py` builds `WHERE`/`GROUP BY`/`SELECT` as SQL *fragments* referencing
  `{param:Type}` placeholders; the actual values travel separately via `clickhouse-connect`'s
  `parameters=`.
- **Caps are ClickHouse query settings, not an application-level timeout wrapper**
  (`query_max_execution_time_seconds`, `query_max_rows_to_read`, `query_result_limit` in
  `pulse/core/config.py`) -- ClickHouse's own enforcement is the real boundary, matching CLAUDE.md §5's
  "every ClickHouse query carries `max_execution_time`, row/scan caps, and a `LIMIT`."
- **Timezone bucketing, not the `WHERE` bound, is where timezone-aware SQL actually matters.** The range
  bound is converted to UTC once in Python (`_utc_bounds`, inclusive of both calendar dates in the
  resolved timezone -- `to` extends through the end of that local day); the `GROUP BY` bucket expression
  (`toStartOf<granularity>(timestamp, {tz:String})`) is the one place ClickHouse itself does timezone
  work, per SPEC.md #5.3's "store UTC, bucket with tz -- never bucket on raw UTC."
- **Query routes accept either a JWT or a read API key, the first endpoint to need both at once.**
  `get_current_user_optional` (`pulse/core/security.py`) is `get_current_user` without the raise; the
  combined check lives in `resolve_query_scope` (`pulse/api/dependencies.py`), not spread across the
  route -- an `X-API-Key` header takes precedence when present (validated as a **read** key scoped to
  exactly this project, the same read/write split Phase 5 already enforces for `/ingest`), otherwise
  falls back to the JWT + membership check every other org-scoped route uses.
- **Result caching is keyed by `(spec, org_id, project_id)`, not by a hand-rolled cache key.** A validated
  Pydantic model's own `model_dump_json()` is already deterministic for equivalent input (field order
  follows the model's declaration, not the client's), so `pulse/query/cache.py` just SHA-256s that
  string plus the tenant IDs -- no separate canonicalization step. TTL is short
  (`query_cache_ttl_seconds`, default 60s) on purpose: short enough to serve the common case (a dashboard
  re-rendering, someone re-running the same trend) without returning badly stale numbers, per CLAUDE.md
  §6's "cache query results in Redis with a short TTL."
- **Only `trend` is built this phase.** `funnel`/`retention` specs exist in SPEC.md #4.2's shape sketch
  but have no builder yet -- that's Phase 12/13's own job, reusing this same parameterization discipline.

### 6.9 Funnel query mechanism (implemented Phase 12)

Builds on Phase 11's shared tenant/range/filter parameterization (`_tenant_and_range_where`,
`_apply_filters` in `pulse/query/builder.py`, extracted from the trend builder rather than duplicated)
rather than inventing a second query-building path.

- **The SQL builder emits a raw per-user level histogram; step counts, conversion %, and drop-off are
  computed in Python, not SQL.** `build_funnel_query` groups by user (`if(user_id != '', user_id,
  anonymous_id)`, the same effective-identity expression as trend's `unique_users` measure) and computes
  `windowFunnel(...)` per user, then aggregates to `(level[, breakdown]) -> count`. Turning that histogram
  into "how many users reached step *k*" is `pulse/query/service.py`'s `_funnel_step_results`: reaching
  windowFunnel level >= k means step k was completed in order, so a step's count is the sum of every level
  at or above it -- cumulative math is far more natural to express and test in Python than as nested SQL.
- **`windowFunnel` requires `DateTime`/`Date`/unsigned-number first argument, not `DateTime64(3)`** -- a
  real error hit against live ClickHouse (24.8), not caught by reasoning about the function's docs alone:
  `events.timestamp` is `DateTime64(3)` (SPEC.md #5.1), so the builder wraps it in `toDateTime(...)`,
  truncating to second precision. This doesn't lose meaningful conversion-window precision -- windows are
  specified in whole hours/days (`FunnelWindow.unit`), never sub-second.
- **Ordering is enforced by `windowFunnel`'s own semantics, not extra application logic.** A user who
  completes step 2 before step 1, or skips a middle step, cannot reach a level past the last step actually
  satisfied in sequence -- verified live in Phase 12's own hand-computed fixture test (SPEC.md #7), which
  deliberately includes a "step 2 before step 1" user and a "skips the middle step" user, not just the
  three-of-three-in-order happy path.
- **The conversion window bounds progress from a user's first matching event, not the query's date
  range.** `window_seconds()` converts `FunnelWindow` (`{value, unit}`, hour or day only -- a funnel
  spanning weeks isn't a meaningful "one journey" window) to the plain integer ClickHouse's
  `windowFunnel(<seconds>)` expects. This is independent of `range.from`/`range.to`, which only bounds
  which raw events are considered at all, matching SPEC.md #4.2's separate `window` and `range` fields.
- **Breakdown reuses the same pattern as trend's breakdown column**, added to both the inner
  (per-user-per-breakdown-value) and outer (per-level-per-breakdown-value) `GROUP BY`, then split into
  separate cumulative step sequences per breakdown value in `_funnel_step_results` -- sorted by breakdown
  value for deterministic output, since dict iteration order isn't guaranteed to be stable across runs in
  a way tests (or a cache) should depend on.
- **Tenant-leakage and cache-key reuse are automatic, not re-implemented.** `run_funnel` is structurally
  identical to `run_trend` (`get_project` scoping check, `cache.cache_key`/`get_cached`/`set_cached`, the
  same `settings`/`max_execution_time`/`max_rows_to_read` caps) -- `cache.py`'s `cache_key` was widened
  from `TrendSpec` to a new `InsightSpec = TrendSpec | FunnelSpec` union rather than duplicated, so a
  funnel result is cached and tenant-scoped by construction, not by a second hand-written check.
- **Retention (Phase 13) is the only insight kind still unbuilt.** `InsightSpec` documents this directly
  in a comment rather than leaving it implicit.

### 6.10 Retention query mechanism (implemented Phase 13)

**Deliberate deviation from this section's original sketch:** SPEC.md #5.3 originally said retention
would be built on ClickHouse's `retention(...)` aggregate function. It isn't -- `retention()` answers "did
this user do cond1, and did they also do condN at some point," which is presence-based, not
period-relative: it can't express "checked at exactly N periods after *this user's own* cohort start,"
because every user's cohort start is a different row value, not a single query-wide constant the function
can take as an argument. Building that with `retention()` would mean one call per possible cohort period,
generated dynamically -- fragile and unbounded. Instead, following the same split Phase 12 established
(SQL emits raw per-user facts; Python computes the grid):

- **Two queries, not one.** `build_retention_cohort_query` gets each user's cohort period (the bucket of
  their *first* `born_event` in range); `build_retention_activity_query` gets every `(user, period)` where
  they did `return_event`, **widened past `range.to`** by `periods * period-length` -- a cohort near the
  end of the range must still be checkable at its later offsets, or retention would silently look worse
  near the range boundary for a reason that has nothing to do with actual user behavior. Both reuse the
  existing `_tenant_and_range_where`/`_apply_filters` helpers and the trend builder's own `_bucket_expr`
  (day -> `Granularity.DAY`, week -> `Granularity.WEEK`) rather than a third bucketing implementation.
- **`pulse/query/service.py`'s `_retention_grid_rows` does the merge:** for each cohort, for each offset in
  `[0, periods)`, count how many of that cohort's users have `cohort_period + offset * period_length` in
  their activity-period set. Output is a flat `(cohort_period, cohort_size, period_offset, retained,
  retention_pct)` row per cohort/offset pair -- a grid flattened to rows, the same shape convention trend
  and funnel results already use, easy for a future frontend to pivot into an actual grid or curve.
- **A real ClickHouse asymmetry, caught live against the container, not from reading docs:**
  `toStartOfWeek` returns `Date`, while `toStartOfDay` returns `DateTime` -- the same `_bucket_expr` call
  therefore comes back as a bare Python `date` for week periods and a full `datetime` for day periods.
  Query building itself is unaffected (date/date and datetime/datetime arithmetic both work), but it would
  have made the API response shape inconsistent (`"2026-08-03"` vs `"2026-08-03T00:00:00+00:00"` for
  `cohort_period`) depending on which `period` was requested. Fixed with `_as_utc_datetime`, coercing
  either ClickHouse return shape to midnight UTC `datetime` right after the row is read, so the grid is
  the same shape regardless of period. Never surfaced in Phase 11 because trend's own engine tests only
  ever exercised `granularity="day"` against live ClickHouse, never `"week"`.
- **`return_event` may equal `born_event`**, per SPEC.md #4.2's "came back at all" case -- no special-casing
  needed, since the activity query is just a second independent scan for whatever event name it's given;
  when it's the same name as the born event, the born event itself is naturally also activity in period 0.
- **Tenant scoping, caching, and caps are inherited exactly like trend/funnel** -- `run_retention` has the
  same `get_project` check, `cache.cache_key`/`get_cached`/`set_cached` calls, and per-query
  `max_execution_time`/`max_rows_to_read` settings, via the same `InsightSpec` union, not a fourth
  hand-written scoping path.
- **All three insight kinds (trend/funnel/retention) are now built** -- `InsightSpec` is the complete set
  SPEC.md #4.2 originally sketched.

### 6.11 Frontend foundation mechanism (implemented Phase 14)

The first frontend phase -- nothing existed beyond Phase 0's placeholder scaffold (a bare `app/`, a
server-rendered health check on `/`). Ported the portable pieces of Helix's own frontend shell (a sibling
project, `ALL PROJECTS/helix`, explicitly named as portable in `PULSE_PROJECT_GUIDE.md`'s Phase 14 entry)
rather than building from scratch, adapted where pulse's actual backend shape differs.

- **Session persistence deviates from Helix's own pattern, because the backend shape differs.** Helix's
  `/auth/refresh` reads an httpOnly cookie the backend itself sets; pulse's (`SPEC.md` §6.2, built Phase
  3) returns the refresh token in the JSON response body instead -- there's no cookie mechanism to read at
  all. Confirmed with the user before building: the access token stays in-memory only (never persisted, as
  in Helix), but the refresh token now persists in `localStorage`
  (`src/lib/refresh-token-storage.ts`) so a page reload can silently recover a session -- the trade-off
  (more XSS-exposed than an httpOnly cookie) is deliberate and documented, not accidental. Verified live,
  not just in the component tests: registered and logged in through the real running API, confirmed the
  session survives both a plain reload and a fresh deep link straight to a nested
  `/orgs/[orgId]/projects/[projectId]` route, and confirmed logout clears the stored token and redirects to
  `/login`.
- **No server-side "active org" concept, unlike Helix's `user.active_org_id` + `POST /orgs/{id}/switch`.**
  Pulse's own API has no such endpoint -- which org/project you're looking at is purely the URL's own
  `/orgs/[orgId]/projects/[projectId]` segments (`OrgProjectSwitcher`, two plain `<select>`s driven by the
  real `GET /orgs` and `GET /orgs/{org_id}/projects`, not a fancier menu component). This mirrors the
  backend's own URL nesting convention rather than inventing a separate frontend-only org model.
- **Scope deliberately stops at proving the shell, not building every feature it could wrap.** Confirmed
  with the user first: since no frontend page existed before this phase, "wrapping existing features"
  (the DoD's own wording) meant login/register, an org list/create page, a project list/create page, and a
  minimal project-home placeholder -- enough to prove auth routing, the switcher, and deep-linking all
  resolve correctly end to end. Schema registry, API keys, and insight/dashboard UIs are real, already-built
  backend features with no frontend yet, but building pages for them here would blur into Phase 15/16/19's
  own scope rather than this phase's "foundation."
- **A real, pre-existing bug was found and fixed, not introduced by this phase:** Phase 0's
  `eslint.config.mjs` used the old `FlatCompat({...}).extends("next/core-web-vitals", "next/typescript")`
  shim for eslintrc-style shareable configs, which throws `TypeError: Converting circular structure to
  JSON` under `eslint-config-next` 16's plugin set -- reproduced against the untouched Phase 0 scaffold
  (via `git stash`) before writing the fix, confirming it wasn't something this phase's own changes caused.
  Fixed by importing `eslint-config-next`'s native flat configs directly
  (`eslint-config-next/core-web-vitals`, `eslint-config-next/typescript`), the modern replacement for the
  legacy shim.
- **A second real bug, this time surfaced by the new lint rule itself:** `eslint-config-next` 16 ships
  `eslint-plugin-react-hooks` 7, whose `set-state-in-effect` rule flagged a synchronous `setInitializing`
  call in the session-recovery effect's no-stored-token branch. Fixed by routing every branch through the
  same promise chain (a `Promise.resolve(null)` sentinel for "nothing to recover") so `setInitializing`
  only ever runs inside `.finally()`, matching the pattern the other branches already used -- not a
  suppression, a genuine restructure.
- **A third bug, found only by actually running the app against the real stack, not by any test:** the
  running `docker-api-1` container's Postgres had never had `alembic upgrade head` run against it (a
  side effect of this session's earlier Docker Desktop instability, unrelated to this phase's own code),
  which surfaced in the browser as a confusing "blocked by CORS policy" console error -- the real cause was
  a `500` on `/auth/register` with no `Access-Control-Allow-Origin` header on the *error* response, which
  Chrome reports as a CORS failure regardless of the actual server-side cause. A reminder that a CORS error
  in the browser console is a symptom, not necessarily a CORS *configuration* problem -- confirmed by
  `curl`ing the endpoint directly and reading the real traceback server-side before assuming the
  documented-as-wide-open CORS policy (`SPEC.md` §6.4) was somehow misconfigured.
- **Testing is split three ways, each verified for real:** Vitest + Testing Library component tests
  (`ProtectedRoute`, `GuestRoute`, `AppShell`, `OrgProjectSwitcher`, the storage helper) wired into a new
  CI job (`test-frontend`, mirroring Phase 10's `test-sdk-js` precedent); two Playwright e2e flows
  (register→login→org picker with a reload, and create-org→create-project→project-home) run and verified
  locally against the real docker-compose stack and a real `next dev` server, kept out of CI for now per
  the user's confirmed scope decision (`playwright.config.ts` documents why); and the full flow was also
  driven manually through the built-in browser tool end to end as a final check, which is what actually
  caught the CORS/migrations bug above -- none of the automated tests would have.

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

**Phase 7 — Ingest API.** ☑ `POST /ingest` batch, write-key auth, light validation, enqueue to Redis
Streams, `202`; ☑ size limits + rate limit; ☑ browser CORS. Tests: batch buffered (nothing hits ClickHouse
synchronously); oversized/malformed rejected; write-key scoping; p99 latency sanity. See §6.4.

**Phase 8 — Ingestion workers.** ☑ consumer-group workers; ☑ batch by size/time; ☑ registry validation +
enrichment; ☑ **`event_id` dedup**; ☑ large batched inserts; ☑ **DLQ**; ☑ raw-batch archive to object
storage. Tests: idempotent re-consume (no double count); poison → DLQ; **worker crash mid-batch → no loss,
no dup**; backpressure behavior. See §6.5.

**Phase 9 — Schema registry.** ☑ auto-register new events/properties with inferred types; ☑ deprecate/hide;
☑ mark PII. Tests: new event auto-registers; type-conflict flagged; unknown events still ingest. See §6.6.

**Phase 10 — SDK.** ☑ TS SDK (`identify`/`track`/`page`) with local buffer, batched flush on
interval/size/`beforeunload`, backoff retry, `event_id`; ☑ thin Python server SDK. Tests: offline buffering,
flush triggers, retry/idempotency, no loss on unload. See §6.7.

**Phase 11 — Query engine + trends.** ☑ spec→SQL builder with **org/project injected**, caps, timeouts; ☑
Redis result cache. Tests: spec→SQL correctness; **tenant-leakage tests**; tz-correct bucketing; cache
hit/miss. See §6.8.

**Phase 12 — Funnels.** ☑ `windowFunnel`-based; ☑ per-step counts + conversion/drop-off; ☑ breakdown.
Tests: ordering enforced; window boundaries; **hand-computed fixture funnel matches exactly**. See §6.9.

**Phase 13 — Retention.** ☑ cohort grids + curve, built on the same shared query-building layer.
Tests: cohort assignment; day/week bucketing; **hand-computed retention fixture matches**. See §6.10.

**Phase 14 — Frontend foundation.** ☑ shell/nav/design system/data layer/auth routing/org+project switcher.
Tests: components + a couple of Playwright flows. See §6.11.

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
- 2026-09-17 — Phase 5 — Added §6.3 (the role matrix: `require_role(minimum)` as a declarative dependency
  factory replacing Phase 4's single `require_elevated_role`, plus the owner-can-only-be-granted-by-owner
  guard). New `ApiKey` table/endpoints (`POST/GET /orgs/{id}/projects/{id}/keys`, `DELETE .../keys/{id}`),
  documented as not RLS-protected per §4.1's refined token-redemption rule, despite also having a real
  management/browse use — redemption on the future ingest/query hot path wins. Corrected §6's route for
  keys from the bare `/projects/{id}/keys` shorthand to `/orgs/{id}/projects/{id}/keys`, consistent with
  how every other project sub-resource is already nested (and necessary: resolving which org a bare
  `project_id` belongs to, before there's org context to check membership, is the same problem invites'
  URL shape already solves). `require_write_key`/`require_read_key` exist and are tested ahead of Phase
  7/11 having a real endpoint to attach them to.
- 2026-09-17 — Phase 6 — Resolved §5.1's open dedup question: `ReplacingMergeTree(received_at)` with
  `event_id` appended to `ORDER BY` (not just "keyed by `event_id`" as the pre-Phase-6 text put it) --
  `ReplacingMergeTree` dedupes by the *entire* sorting key, and `event_id` wasn't in it, which would have
  either missed true duplicates or (worse) silently collapsed distinct events that happen to share
  `(org_id, project_id, event_name, timestamp)`. Documented the precedence this implies: this is a
  background, eventual safety net, not the real guarantee -- that's Phase 8's synchronous, pre-insert
  worker-level Redis dedup. Added the ClickHouse migration CLI (`python -m pulse.clickhouse_migrations`,
  documented in `README.md`) -- the runner existed since Phase 1 but had never been given a real product
  migration or a one-line way to invoke it, mirroring `alembic upgrade head` for the other store.
- 2026-09-17 — Phase 7 — Added §6.4 (the Ingest API mechanism: one global Redis Stream rather than one per
  project, whole-batch-reject on a malformed event, two independent size guards, write-key-id-keyed rate
  limiting, and app-wide rather than path-scoped CORS -- each with the alternative considered and why it was
  rejected). New `pulse/ingest/` module (`schemas.py`, `service.py`, `router.py`); reuses Phase 5's
  `require_write_key` unchanged. Verified live end-to-end against the real containers, not just the test
  suite: registered a user, created an org/project/write-key through the real HTTP API, posted a batch to
  the running `/ingest`, confirmed the event landed in the Redis stream with `org_id`/`project_id` correctly
  injected from the resolved key (never from the request body), and confirmed ClickHouse's `events` table
  stayed at zero rows throughout. 64 tests pass (52 -> 64); `ruff`/`mypy` clean.
- 2026-09-17 — Phase 8 — Added §6.5 (the ingestion worker mechanism: which parts of "registry validation +
  enrichment" are deferred/scoped-down and why, the single-`XREADGROUP` size+time trigger, the
  check-before-insert/mark-after-insert ordering that makes the crash-recovery story correct, the DLQ as a
  second Redis Stream, and the exact property-stringification convention). New `pulse/worker/consumer.py`
  (consumer-group setup + read/ack), `pulse/worker/processing.py` (parse/dedup/insert/archive/DLQ, the
  testable `process_batch` core), `pulse/repositories/object_storage.py` (new `minio` dependency); rewrote
  the Phase 0 placeholder `pulse/worker/main.py` into the real consume loop. `infra/docker/docker-compose.yml`
  gained a MinIO healthcheck (missing since Phase 0) and wired `S3_*` env vars + a `service_healthy`
  dependency into `ingest-worker`. Verified live end-to-end, not just the test suite: brought up the real
  worker against a genuine backlog of 206 events (accumulated in the real Redis stream across earlier
  phases' test runs) and watched it drain the entire backlog in one batch on first startup, correctly
  grouped under one `_ingest_batch` id, zero to the DLQ; then posted one fresh event through the real
  `/ingest` and watched the worker's very next cycle pick it up under a *different* batch id, with a numeric
  property (`19.99`) and a boolean property (`true`) stringified exactly per the documented convention, and
  confirmed both batches' raw JSON landed in the MinIO bucket. 70 tests pass (64 -> 70); `ruff`/`mypy` clean.
- 2026-09-17 — Phase 9 — Added §6.6 (the schema registry mechanism: best-effort decoupling verified against
  a *real* FK-violation failure live, not just simulated in tests; the process-lifetime cache design;
  bool-before-number type-inference ordering; type conflicts flagged, never overwritten; the
  event-agnostic-property path deliberately left unbuilt; and the Phase-5-precedent URL nesting correction).
  New `pulse/models/schema_registry.py` (`EventSchema`, `PropertySchema`, RLS-protected), migration
  `0007_schema_registry`, `pulse/registry/service.py` (`register_batch` -- the cache + get-or-create +
  conflict detection + grouped volume bump -- plus the API-facing list/update functions), new router
  `pulse/api/schema_registry.py`. `pulse/worker/processing.py` gained `ParsedEvent.raw_properties` (the
  pre-stringification values Phase 8 never exposed) and a `register_events` call after each successful
  insert. Verified live end-to-end: posted a real event with a number/bool/string/datetime property mix
  through `/ingest`, watched the worker's next cycle register all four types correctly via
  `GET .../schema/events/{id}/properties`, deprecated the event through the real `PATCH` endpoint and
  confirmed it persisted -- and, unplanned but genuinely useful, watched the registry's best-effort
  decoupling hold under an actual `ForeignKeyViolationError` from a backlog of events whose orgs/projects
  no longer existed (dropped by an intervening `alembic downgrade base` between phases): every one of those
  events still landed in ClickHouse correctly. 78 tests pass (70 -> 78); `ruff`/`mypy` clean.
- 2026-09-17 — Phase 10 — Added §6.7 (the ingestion SDK mechanism: the sendBeacon-cannot-send-headers
  conflict resolved with `fetch({keepalive:true})` instead; the real "no loss" guarantee being
  `localStorage`, not the unload-time network call succeeding; browser scoped as this phase's real
  target with Node architected-for-but-not-shipped; the bounded-buffer safety valve; and the thin
  Python SDK's deliberate zero-runtime-dependency choice). New standalone packages `packages/sdk-js`
  (`@pulse/sdk-js` -- `src/{index,buffer,transport,ids}.ts`, `tsup` build, `vitest`+`jsdom` tests, a
  `demo/index.html`) and `packages/sdk-python` (`pulse-sdk` -- `pulse_sdk/client.py`, stdlib-only).
  Also fixed a pre-existing CI gap found while touching `.github/workflows/ci.yml`: the `test` job's
  service containers never included MinIO, so Phase 8/9's object-storage tests had been silently
  unexercised in CI since they were written; added new `test-sdk-js`/`test-sdk-python` jobs. Verified
  live end-to-end through the *actual* running backend, not mocks: opened the built demo page in a
  real browser, drove it through init/identify/track/page/flush, confirmed a genuine `202` from
  `/ingest` (including a real CORS preflight), and watched the worker register all three resulting
  events (`$identify`, `button clicked`, `page viewed`) via the schema registry API. Separately
  verified "no loss on unload" for real: tracked an event, navigated away without ever clicking flush,
  and confirmed a `keepalive` `POST /ingest` fired and the worker processed it on its next cycle --
  `button clicked`'s `volume_estimate` incremented, proving the unload path actually delivers, not just
  that it doesn't crash. 25 new tests (18 TS + 7 Python); `ruff`/`mypy`/`tsc` clean across both.
- 2026-09-17 — CI fix (found while closing out Phase 10, not part of it) — **CI's `test` job had been
  running the entire backend suite as the Postgres bootstrap/superuser role, not `pulse_app`.**
  `DATABASE_URL` was set directly to the same `pulse:pulse` credentials as the bootstrap role, with no
  `DATABASE_BOOTSTRAP_URL` override; §4.1's own invariant is that RLS -- even `FORCE ROW LEVEL
  SECURITY` -- is unconditionally bypassed for superusers. Confirmed by reproducing the exact CI env
  locally rather than reasoning about it: `test_tenant_isolation.py`'s two tests, which assert on a
  bare `select(Project)`/`select(Membership)` with no `WHERE org_id = ...` of their own (deliberately,
  so they test the database-level boundary and not application-level filtering), both genuinely fail
  under the old CI env -- cross-org rows leak straight through. Every other test passed anyway, either
  because it doesn't touch RLS-protected tables or because the service layer it calls already adds its
  own explicit `org_id` filter as defense-in-depth (see `pulse/services/projects.py`'s `get_project`:
  "RLS already guarantees a cross-org project_id comes back None; this is belt-and-braces, not the
  actual boundary") -- which is exactly how this went unnoticed: the belt was silently doing the job
  the suspenders were supposed to be verified. Fixed by pointing `DATABASE_URL` at `pulse_app` and
  adding `DATABASE_BOOTSTRAP_URL` (superuser, for `alembic/env.py`'s one-time role-creation step) to
  the `test` job's env, matching every other environment's convention. Full 78-test suite re-verified
  green under the corrected role -- nothing else had been quietly depending on the bypass.
- 2026-09-18 — Phase 11 — Added §6.8 (the query engine mechanism: tenant scope as a function parameter
  rather than a spec field, every client-controlled value as a named ClickHouse parameter, caps as
  ClickHouse query settings rather than an app-level timeout, UTC-bound-but-tz-bucketed range handling,
  the dual JWT-or-read-key auth path, and the spec+tenant-keyed cache). New `pulse/query/` module
  (`spec.py`: `TrendSpec` and its nested filter/range/measure shapes; `builder.py`: `build_trend_query`;
  `cache.py`: `cache_key`/`get_cached`/`set_cached`; `service.py`: `run_trend`, the cache-then-ClickHouse
  orchestration), new router `pulse/api/query.py` (`POST .../query/trend`). `pulse/core/security.py`
  gained `get_current_user_optional`; `pulse/api/dependencies.py` gained `resolve_query_scope`, the
  either-JWT-or-read-key check. Four new `query_*` settings in `pulse/core/config.py`. 98 tests pass (78
  -> 98, +10 query-builder unit tests, +5 query-engine integration tests); `ruff`/`mypy` clean. Verified
  via the full local suite against the real containers (ClickHouse/Postgres/Redis), not mocks --
  tenant-leakage, timezone bucketing, and cache hit/miss all exercised against genuine inserted events,
  not fixtures standing in for the store.
- 2026-09-18 — Phase 12 — Added §6.9 (the funnel query mechanism: a raw per-user level histogram from
  SQL, with cumulative step counts/conversion/drop-off computed in Python; the real `DateTime64(3)` ->
  `windowFunnel` type error hit against live ClickHouse and fixed with `toDateTime(...)`; ordering
  enforced by `windowFunnel`'s own semantics, verified by a fixture with an out-of-order and a
  skip-a-step user, not just the happy path; the conversion window's independence from the query date
  range; and cache/tenant-scoping reuse via the new `InsightSpec = TrendSpec | FunnelSpec` union rather
  than a second hand-written path). `pulse/query/builder.py` gained `build_funnel_query`,
  `window_seconds`, and two extracted shared helpers (`_tenant_and_range_where`, `_apply_filters`) used
  by both the trend and funnel builders. `pulse/query/spec.py` gained `FunnelStep`/`FunnelWindow`/
  `FunnelSpec`. `pulse/query/service.py` gained `run_funnel`/`FunnelResult`/`_funnel_step_results`.
  `pulse/api/query.py` gained `POST .../query/funnel`. A real bug was caught in the test suite itself,
  not the implementation: the first `test_funnel_never_leaks_across_tenants` draft generated three
  *identical* events for one user (`user_id="b1"` three times) expecting 3 users, when `windowFunnel`
  correctly groups by user and returned 1 -- fixed by generating three distinct users
  (`f"b{i}"`), a reminder that "the engine is wrong" and "the fixture is wrong" need to both be checked
  before trusting a failing assertion. 108 tests pass (98 -> 108, +11 funnel-builder unit tests, +3
  funnel-engine integration tests); `ruff`/`mypy` clean. Verified against the real ClickHouse container,
  not mocks, including the ordering/window-boundary fixture and a dedicated funnel tenant-leakage test
  (CLAUDE.md #4: tenant-leakage tests are mandatory for every query type, not just trend's).
- 2026-09-18 — Phase 13 — Added §6.10 (the retention query mechanism) and corrected §5.3's original
  `retention()`-based sketch: ClickHouse's `retention()` aggregate can't express a per-user-relative
  period offset (every user's cohort start is a different value, not a query-wide constant), so retention
  is instead two queries -- a per-user cohort period and a per-user activity-period set, the latter
  deliberately widened past `range.to` by `periods * period-length` so a cohort near the range's end can
  still show later-offset retention -- merged into a flat cohort/offset grid in Python, the same
  SQL-emits-facts/Python-computes-the-shape split Phase 12 established. `pulse/query/builder.py` gained
  `build_retention_cohort_query`/`build_retention_activity_query`, reusing trend's own `_bucket_expr` (no
  third bucketing implementation). `pulse/query/spec.py` gained `RetentionPeriod`/`RetentionSpec`;
  `InsightSpec` is now `TrendSpec | FunnelSpec | RetentionSpec` -- all three insight kinds SPEC.md #4.2
  originally sketched are now built. `pulse/query/service.py` gained `run_retention`/`RetentionResult`/
  `_retention_grid_rows`. `pulse/api/query.py` gained `POST .../query/retention`. A real ClickHouse
  asymmetry was caught live, not from documentation: `toStartOfWeek` returns `Date` while `toStartOfDay`
  returns `DateTime`, so the same bucket expression came back as a bare `date` for week periods and a full
  `datetime` for day periods -- invisible until an actual week-period query ran against live ClickHouse,
  since Phase 11's own engine tests only ever exercised day granularity; fixed with `_as_utc_datetime`,
  normalizing both shapes to midnight-UTC `datetime` so the API response is consistent regardless of
  period. Also caught a bug in the test fixture itself (not the implementation): the first hand-computed
  grid assertion hardcoded a full ISO-datetime cohort-period key before that normalization fix existed,
  which would have papered over the exact asymmetry it was meant to catch -- worth naming since it's the
  second phase running in which "the test's own expectation was wrong" needed ruling out before trusting a
  failing assertion, the same lesson Phase 12's tenant-leakage fixture taught. 119 tests pass (108 -> 119,
  +7 retention-builder unit tests, +4 retention-engine integration tests); `ruff`/`mypy` clean. Verified
  against the real ClickHouse container: a two-cohort, five-user hand-computed week-bucketing grid
  (including a user retained only after the query range's own `to`, proving the window-widening works, not
  just that it compiles), a day-bucketing grid, the `return_event == born_event` case, and a dedicated
  retention tenant-leakage test.
- 2026-09-18 — Phase 14 — Added §6.11 (the frontend foundation mechanism): session persistence deviates
  from Helix's httpOnly-cookie pattern to a localStorage-persisted refresh token, confirmed with the user
  first, because pulse's own `/auth/refresh` (unlike Helix's) returns the refresh token in the JSON body,
  not a cookie; no server-side "active org" concept, unlike Helix's `active_org_id` + `/switch` endpoint --
  the org/project switcher is purely URL-driven, mirroring the backend's own nesting; scope confirmed with
  the user as shell+auth+switcher+minimal proof-of-concept pages only, not full CRUD for every existing
  backend feature. New `frontend/src/` tree (`lib/{api,auth-api,auth-context,orgs-api,projects-api,
  query-client,refresh-token-storage}.ts(x)`, `components/{ui/*,ProtectedRoute,GuestRoute,AppShell,
  OrgProjectSwitcher}.tsx`, `app/{login,register,orgs,orgs/[orgId]/projects,
  orgs/[orgId]/projects/[projectId]}` routes); restructured `app/` under `src/app/` to match. New CI job
  `test-frontend` (lint/typecheck/vitest); Playwright e2e flows written and verified locally, deliberately
  kept out of CI this phase (confirmed with the user). Three real bugs found and fixed, none of them this
  phase's own regressions: a pre-existing Phase-0 ESLint config bug (the old `FlatCompat` eslintrc shim
  throws under `eslint-config-next` 16, reproduced against the untouched scaffold via `git stash` before
  fixing it with the native flat-config imports); a genuine `react-hooks/set-state-in-effect` violation in
  the session-recovery effect, fixed by routing every branch through one promise chain instead of a
  synchronous early-return `setState`; and a confusing browser-reported "CORS policy" error that was
  actually an unrelated `500` on `/auth/register` because the running `docker-api-1` container's Postgres
  had never had `alembic upgrade head` run against it (fallout from this session's earlier Docker Desktop
  instability, unrelated to any code in this phase) -- found only by driving the real app through the
  built-in browser end to end, not by any automated test. 13 new component/unit tests pass;
  lint/typecheck/`npm run build` all clean; both Playwright flows pass locally against the real stack.
