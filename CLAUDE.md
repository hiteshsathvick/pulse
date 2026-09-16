# CLAUDE.md — Operating Rules for the Pulse Repo

You are the primary engineering assistant for **Pulse**, a self-hostable real-time product-analytics /
event data platform. These rules govern how you work in this repository. Read this file and `SPEC.md` in
full at the start of every session before doing anything else. If a request conflicts with these rules, say
so and stop rather than silently working around them.

---

## 1. The prime directives

1. **Read `SPEC.md` and this file before acting.** They are the source of truth. Code that disagrees with
   `SPEC.md` is a bug in one of them — resolve it, don't ignore it.
2. **Work one phase at a time.** We build strictly phase-by-phase per `SPEC.md` §7 and
   `PULSE_PROJECT_GUIDE.md`. Never implement a later phase's work "while you're here." If you see a
   dependency ordering problem, raise it and wait.
3. **Plan before code, always.** For any phase or non-trivial change, produce a written plan first (files,
   data changes, API changes, tests, risks) and **wait for my approval** before writing application code.
4. **A phase is done only when its DoD checkboxes are all true and CI is green.** Tests specified for the
   phase must exist and pass. Do not declare a phase complete otherwise, and do not start the next one.
5. **When behavior changes, update `SPEC.md` in the same PR.** The spec is not allowed to go stale. Add a
   line to its change log.

---

## 2. Architecture invariants (violating any of these is a defect)

These mirror `SPEC.md` §3. Treat them as compile-time law:

1. **Two stores, two roles.** PostgreSQL is the control plane (users, orgs, projects, keys, registry,
   insights, dashboards, alerts, billing). ClickHouse is the event plane (behavioral events + rollups).
   Never put event-scale data in Postgres. Never put control-plane relational data in ClickHouse.
2. **Write path ≠ read path.** `/ingest` validates lightly, appends to the buffer, and returns `202`. It
   **never** inserts into ClickHouse synchronously. Only ingestion workers write events, in large batches.
3. **Tenant scope is injected, never trusted.** Every ClickHouse query is produced by the query engine,
   which always injects `org_id` and `project_id` predicates. Client input never supplies them. There is no
   RLS in ClickHouse — the query-building layer is the *only* isolation boundary, so it must be airtight and
   tested.
4. **`event_id` is the idempotency key.** Workers dedup on it. Re-processing a batch must never double-count.
5. **Bad data is routed to the DLQ, never dropped.** Malformed / schema-violating events go to the
   dead-letter sink with enough context to debug and replay.
6. **Modules own their tables.** A module never reaches into another module's tables; cross-module access
   goes through that module's service interface. This is what keeps future service extraction a deployment
   change, not a rewrite.
7. **No premature microservices, no premature Kafka, no premature dedicated vector/search infra.** Start
   with the modular monolith, Redis Streams, and ClickHouse as specified. Extraction happens only when a
   real, measured bottleneck justifies it.

---

## 3. Coding standards

- **Python:** 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, fully type-annotated. `ruff` clean, `mypy` clean.
  No untyped public functions. Prefer dependency-injection (FastAPI `Depends`) over globals.
- **Layering:** `api` (routers/schemas) → `services` (business logic) → `repositories`/`clients` (Postgres,
  ClickHouse, Redis). Routers stay thin; no SQL in routers; no HTTP concerns in services.
- **ClickHouse access** goes through a dedicated client/query-builder layer. **Never** hand-concatenate
  user input into SQL — parameterize, and inject tenant predicates in the builder, not at call sites.
- **TypeScript/Next.js:** App Router, TanStack Query for server state, Tailwind, components typed. No `any`
  without a written reason.
- **Errors:** use the standard error envelope (Phase 1). No bare `except:`; no swallowing exceptions. Log
  with correlation IDs; never log PII, secrets, or raw event properties that may contain PII.
- **Config & secrets:** env-only, via the settings module. Never hardcode or commit secrets. Keep
  `.env.example` current (note: two DB credential sets + per-project write/read keys).
- **Migrations:** Postgres via Alembic (reversible); ClickHouse via the versioned DDL runner. Every schema
  change ships a migration; never mutate a live schema by hand.

---

## 4. Testing gates (do not skip, do not weaken to make CI pass)

- Every phase ships the tests named in its `SPEC.md` §7 entry. If a test is hard, the answer is a better
  test, not no test.
- **Never delete or `xfail`/`skip` a test to get CI green.** If a test is genuinely wrong, fix the test and
  explain why in the PR; don't silence it.
- **Funnel and retention math must be verified against fixtures with hand-computed expected answers.**
  "It ran without error" is not verification for analytics math — the numbers must be provably correct.
- **Tenant-leakage tests are mandatory** for every query type and for the write-key/read-key boundary. A
  cross-tenant read or a write-key that can query is a release blocker.
- **Reliability tests** for ingestion: idempotent re-consume (no dup), poison→DLQ (no drop), worker crash
  mid-batch (no loss). These are the heart of the project — treat failures here as P0.
- Run the phase's tests locally and show the output before claiming done.

---

## 5. Security & data-handling rules

- Enforce tenant scope in every query; enforce the write-key (append-only) vs read-key (query-only) split.
- PII: respect per-project allow/deny lists; hash or drop denied properties **before** they reach
  ClickHouse. No PII in logs or error messages.
- Respect per-project retention (ClickHouse TTL) and keep the object-storage archive under the same policy.
- Implement the user-deletion path so it actually removes a subject's events across partitions.
- For **NL-to-query**: the LLM may only produce a validated **insight spec**, never raw SQL. The spec is
  compiled by the safe query builder so scoping and caps are inherited. Show the interpreted spec to the
  user before running it. Assume every NL input is hostile and test injection attempts that try to escape
  tenant scope or produce writes — they must fail.
- Query safety: every ClickHouse query carries `max_execution_time`, row/scan caps, and a `LIMIT`. Never
  let an ad-hoc or NL query run unbounded against the cluster.

---

## 6. Performance discipline

- Respect the `events` table's `ORDER BY (org_id, project_id, event_name, timestamp)`; write queries that
  use it as a range scan; flag any query pattern that can't.
- Prefer rollups/materialized views (Phase 17+) for dashboard queries; fall back to raw only when needed,
  and say so.
- Insert into ClickHouse in large batches only. Never per-event or per-request inserts.
- Cache query results in Redis with a short TTL keyed by (spec + org + project + range). Invalidate/skip
  cache correctly for live-ish views.
- When you change anything on the hot path, note the expected performance impact; back big claims with the
  load-test harness.

---

## 7. Git & workflow conventions

- Branch per phase: `feature/phase-N-<slug>` → PR into `main`. Tag `phase-N-complete` at each DoD.
- Small, focused commits with conventional-commit-style messages (`feat:`, `fix:`, `test:`, `docs:`,
  `refactor:`, `perf:`, `chore:`). One logical change per commit.
- Every mutating feature also writes an audit-log entry (per `SPEC.md`).
- End commit messages with the attribution lines this environment specifies, if any.
- Never force-push `main`. Never commit `.env`, secrets, or large data fixtures (generate fixtures in code).

---

## 8. How to run a phase (the loop)

For each phase:
1. Re-read `SPEC.md` §7 for this phase and its DoD checkboxes.
2. Produce a plan (files, DB/API changes, tests, risks). **Wait for approval.**
3. Implement **only** this phase. Keep to the architecture invariants (§2).
4. Write the specified tests; run them and the phase acceptance check locally; show output.
5. Fix until green. Do not weaken tests to pass.
6. Commit in focused chunks; update `SPEC.md` if behavior changed; tag `phase-N-complete`.
7. Stop and report. Do not start the next phase until told.

---

## 9. When to stop and ask

Stop and ask me rather than guessing when:
- A change would violate an architecture invariant (§2) or a security rule (§5).
- The spec is ambiguous or seems wrong for the situation.
- A dependency, library, or infra choice not in `SPEC.md` §2 seems necessary — propose it, don't just add it.
- You'd need to skip/delete/weaken a test, or a DoD checkbox can't be met as written.
- Something looks like it needs a later phase's work to function.

Do not rabbit-hole. If you're stuck after two or three real attempts, stop, explain what you tried and what
failed, and ask how to proceed.

---

*This file is the guardrail I point back to. Keep it accurate: if we agree to change a rule, update it here.*
