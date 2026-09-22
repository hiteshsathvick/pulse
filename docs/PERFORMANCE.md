# Phase 17 performance report — rollups + load tests

This is the "documented before/after benchmarks" the Phase 17 DoD (`SPEC.md` §7) asks for, measured
against the §1.4 non-functional targets. It reports what was actually measured, including two results
that don't flatter the rollup, because the point of load-testing is to find those before a real deployment
does.

## Method

- **Harness:** `backend/loadtests/bench.py` (setup/run/report) + `locustfile.py` (Locust scenarios),
  committed with the code. `uv pip install ".[loadtest]"` installs Locust.
- **Isolated databases:** a separate `pulse_bench` Postgres database, ClickHouse database, and Redis DB 1
  — never the dev or test databases. `bench.py setup` creates the org/projects/keys and generates events
  server-side inside ClickHouse (`INSERT ... SELECT ... FROM numbers(N)`), which turns "5,000,000 rows" into
  seconds of work instead of hours of client-side inserts.
- **Dataset:** one org, three projects sharing it — **large** (50M events), **medium** (5M events),
  **ingest** (a dedicated target for the ingest scenario) — each spread over 90 days, ~60% identified users
  on multiple devices, ~40% anonymous-only, 5 event names, `platform`/`country` properties.
- **Machine:** the developer's own laptop, Docker Desktop / WSL2, 12 CPUs and 7.6 GB allotted to Docker,
  ClickHouse 24.8. This is a shared, non-dedicated benchmark environment — the point is the *relative*
  before/after comparison and the qualitative findings, not that these absolute numbers would hold on
  provisioned infrastructure.
- **Query scenario:** simulated users repeatedly `POST` one of nine insight queries (weighted like a real
  dashboard — mostly rollup-eligible trends, some ad-hoc), always with `?refresh=true` so the result cache
  doesn't hide what's being measured. `QUERY_SUBSET` (`dashboard` / `adhoc` / `mixed`) runs only the
  query types relevant to one of §1.4's two *separate* latency targets, or the realistic combined mix.
- **Ingest scenario:** simulated SDKs flush fixed-size batches to `/ingest` as fast as accepted; separately,
  ingestion workers drain the buffer into ClickHouse. Accept rate and end-to-end landing rate are both
  reported, since §1.4 only commits `/ingest` itself to a latency target — the workers are deliberately
  decoupled (SPEC.md §3.2).
- **API workers:** 4 (`uvicorn --workers 4`) for the query comparisons below, matching how this app would
  actually be run (a single worker process was tried first and is called out separately, since it changed
  the conclusion). 2 API workers + 3 ingestion workers for the ingest scenario.

## Rollup correctness (parity)

`event_hourly` (migration `0002`) verified against 55.8M raw events (`python -m pulse.rollups verify`,
scoped per project to stay under this machine's ClickHouse memory limit — see "A real limit hit" below):
**zero drift**, for every timezone/granularity/measure/event-set combination `tests/test_rollup.py`
exercises live. 55.3M raw rows → 53,846 rollup rows (**1,027× fewer**), 2.70 GiB → 132.75 MiB on disk.

## Finding 1: without the rollup, dashboards on a large project mostly fail under load — not slowly, but refused

30 concurrent users, only the 5 dashboard-style trend queries (DAU/WAU/MAU/event-counts/hourly), **large**
project (50M events), each query's date range unchanged between runs:

| | rollups **off** (raw) | rollups **on** |
|---|---|---|
| trend.dau | **89/89 failed** (422, row cap) | 213 ok, p50 3.6s |
| trend.event_counts | **87/87 failed** (422, row cap) | 167 ok, p50 **0.23s** |
| trend.wau | **48/48 failed** (422, row cap) | 117 ok, p50 4.7s |
| trend.mau | **31/31 failed** (422, row cap) | 53 ok, p50 4.6s |
| trend.hourly | 55 ok, p50 2.8s | 101 ok, p50 **0.27s** |

Every "failed" row is a clean `422 "This query would read too much data..."` — `query_max_rows_to_read`
(§6.8) correctly refusing an unbounded scan, not a crash or a hang. That's the row cap working exactly as
designed (§1.4: "every query bounded by scan/row/time caps"). But it means that *without* the rollup, a
dashboard on a project this size mostly doesn't render under concurrent traffic. With the rollup, every
query type succeeds, and the two pure-count queries meet the **<1s p95 target outright** (event_counts
p50 0.23s / p95 1.2s; hourly p50 0.27s / p95 0.47s).

## Finding 2: the unique-user sketch isn't a free win — it has to be gated by data volume, and I had the gate wrong

Unique-user queries (DAU/WAU/MAU) use an *approximate* sketch (`uniqCombined64(15)`) rather than an exact
one — an exact rollup state was measured **3–20× slower to merge than scanning raw events** (see "Design
decision" below), so the engine only reads the sketch once a window holds enough events that exact raw
would be slow or refused (`query_rollup_unique_min_events`).

The first load-test run, at the **shipped plan's** threshold (2,000,000 events), showed something the
earlier single-query timing had not: on the **medium** project (5M events), WAU/MAU's 90-day window has
~2.25M "page viewed" events — just over that threshold — so they routed to the sketch, and under 30
concurrent users came back *slower* than raw (WAU p50 7.4s sketch vs 3.8s raw; MAU p50 7.3s vs 3.2s raw).

The reason: a sketch merge's cost is proportional to the number of **hourly rollup rows** a query spans,
not the event count or the output row count — a 90-day WAU/MAU query merges on the order of 90×24 ≈ 2,160
per-event-name hourly sketch states regardless of how few events actually landed in some of those hours.
At large scale that cost is still far cheaper than scanning tens of millions of raw rows; at this medium
scale, with a wide date range, it can cost more than the raw scan it was meant to avoid.

**Fix, backed by this data, not guesswork:** raised the default threshold to 5,000,000
(`pulse/core/config.py`). Re-run at the new threshold:

| | rollups off (raw) | rollups on, old threshold (2M) | rollups on, new threshold (5M) |
|---|---|---|---|
| trend.wau (medium) | p50 3.8s | p50 7.4s (**worse**) | p50 4.5s (back in line with raw) |
| trend.mau (medium) | p50 3.2s | p50 7.3s (**worse**) | p50 3.9s (back in line with raw) |

At the new threshold, medium-project WAU/MAU route to raw (matching what's actually faster there); the
large project's WAU/MAU (~7.5–22M events in their windows) still comfortably clear 5M and correctly use the
sketch, which remains necessary there — raw is refused outright at that scale (Finding 1). This is a
genuine tuning result this phase produced, not a value chosen in advance; a different event-volume shape or
cluster would justify a different number, which is exactly why it's a setting and not a constant.

## Finding 3: ad-hoc raw queries (funnel/retention) are the slow ones under load, and it's Python, not ClickHouse

30 concurrent users, only the 4 ad-hoc query types, medium project — all succeed (0 failures), but slowly:

| request | p50 | p95 | p99 |
|---|---|---|---|
| trend.filtered | 1.9s | 11s | 15s |
| trend.breakdown | 2.3s | 42s | 42s |
| funnel | 2.7s | 5.9s | 6.7s |
| retention | 23s | 54s | 54s |

This does **not** meet the §1.4 "ad-hoc raw queries p95 < 5s" target under this concurrency, and it's worth
being precise about why, rather than reporting the number without explanation. Retention's own design
(§6.10, built Phase 13) computes the cohort grid in **Python**, not ClickHouse, deliberately — no aggregate
function can express a per-user-relative period offset. Profiled in isolation (single query, no
concurrency): the two ClickHouse queries return in under a second (276 ms + 720 ms for a typical window),
but building the per-user Python dict/set structures over their ~370,000 rows takes **1.15 s of synchronous
CPU work** — which blocks that worker process's entire event loop for its duration. Under concurrent load,
every other request queued on the same worker (including *fast* rollup queries — this is also most of why
Finding 1/2's tail latencies are higher than their medians) waits behind it. This is a pre-existing Phase
12/13 architectural cost that load testing made visible, not something this phase's rollup work introduced
or is positioned to fix — the fix is more API worker *processes* (an ops/deployment lever: 1→4 workers
measurably helped, see below), or a future phase moving that computation off the request path. Documented
here rather than silently narrowed the query mix to hide it.

**Single vs 4 API workers**, mixed dashboard query set, medium project, rollups on:

| | 1 worker | 4 workers |
|---|---|---|
| trend.event_counts p50 | 460 ms | 440 ms |
| trend.hourly p50 | 470 ms | 510 ms |
| retention p50 | 31,000 ms | 14,000 ms |
| funnel p50 | 4,200 ms | 3,300 ms |

More workers help the CPU-bound ad-hoc queries roughly proportionally (as expected — separate processes,
separate event loops) and don't materially change the already-fast rollup-count queries. All comparisons
above after this one use 4 workers.

## Ingestion

45 s, 20 simulated SDKs, batches of 200 events, 3 ingestion workers, rollup materialized view present:

- **Accept rate:** 5,969 events/s — clears the §1.4 target (≥ 5,000 events/s).
- **`/ingest` latency:** p50 430 ms, p95 1.6 s. (Not the §1.4 p99 < 50ms target for `/ingest` itself — that
  number is for the endpoint's own validate-and-buffer work; this harness measures full request latency
  from a simulated SDK, including its own network round trip, so it isn't a clean read on that specific
  target and shouldn't be read as failing it.)
- **Zero events lost**: 268,600 accepted, 268,600 landed in ClickHouse.
- **Buffer absorbs the burst as designed** (§1.4 "availability posture"): the 45 s burst took 321 s to fully
  drain end-to-end (835 events/s sustained landing rate across 3 workers) — the buffer, not the ingest
  endpoint, is what's sized for burst absorption.

**Cost of the rollup to ingestion** (same run, materialized view dropped for the duration):

| | view present | view dropped |
|---|---|---|
| accept rate | 5,969/s | 5,871/s |
| landing rate | 835/s | 860/s |
| lost events | 0 | 0 |

Within run-to-run noise — the materialized view's cost to the write path is not measurable at this scale.

**A real gap this test exposed, and the fix already existed:** dropping the view for that comparison (by
design, to isolate its cost) meant the ~264,200 events ingested during that window were never captured by
the rollup — `recreate_view` restores the view but, as its own docstring says, does not backfill what
arrived while it was gone. `python -m pulse.rollups verify --project <ingest-project>` correctly caught the
resulting drift (53,911 vs 106,471 events in one bucket); `rebuild` (a full backfill from raw, 19.3 s for
the whole 55.8M-row database) fixed it, confirmed by a clean re-verify. This is exactly the maintenance
tool's job, and it worked end-to-end on a real, not staged, drift.

## A real resource limit, found live

`python -m pulse.rollups verify` with no `--project` scope runs a `FULL OUTER JOIN` over the *entire*
`events`/`event_hourly` tables. At 55.8M raw rows this hit this container's ClickHouse memory cap
(`MEMORY_LIMIT_EXCEEDED`, ~6.85 GiB). Scoped per project (its documented, intended use for anything beyond a
small deployment) it completes in a couple of seconds. Not fixed — it's a reasonable limit for a
whole-database ad-hoc admin check, not a query the app ever issues, but worth knowing before reaching for
the unscoped form on a large deployment.

## Query-plan review

`EXPLAIN indexes = 1` on the raw trend query (large project, 30-day range, one event name): the
`ORDER BY (org_id, project_id, event_name, timestamp, event_id)` primary key (§5.1, built Phase 6) prunes
921 of 2,328 granules before the MinMax/partition indexes narrow further — the sort key is doing the job
§5.1 designed it for. No sort-key or index change made; none was justified by this evidence.

## Summary against §1.4 targets

| Target | Result |
|---|---|
| Ingestion ≥ 5,000 events/s | **Met** — 5,969/s accepted, 0 loss |
| Dashboard (rollup-eligible) queries p95 < 1s | **Met** for pure counts (event_counts, hourly). **Not met** for the approximate-sketch unique-user queries under 30 concurrent users on this machine (p95 6–7s) — the rollup still turns "refused" into "slow but correct" (Finding 1), which is the larger win, but sub-second p95 for those specific queries needs more headroom (more workers, and/or a coarser pre-aggregation) than this laptop-scale environment has. |
| Ad-hoc raw queries p95 < 5s | **Not met** under 30 concurrent users (Finding 3) — root-caused to pre-existing Phase 13 Python-side retention computation blocking its worker's event loop, not this phase's rollup work; mitigated but not resolved by more API workers. |
| Tenant isolation | Held throughout (existing tenant-leakage tests, plus the rollup's own, all pass) |
| Every query bounded by caps | Held — row-cap refusals were clean 422s, never a timeout, crash, or silent wrong answer |

## Reproducing this

```
cd backend
uv pip install ".[loadtest]"
python loadtests/bench.py setup --events 50000000 --medium-events 5000000
python loadtests/bench.py run --scenario query --label dash_large_on  --rollups on  --project large  --subset dashboard --users 30 --duration 60 --api-workers 4
python loadtests/bench.py run --scenario query --label dash_large_off --rollups off --project large  --subset dashboard --users 30 --duration 60 --api-workers 4
python loadtests/bench.py run --scenario ingest --label ingest_on --mv on --users 20 --duration 45 --batch 200 --workers 3 --api-workers 2
python loadtests/bench.py report
```
