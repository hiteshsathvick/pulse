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

> As of Phase 16: `User`/`Organization`/`Membership`/`Project` (Phase 2), `RefreshToken` (Phase 3, not
> listed above — see §6.2), `Invite` and `AuditLog` (Phase 4), `ApiKey` (Phase 5, ahead of this table's
> original phase note — see §4.1), `EventSchema`/`PropertySchema` (Phase 9, see §6.6), `Insight` (Phase 15,
> see §6.12), `Dashboard`/`DashboardItem` (Phase 16, see §6.13). The rest of this list is the full eventual
> shape; each remaining table is built in the phase that needs it (`Alert` Phase 19, `Billing` Phase 20).

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

### 5.2 Rollups (implemented Phase 17)

**As built, deviating from this section's original sketch in three ways** (see §6.14 for the full
mechanism and the load-test evidence behind each):

```sql
-- hourly count + approximate unique users per event, per project (AggregatingMergeTree via MV)
CREATE TABLE event_hourly
(
    org_id      UUID,
    project_id  UUID,
    event_name  LowCardinality(String),
    hour        DateTime('UTC'),
    events      SimpleAggregateFunction(sum, UInt64),
    users_state AggregateFunction(uniqCombined64(15), String)
)
ENGINE = AggregatingMergeTree
PARTITION BY (org_id, toYYYYMM(hour))
ORDER BY (org_id, project_id, event_name, hour)
TTL hour + INTERVAL 365 DAY;

CREATE MATERIALIZED VIEW mv_event_hourly TO event_hourly AS
SELECT org_id, project_id, event_name,
    toStartOfHour(toDateTime(timestamp, 'UTC'), 'UTC') AS hour,
    count() AS events,
    uniqCombined64State(15)(if(user_id != '', user_id, anonymous_id)) AS users_state
FROM events
GROUP BY org_id, project_id, event_name, hour;
```

- **HOURLY, not daily-UTC.** Trends bucket by the *project's* timezone (§5.3); a UTC-day rollup can only
  answer for UTC projects. An hour bucket re-buckets into a correct local day/week/month for any timezone
  whose UTC offset is a whole number of hours throughout the query's range — checked per query
  (`is_rollup_eligible`), not assumed from the zone name, since some zones (Lord Howe) are whole-hour part
  of the year and fractional the rest.
- **Counts are exact; unique users are an approximate sketch, deliberately gated by data volume.** An exact
  `uniqExactState` was measured 3-20× *slower to merge* than scanning raw events (merging exact per-hour
  user sets costs more than the scan it replaces) — see §6.14. `uniqCombined64(15)` is used instead, and
  only once a query's window holds enough events (`query_rollup_unique_min_events`, load-test-tuned to
  5,000,000 — see `docs/PERFORMANCE.md`) that exact raw would be slow or refused; smaller windows stay on
  exact raw. The response's `approximate` field says which happened.
- **An explicit target table** (`TO event_hourly`), not the sketch's own implicit inner table, so it can be
  backfilled, verified (`python -m pulse.rollups verify`), and rebuilt (`rebuild`) directly.

Rollup-eligible trends (no filters, no breakdown, `count` or `unique_users`) read from `event_hourly`;
everything else (funnels, retention, filtered/breakdown trends) always reads raw, by design — they need
per-user event order or per-property data the rollup doesn't hold.

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
- **A fourth bug, found later in `next dev` (not by Phase 14's own tests): session recovery ran twice under
  React Strict Mode and the second run wiped the session.** The refresh token is single-use (the backend
  rotates it on every redeem), and Strict Mode runs the mount effect twice back to back, so both runs read
  the same not-yet-rotated token from `localStorage` and sent two `POST /auth/refresh` calls: the first
  returned `200` (rotated), the second `401`, and the failure path cleared the stored token -- leaving
  `localStorage` empty and an empty orgs page after any reload or deep link. An effect cleanup/ignore flag
  was ruled out: it would only discard the stale run's *result*, while its request would still be sent and
  still `401`. Fixed at the request instead: `recoverSession` in `auth-context.tsx` (refresh, persist the
  rotated token, `fetchMe`) keeps one module-level in-flight promise keyed by the stored token, so both
  effect invocations share a single request, then clears it once settled so a later, separate mount always
  recovers fresh. It never rejects (a failure clears the stored token and resolves `null`), so the effect
  keeps its single `.then().finally()` chain and `set-state-in-effect` is still satisfied without a
  suppression. `auth-context.test.tsx` covers it under `<React.StrictMode>` with a mock that 401s a
  re-used token (one `apiRefresh` call, session recovered, rotated token stored), plus the rejected-refresh,
  no-stored-token, and later-remount cases; confirmed the new tests fail against the unfixed code first.
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

### 6.12 Insight builder + charts mechanism (implemented Phase 15)

The first phase that surfaces Phases 11-13's query engine in the UI, plus the `Insight` table that lets a
built query be saved and reopened.

**Backend (`Insight` persistence)**
- **`insights` table (migration `0008`)** -- `id, org_id, project_id, name, kind, spec JSONB, created_by`,
  RLS-protected on `org_id` exactly like `event_schemas` (a browse-this-project's-saved-insights pattern,
  not token redemption). `kind` (`insight_kind` enum) is denormalized from `spec.kind` so a list can label
  by type without parsing JSONB; `update_insight` re-derives it whenever the spec changes, so the two can't
  drift.
- **A saved spec can never be one the query engine can't compile.** The write requests take the spec as
  `InsightSpec` behind a `kind` discriminator (`pulse/api/insights.py`), so FastAPI rejects a bad spec with a
  clean `422` before any row is written -- the same Pydantic models the query endpoints use, not a second
  validator. The service stores `model_dump(mode="json", by_alias=True)`, so `DateRange` keeps its `"from"`
  alias and a stored spec can be POSTed straight back to `/query/{kind}`.
- **Endpoints** under `/api/v1/orgs/{org_id}/projects/{project_id}/insights`: `GET` (list, newest-updated
  first), `POST`, `GET /{id}`, `PATCH /{id}` (rename and/or replace the spec; at least one required),
  `DELETE /{id}`. Read needs `viewer`; write/delete need `member` (this repo's roles are
  viewer/member/admin/owner). Every mutation writes an `AuditLog` row in the same transaction. An insight
  is only reachable through its own project's URL -- a sibling project in the same org gets a `404`.
- **A real bug caught by the new tests, worth remembering for any future RLS-table service:** the org
  scope (`set_config(..., true)`) is *transaction-local*. Calling `session.refresh(obj)` **after**
  `session.commit()` opens a fresh, unscoped transaction, so the RLS policy's
  `current_setting('app.current_org_id')::uuid` reads `''` and Postgres raises `invalid input syntax for
  type uuid: ""` -- not a permissions error, which made it look like a driver problem. Fix: flush and
  refresh *before* commit (needed here because `updated_at` is a server-side `onupdate` value SQLAlchemy
  expires after flush).

**Frontend (builder + charts)**
- **Chart library: Recharts** (the choice §2 deferred to this phase). Line/bar/table trends use it;
  **funnel and retention are custom components** (a step-bar list; a cohort × period heat grid) because
  neither is a standard chart shape, so a charting library would not have saved any work there.
- **The form edits a string-typed `Draft`; `buildSpec()` (`src/lib/insight-spec.ts`) is the only place a
  spec is produced**, and it validates the same rules the backend enforces (≥1 event / ≥2 funnel steps,
  positive integer window, retention `periods` 1-52, ordered date range, complete filter rows). An invalid
  form shows why and sends nothing. `specToDraft()` is its inverse, which is what makes a saved insight
  reload into the form losslessly (round-trip tested for all three kinds, including a non-default `tz`).
  Retention has no filters or breakdown in the API, so the UI hides them for that kind rather than
  collecting fields that would be silently dropped.
- **Schema-driven autocomplete uses a native `<input list>` + `<datalist>`, not a custom combobox** --
  accessible for free and, importantly, still accepts free text: the registry only learns an event/property
  name once it has been ingested, so a user can legitimately type one it doesn't know yet. Event
  suggestions come from `GET /schema/events`; property suggestions from the properties of the events
  currently picked. `hidden` events/properties are excluded; the sum/average measure offers numeric
  properties only.
- **Result shaping is pure and separate from rendering** (`src/lib/insight-results.ts`: `pivotTrend`,
  `groupFunnel`, `pivotRetention`), so the pivoting is unit-tested without a DOM. Bucket labels are
  trimmed from the server's string rather than round-tripped through `Date`, which would re-shift them
  into the browser's timezone and undo the query engine's project-timezone bucketing (§5.3).
- **Routes:** `.../insights` (list), `.../insights/new`, `.../insights/[insightId]` (opens a saved insight:
  form pre-filled from its stored spec and its result re-run automatically). The project home links to it.

**Testing.** Backend `tests/test_insights.py` (14): each kind round-trips through save → reload with an
identical parsed spec; six invalid specs are rejected and nothing is stored; rename / re-spec keeps `kind`
in sync; delete; viewer-read-only; **tenant-leakage** (another org gets `404` on read/list/patch/delete, and
RLS independently hides it at the service layer); a sibling project can't reach it. Frontend (43 new
Vitest tests): spec building/validation/round-trip, result pivoting and formatting, every result type
rendering (chart wrapper + table view for trend, steps/drop-off for funnel, grid + unobserved-cell dash for
retention, and each empty state), and the builder end to end against a mocked API (autocomplete contents,
the exact spec sent for each kind, save/update/delete, API errors surfaced, saved insight pre-fills and
auto-runs). Recharts needs a `ResizeObserver` stub in `vitest.setup.ts` (jsdom has none); tests assert on
the chart's wrapper and the table view, not SVG geometry.

**Verified live, and what that caught.** Beyond the tests, the phase was driven through the built-in
browser against a real API, Postgres, and ClickHouse seeded with a synthetic event stream (a local demo
account/dataset, not part of the repo): the deep-linked insights list, opening a saved funnel (form
pre-filled, result auto-run, drop-offs summing correctly), building a trend from scratch (autocomplete
offering the registry's events, then the picked event's properties, numeric-only for a sum), the real
Recharts line chart with three breakdown series, saving (redirects to the new insight's own page), and
the retention grid. Two defects surfaced that no unit test had, both fixed with regression tests:
- **Float noise in summed decimals** (`88.97999999999999` in the trend table). Values now go through
  `formatValue()` (whole numbers stay whole, at most two decimals, thousands grouped), in the table and
  the chart tooltip.
- **Retention showed `0.0%` for periods that hadn't happened yet.** The engine (§6.10) returns a row for
  every cohort × offset, so a cohort born last week had offsets 1..N reading 0. That misstates retention
  (it reads as "nobody came back"). `RetentionResult` now renders a dash ("Not observable yet") for any
  cell whose period start is in the future (`hasPeriodStarted`), so the grid is the correct staircase.
  This needs the period length, which the response rows don't carry, so the retention `QueryResult`
  variant now carries the spec's `period`.

**Found along the way, outside this phase:** in `next dev`, a full reload lost the session -- Phase 14's
recovery effect (`auth-context.tsx`) had no in-flight guard, and React Strict Mode runs effects twice, so
two refreshes raced on one single-use refresh token (one 200, one 401 that cleared state). A production
build runs the effect once and was unaffected, which is why the live checks above used `next build` +
`next start`. Fixed separately as a Phase 14 bug fix (see the 2026-09-21 Phase 14 change-log entry), not
as part of this phase.

**Deliberately not done this phase:** dashboards / arranging saved insights (Phase 16); a Playwright flow
for the builder (the DoD's named tests are the three above; Phase 14's precedent kept e2e out of CI);
role-aware hiding of Save/Delete for viewers (the API's `403` is shown instead); unsaved-changes prompt on
navigation.

### 6.13 Dashboards mechanism (implemented Phase 16)

Arranges saved insights (§6.12) on a shared grid, with a per-dashboard date range and a refresh that
really is fresh. No new query capability: every tile runs the same `/query/{kind}` endpoints as the builder.

**Backend**
- **Tables (migration `0009`):** `dashboards` (`name, layout, default_range, shared_scope, created_by`) and
  `dashboard_items` (`dashboard_id, insight_id, position`), both RLS-protected on `org_id`. Beyond §4's
  sketch, an item also carries its own `org_id` (so its policy is a direct check, never a join) and the
  usual timestamps, and `(dashboard_id, insight_id)` is unique. Deleting an insight deletes its items
  (`ON DELETE CASCADE`): it simply disappears from the dashboards that showed it. `layout` holds only grid
  configuration (`{"columns": 12}`); each tile's placement lives on its item as `{x, y, w, h}`.
- **Sharing is two separate rules** (`pulse/dashboards/service.py`, the only place they live):
  *who can see it* -- `shared_scope` `private` (its creator only; not even an org owner or admin) or `org`
  (every member); and *who can edit it* -- never a viewer, always the creator, and an admin/owner for any
  dashboard they can see. There is no per-user grant table: the spec's data model has a single
  `shared_scope` column, and scope + role covers "share it read-only with a teammate". An invisible
  dashboard reads as `404`, not `403`, so a private dashboard's existence isn't leaked; a visible but
  uneditable one is `403`. The response carries a server-computed `can_edit`, so the UI never
  re-implements these rules.
- **Endpoints** under `/orgs/{org_id}/projects/{project_id}/dashboards`: `GET` (only what the caller can
  see, with an item count), `POST` (member+), `GET /{id}` (with each item's insight and spec embedded, in
  reading order), `PATCH /{id}` and `DELETE /{id}` (member+, then the edit rule). **`PATCH` takes an
  optional `items` list that replaces the whole layout in one transaction** -- there is no partial layout
  update, so a saved dashboard is never half old, half new. (An earlier plan had a separate
  `PUT /{id}/items`; folding it into `PATCH` makes rename-plus-relayout atomic too.) Every mutation is
  audit-logged with which fields changed.
- **Layout validation is server-side and independent of the UI:** each tile inside the 12-column grid
  (`x + w <= 12`, `1 <= h <= 8`), no two tiles overlapping, no insight twice, at most 20 tiles, and every
  insight must exist *in this project* -- another org's insight is simply absent under RLS, so it fails the
  same way as a nonexistent one (`422`, without confirming it exists elsewhere).
- **Two persistence details worth remembering:** a layout-only change doesn't modify the `dashboards` row,
  so `updated_at` (an `onupdate` default) would never move -- it is touched explicitly. And re-saving an
  insight that was already on the dashboard must not trip the unique constraint: the old items are removed
  with a Core `DELETE` that executes immediately, because an ORM `session.delete()` would be ordered
  *after* the same flush's `INSERT`s.
- **Refresh:** the three query endpoints accept `?refresh=true`, which skips the Redis *read* but still
  writes the fresh result back (`run_trend/funnel/retention(..., refresh=True)`). Without it a Refresh
  button could show up to `query_cache_ttl_seconds` (60 s) of stale numbers; with the write-back, the next
  ordinary call is served the up-to-date value rather than the old one.

**Frontend**
- **Range.** `default_range` is `relative` ("last N days", N 1-366, counting today) or `absolute`. A saved
  insight carries its own fixed dates; on a dashboard the dashboard's range overrides only `from`/`to`
  (`withRange`) -- a per-insight timezone override, funnel window, retention periods etc. are left as
  saved. "Today" is resolved in the **project's** timezone, not the browser's, because the query engine
  bounds and buckets by project-local dates (§5.3); otherwise "last 7 days" is off by one near midnight.
  Anyone, including a read-only viewer, can change the range for their own view; only an editor can change
  the saved default.
- **Layout editing works on an ordered list of sized tiles, not free-form rectangles.** `packLayout` places
  tiles left to right, wraps at 12 columns, and starts each row below the tallest tile in the previous one,
  so an overlapping or out-of-bounds layout is unrepresentable from the UI (a randomized test asserts it for
  arbitrary mixes). Sizes are Small 4x3, Medium 6x3, Large 12x4; a tile is reordered with Up/Down. **No
  drag-and-drop** -- it needs a grid library and is the hardest part to test reliably; because positions
  are stored as `{x, y, w, h}`, adding it later needs no migration. `buildPatch` sends only what changed
  (a rename doesn't rewrite the layout).
- **The page** renders the saved placement as a CSS grid from `md` up (140 px rows) and stacks tiles in one
  column below that. Each tile fetches for itself (a failing insight shows its own error and leaves the
  rest working) and keeps its previous chart on screen while a new range or refresh loads. Refresh bumps a
  counter that both changes the query key and sends `refresh=true`; auto-refresh (off / 1 min / 5 min) is a
  client-side timer that does the same and is deliberately not persisted (a saved interval would need a
  column beyond the spec's model). A brand-new empty dashboard opens straight in the editor; the
  create form is hidden from viewers (the API would refuse it).

**Testing.** Backend (16 new; 149 total pass): CRUD and defaults; invalid ranges; **layout persistence**
(out-of-order input reloads in reading order, a second save replaces the first including a re-placed
insight, `[]` clears it, a layout-only change moves `updated_at`); seven kinds of invalid layout rejected
with the saved one untouched; a cross-project insight refused; deleting an insight drops its tiles; the
full **sharing matrix** (private / org x owner / admin / creator / other member / viewer, including 404 vs
403 and that changing scope changes visibility immediately); **tenant leakage** (another org gets 404 on
everything, cannot place this org's insight, and RLS independently hides it below the router); and
**refresh correctness** (a normal call returns the cached stale value, `refresh` returns the new one and the
next ordinary call then agrees, for all three insight kinds and over HTTP). Frontend (59 new; 119 total):
range resolution incl. timezone boundaries (the same instant is a different calendar date in India and
California), `withRange` for each insight kind, layout packing/operations, draft diffing; and the view,
editor and list components against a mocked API.

**Verified live** in the built-in browser against a real API, Postgres and ClickHouse, with two real
accounts: an owner assembled a trend + funnel + retention dashboard shared with the org (the saved
placement applied exactly at desktop width, and survived a full reload); changing the range and pressing
Refresh re-ran all three tiles (`?refresh=true`, and a signup inserted directly into ClickHouse appeared);
a *viewer* then saw the dashboard read-only (no Edit button, own range change works), and driving the API
as that viewer returned `403` for rename, re-layout, delete and create, and `404` for a private dashboard.
No defects surfaced in that run.

**Deliberately not done:** drag-and-drop; per-user sharing/ACLs; dashboard-wide filters; duplicating a
dashboard; public links; text tiles; alerts (Phase 19); a Playwright flow (as in Phase 15, e2e stays out of
CI); an unsaved-changes prompt; and **reading from rollups** -- `PULSE_PROJECT_GUIDE.md`'s Phase 16 entry
lists it, but this spec assigns rollups to Phase 17 with their own parity tests and benchmarks, so this
phase follows the spec. **Known risk until Phase 17:** a dashboard fires one query per tile at once (at
most 20), and per-tenant query rate limits don't exist yet.

### 6.14 Rollups + performance mechanism (implemented Phase 17)

The dashboard risk §6.13 flagged is resolved here: rollups make the query type dashboards mostly use fast
and cheap, and a per-org rate limit bounds how many queries any one tenant can fire at once.

**Rollup and routing.** §5.2 has the schema and the three deviations from its original sketch (hourly not
daily-UTC; exact counts / approximate gated unique users; an explicit target table). The router
(`pulse/query/rollup.py`) decides per trend query: `is_rollup_eligible` checks the *shape* (count or
unique_users, no filters, no breakdown, every point in the range at a whole-hour UTC offset); for an
eligible count query, always the rollup. For an eligible unique-user query, `_plan_trend`
(`pulse/query/service.py`) runs one cheap extra query (`build_event_total_query`, reading only the rollup's
exact `events` column, never the sketch) to estimate the window's event volume, then picks exact raw below
`query_rollup_unique_min_events` or the approximate sketch at or above it. This decision -- and the
response's `source`/`approximate` fields -- are computed fresh on every call and also stored in the result
cache (`pulse/query/cache.py`'s `CachedResult`), so a cache hit reports the same provenance a miss would
have.

**Rate limiting.** `check_query_rate_limit` (`pulse/core/rate_limit.py`) is a per-*org* fixed-window Redis
counter, same pattern as the existing login/ingest limiters, called once a query is past the cache check
(a cache hit costs nothing) and past `ProjectNotFound`. Over the limit: `429` with a `Retry-After` header
computed from the key's actual TTL, re-arming the key if a crash ever left it without one (so a stuck
counter can't lock an org out forever). Shared across trend/funnel/retention -- one budget per tenant, not
one per insight kind.

**A caps-aware query path.** Every ClickHouse call across all three insight kinds now goes through one
helper (`_query` in `pulse/query/service.py`) that recognizes ClickHouse's own cap errors (row/time/memory
limit codes) and raises `QueryTooExpensive`, mapped to a clean `422` with actionable advice -- instead of a
generic `500`. Load testing is what surfaced how often this fires in practice (see `docs/PERFORMANCE.md`
Finding 1): without the rollup, a large project's dashboard queries are refused by this path almost
universally under concurrent load, never hang or crash.

**Maintenance.** `pulse/rollups/maintenance.py` + `python -m pulse.rollups {verify,rebuild}`: `verify`
diff-checks `events` against `event_hourly` (event counts exactly, unique-user sketches within a tolerance,
since the sketch is approximate by design), optionally scoped to one project; `rebuild` truncates and
backfills from raw with the ingestion workers stopped. Both were exercised live against a genuine drift
(not staged) during the Phase 17 load tests -- see `docs/PERFORMANCE.md`'s ingestion section.

**Load testing.** `backend/loadtests/` (`bench.py` + `locustfile.py`, committed): an isolated benchmark
database/ClickHouse database/Redis DB, server-side event generation (millions of rows in seconds), a query
scenario (nine insight types weighted like a real dashboard, `QUERY_SUBSET` to measure §1.4's two separate
latency targets apart, `?refresh=true` so the cache never hides what's being measured) and an ingest
scenario (accept rate and true end-to-end landing rate reported separately, since the buffer is deliberately
decoupled from the endpoint, §3.2). Full method, results, and the two findings that changed a design
decision (the unique-user threshold) or were deliberately left as documented, unfixed limitations (ad-hoc
query latency under load) are in `docs/PERFORMANCE.md`, not restated here.

**One shipped value changed by what load testing found, not by the original plan:**
`query_rollup_unique_min_events` was planned and first measured (single query, uncontended) at 2,000,000;
concurrent load testing showed that value routed wide weekly/monthly queries on a moderate-sized project
into a sketch-merge cost that was *slower* than the raw scan it was meant to avoid (a sketch merge's cost is
proportional to how many hourly rollup rows a query spans, not the event count). Raised to 5,000,000 based
on that evidence, re-verified to fix the regression without breaking the large-project case that still
needs the sketch. `docs/PERFORMANCE.md` Finding 2 has the numbers.

**Deliberately not done:** the query-plan review (`EXPLAIN`) confirmed the existing Phase 6 sort key prunes
effectively, so no index/sort-key change was made; a coarser (daily) rollup tier for very wide ranges, which
would reduce the sketch-merge row count further, is a natural Phase 17 follow-on rather than something this
phase's DoD asked for; and the root cause behind ad-hoc query latency under load (retention's Phase 13
Python-side computation blocking its worker's event loop) is documented, not fixed -- it predates this
phase and fixing it would mean moving that computation off the request path, out of scope here.

### 6.15 NL-to-query mechanism (implemented Phase 18)

A new `pulse/ai/` module (no tables of its own -- composes `pulse/registry/` and `pulse/query/spec.py`),
plus one new route, `POST .../query/nl`, added to the existing `pulse/api/query.py` router (so it inherits
`resolve_query_scope`'s JWT-or-read-key auth for free, the same as trend/funnel/retention).

- **Translate and run are two separate requests, not one.** `/query/nl` only ever returns
  `{status, spec | message, warnings}` -- it never executes a ClickHouse query itself. The DoD's
  "interpreted spec shown before running" is true by construction: the frontend shows the spec, and only a
  second, ordinary call to `/trend`/`/funnel`/`/retention` (ClickHouse-side, this section's exact same
  build_trend_query et al.) runs it. There is no auto-run code path to have gotten wrong.
- **`LLMProvider` (`pulse/ai/provider.py`)** is the one abstraction the rest of the app depends on --
  matching SPEC.md #2's "AI: provider abstraction," rebuilt fresh here since Pulse has no sibling project to
  port it from. `MockProvider` (deterministic, offline, free) is `Settings.ai_provider`'s default and the
  only one CI ever calls; `AnthropicProvider` is real, gated behind `ai_provider=anthropic` +
  `anthropic_api_key`, and forces the one tool call via Anthropic's `tool_choice` so a prompt-injection
  attempt in the question has no free-text channel to answer through.
- **The only thing a provider is trusted to produce is a JSON dict for one tool, `submit_insight_query`**
  (`pulse/ai/schema.py`). `pulse/ai/translator.py` re-parses that dict through
  `pulse.query.spec.DiscriminatedInsightSpec` -- the exact same discriminated union `pulse/api/insights.py`
  already used for a human-built spec (moved into `pulse/query/spec.py` this phase so both share one
  definition rather than duplicating it). A spec type has no `org_id`/`project_id`/SQL field at all, so
  there is nothing for a provider to smuggle a tenant override or a raw query through even if it tried --
  proven by `test_nl_translator.py`'s "smuggled tenant field" test and the dedicated
  `tests/test_nl_injection.py` adversarial suite (SPEC.md #8's injection-suite requirement).
- **Grounding is read-only and pre-scoped.** The prompt embeds this one project's active, most-seen event
  names (`registry.service.list_events`, already `org_id`/`project_id`-scoped) and today's date resolved in
  the project's own timezone, so relative phrases ("last week") resolve the same way a human builder's
  range would. An event name outside the registry is a soft, non-blocking warning shown next to the spec,
  not a rejection -- Phase 9's own precedent ("unknown events still ingest") applies here too.
- **Ambiguous -> clarify, not a guess.** The tool schema's `status` is `"ok"` (with a `spec`) or `"clarify"`
  (with a short `message`); the system prompt tells the model to prefer clarifying over guessing when the
  question doesn't map cleanly to a trend/funnel/retention shape, and `translator.py` rejects anything that
  is neither -- a malformed or missing-message response raises `TranslationFailed`, mapped to a clean `422`,
  never passed through.
- **A separate, per-org rate limit** (`ai:translations:{org_id}`, `pulse/core/rate_limit.py`'s
  `check_ai_rate_limit`) budgets the translate call itself, independent of `query_rate_limit_*` -- a
  clarify response still cost a model call, so it isn't shielded by the ClickHouse-side limiter the way a
  cache hit is.
- **The eval set is one fixture list, two tests** (`backend/tests/nl_eval/`). `test_mock_eval_set_meets_its_
  accuracy_threshold` runs unconditionally in CI against `MockProvider` at a 100% threshold (mock matching
  is exact-or-broken, not approximate -- this is the harness that actually gates every push, per the DoD's
  "eval set gates regressions"). `test_live_eval_set_meets_its_accuracy_threshold` runs the identical
  fixtures against the real Anthropic provider at an 80% threshold, but only when you set
  `PULSE_TEST_LLM_LIVE=1` and `ANTHROPIC_API_KEY` -- confirmed with the user first: CI stays free and
  deterministic, and a true accuracy check against the real model is available on demand rather than run
  (and paid for) on every push.
- **`MockProvider`'s phrase-matching (`pulse/ai/mock_patterns.py`) anchors its trailing "when" clause to a
  finite set of known phrases, not a generic `(.+)`.** A generic capture left a real ambiguity: a
  multi-word event name immediately followed by a time phrase (e.g. "onboarding finished last week") could
  be split in the wrong place by regex backtracking. Anchoring the date-phrase alternation forces the
  event-name group to absorb everything else unambiguously -- caught and fixed while building the eval
  fixtures, not by a test written after the fact.

**Deliberately not done:** OpenAI/local providers (SPEC.md #2 lists them as pinned intentions; the provider
abstraction is built to add them later without touching `translator.py`, but only Anthropic is wired up now
-- confirmed with the user first); saving a translated spec directly as an `Insight` from the NL box (the
existing manual builder's Save already covers this once a spec exists, and duplicating that flow wasn't
part of this phase's DoD); property-key grounding/warnings (only event names are checked against the
registry, matching what the DoD's tests actually asked for).

**Verified live** against a real API, Postgres, ClickHouse, and Redis: registered an account, created an
org and project, and asked the NL box "How many times did checkout completed happen last week?" -- it
returned an interpreted trend spec plus the expected "hasn't been recorded yet" warning (a fresh project
has no events), pressing Run executed a real `/trend` call and rendered "No events matched this query" (an
empty result is still a real, correct result), and a follow-up unrelated question ("What's the weather like
today?") correctly returned a clarify message instead of guessing.

### 6.16 Anomaly + alerts mechanism (implemented Phase 19)

A new `pulse/alerts/` module (no tables of its own beyond `alerts`/`alert_events`, added this phase --
composes `pulse/query/` and `pulse/insights/`, never reimplementing either), a new `alert-worker` process
(`pulse/alerts/main.py`, its own docker-compose service -- confirmed with the user first, over an
externally-cron'd one-shot CLI, to keep a self-hosted deploy cron-free), and `POST .../alerts` +
`.../alerts/{id}/evaluate-now` + `.../alerts/events` routes (`pulse/api/alerts.py`).

- **Two rule kinds, deliberately different insight-kind scope.** A `ThresholdRule` (`comparator`, `value`)
  works against any insight kind -- trend's latest bucket, funnel's final-step conversion %, or retention's
  most recent cohort's *period-1* value (the one unambiguous single number a 2D cohort grid has; picking the
  furthest offset instead would conflate different cohorts' different measurement horizons as data
  accumulates). An `AnomalyRule` (`method`, `window`, `sensitivity`) only ever validates against a trend
  insight -- rejected at alert-creation time with a `422` otherwise (`pulse/alerts/service.py`'s
  `AnomalyRequiresTrend`) -- because a bucketed time series is the one thing it needs that only a trend
  query already produces; funnel/retention are snapshot-shaped, and inventing a new evaluation-history table
  just to retrofit one was more than this phase's DoD asked for.
- **An insight's own saved date range is a snapshot, not a live window.** Every evaluation shifts it to end
  "today" (in the project's timezone) before reusing the exact same `run_trend`/`run_funnel`/`run_retention`
  the query API calls -- `_shifted_range` preserves the saved spec's lookback *duration* but slides it
  forward, and the anomaly path builds its own `window`-sized range instead. Nothing here is a second query
  path: tenant scoping, caps, the result cache, and the per-org rate limiter are all inherited from the
  unchanged query engine, exactly as Phase 18's NL translator inherited them for its own reason.
- **Statistics: trailing z-score, and a same-weekday seasonal variant of it** (`pulse/alerts/anomaly.py`,
  pure functions, hand-computed test fixtures -- SPEC.md #8's "known hand-computed expected answers," same
  discipline as funnel/retention). Deliberately just this, not a full seasonal-decomposition model, per
  CLAUDE.md's "robust statistical methods... before anything fancier." The seasonal variant exists because a
  naive trailing window is demonstrably wrong the moment day-of-week seasonality is real: going into a
  weekend, a short trailing window is weekday-heavy (only the most recent Sunday in a 6-day window), so its
  mean/stdev are pulled toward weekday behavior and an entirely ordinary weekend number reads as an
  outlier -- `test_anomaly.py` proves both halves of this (the naive method wrongly flags it; the seasonal
  method, filtering history to the same weekday first, correctly doesn't) and that the seasonal method still
  catches a real same-weekday anomaly, not just avoids false positives.
- **Fires once per breach episode, not once per evaluation tick.** `Alert.is_breaching` is the evaluator's
  own state (not user input): a breach only fires -- writes an `AlertEvent`, triggers delivery -- on the
  `False -> True` transition; a still-breaching alert is silently re-evaluated and left alone; a `True ->
  False` transition clears the flag (`recovered=True`) with no event of its own. Changing an alert's rule
  resets `is_breaching` to `False`, so a changed rule starts a fresh episode rather than inheriting the old
  rule's breach state.
- **`evaluate_all_enabled` (the alert-worker's per-cycle entry point) cannot select across every org's
  alerts in one query.** An unscoped `session_scope()` default-denies every RLS-protected table (SPEC.md
  #4.1); `Organization` itself is the one exception (it carries no `org_id`; it *is* the tenant), so it's
  the one table this lists unscoped, then loops `session_scope(org_id=...)`-scoped per org for everything
  else -- a new pattern this codebase hadn't needed before, since nothing earlier was a background sweep
  across every tenant at once. `test_alert_evaluation.py`'s own cross-org test proves this doesn't leak.
- **Delivery: real webhook, real signing, no real email yet.** `EmailProvider` mirrors `pulse/ai/provider.py`'s
  shape exactly -- an ABC + `ConsoleEmailProvider` (the only implementation that ships this phase, logs
  instead of sending, the same stand-in Phase 4's invite emails already used: "no email provider chosen
  yet") -- confirmed with the user first, since a real SMTP provider needs infra (a mail server for local
  dev/testing) this phase doesn't add. The webhook sender IS real: an outbound `httpx` POST, HMAC-SHA256-signed
  (`X-Pulse-Signature: sha256=...`, the Stripe/GitHub convention -- there was no existing signing pattern in
  this codebase to mirror, so this establishes one) when `alert_webhook_secret` is set, unsigned otherwise
  since a self-hosted deployment may have no receiver that checks a signature at all. In-app delivery has no
  send step -- the `AlertEvent` row itself is the notification, polled via `GET .../alerts/events` and
  dismissed via `POST .../alerts/events/{id}/ack`, the same client-timer-polling pattern Phase 16's dashboard
  auto-refresh already established (no SSE/WebSockets, which would be a new architectural precedent this
  codebase has never used). `deliver()` never raises -- one channel's failure is recorded and the others
  still run, so a fire is never lost because email failed.
- **Frontend** (`components/alerts/`): `AlertList` (create + list, mirrors `DashboardList`'s inline-form
  pattern; the rule-type picker filters the insight dropdown to trend-only insights when "anomaly" is
  selected, so the form can't even offer a combination the API would refuse) and `AlertDetail` (enable/
  disable, delete, an "Evaluate now" button running the exact same `evaluate_alert()` path the worker's own
  schedule uses -- not a second, lighter implementation -- plus the events feed with acknowledge). Linked
  from the project home page alongside Insights/Dashboards, not nested inside the insight detail page --
  an alert references one insight but is its own resource with its own lifecycle, the same relationship
  `DashboardItem` has to `Insight`.

**Deliberately not done:** a real SMTP provider (confirmed with the user first -- documented follow-up, same
deferral Phase 4 already made); webhook retry/backoff (explicitly Phase 21's job per SPEC.md #7, which
promises "signed, retried outbound webhooks" as its own deliverable -- this phase ships the signing, not the
retry); anomaly detection on funnel/retention insights (no time-series history exists for either); a
project-wide dashboard-style alert overview (each alert already has its own detail page with its own event
feed).

**Verified live** against the real API/Postgres/ClickHouse/Redis stack via the test suite's own live
ClickHouse-backed integration tests (`test_alert_evaluation.py`): a threshold alert on real inserted events
fired exactly once while the breach persisted across repeated evaluations, then recovered (no second event)
once enough events pushed it back over the line; a funnel alert correctly used the final step's real
conversion percentage; a retention alert correctly used the latest real cohort's period-1 percentage; an
anomaly alert fired on a real 10x spike over a ten-day real baseline and did not fire on a normal day at the
same baseline; and `evaluate_all_enabled` evaluated two real orgs' alerts against only their own real
ClickHouse data, never leaking one into the other's result.

### 6.18 Billing & usage metering mechanism (Phase 20)

A new `pulse/billing/` module and a new `billing-worker` process (`pulse/billing/main.py`, mirroring
`pulse/alerts/main.py`'s shape exactly, including the same unscoped-`Organization`-then-per-org-scoped
sweep Phase 19 established). **Design fork, confirmed with the user first, mid-phase:** Stripe test mode
turned out to need a real account this session couldn't set up, and separately the user didn't want a
paid/external dependency for this piece at all -- so a self-hosted `MockPaymentProvider`
(`pulse/billing/providers.py`) is the real default and the only payment path CI or a fresh clone ever
exercises, needing zero external account, ever. A real `StripePaymentProvider` is kept behind the exact
same interface as an optional swap-in (`Settings.payment_provider = "stripe"`) for whenever a real
processor is wanted later -- this is what makes the phase fully closeable today rather than staying in the
"built, not connected" limbo an earlier version of this section described.

- **A `PaymentProvider` interface, not a Stripe-specific one.** `pulse/billing/providers.py` defines
  `create_customer`/`create_checkout_session`/`create_portal_session`/`list_invoices` as an ABC, mirroring
  `pulse/ai/provider.py`'s own provider-abstraction shape. `MockPaymentProvider` implements all four purely
  locally (no network); `StripePaymentProvider` (`pulse/billing/stripe_client.py`) wraps the real Stripe SDK
  behind the identical interface, so `pulse/api/billing.py`'s route handlers call `provider.xxx(...)`
  without knowing or caring which one is configured. `Subscription.payment_customer_id` /
  `payment_subscription_id` (migration `0012`, renamed from `stripe_customer_id`/`stripe_subscription_id`
  the moment this fork was decided) hold whichever provider's ids, honestly, not Stripe's specifically.
- **Checkout and the Portal both redirect to Pulse's own `/orgs/{id}/billing/mock-checkout` page** under
  the mock provider -- the same one page doubles as "upgrade" (shows Confirm, on the free plan) and "manage"
  (shows Cancel, on Pro), so there's no separate mock portal page to maintain in parallel. Its Confirm/Cancel
  actions call `POST .../billing/mock/subscribe` and `.../mock/cancel`, which build the exact same
  event shape a real Stripe webhook payload would carry and hand it to `pulse/billing/webhooks.py`'s
  `handle_event()` -- the one and only place "how does a subscription change get applied" is implemented,
  never duplicated between the real-webhook path and the mock-action path. `mock/*` routes are disabled
  (`400`) whenever `payment_provider` is switched to `"stripe"`, so the two paths can never be exercised
  against each other by mistake.
- **Plans are a static config, not a database table.** `pulse/billing/plans.py`'s `PLANS` dict is the only
  place a plan's monthly event quota lives -- a real processor would own *pricing* (a Price id,
  `Settings.stripe_pro_price_id`, only meaningful for the Stripe path), but the quota number itself never
  depends on any payment provider being configured, which is what lets the free plan work fully offline.
- **Every org gets a `Subscription` row (`plan=free`, `status=active`) in the same transaction that creates
  the org** (`pulse/services/orgs.py::create_organization`, alongside the owner `Membership` -- the same
  atomicity reasoning: a crash between separate commits could otherwise leave an org with no billing
  record at all). This is what lets the ingest-path quota check assume a `Subscription` always exists,
  never a `None` case on the hot path.
- **Metering is org-wide, not per-project or per-spec, and reads the `event_hourly` rollup, never raw
  events** (`pulse/billing/usage.py`) -- unlike the query engine's own rollup queries
  (`pulse/query/rollup.py`), which are always scoped to one project and one spec's event list. Event counts
  are an exact `sum`; MTU merges the rollup's per-(project, event_name, hour) `uniqCombined64` sketch states
  across *everything* for the org, the same sketch type the query engine already uses for a single trend's
  unique-user count, just merged more broadly. `test_billing_usage.py` proves this against real inserted
  ClickHouse events, including that counts stay exact across multiple projects and event names and that a
  prior month's events are correctly excluded. This half of the phase never depended on any payment
  provider at all, mock or real.
- **The billing-worker recomputes and upserts (never increments) each org's current-period `UsageRecord`
  every `billing_usage_interval_seconds`** (default 5 minutes) -- a fresh `sum`/merge from ClickHouse each
  cycle, not an accumulator, so a worker restart or a double-run can never double-count.
- **The ingest-path quota check reads the last-computed `UsageRecord`, not a live ClickHouse query per
  request** (`pulse/billing/service.py::check_ingest_quota`, called from `pulse/ingest/router.py` right
  after the existing per-key rate limiter -- a different resource: request rate vs. monthly volume). Quota
  freshness lags by at most one worker cycle; the buffer, not this check, is `/ingest`'s real durability
  guarantee, so that lag is an acceptable trade-off. Soft (`>=` `billing_soft_limit_ratio`, default 80%,
  of the plan's quota) still returns `202` with a `quota_warning` field on `IngestBatchResponse`; hard
  (`>=` the quota) returns `402 Payment Required` before the batch ever reaches the buffer -- a real,
  literal use of the HTTP status code's original meaning. **Verified live directly against the running
  `/ingest` endpoint via curl, not just tests**: a genuine `402` once at quota, and a genuine `202` with a
  populated `quota_warning` once past the soft threshold.
- **Invoices are synthesized, not mirrored.** The mock provider returns one representative "paid" invoice
  (`Settings.mock_pro_price_cents`) once an org is actually on Pro -- a customer id alone (created the
  moment Upgrade is first clicked) isn't proof of a paid period, so `GET .../billing/invoices` returns `[]`
  until `mock/subscribe` (or, on the real path, a completed Stripe checkout) moves the plan to Pro. A real
  `StripePaymentProvider` reads Stripe's own invoice list live instead; neither path ever mirrors invoice
  data into Postgres.
- **The real webhook route's verified signature is the boundary, not RBAC** (`POST /api/v1/webhooks/stripe`,
  unscoped by org since a real processor calls it directly with no Pulse-issued credential) -- the same
  token-is-the-boundary reasoning `RefreshToken`/`Invite`/`ApiKey` already use, just via HMAC instead of a
  stored hash. This route is unused while `payment_provider` is `"mock"` (the default) -- nothing external
  ever calls it in that mode -- but its logic is fully exercised anyway, since the mock actions call the
  exact same `handle_event()`. `customer.subscription.created`/`updated` upgrades an org to Pro only on a
  genuinely `active`/`trialing` status; `past_due`/`incomplete` update the stored status without silently
  downgrading the plan. `customer.subscription.deleted` reverts to Free. `test_billing_webhooks.py` verifies
  both halves: the pure event-handling logic against plain dict fixtures, and the real route's signature
  verification against a payload the test suite signs itself with a locally-known secret (this scheme needs
  nothing but the shared secret to verify, so it never touches a real account or the network) -- including
  that a tampered payload with an otherwise-valid signature header is still rejected.

**Deliberately not done:** a real Stripe connection (available as a documented swap-in behind
`payment_provider="stripe"` whenever wanted, not required); mirroring invoices into Postgres (read live
instead); proration logic; multi-currency and seat-based billing (SPEC.md only asks for usage-based).

**Verified live** against the real API/Postgres/ClickHouse/Redis stack: org creation auto-creating a free
`Subscription`; the usage bar against real Postgres state in the browser; and, directly against the running
`/ingest` endpoint via curl, a real quota `402` once at the limit and a real `quota_warning`-bearing `202`
once past the soft threshold. The full mock Checkout -> Confirm -> Pro -> Invoice -> Cancel -> Free loop was
then clicked through end to end in the browser: Free plan (0/10,000, no invoices) -> "Upgrade to Pro"
redirecting to a same-origin `/billing/mock-checkout` page (never a stripe.com domain) -> "Confirm
subscription" -> Pro plan (0/1,000,000, one $29.00 paid invoice) -> "Manage billing" -> "Cancel subscription"
-> back to Free plan (0/10,000, invoices empty again).

**Real bug caught live, not by a test first:** the first click-through showed a genuinely inconsistent
page after "Confirm subscription" -- the heading still read "Free plan" while the quota line correctly
showed Pro's 1,000,000 limit and the new invoice had already appeared. The backend was right (the quota and
invoice queries had refetched); only the plan label was stale. Root cause: `query-client.tsx` sets a global
`staleTime` of 30s, and `MockCheckoutPage` runs its own `useQuery` for the same `["billing-subscription",
orgId]` key the billing page reads -- so navigating back within 30s of that fetch served the pre-upgrade
cached value instead of refetching. `["billing-usage", orgId]` looked fine only because the billing page's
own earlier fetch of it happened to be more than 30s stale by the time of the redirect. Fixed by having both
`subscribeMutation` and `cancelMutation` call `queryClient.invalidateQueries()` on all three billing query
keys (`billing-subscription`, `billing-usage`, `billing-invoices`) in `onSuccess`, before navigating back.
Reproduced with a plain reload before the fix (label corrected itself once the 30s window passed, confirming
it was a cache-timing bug and not a backend defect) and confirmed fixed afterward: the label now updates
immediately on both the Confirm and Cancel transitions, no reload needed.

### 6.19 Public API, exports & webhook reliability mechanism (Phase 21)

Three additions, all reusing existing mechanisms rather than inventing new auth, query, or delivery layers.

**Raw event export** (`GET .../export/events`, `pulse/api/export.py`, new): the route table's one new route.
Reuses `resolve_query_scope` unchanged -- a read API key or a JWT, the exact same auth the query routes
have accepted since Phase 11, so this needed zero new auth code. Streams rows directly out of ClickHouse via
`clickhouse_connect`'s `query_rows_stream` into a `StreamingResponse`, so memory stays flat regardless of
export size -- unlike every other ClickHouse read in this codebase, which materializes its (small,
aggregated) result in memory. JSON output is **newline-delimited** (one JSON object per line,
`application/x-ndjson`), not a single JSON array: an array needs the whole body buffered to close its
brackets, which would defeat streaming entirely. `ORDER BY timestamp` costs a real sort when the range spans
more than one event name (the table's own physical order is `(org_id, project_id, event_name, timestamp,
...)`), accepted because a chronological export is what a data-ownership export is for, and it's bounded by
a new `export_max_rows` setting (a literal SQL `LIMIT`, so the stream can never emit more than that many rows
no matter how large the underlying range) and by `max_execution_time`. Unlike interactive queries
(`query_service.QueryTooExpensive` -> a clean 422), a cap hit *mid-stream* has no clean way to change an
already-started 200 response's status code -- documented as an accepted, honest limitation of any streamed
HTTP export, not one this phase solves.

**Query-result CSV export** (`format=csv` on the existing `POST .../query/{trend,funnel,retention}`
routes, `pulse/api/query.py`): no new route, since these results are already small and fully materialized in
memory for the JSON path -- `format=csv` just serializes the identical `result.results` differently, calling
the exact same `query_service` function either way, so the two output formats can never drift apart. Returning
a `Response` directly from a route bypasses its declared `response_model` (documented FastAPI behavior), so
the JSON path keeps its Pydantic-validated schema unchanged.

**Webhook retry/backoff** (`pulse/alerts/delivery.py`): Phase 19 shipped HMAC signing but deliberately
deferred retry to this phase (SPEC.md said so explicitly). `send_webhook_with_result` now retries with
doubling backoff (`alert_webhook_max_retries`, `alert_webhook_retry_backoff_seconds`; defaults 3 retries,
1s/2s/4s) -- but only on a *transient* failure (timeout, connection error, 5xx). A 4xx fails immediately
without retrying: the receiver itself rejected the request, and retrying the identical payload won't change
that. `_send_webhook` (used by `deliver()`) is now a thin wrapper over this, so alert delivery's existing
behavior and tests needed no changes beyond the retry itself.

**Webhook test-send** (`POST .../webhooks/test`, `pulse/api/webhooks.py`, new; Admin+ only). **Design fork,
confirmed with the user first:** SPEC.md's route table lists a bare `POST /api/v1/webhooks (mgmt)`, ambiguous
between (a) making the existing alert webhook reliable plus a way to verify a URL before relying on it, or
(b) a whole new generic webhook-subscription system decoupled from alerts. Chose (a) -- Phase 21's own DoD
line says verbatim "alert webhooks deliver reliably," nothing about other event types, and nothing in the
roadmap needs a second event source today. Sends one synthetic signed payload through the same
`send_webhook_with_result` retry path and reports `{delivered, status_code}`, so a user finds out their
receiver is misconfigured before a real alert ever depends on it, not the first time one fires.

**Deliberately not done:** pagination/continuation past `export_max_rows` (not asked for by this phase's
DoD); a generic webhook-subscription model for non-alert event types (see the design fork above); mirroring
export activity into an audit log (exports are reads, not mutations -- `docs/SPEC.md #6` already scopes
audit logging to mutating actions only).

**Tests:** `test_export_api.py` (10) -- tenant isolation, a wrong-project read key rejected with 404 (same
convention `test_query_auth_accepts_jwt_or_read_key...` already established), NDJSON and CSV structure and
properties round-tripping, the `event_name` filter, an empty range streaming zero rows cleanly, and the row
cap actually truncating a 5-event seed down to 2. `test_query_engine.py` gained one new test proving
`format=csv` is available on all three insight kinds and its row count/columns match the JSON path exactly.
`test_alert_delivery.py` gained retry-specific tests: a connection error and a 5xx each retrying and
recovering, a 4xx failing on the first attempt with no retry at all, and exhausting every retry reporting the
last real status code (or `None` when every attempt raised rather than got a response). `test_webhooks_api.py`
(new) covers the test-send endpoint's RBAC (Admin allowed, Member forbidden, another org's member 404) and
both its success and failure result shapes, again against a mocked transport, never a real network.

### 6.20 Security hardening, retention & PII mechanism (Phase 22)

Three design forks, each confirmed with the user first (all three "recommended" options chosen).

**Retention** (`pulse/retention/`, new). `Project.retention_days: int | None` (migration `0013`) overrides
`Organization.retention_days` (which already existed, unused, since Phase 4) when set; null falls back to
the org default. **Design fork:** native ClickHouse TTL is static per table, so a genuinely per-project
window needs either rebuilding a `multiIf(project_id IN (...), ...)` TTL expression on every settings
change, or a scheduled deletion job. Chose the latter -- a new `retention-worker` process mirroring
`alert-worker`/`billing-worker`'s exact shape (sleep loop, unscoped-`Organization`-then-per-org-scoped
sweep), issuing a real `ALTER TABLE events DELETE WHERE ... AND timestamp < cutoff` per project
(`mutations_sync: 1`, so the sweep's own log line reflects real completed state, not a queued mutation). A
project's window takes effect on the *next* sweep after the setting changes; nothing is baked in at ingest
time. `PATCH .../projects/{id}` gained `retention_days`, with real three-state PATCH semantics (field
omitted -> unchanged; explicit `null` -> clears the override back to the org default; a value -> sets it) via
a `projects_service.UNSET` sentinel, since plain `None` can't distinguish "not sent" from "explicitly
cleared" for a field whose real value is itself `int | None`.

**PII enforcement** (`pii_rules` table, migration `0014`; `pulse/worker/processing.py::apply_pii_rules`).
**Design fork:** Phase 9 already added `PropertySchema.is_pii`, settable via an existing PATCH route, but
nothing read it -- and it's inherently reactive (only protects a property after an event carrying it has
already been ingested and registered once unprotected) and scoped per event name (the same property key on
a different event needs marking again). Chose a new, proactive, project-level list instead: `pii_rules`
(`org_id, project_id, property_key, action: hash|drop`), checked at ingest independent of schema-registry
state, so a rule protects "email" from the very first event that carries it, regardless of which event
name. `is_pii` stays exactly as it was -- descriptive/informational only, not repurposed. `drop` removes the
key from both the ClickHouse-bound `properties` Map and the schema registry's `raw_properties` input (so a
dropped property never surfaces in the query UI's autocomplete either); `hash` replaces both with the same
keyed HMAC-SHA256 (`pii_hash_secret`) -- deterministic (same input -> same hash, so unique-user-style
grouping still works on a hashed property) but not reversible or rainbow-table-able without the secret.
Wired into `process_batch` via an *injected* `pii_rules_fetcher` callable defaulting to `None` ("no rules for
anything in this batch," touching Postgres not at all) specifically so the ingest worker's existing, entirely
Postgres-free test suite needed zero changes -- only `pulse/worker/main.py` wires the real fetcher
(`pii_rules_service.get_rules_for_project`), cached per (org_id, project_id) within one batch to avoid
N+1 Postgres reads when many events share a project.

**GDPR subject deletion** (`pulse/services/deletion.py::delete_subject`, `POST
.../subjects/delete`, Owner-only -- the single most destructive action in the app). A "subject" is an
arbitrary end-user identifier the customer's own app assigns via the ingestion SDK
(`events.user_id`/`anonymous_id`), never a Pulse console `User` -- an entirely separate identity. A real
`ALTER TABLE events DELETE WHERE ... AND (user_id = ... OR anonymous_id = ...)` mutation, `mutations_sync: 1`
again so "delete my data" is actually done by the time the call returns, not merely queued. **Design fork:**
scoped to ClickHouse `events` only, confirmed with the user first. `event_hourly`'s aggregated
`uniqCombined64` sketches can't have one subject's contribution surgically removed without a full
`python -m pulse.rollups rebuild` (Phase 17) -- a **global**, ingestion-must-be-stopped operation, genuinely
unsafe to auto-trigger from a live API call (this was corrected mid-design after actually reading
`maintenance.rebuild()`'s implementation, which has no project scope and explicitly requires the ingestion
workers stopped first -- the original plan assumed a scoped rebuild existed; it doesn't). The raw batch
archive in object storage (Phase 8) is batch-shaped, not per-subject-editable without rewriting archive
files. Both are documented, deliberate gaps in `docs/THREAT_MODEL.md`, not silently ignored. A best-effort
audit-log write (`subject.deleted`) follows the ClickHouse mutation -- not atomic with it (two different
databases), but "every mutating action writes an audit log" still applies to the app's single most
destructive one.

> **Superseded by Phase 24 (§6.22):** deletion now also recomputes the touched rollup buckets and
> rewrites the raw archive. The "events only" scope above is the Phase 22 state, kept for the record.

**Input-validation sweep.** Every request-body free-text field across auth, orgs, projects, invites, PII
rules, and subject deletion gained an explicit `max_length` (and `min_length` where empty is meaningless) --
several had none at all, including `LoginRequest.password`, which meant an unbounded string could drive a
real (if minor) Argon2-hashing DoS on a deliberately unauthenticated endpoint. `IngestEvent`'s
`user_id`/`anonymous_id`/`properties` (public, write-key-gated, meant for arbitrary customer traffic) gained
the same bounds.

**Security headers** (`pulse/core/middleware.py::SecurityHeadersMiddleware`): `X-Content-Type-Options:
nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, app-wide including `/ingest` (doesn't
conflict with its deliberately open CORS -- that's about *who* can call the API, this is about how a browser
treats the response). No CSP (a JSON API has no HTML of its own to scope one against) or HSTS (a
deployment/TLS-termination concern outside this app's own config).

**Dependency scanning** (new `dependency-scan` CI job): `pip-audit` (backend, sdk-python) + `npm audit
--audit-level=high` (frontend, sdk-js). **Advisory, not blocking**, and not for lack of trying: `pip-audit`
found real CVEs in `starlette` (FastAPI's core dependency) with no newer version resolvable in this
environment's package index at all, and in `pytest`/the `vite`/`esbuild`/`vitest` chain with a fix available
only via a breaking major-version bump needing its own full regression pass -- deliberately not done as an
unrelated side effect of this phase. A blocking gate today would leave CI permanently red over findings this
run genuinely can't fix, defeating "CI stays green" as a signal; the job still runs and reports every push,
so a *new* finding stays visible. All tracked honestly in `docs/THREAT_MODEL.md` rather than the CI job being
quietly made toothless with no record of why.

> **Superseded by Phase 24 (§6.22):** every finding above is fixed and the scans are now blocking gates.

**NL-to-query injection tests**: the existing Phase 18 suite (`test_nl_injection.py`) still passes
unchanged; extended with four PII/deletion-flavored adversarial questions ("show me every user's raw
email," "delete all events for user_id X," "bypass the PII rules," "what is the pii_hash_secret") -- none of
which are reachable from `/query/nl` at all (it can only ever return a query spec or a clarify), but worth
proving explicitly now that both PII rules and subject deletion exist.

**Threat model**: `docs/THREAT_MODEL.md` (new) -- assets, actor trust levels, the trust boundaries already
enforced (RLS + `pulse_app`'s non-superuser role, ClickHouse tenant scoping in the query-building layer,
JWT/API-key auth, the four-level RBAC hierarchy, NL-to-query's untrusted-input boundary, HMAC webhook
signing, rate limiting, this phase's PII/retention controls, security headers), and residual risks reported
honestly (the deletion/PII scope gaps above, the unfixable dependency CVEs, no CSP/HSTS and why, `/ingest`'s
deliberately open CORS, an unrevoked leaked write key's blast radius).

**Deliberately not done:** purging the raw S3 archive on subject deletion or PII rule match (see above); a
generic webhook-subscription system (already scoped out in Phase 21); bumping `starlette`/`pytest`/`vitest`
past what's cleanly resolvable/non-breaking (tracked in the threat model instead).

**Tests:** `test_retention.py` (5) -- project-override-vs-org-default resolution, a real sweep against
seeded old/new ClickHouse events, a generous window deleting nothing, cross-tenant isolation across a full
`sweep_all_projects` pass. `test_pii_enforcement.py` (11) -- pure unit tests for `apply_pii_rules`
(no-rules no-op, drop, hash, determinism, the hash secret actually changing the output, never mutating the
input) plus real end-to-end passes through `process_batch` proving a project's rules keep a marked property
out of ClickHouse while an unmarked property and a different project's events pass through untouched, and
that no fetcher at all remains a true no-op. `test_deletion.py` (9) -- service-layer deletion by
`user_id`/`anonymous_id`, cross-tenant isolation, the audit-log write, and API RBAC (Owner allowed, Admin
forbidden, a clean 422 with no identifier, an unknown project 404). `test_pii_rules_api.py` (6) and
`test_project_retention_api.py` (5) -- CRUD, RBAC, duplicate-key 409, cross-project isolation, and the
three-state PATCH semantics respectively. `test_nl_injection.py` unchanged in structure, +4 adversarial
cases. ruff / ruff format / mypy (`pulse`, strict) all clean.

### 6.21 Observability, CI/CD & deployment mechanism (Phase 23)

Three design forks, each confirmed with the user first (all "recommended" options): deployment is **built
and validated, not applied** (no Render/ClickHouse Cloud/AWS account, no spend); the observability stack is
**self-hosted in docker-compose**; Sentry is an **env-gated SDK**, no account needed. Full detail in
`docs/OBSERVABILITY.md` and `docs/DEPLOYMENT.md`.

**One trace from SDK to ClickHouse** (`pulse/observability/tracing.py`). HTTP hops propagate context on
their own; the Redis stream is a process boundary that doesn't, so `/ingest` writes the W3C `traceparent`
into every stream entry (`ingest/service.py::buffer_batch`) and the worker reads it back as the parent of its
spans. A worker batch mixes events from many requests, so after the batch lands each traced event gets its
**own** `worker.process_event` span and child `clickhouse.insert` span, parented into **its own** trace and
carrying the shared insert's real timing -- traces are never merged. Both SDKs mint a fresh `traceparent` per
flush (zero-dependency by design, so the SDK's own span is never exported and shows as the missing parent).
The module keeps its own `TracerProvider` rather than OpenTelemetry's set-once global, so tests can attach an
in-memory exporter without depending on import order. Span emission can never raise into ingestion.
**Verified live**: one event POSTed to the containerized API with a client `traceparent`, then fetched from
Tempo by trace id -- 8 spans, one trace id, two services (`pulse-api` server span whose parent is the
SDK-supplied span id, `ingest.buffer`, then across the stream `worker.process_event` -> `clickhouse.insert`).

**Metrics + Grafana** (`pulse/observability/metrics.py`, `infra/observability/`). Prometheus instruments for
exactly the signals SPEC names: ingest accepted, worker events by outcome (`poisoned` *is* the DLQ rate),
batch size/duration, end-to-end landing delay, stream length / consumer lag / pending / DLQ depth, and API
latency by route **template** (never the raw path -- cardinality). p50/p95/p99 come from Prometheus
`histogram_quantile`, not the app. Served on a dedicated `METRICS_PORT` per process, never the public API
port. `docker-compose.observability.yml` (a layered override, so the base stack is unchanged and the
settings are genuinely unset without it) adds an OTel Collector, Tempo, Prometheus and Grafana with the
datasources and the **Pulse - ingestion & query health** dashboard provisioned as code. Consumer lag is
**derived** (`stream length - pending`) rather than read from `XINFO ... lag`, which goes null once entries
are deleted from a stream -- reading null as 0 would report a healthy queue exactly when it isn't.

**A real bug the dashboard caught on first use.** The stream-length panel read 5,000+ and only rose, with 0
pending and 0 lag: `XACK` alone never removes an entry, so the ingest stream grew without bound (a Phase 7/8
issue no test could see). It matters because the deployed Redis must be `noeviction` -- the stream is the only
copy of an un-landed event, so an evicting policy silently deletes data -- and a full `noeviction` Redis fails
`/ingest` permanently. `worker/consumer.py::ack` now acks **and** deletes in one MULTI/EXEC (safe: an acked
entry is by construction already in ClickHouse or the DLQ, and the archive), with a regression test that
also proves an un-acked entry is *kept*. Verified live: 180 events accepted, 180 landed, stream length 0.

**Sentry** (`pulse/observability/sentry.py`). Initialized only when `SENTRY_DSN` is set. Privacy is configured,
not defaulted: `max_request_body_size="never"`, `send_default_pii=False`, and -- found by checking the SDK's
actual defaults rather than assuming -- `include_local_variables=False`, because the SDK captures every stack
frame's locals by default and in this codebase a frame is where an event batch or password lives. Pinned by a
test using a capturing transport (no account).

**Migrations on deploy** (`pulse/migrate.py`). `python -m pulse.migrate` migrates Postgres (alembic) then
ClickHouse, idempotently, failing fast, as one shell-free command (Render exec's docker commands without a
shell). It is the API's `preDeployCommand`, so a bad migration fails the deploy and the previous version keeps
serving.

**Deploy configuration as code.** `infra/render/{staging,prod}.render.yaml` (compute only; validated against
Render's published JSON schema, with a negative control proving the validator can fail) -- staging
`autoDeployTrigger: checksPass`, prod `"off"` (quoted: an unquoted `off` is boolean `false` in YAML 1.1, which a
test caught). `infra/terraform/` owns the data services and every secret (Render Postgres and Key Value,
ClickHouse Cloud, an S3 bucket with a least-privilege IAM user) and writes the connection strings into a Render
env group the Blueprints pull with `fromGroup` -- nothing committed or pasted; validated against the real
Render, ClickHouse and AWS provider schemas. `.github/workflows/deploy-prod.yml` is manual-only: it refuses a
commit CI hasn't passed, waits on a required-reviewer approval of the `production` GitHub Environment (the
"one gated click"), then `infra/scripts/render_release.py` releases the **API first** (waiting until it is
live, migrations applied) and only then the workers and frontend, aborting on any failure or timeout.

**Full CI.** New `infra-validate` job (Terraform fmt/validate, Blueprint schema, both compose files), an
advisory Trivy scan of the built image (advisory for the same reason as the dependency scan), and a pytest
failure reporter that emits each failure as a `::error::` annotation -- GitHub step logs need a signed-in
browser, annotations are readable without one.

**Also found and fixed:** the backend Dockerfile copied source before `pip install`, so every code change
re-installed every dependency (12+ minutes locally, the same cost per CI run); dependencies now have their own
cached layer -- measured: cold build 1m25s, rebuild after a source change **9 seconds**.

**Deliberately not done / not proven:** no live deploy against real accounts (config validated, not proven --
expect first-deploy surprises); Render workers aren't scraped by Prometheus (the dashboards are proven locally,
not in deployed environments); no remote Terraform state backend configured; image signing/SBOM; rollback
automation; Prometheus multiprocess mode for a multi-worker API container.

**Tests:** `test_observability.py` (12) -- the trace-continuity test (one trace id and the exact parent chain
request -> `ingest.buffer` -> `worker.process_event` -> `clickhouse.insert` across the real Redis stream and
ClickHouse), per-request trace separation within a mixed batch, untraced entries producing no spans,
`traceparent` forwarding from a real `/ingest` request into the stream, worker outcome counters, landing
delay/batch size, stream gauges, route-template labelling, the metrics server serving Prometheus text
idempotently, and Sentry's no-op-without-DSN and privacy posture. `test_migrate.py` (4) -- ordering, fail-fast,
and a real end-to-end run on the live databases plus an idempotent second run. `test_deploy_config.py` (24) --
Blueprint parity, staging-auto/prod-gated policy, no plaintext secrets, real modules in every worker command,
Terraform env-var names against real `Settings` fields, `noeviction`, the non-owner DB role, every dashboard
metric actually exported, Prometheus targets matching the metrics-serving services, and the release script's
ordering/failure/timeout behaviour with a fake Render client. Plus one stream-growth regression test in
`test_worker.py`, one traceparent test in each SDK (JS 19, Python 8 pass), ruff / ruff format / mypy clean.

### 6.22 Residual-risk hardening mechanism (Phase 24)

Not a numbered phase in the original roadmap: after Phase 23 the user asked for a Phase 24 and, with nothing
defined for it, chose "close the documented residual risks" (proposed and approved before any work). Four
items, each taken from a gap the earlier phases had written down rather than fixed.

**1. GDPR subject deletion now reaches every store** (`pulse/services/deletion.py`, `pulse/archive.py`,
`pulse/rollups/maintenance.py::repair_buckets`). Phase 22 deleted from `events` only and documented two gaps.

- *The rollup.* `event_hourly` holds a per-hour unique-users sketch, which cannot have one user subtracted;
  Phase 22 believed the only remedy was the global, ingestion-stopped `rebuild`. It isn't: the (event name x
  hour) buckets a subject touched can be **recomputed from the rows that remain**. `delete_subject` reads
  which buckets the subject is in *before* deleting (afterwards there is nothing to ask), deletes from
  `events`, then `repair_buckets` deletes and reinserts only those buckets, scoped to one project. Ingestion
  keeps running, so an event can land in a bucket between the delete and the reinsert and be counted twice
  (the materialized view counts it, and so does the recompute). Event counts are exact, so the existing
  `verify` comparison, scoped to the same buckets, detects it and the (idempotent) repair runs again, up to
  four attempts; if it still doesn't verify, `RollupRepairFailed` is raised rather than reporting success.
- *The archive.* Objects were one-per-batch (`raw/YYYY/MM/DD/<batch>.json`), mixing every tenant, so finding a
  subject meant reading everything. The worker now writes one object per (org, project) per batch under
  `raw/<org>/<project>/YYYY/MM/DD/<batch>.json`, with keys built only from parsed UUIDs so an entry cannot
  choose its path; erasure reads one project's prefix, and an object left empty is deleted, otherwise
  rewritten without the subject. Old-layout objects are still scanned and filtered per entry, so another
  tenant's events in a legacy mixed object are never lost. An unreadable object is counted in the response
  (`unreadable_objects`), never silently skipped.
- *The API* now returns 200 with a report (`rollup_buckets_recomputed`, `rollup_verified`, archive counts)
  instead of an empty 204 -- a deletion should say what it did. Nothing else called it.
- *Still not covered* (docs/THREAT_MODEL.md): an event accepted but not yet landed (in the Redis stream) lands
  after the deletion; archive entries whose tenant fields don't parse (`raw/_unattributed/`); backups. PII
  *rules* still don't rewrite the archive (deletion does; per-property rules don't).

**2. Dependencies and scans.** The starlette "no fix resolvable" finding was the app's own `fastapi<0.116`
pin, not the ecosystem: relaxing it resolves FastAPI 0.141 with starlette 1.7 (past every listed fix).
pytest 8 -> 9 required `pytest-asyncio` 0.24 -> 1.x, the change most likely to disturb this suite's
event-loop scoping; the full suite passed unchanged (409 -> 409). sdk-js moved to vitest 4, the version the
frontend already runs cleanly on Node 20 (5 findings -> 0; build, type-check and 19 tests pass). Deprecated
starlette status constants (`HTTP_422_UNPROCESSABLE_ENTITY`, `HTTP_413_REQUEST_ENTITY_TOO_LARGE`) were
replaced. The scans are now **blocking**: `pip-audit`, `npm audit --audit-level=high`, and Trivy on the
backend image (fixable HIGH/CRITICAL). Two things this uncovered: the Python audits had been running on a
runner where the project was never installed, so they audited almost nothing (each now installs into its own
virtualenv first; verified with fresh unlocked installs the way CI does them); and Trivy's first honest run
found 16 HIGH/CRITICAL findings in the Debian base image, all already fixed upstream, so the Dockerfile now
runs `apt-get upgrade` (rescan: 0). The cost of blocking is documented: a newly published advisory can turn CI
red with no change of ours.

**3. Deployed observability** (`infra/observability/*.render.*`, `pulse/observability/metrics_route.py`,
Blueprints). Render workers can't receive private-network traffic, so they became private services (`pserv`)
serving `/metrics` on 9100; the API, a web service, can only be reached on its primary port, so it gained
`GET /metrics` -- **absent (plain 404) unless `METRICS_TOKEN` is set**, otherwise bearer-token only,
constant-time compared. Render hostnames have random suffixes, so Prometheus gets each as an env var via
`fromService` and scrapes its `<host>-discovery` name (Render's per-instance DNS, following Render's own
Prometheus guide), which also makes a 2-instance API scrape correctly instead of alternating between
instances through a load balancer. Prometheus (persistent disk) and Grafana (login required) are in both
Blueprints; Prometheus pulls a new one-secret env group rather than the managed group's database
credentials. Terraform generates the token. There is no Tempo in the deployment (traces need an external OTLP
endpoint).

**4. Terraform remote state and release rollback.** Remote state is an opt-in `*_override.tf` (an example
file, git-ignored copy) so the default stays local and no bucket name is committed; CI validates the example.
`render_release.py` now records each service's live deploy first and, on any failure, waits for in-flight
deploys to settle then rolls back the ones that had gone live (workers first, API last), reporting a failed
rollback, a first release with nothing to roll back to, and that **migrations are forward-only and not undone**.

**Verified beyond the tests:** the Render Prometheus and Grafana images were built and run -- the API and an
ingest worker given Render-style `-discovery` DNS aliases were both discovered and scraped `up` (the API with
its bearer token on its primary port), the entrypoint refused missing, non-alphanumeric and slash-containing
values, data landed on the mounted volume, and Grafana required login, provisioned its datasource from the
environment and the dashboard, and queried Prometheus. Terraform (fmt, validate) and both Blueprints (against
Render's published schema, with a negative control) validate; promtool accepts the rendered config and
rejects a typo'd one. `terraform init` with the remote-state recipe selected the S3 backend and wrote a
workspace's state to a bucket (MinIO); `encrypt = true` was honored (MinIO refused it without a KMS, so the
write proof used `encrypt=false` on MinIO only). A negative control on the repair: with the scoped
verification disabled, exactly the two race tests that depend on it fail.

**Not proven:** anything on real Render (whether `fromService ... property: host` yields the name the
`-discovery` hostname is built from, that a private service's `PORT` picks the routed port, a root-writable
disk), rollback against the real Render API (tested against a fake client and Render's documented endpoints),
DynamoDB state locking, real AWS. The rollup repair's race handling is proven with a deterministic simulated
race, not under real concurrent load.

**Tests:** `test_archive.py` (8) -- the per-tenant split, hostile/missing tenant fields can't choose a key,
erasure keeps everyone else's entries, an object holding only the subject is deleted, anonymous-id matching,
another project with the same subject id untouched, a legacy tenant-mixed object cleaned without losing other
tenants, an unreadable object reported. `test_rollup_repair.py` (5) -- the rollup no longer counts a deleted
subject (events and unique users, and a bucket only they were in disappears), another project untouched, a
simulated mid-repair ingest is detected and the repair converges (asserting the second pass happened), a
repair that can't converge raises, a no-op. `test_deletion.py` (+1, and the API test updated) -- one deletion
through the real worker removes a subject from events, rollup and archive together. `test_observability.py`
(+3) -- `/metrics` is a 404 without a token, 401 for missing/wrong tokens with nothing leaked, and serves
Prometheus text with the right one. `test_deploy_config.py` (24 -> 53) -- worker/port/scrape-template
consistency, every `fromService` reference real, Prometheus's env group holding only the token, Terraform
sharing one token, Grafana login and datasource uid, LF pinned for shell scripts (which caught a real CRLF
regression), CI gates staying blocking, Dockerfile COPY sources existing, and nine rollback cases (snapshot
before triggering, rollback order, settle-before-rollback, failed rollback, first release, no-hang timeout, and
the HTTP client's parsing).

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

**Phase 15 — Insight builder + charts.** ☑ builder with schema-driven autocomplete; ☑ line/bar/table +
funnel + retention viz; ☑ save insights. Tests: builder emits valid specs; each result type renders; saved
insights reload. See §6.12.

**Phase 16 — Dashboards.** ☑ dashboard CRUD; ☑ arrange saved insights; ☑ per-dashboard range + refresh; ☑
RBAC sharing. Tests: layout persistence; permission-scoped sharing; refresh correctness. See §6.13.

**Phase 17 — Rollups + performance.** ☑ MVs per §5.2; ☑ router prefers rollups; ☑ per-tenant query rate
limits; ☑ load tests. Tests: **rollup vs raw parity** (zero drift, 55.8M events); documented before/after
benchmarks -- ingestion and pure-count dashboard queries meet their §1.4 targets, two targets are
documented as not met under this environment's load with the reason identified, not hidden (see §6.14 and
`docs/PERFORMANCE.md`).

**Phase 18 — NL-to-query.** ☑ NL → **insight spec** (never raw SQL) → existing safe builder; ☑ interpreted
spec shown before running; ☑ read-only/scope/cap guardrails; ☑ eval set gates regressions. Tests:
injection attempts can't cross tenant or write; ambiguous → clarify; NL→spec eval thresholds. See §6.15.

**Phase 19 — Anomaly + alerts.** ☑ threshold + statistical anomaly (moving avg + z-score / seasonal
baseline); ☑ alert rules; ☑ email + in-app + outbound webhook. Tests: fires on breach not noise;
seasonality handled; delivery honored. See §6.16.

**Phase 20 — Billing/metering.** ☑ payment-provider test mode (a self-hosted `MockPaymentProvider` by
default -- design fork confirmed with the user first, mid-phase: Stripe test mode needed a real account
this session couldn't set up, and a paid/external dependency wasn't wanted for this piece either, so the
mock provider is the real default and real Stripe an optional swap-in behind the same interface, see §6.18);
☑ meter events/MTU from **real ingestion counts**; ☑ quotas + soft/hard limits; ☑ invoices (synthesized by
the mock provider once an org is actually on Pro; a real provider reads its own invoice list live instead).
Tests: ☑ metering accuracy vs ingested volume; ☑ quota enforcement; ☑ webhook handling (signature
verification + event processing, against a locally HMAC-signed payload -- needs no real account of any
kind). See §6.18.

**Phase 21 — Public API/exports/webhooks.** ☑ scoped read API; ☑ streamed CSV/JSON export (results + raw
events); ☑ signed, retried outbound webhooks. Tests: ☑ key scoping; ☑ export completeness; ☑ webhook
signing/retry. See §6.19.

**Phase 22 — Security/retention/PII.** ☑ per-project TTL retention (scheduled-sweep mechanism, not native
ClickHouse TTL -- see §6.20); ☑ PII allow/deny + hash/drop at ingest; ☑ user-deletion across partitions
(ClickHouse only -- rollup/archive gaps documented, see §6.20); ☑ input-validation sweep, headers, dep scan
(advisory, see §6.20); ☑ threat model (`docs/THREAT_MODEL.md`). Tests: ☑ TTL expires data; ☑ deletion removes
a subject's events; ☑ PII fields never reach ClickHouse; ☑ injection suite passes. See §6.20.

**Phase 23 — Observability/CI-CD/deploy.** ☑ OTel trace SDK→API→buffer→worker→ClickHouse; ☑ Grafana:
ingestion lag, batch size, events/sec, DLQ rate, query p50/95/99; ☑ Sentry (env-gated); ☑ full CI;
☑ migrations-on-deploy (both stores); ◐ staging+prod on Render (Blueprints + workflows built and validated,
**not applied** -- see §6.21); ◐ IaC (Terraform validated, not applied). DoD: ◐ merge→staging auto (configured
via `autoDeployTrigger: checksPass`, not proven by a live deploy); ☑ prod one gated click (manual workflow +
required-reviewer environment, release logic tested); ☑ one trace follows an event end to end (verified live
in Tempo). **Phase 23 is not closed out** until a real deploy has exercised the staging/prod half.

**Phase 24 — Residual-risk hardening (post-roadmap; agreed after Phase 23).** ☑ subject deletion removes the
subject from `events`, the hourly rollup and the raw archive (verified through the real worker); ☑ the
starlette/pytest/vitest findings are fixed and dependency + image scans are blocking; ☑ deployed
Prometheus/Grafana + a token-protected API `/metrics` (verified against the real images with simulated Render
DNS); ☑ opt-in Terraform remote state; ☑ automatic rollback in the release script. ◐ Everything deployed-side
is verified locally, not on Render (§6.22). Phase 23's deploy half is still open.

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
- 2026-09-21 — Phase 14 (bug fix, no new phase work) — Fixed session loss on full reload / deep link in
  `next dev`: `AuthProvider`'s recovery effect sent two `POST /auth/refresh` calls with the same single-use
  refresh token under React Strict Mode (first `200`, second `401`, then the failure path cleared
  `localStorage`). Recovery now goes through one module-level in-flight promise keyed by the stored token
  (`recoverSession` in `frontend/src/lib/auth-context.tsx`), shared by both effect invocations and cleared
  on settle; the effect keeps its single promise-chain shape, no lint suppression. No API or backend
  change. Updated §6.11; added `frontend/src/lib/auth-context.test.tsx` (4 tests, Strict Mode, verified
  failing before the fix).
- 2026-09-21 — Phase 15 — Added §6.12 (the insight builder + charts mechanism) and ticked the Phase 15
  DoD. Backend: an `insights` table (migration `0008`, RLS-protected on `org_id` like `event_schemas`) and
  list/create/get/patch/delete endpoints under `/orgs/{org_id}/projects/{project_id}/insights` (viewer
  reads, member+ writes, every mutation audit-logged); the spec is validated by the same Pydantic models
  the query endpoints use behind a `kind` discriminator, so a saved spec can never be one the engine can't
  compile, and is stored with the `"from"` alias so it can be POSTed straight back to `/query/{kind}`.
  Frontend: Recharts for line/bar/table trends; custom funnel step-bars and a retention cohort grid; a
  `Draft` → `buildSpec()` builder that mirrors the backend's validation, with native `<datalist>`
  autocomplete from the schema registry (hidden entries excluded, numeric-only for sum/average, free text
  still allowed); routes `.../insights`, `.../insights/new`, `.../insights/[insightId]` (a saved insight
  pre-fills the form and re-runs on open). One real backend bug caught by the new tests: the org scope is
  transaction-local, so `session.refresh()` *after* `commit()` ran in an unscoped transaction and RLS
  raised `invalid input syntax for type uuid: ""` (fixed by flushing/refreshing before commit). Two real
  frontend defects caught only by driving the app in the built-in browser against a real stack, both
  fixed with regression tests: float noise in summed decimals (`88.97999999999999`), and retention showing
  `0.0%` for periods that hadn't happened yet (now a dash; the retention result carries its `period`).
  A pre-existing Phase 14 session-loss-on-reload bug in `next dev` was found and fixed separately (entry
  above). Not done, by design: dashboards (Phase 16), a Playwright flow for the builder, role-aware hiding
  of Save/Delete for viewers, an unsaved-changes prompt. 14 new backend tests (133 total pass); 43 new
  frontend tests; ruff / ruff format / mypy (`pulse`, strict) / eslint / tsc / `npm run build` all clean.
- 2026-09-21 — CI fix (no phase work) — The `test` job's "Start MinIO" step had failed (`docker run` exit 125)
  on every master run since at least 2026-09-18, so the backend suite (including Phase 8/9's object-storage
  archiving tests and, since Phase 15, the insights tests) never actually ran in CI. Two causes: the
  `minio/minio` Docker Hub repository no longer exists (MinIO now publishes to quay.io), and the step
  published MinIO on host port 9000, which the ClickHouse service container already holds for its native
  port -- the clash the local compose file already avoids by using 9002. CI now runs
  `quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z` (pinned, not `latest`) on 9002, with `S3_ENDPOINT_URL`
  and the readiness probe updated to match; the same pinned image replaces `minio/minio:latest` in
  `infra/docker/docker-compose.yml`, which only worked on machines that still had a cached copy. Verified
  locally by starting that exact image with the CI `docker run` command and running `tests/test_worker.py`
  against it (8 pass; the same 8 error with MinIO stopped, so they do exercise it). The full CI run itself
  can only be confirmed on GitHub.
- 2026-09-21 — Phase 16 — Added §6.13 (the dashboards mechanism) and ticked the Phase 16 DoD. Backend: RLS-
  protected `dashboards` / `dashboard_items` tables (migration `0009`) and list/create/get/patch/delete
  endpoints; sharing is `private` | `org` for visibility plus a separate edit rule (never a viewer; the
  creator; an admin/owner on anything they can see), with invisible dashboards returning `404` and a
  server-computed `can_edit`; the whole layout is replaced atomically through `PATCH` (a deviation from the
  plan's separate `PUT /items`) and validated for bounds, overlap, duplicates, the 20-tile cap and
  same-project insights; and `?refresh=true` on the three query endpoints skips the cache read but
  re-fills it. Frontend: `.../dashboards` and `.../dashboards/[dashboardId]`, a relative/absolute range
  that overrides each insight's saved dates and resolves "today" in the project's timezone, a tile editor
  that packs an ordered list of sized tiles into a 12-column grid (so overlap is unrepresentable; no
  drag-and-drop), per-tile loading and errors, Refresh and optional auto-refresh. Verified live with an owner
  and a second, viewer-role account (read-only in the UI, `403`/`404` from the API). Not done, by design:
  drag-and-drop, per-user sharing, dashboard-wide filters, a Playwright flow, and rollups (the project
  guide lists them under Phase 16, this spec under Phase 17; the spec was followed). 16 new backend tests
  (149 total pass); 59 new frontend tests (119 total pass); ruff / ruff format / mypy (`pulse`, strict) /
  eslint / tsc / `npm run build` all clean.
- 2026-09-22 — Phase 17 — Added §6.14 (rollups + performance mechanism), corrected §5.2's original sketch
  to match what was actually built, and ticked the Phase 17 DoD. Backend: an hourly `event_hourly` rollup
  (ClickHouse migration `0002`) -- hourly not daily-UTC (trends bucket by project timezone), counts exact,
  unique users an approximate `uniqCombined64(15)` sketch gated by event volume (an exact sketch measured
  3-20x slower to merge than scanning raw); a router (`pulse/query/rollup.py`) that only uses the rollup for
  query shapes it can answer exactly or within tolerance, checking whole-hour timezone offsets per query
  range, not per zone name; a per-org query rate limit (429 + `Retry-After`, shared across trend/funnel/
  retention, cache hits free); every ClickHouse call now recognizes cap errors and returns a clean `422`
  instead of a `500`; `python -m pulse.rollups {verify,rebuild}` for drift checking and repair. Frontend:
  the trend result carries `approximate`, shown as a note on the chart so an estimate is never presented as
  exact. A committed Locust-based load-test harness (`backend/loadtests/`) generates events server-side
  inside ClickHouse and measures query and ingest scenarios against an isolated benchmark database.
  Ingestion (5,969 events/s accepted, zero loss) and pure-count dashboard queries meet their §1.4 targets
  outright; two targets are documented as not met under this environment's concurrent load, with root
  causes identified rather than papered over -- see `docs/PERFORMANCE.md`, which also documents a real
  finding that changed a shipped value: the unique-user sketch threshold was planned and first measured
  (single query) at 2,000,000, but concurrent load testing showed that value made wide weekly/monthly
  queries on a moderate-sized project slower with the sketch than without it, so it was raised to
  5,000,000 based on that evidence. A query-plan review (`EXPLAIN`) confirmed the existing Phase 6 sort key
  needs no change. 43 new backend tests (192 total pass); 1 new frontend test covering the trend result's
  new `approximate` note (120 total pass); ruff / ruff format / mypy (`pulse`, strict) / eslint / tsc all
  clean.
- 2026-09-22 — Phase 18 — Added §6.15 (NL-to-query mechanism) and ticked the Phase 18 DoD. Backend: new
  `pulse/ai/` module -- `LLMProvider` abstraction (`MockProvider`, deterministic/free/the default; real
  `AnthropicProvider` gated behind `ai_provider=anthropic` + `anthropic_api_key`, confirmed with the user
  first as the one real provider to wire up now, OpenAI/local deferred); `pulse/ai/translator.py` re-parses
  a provider's tool-call JSON through the exact same `DiscriminatedInsightSpec` union `pulse/api/insights.py`
  already used (moved into `pulse/query/spec.py` this phase so both share it instead of duplicating it) --
  a spec type has no tenant/SQL field at all, so there's nothing to smuggle through even if a provider
  tried. New `POST .../query/nl` route (added to the existing query router, so it inherits JWT-or-read-key
  auth for free) only translates and returns the interpreted spec; it never executes -- running it is a
  second, ordinary call to the unchanged `/trend`/`/funnel`/`/retention` endpoints, which is what makes
  "interpreted spec shown before running" true by construction rather than a flag. Grounding reads only the
  asking project's own registered event names (already org/project-scoped); an unregistered event name is a
  soft warning, never a rejection, matching Phase 9's "unknown events still ingest" precedent. A separate
  per-org rate limit (`ai_rate_limit_*`) budgets the translate call itself, independent of the ClickHouse
  query limiter. Eval set/regression gate: confirmed with the user first that CI runs `tests/nl_eval/`'s
  fixed NL→spec fixtures only against the free `MockProvider` (100% threshold, since mock matching is
  exact-or-broken); the identical fixtures also run against the real Anthropic provider at an 80% threshold,
  but only opt-in locally (`PULSE_TEST_LLM_LIVE=1` + a real key), never in ordinary CI. Dedicated
  `tests/test_nl_injection.py` runs ten adversarial questions (prompt-injection, SQL-injection-shaped
  strings, cross-tenant asks) through the real translation path and proves every one lands as either a
  same-project spec or a clarify -- never anything else. A real regex-ambiguity bug in the mock's own
  phrase-matcher (a multi-word event name could be mis-split from a trailing "last week"-style clause) was
  caught and fixed while building the eval fixtures, before it ever reached a test failure. Frontend: a new
  `NLQueryBox` (ask a question, see the interpreted spec plus any warnings, Run reuses the existing
  `runInsightQuery`/`InsightResult` exactly as the manual builder does) mounted alongside, not replacing,
  the existing insight builder on the "New insight" page; a small `warning` `Alert` variant was added
  (previously only error/success existed) since a grounding warning is a different severity than a
  translation failure. Verified live against a real API/Postgres/ClickHouse/Redis, not just tests: asked
  "How many times did checkout completed happen last week?" against a fresh project, got back the
  interpreted spec plus the expected "hasn't been recorded yet" warning, pressed Run and got a real
  (empty, correctly so) result from `/trend`, then asked an unrelated question and got a clarify message
  instead of a guess. 26 new backend tests, 1 skipped by design (192 → 217 passed, 1 skipped); 5 new
  frontend tests (120 → 125 total pass); ruff / ruff format / mypy (`pulse`, strict) / eslint / tsc all
  clean.
- 2026-09-22 — Phase 19 — Added §6.16 (anomaly + alerts mechanism) and ticked the Phase 19 DoD. Backend: new
  `pulse/alerts/` module -- `ThresholdRule` (any insight kind: trend's latest bucket, funnel's final-step
  conversion %, retention's latest-cohort period-1 %) and `AnomalyRule` (trend insights only, rejected on
  funnel/retention at creation with a 422 -- confirmed with the user first as the phase's scope boundary,
  since only trend already produces the bucketed time series an anomaly rule needs); pure, hand-tested
  trailing-z-score and same-weekday-seasonal-z-score functions (`pulse/alerts/anomaly.py`); an evaluator that
  reuses the unchanged `run_trend`/`run_funnel`/`run_retention` query engine (never a second query path,
  so tenant scoping/caps/cache/rate-limit are all inherited) against a freshly-shifted "ends today" range,
  and fires an `AlertEvent` only on the breach transition -- a persisting breach re-evaluates silently, a
  recovery clears the flag with no event of its own. A new `alert-worker` process (confirmed with the user
  first, over an externally-cron'd one-shot CLI) sleep-loops evaluating every enabled alert; since an
  unscoped Postgres session default-denies every RLS-protected table, it lists `Organization` (the one
  table that isn't RLS-scoped) unscoped and then loops org-scoped for everything else -- a new
  cross-tenant-sweep pattern this codebase hadn't needed before Phase 19. Delivery: a real, HMAC-signed
  (`X-Pulse-Signature`) outbound webhook; a `ConsoleEmailProvider`-only email side (confirmed with the user
  first -- the same "no provider chosen yet" deferral Phase 4's invite emails already made, no new mail-
  server infra this phase); in-app delivery is just the `AlertEvent` row itself, polled via
  `GET .../alerts/events` and dismissed via an ack endpoint, the same client-timer-polling pattern Phase
  16's dashboard auto-refresh already used. New `POST .../alerts/{id}/evaluate-now` for on-demand testing,
  running the identical evaluation path the worker uses on its own schedule. Frontend: `AlertList` (create +
  list, mirrors `DashboardList`'s inline-form pattern; picking "anomaly" filters the insight dropdown to
  trend-only insights so the form can't offer a combination the API would refuse) and `AlertDetail`
  (enable/disable, delete, Evaluate now, the events feed with acknowledge), linked from the project home
  page as a third sibling to Insights/Dashboards, not nested inside an insight's own page -- an alert is its
  own resource with its own lifecycle, the same relationship `DashboardItem` has to `Insight`. Verified live
  via the test suite's own real ClickHouse-backed integration tests (not mocked): a threshold alert fired
  once on a real breach and stayed quiet through repeated re-evaluation of the same persisting breach, then
  recovered once real data pushed it back over the line; funnel/retention alerts correctly read their
  real, kind-specific single value; an anomaly alert fired on a real 10x spike over a real ten-day baseline
  and stayed quiet on a normal day at the same baseline; `evaluate_all_enabled` evaluated two real orgs'
  alerts without leaking one into the other's result. 46 new backend tests (217 → 263 passed, 1 skipped);
  13 new frontend tests (125 → 138 total pass); ruff / ruff format / mypy (`pulse`, strict) / eslint / tsc
  all clean.
- 2026-09-22 — Phase 20 (started, not closed out) — Added §6.18 (billing & usage metering mechanism) and
  partially ticked the Phase 20 DoD -- confirmed with the user first that Stripe test mode would be built
  and fully tested against mocks this session ("build now, connect later," the same pattern the Microsoft
  365 integration used in the sibling Jarvis project), leaving the phase open per CLAUDE.md #1.4 until a
  real Stripe account is connected and Checkout/webhook/invoices are verified live. Backend: new
  `pulse/billing/` module -- a static plan/quota config (`pulse/billing/plans.py`, no database table: Stripe
  owns pricing, Pulse only needs the quota number, which needs no Stripe setup at all); every org gets a
  `plan=free` `Subscription` row in the same transaction that creates the org (alongside the owner
  `Membership`, the same atomicity reasoning); org-wide usage metering from the `event_hourly` rollup, never
  raw events (`pulse/billing/usage.py`) -- an exact event-count sum and an MTU merge of the rollup's
  per-(project, event_name, hour) `uniqCombined64` sketch states across everything for the org, unlike the
  query engine's own rollup queries, which stay scoped to one project and one spec. A new `billing-worker`
  process (mirroring `pulse/alerts/main.py`'s shape exactly) recomputes and upserts each org's current-period
  usage every 5 minutes; the ingest-path quota check (`pulse/billing/service.py`, called from
  `pulse/ingest/router.py` right after the existing per-key rate limiter) reads that cached value rather than
  querying ClickHouse per request, and returns a new `quota_warning` field on soft breach or a real
  `402 Payment Required` -- before the batch ever reaches the buffer -- on hard breach. Stripe SDK access
  goes through one thin wrapper (`pulse/billing/stripe_client.py`, mirroring `object_storage.py`'s
  sync-client-in-a-thread shape) gated entirely behind optional settings that default to unset, so every
  Stripe-backed endpoint degrades to a clean `503` instead of ever attempting a call with no key; a Stripe
  Customer is created lazily on first Checkout, not at org-creation time. `POST /api/v1/webhooks/stripe`
  verifies the signature as the boundary (not RBAC, the same token-is-the-boundary reasoning
  `RefreshToken`/`Invite`/`ApiKey` already use) and only upgrades an org to Pro on a genuinely
  active/trialing Stripe status, never silently downgrading on a `past_due` payment hiccup Stripe itself
  might still resolve. Frontend: `lib/billing-api.ts` + a `BillingPage` (current plan, a usage-vs-quota bar,
  Upgrade/Manage buttons that redirect to Stripe-hosted pages -- card details never enter Pulse's own UI at
  all, by construction -- and an invoice list), linked from the org's projects page for Admin+ members only.
  A real pre-existing test's exact-dict assertion on `/ingest`'s response broke from the new `quota_warning`
  field and was fixed to check fields independently rather than weakened. 35 new backend tests (263 → 298
  passed, 1 skipped) -- including `test_billing_webhooks.py` verifying real HMAC signature verification
  (valid, invalid, and a tampered-payload-with-a-valid-header case) against a payload the suite signs itself,
  needing no real Stripe account; 7 new frontend tests (138 → 145 total pass); ruff / ruff format / mypy
  (`pulse`, strict) / eslint / tsc all clean.
- 2026-09-22 — Phase 20 (pivot, now closed out) — The prior entry's "build now, connect later" plan hit a
  real wall: the user could not get a usable Stripe account set up in this session, and separately did not
  want a paid/external SaaS dependency for this piece at all (both confirmed explicitly, not assumed). Rather
  than keep the phase blocked on an account that might never materialize, pivoted to a **self-hosted mock
  payment gateway as the real default**, with Stripe demoted to an optional swap-in -- confirmed with the
  user first among several options, this one chosen specifically because it required no external account of
  any kind and kept the phase's architectural story (customer creation, hosted-checkout-style redirect,
  webhook-driven subscription sync, invoice listing) fully intact rather than stubbing it out. New
  `pulse/billing/providers.py`: a `PaymentProvider` ABC (mirroring the `pulse/ai/provider.py` pattern from
  Phase 18) with `MockPaymentProvider` as the unconditional default and `stripe_client.py`'s Stripe calls
  rewrapped as `StripePaymentProvider`, selected via `payment_provider: Literal["mock", "stripe"]` (default
  `"mock"`). The mock provider's "checkout" and "portal" URLs point at a new same-origin
  `/orgs/{orgId}/billing/mock-checkout` page (`MockCheckoutPage.tsx`) rather than a stripe.com domain; its
  "Confirm subscription" and "Cancel subscription" actions hit new `POST .../billing/mock/subscribe` and
  `.../mock/cancel` routes that build a Stripe-event-shaped dict and hand it to the **exact same**
  `handle_event()` the real webhook route uses -- so there remains only one implementation of "how a
  subscription change gets applied," regardless of whether a real Stripe webhook or the mock UI triggered it.
  Columns `subscriptions.stripe_customer_id`/`stripe_subscription_id` renamed to
  `payment_customer_id`/`payment_subscription_id` via a new migration (`0012_payment_provider_rename.py`,
  verified upgrade/downgrade/upgrade against the dev DB) rather than editing the already-written
  `0011_billing.py` in place, consistent with not rewriting migration history even pre-push. `test_billing_api.py`
  rewritten around the mock path as the primary case (checkout/portal never touch a network, `mock_subscribe`/
  `mock_cancel` drive real plan transitions through real route tests) plus a `stripe_provider_selected` fixture
  covering the real-Stripe path stays exercised (503-when-unconfigured, mock actions correctly disabled once
  Stripe is selected); new `test_billing_providers.py` (6 pure unit tests, no DB) covers the provider
  factory directly. 311 backend tests pass, 1 skipped (298 → 311); 150 frontend tests pass (145 → 150,
  including new `MockCheckoutPage.test.tsx`). **Verified live end-to-end in the browser**, including a real
  bug caught live and fixed -- see §6.18's "Real bug caught live" paragraph for the full account (a 30s
  React Query `staleTime` plus a query-key collision between `MockCheckoutPage` and `BillingPage` left the
  plan label showing stale "Free" for up to 30 seconds immediately after a real upgrade, fixed by invalidating
  the three billing query keys in both mutations' `onSuccess`). Phase 20 DoD (§7) now fully ticked; no
  outstanding Stripe-account dependency remains for this phase to be considered done.
- 2026-09-23 — Phase 21 (`10d0724`'s successor, not yet tagged) — Added §6.19 (public API, exports & webhook
  reliability mechanism) and ticked the Phase 21 DoD. **Design fork, confirmed with the user first:**
  SPEC.md's route table lists a bare `POST /api/v1/webhooks (mgmt)`, ambiguous between making the existing
  Phase 19 alert webhook reliable (retry/backoff, deliberately deferred from that phase) plus a way to test a
  URL before relying on it, versus a whole new generic webhook-subscription system for arbitrary event types.
  Chose the former -- Phase 21's own DoD line says verbatim "alert webhooks deliver reliably," and nothing in
  the roadmap needs a second event source today. New `pulse/api/export.py`: `GET .../export/events` streams
  raw events straight out of ClickHouse (`clickhouse_connect`'s `query_rows_stream`) as newline-delimited
  JSON or CSV, reusing `resolve_query_scope` (Phase 11's read-key-or-JWT auth) unchanged. Existing
  `POST .../query/{trend,funnel,retention}` routes gained a `format=csv` option, reusing the same
  `query_service` call as the JSON path so the two can't drift. `pulse/alerts/delivery.py`'s
  `send_webhook_with_result` now retries transient failures (timeout/connection error/5xx) with doubling
  backoff, never retrying a 4xx; new `POST .../webhooks/test` (Admin+) sends one synthetic signed payload
  through the same path. 21 new backend tests (311 → 332 passed, 1 skipped): `test_export_api.py` (10,
  new -- tenant isolation, key scoping, NDJSON/CSV structure, the row cap actually truncating), one new test
  in `test_query_engine.py` (CSV matches JSON on all three insight kinds), 5 new retry-specific tests in
  `test_alert_delivery.py`, `test_webhooks_api.py` (5, new -- RBAC + success/failure result shapes). No
  frontend work this phase -- unlike every UI-touching phase before it, Phase 21's own DoD is written purely
  in terms of external/API-level access ("an external client can query insights and export raw events via
  the API"), not a console feature; a "Test webhook" button on the alert form and an "Export" button on the
  project page are natural follow-ups if wanted, not required by this phase's DoD. ruff / mypy (`pulse`,
  strict) all clean.
- 2026-09-23 — Phase 22 — Added §6.20 (security hardening, retention & PII mechanism) and ticked the
  Phase 22 DoD. Three design forks, each confirmed with the user first before implementing (all three
  "recommended" options chosen): (1) per-project retention via a new scheduled `retention-worker`
  (mirrors alert-worker/billing-worker exactly) issuing real ClickHouse `ALTER TABLE ... DELETE` mutations,
  not native TTL (static per table, can't vary per project without rebuilding a table-wide expression on
  every settings change) -- new `Project.retention_days` (migration `0013`, nullable, falls back to
  `Organization.retention_days`); (2) PII enforcement via a new proactive, project-level `pii_rules` table
  (migration `0014`: property_key -> hash/drop), not Phase 9's existing but purely-descriptive
  `PropertySchema.is_pii` flag, since that flag is reactive (only protects a property after an event
  carrying it has already been ingested unprotected once) and scoped per event name; (3) GDPR subject
  deletion scoped to ClickHouse `events` only, with the rollup (`event_hourly`) and raw S3 archive gaps
  documented rather than silently ignored -- corrected mid-design after actually reading
  `pulse.rollups.rebuild()`'s implementation, which turned out to be global and ingestion-must-be-stopped,
  not safely triggerable from a live API call the way the original plan assumed. New:
  `pulse/retention/` (service + worker), `pulse/services/pii_rules.py` + `pulse/api/pii_rules.py` (Admin+
  CRUD), `pulse/worker/processing.py::apply_pii_rules` (keyed HMAC-SHA256 hashing, `pii_hash_secret`) wired
  via an *injected* fetcher defaulting to `None` so the ingest worker's existing, entirely Postgres-free
  test suite needed zero changes, `pulse/services/deletion.py` + `POST .../subjects/delete` (Owner-only --
  the single most destructive action in the app). Also this phase: an input-validation sweep across every
  free-text request field (several, including `LoginRequest.password`, had no bound at all -- a real, if
  minor, Argon2-hashing DoS vector on an unauthenticated endpoint); `SecurityHeadersMiddleware`
  (`X-Content-Type-Options`/`X-Frame-Options`/`Referrer-Policy`, app-wide); a new advisory (not blocking)
  `dependency-scan` CI job -- advisory because `pip-audit` found real CVEs in `starlette` with no newer
  version resolvable in this environment's index at all, and in `pytest`/the `vite`/`esbuild`/`vitest` chain
  with a fix available only via a breaking major-version bump needing its own regression pass, deliberately
  not done as a side effect of this phase; four new PII/deletion-flavored adversarial questions added to
  Phase 18's existing NL-injection suite (none reachable from `/query/nl` at all, but worth proving
  explicitly). New `docs/THREAT_MODEL.md` -- assets, actor trust levels, every trust boundary already
  enforced by name, and residual risks reported honestly (the deletion/PII scope gaps above, the unfixable
  dependency CVEs, why no CSP/HSTS, `/ingest`'s deliberately open CORS, an unrevoked leaked write key's blast
  radius). 36 new backend tests (332 → 368 passed, 1 skipped): `test_retention.py` (5),
  `test_pii_enforcement.py` (11), `test_deletion.py` (9), `test_pii_rules_api.py` (6),
  `test_project_retention_api.py` (5). No frontend work this phase, same reasoning as Phase 21 -- the DoD is
  written purely in backend/API terms. ruff / ruff format / mypy (`pulse`, strict) all clean.
- 2026-09-24 — Phase 23 — Added §6.21 (observability, CI/CD & deployment) and
  updated the Phase 23 DoD **honestly as partial**: the trace, Grafana, Sentry, full-CI, migrations-on-deploy
  and prod-gate items are ticked; staging/prod on Render, IaC and merge→staging-auto are ◐ (built and
  validated, **not applied** -- no Render/ClickHouse Cloud/AWS account was created, by design), so Phase 23 is
  *not closed out* until a real deploy has exercised that half. Three design forks confirmed with the user
  first: build+validate rather than apply; self-hosted observability (OTel Collector + Tempo + Prometheus +
  Grafana in a layered compose override); env-gated Sentry SDK. Highlights: W3C `traceparent` carried across
  the Redis stream hop (per-event spans in per-request traces; both SDKs mint the trace id) -- verified live in
  Tempo (8 spans, one trace id, `pulse-api` + `pulse-ingest-worker`); Prometheus metrics and a provisioned
  Grafana dashboard covering the SPEC's named signals; Sentry with request bodies, PII and stack-frame locals
  disabled (the SDK's `include_local_variables` default was on); `python -m pulse.migrate` as the API
  `preDeployCommand`; Render Blueprints (staging `checksPass`, prod `"off"`), Terraform for data services and
  secrets, a manual `deploy-prod.yml` behind a required-reviewer environment with an API-first release script;
  new `infra-validate` CI job, advisory Trivy scan, and pytest-failure `::error::` annotations. **Real bug
  found by the new dashboard:** `XACK` never deletes, so the ingest stream grew without bound (5,400+ entries
  seen) -- fixed with ack+XDEL in one MULTI/EXEC plus a regression test. Also fixed: backend Dockerfile
  reinstalled all dependencies on every source change (rebuild 12+ min → 9 s). Known open item: Phase 22's CI
  run is red with an undiagnosed cause (backend `test` job); the new annotation reporter exists to make it
  diagnosable once pushed. 41 new backend tests (368 → 409 passed, 1 skipped): `test_observability.py` (12),
  `test_migrate.py` (4), `test_deploy_config.py` (24), +1 in `test_worker.py`; JS SDK 19 and Python SDK 8
  tests pass. ruff / ruff format / mypy (`pulse`, strict) clean.
- 2026-09-24 — Phase 24 — Added §6.22 and a Phase 24 DoD line. Not in the original roadmap: after Phase 23 the
  user asked for a Phase 24; with nothing defined, "close the documented residual risks" was proposed and approved
  first. (1) **GDPR deletion now covers `events`, the hourly rollup and the raw archive** -- the rollup by
  recomputing only the touched buckets (Phase 22 had believed only the global, ingestion-stopped `rebuild`
  could do it), with a concurrent-ingest double count detected by an exact event-count check and retried, and a
  repair that can't converge raising instead of claiming success; the archive by a new per-org/project layout
  (legacy objects still cleaned); the API now returns a report. (2) **Dependencies:** the "unfixable" starlette
  finding was the app's own `fastapi<0.116` pin -- fastapi 0.141 / starlette 1.7, pytest 9, pytest-asyncio 1,
  vitest 4 in sdk-js (5 findings -> 0); the suite passed unchanged on the new stack. Dependency and image scans
  are now **blocking**; doing that exposed that the Python audits had never installed the project (fixed), and
  Trivy's first honest run found 16 fixable HIGH/CRITICAL findings in the Debian base (Dockerfile now runs
  `apt-get upgrade`; rescan 0). (3) **Deployed observability:** workers became private services, the API gained a
  token-protected `/metrics` (404 unless configured), Prometheus scrapes per-instance via Render's
  `-discovery` DNS, Grafana requires login; verified by running the real images against simulated Render DNS.
  (4) **Terraform remote state** (opt-in override; state write proven against MinIO) and **release rollback**
  (records live deploys first, settles in-flight ones, rolls back workers then API, reports failed rollbacks and
  first releases, and states that migrations are not undone). Also fixed a CRLF hazard for shell scripts
  (`.gitattributes`). 46 new backend tests (409 -> 455 passed, 1 skipped); SDKs unchanged (JS 19, Python 8);
  ruff / ruff format / mypy (`pulse`, strict) clean. **Not proven:** anything on real Render, real-API rollback,
  DynamoDB locking.
- 2026-09-25 — Phase 24 follow-up (CI, after the push) — The first CI run on Phase 24 failed and, with the new
  annotation reporting, was diagnosable from the API alone. Two causes. (a) **The blocking `pip-audit` gate
  caught a real finding on its first run:** `python -m venv` bundles pip 25.0.1, pip-audit audits the whole
  environment including pip, and pip had five advisories fixed in 26.x -- reproduced locally, cleared by
  upgrading pip first (both Python audit steps). Audit findings (pip and npm) are now emitted as `::error::`
  annotations too. (b) **The object store could no longer be pulled:** quay.io began answering 401 to anonymous
  pulls of `quay.io/minio/minio` (the Docker Hub repository was already gone in Phase 8's era), so the "Start
  MinIO" step failed four pull attempts in a row -- not a flake, and invisible locally because machines with a
  cached copy kept working. Replaced with a pinned `chrislusf/seaweedfs:4.47` (a maintained real S3
  implementation) in both compose (service `objectstore`, bound to loopback because it is unauthenticated) and
  CI, after running the archive/erasure/deletion/worker tests against it (27 passed) and then the full suite
  (458 passed, 1 skipped). Two things found on the way: `localhost` inside the container resolves to IPv6 so the healthcheck must
  use 127.0.0.1, and my Phase 22 session-wide bucket fixture failed even static tests when the store was down
  (now tolerant). New guard tests keep the image pinned and identical in compose and CI, keep the store
  loopback-bound, and keep `minio/minio` out of code. The `phase-24-complete` tag was created on the red commit
  and has not been moved.
