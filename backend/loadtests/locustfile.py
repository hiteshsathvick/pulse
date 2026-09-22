"""Locust scenarios for Pulse. Run through `bench.py`, which sets up the
environment; see docs/PERFORMANCE.md.

  QueryUser   a dashboard: a weighted mix of the insights a real one shows.
  IngestUser  a client SDK flushing batches to POST /ingest.

Select with `--class-picker`-style env: LOAD_SCENARIO=query|ingest|mixed.
"""

import json
import os
import random
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from locust import HttpUser, between, constant, events, task

_CONFIG = json.loads(
    Path(os.environ.get("BENCH_CONFIG", Path(__file__).parent / ".bench.json")).read_text()
)
_SCENARIO = os.environ.get("LOAD_SCENARIO", "query")
# What the run says it is testing. Every response's `source` is checked against
# it, so a "rollups on" run can't quietly be measuring raw (or the reverse).
_EXPECT_ROLLUPS = os.environ.get("EXPECT_ROLLUPS", "on") == "on"
# Unique-user trends legitimately go either way with rollups on: exact raw for a
# small window, the rollup's sketch for a large one. Which one is recorded below.
_UNIQUE_QUERIES = {"trend.dau", "trend.wau", "trend.mau"}
_ROUTING: Counter = Counter()
_INGEST_BATCH = int(os.environ.get("INGEST_BATCH", "100"))
_DAYS = _CONFIG["days"]

_ORG = _CONFIG["org_id"]
_PROJECT = _CONFIG[f"{os.environ.get('BENCH_PROJECT', 'large')}_project_id"]
_QUERY_BASE = f"/api/v1/orgs/{_ORG}/projects/{_PROJECT}/query"


def _range(days: int) -> dict[str, str]:
    today = datetime.now(UTC).date()
    return {"from": str(today.fromordinal(today.toordinal() - days + 1)), "to": str(today)}


# name -> (endpoint, spec, answered-from-rollup-when-enabled)
# The first five are what a dashboard is mostly made of (DAU/WAU/MAU, event
# counts, an hourly view) and are rollup-eligible. The rest need per-user or
# per-property data and always read raw events -- the "ad-hoc" queries.
def _queries() -> dict[str, tuple[str, dict, bool]]:
    return {
        "trend.dau": (
            "trend",
            {
                "kind": "trend",
                "events": ["page viewed"],
                "measure": "unique_users",
                "range": _range(30),
                "granularity": "day",
            },
            True,
        ),
        "trend.event_counts": (
            "trend",
            {
                "kind": "trend",
                "events": ["page viewed", "button clicked"],
                "measure": "count",
                "range": _range(30),
                "granularity": "day",
            },
            True,
        ),
        "trend.wau": (
            "trend",
            {
                "kind": "trend",
                "events": ["page viewed"],
                "measure": "unique_users",
                "range": _range(min(_DAYS, 90)),
                "granularity": "week",
            },
            True,
        ),
        "trend.mau": (
            "trend",
            {
                "kind": "trend",
                "events": ["page viewed"],
                "measure": "unique_users",
                "range": _range(min(_DAYS, 90)),
                "granularity": "month",
            },
            True,
        ),
        "trend.hourly": (
            "trend",
            {
                "kind": "trend",
                "events": ["page viewed"],
                "measure": "count",
                "range": _range(7),
                "granularity": "hour",
            },
            True,
        ),
        "trend.filtered": (
            "trend",
            {
                "kind": "trend",
                "events": ["page viewed"],
                "measure": "count",
                "filters": [{"key": "platform", "op": "eq", "value": "ios"}],
                "range": _range(30),
                "granularity": "day",
            },
            False,
        ),
        "trend.breakdown": (
            "trend",
            {
                "kind": "trend",
                "events": ["page viewed"],
                "measure": "count",
                "breakdown": "country",
                "range": _range(30),
                "granularity": "day",
            },
            False,
        ),
        "funnel": (
            "funnel",
            {
                "kind": "funnel",
                "steps": [
                    {"event": "signed up"},
                    {"event": "added to cart"},
                    {"event": "checkout completed"},
                ],
                "window": {"value": 7, "unit": "day"},
                "range": _range(30),
            },
            False,
        ),
        "retention": (
            "retention",
            {
                "kind": "retention",
                "born_event": "signed up",
                "return_event": "page viewed",
                "period": "week",
                "periods": 8,
                "range": _range(min(_DAYS, 90)),
            },
            False,
        ),
    }


_QUERIES = _queries()
# Weights: a dashboard is mostly rollup-eligible trends.
_ALL_WEIGHTS = {
    "trend.dau": 20,
    "trend.event_counts": 15,
    "trend.wau": 10,
    "trend.mau": 5,
    "trend.hourly": 10,
    "trend.filtered": 12,
    "trend.breakdown": 8,
    "funnel": 10,
    "retention": 10,
}
# SPEC.md 1.4 states two *separate* latency targets -- "dashboard insights
# (rollup-eligible) p95 < 1s" and "ad-hoc raw queries p95 < 5s" -- not one
# combined number. QUERY_SUBSET measures them apart, matching that split;
# "mixed" (the default) is the realistic combined-traffic shape.
_DASHBOARD = {"trend.dau", "trend.event_counts", "trend.wau", "trend.mau", "trend.hourly"}
_ADHOC = {"trend.filtered", "trend.breakdown", "funnel", "retention"}
_SUBSET = os.environ.get("QUERY_SUBSET", "mixed")
_ALLOWED = {"dashboard": _DASHBOARD, "adhoc": _ADHOC, "mixed": set(_ALL_WEIGHTS)}[_SUBSET]
_WEIGHTS = {name: w for name, w in _ALL_WEIGHTS.items() if name in _ALLOWED}


class QueryUser(HttpUser):
    """Closed-loop: each simulated user issues a query, waits briefly, repeats.
    `refresh=true` on every request, deliberately: the Redis cache would
    otherwise answer almost everything and hide the engine being measured."""

    wait_time = between(0.05, 0.15)
    abstract = _SCENARIO == "ingest"

    def on_start(self) -> None:
        # A read key is scoped to exactly one project; pick the one matching
        # the project this run targets.
        project = os.environ.get("BENCH_PROJECT", "large")
        self.headers = {"X-API-Key": _CONFIG[f"{project}_read_key"]}
        self.names = list(_WEIGHTS)
        self.weights = [_WEIGHTS[n] for n in self.names]

    @task
    def query(self) -> None:
        name = random.choices(self.names, self.weights)[0]
        endpoint, spec, rollup_eligible = _QUERIES[name]
        with self.client.post(
            f"{_QUERY_BASE}/{endpoint}?refresh=true",
            json=spec,
            headers=self.headers,
            name=name,
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}: {response.text[:120]}")
                return
            if endpoint == "trend":
                body = response.json()
                got, approximate = body.get("source"), body.get("approximate")
                _ROUTING[(name, got, bool(approximate))] += 1
                if not (rollup_eligible and _EXPECT_ROLLUPS):
                    want = {"raw"}
                elif name in _UNIQUE_QUERIES:
                    want = {"raw", "rollup"}
                else:
                    want = {"rollup"}
                if got not in want:
                    response.failure(f"served from {got!r}, this run expects one of {sorted(want)}")
                    return
                if approximate != (got == "rollup" and name in _UNIQUE_QUERIES):
                    response.failure(f"approximate={approximate} is inconsistent with source={got}")
                    return
            response.success()


_NAMES = ["page viewed", "button clicked", "signed up", "added to cart", "checkout completed"]


class IngestUser(HttpUser):
    """A client SDK flushing a batch of events as fast as the server accepts
    them. Events are unique (fresh event_id), so nothing is deduplicated."""

    wait_time = constant(0)
    abstract = _SCENARIO == "query"

    def on_start(self) -> None:
        self.headers = {"X-API-Key": _CONFIG["write_key"]}

    def _batch(self) -> dict:
        now = datetime.now(UTC).isoformat()
        return {
            "batch": [
                {
                    "event_id": str(uuid.uuid4()),
                    "event": random.choice(_NAMES),
                    "user_id": f"load-{random.randrange(50_000)}",
                    "anonymous_id": f"load-anon-{random.randrange(100_000)}",
                    "timestamp": now,
                    "properties": {"platform": random.choice(["web", "ios", "android"])},
                }
                for _ in range(_INGEST_BATCH)
            ]
        }

    @task
    def ingest(self) -> None:
        with self.client.post(
            "/ingest", json=self._batch(), headers=self.headers, name="ingest", catch_response=True
        ) as response:
            if response.status_code != 202:
                response.failure(f"HTTP {response.status_code}: {response.text[:120]}")
            else:
                response.success()


@events.quitting.add_listener
def _write_routing(**_: object) -> None:
    """Which path actually answered each query kind, so the results can say
    "DAU came from the sketch" instead of assuming it."""
    out = os.environ.get("BENCH_ROUTING_OUT")
    if out:
        rows = [
            {"query": q, "source": source, "approximate": approx, "count": n}
            for (q, source, approx), n in sorted(_ROUTING.items())
        ]
        Path(out).write_text(json.dumps(rows, indent=2))
