import asyncio
import random
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import clickhouse_connect
import pytest

from alembic import command
from alembic.config import Config
from pulse.clickhouse_migrations.runner import migrate
from pulse.core.config import get_settings
from pulse.events.fixtures import generate_fake_event
from pulse.events.repository import insert_events
from pulse.models import User
from pulse.query import cache, rollup
from pulse.query import service as query_service
from pulse.query.spec import TrendSpec
from pulse.repositories.clickhouse import get_client as get_clickhouse_client
from pulse.repositories.postgres import session_scope
from pulse.repositories.redis import get_client as get_redis_client
from pulse.services import orgs as orgs_service
from pulse.services import projects as projects_service
from tests.clickhouse_schema import drop_event_schema

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_MIGRATIONS_DIR = _BACKEND_ROOT / "pulse" / "clickhouse_migrations" / "migrations"

WHOLE_HOUR_TIMEZONES = [
    "UTC",
    "America/New_York",  # DST starts 2026-03-08
    "Europe/London",  # DST starts 2026-03-29
    "Asia/Tokyo",
    "Pacific/Auckland",  # DST ends 2026-04-05
]
# Offsets that aren't a whole number of hours: an hour bucket can straddle two
# local days, so these must NOT be answered from the rollup.
FRACTIONAL_TIMEZONES = [
    "Asia/Kolkata",  # +5:30
    "Asia/Kathmandu",  # +5:45
    "Australia/Lord_Howe",  # +10:30 / +11:00
    "America/St_Johns",  # -3:30 / -2:30
]
_RANGE = {"from": "2026-01-01", "to": "2026-04-10"}


def _alembic_config() -> Config:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return config


@pytest.fixture(scope="module", autouse=True)
def _control_plane_schema():
    config = _alembic_config()
    command.upgrade(config, "head")
    yield
    command.downgrade(config, "base")


async def _with_fresh_clickhouse_client(body) -> None:
    settings = get_settings()
    client = await clickhouse_connect.get_async_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        database=settings.clickhouse_database,
        secure=settings.clickhouse_secure,
    )
    try:
        await body(client)
    finally:
        await client.close()


@pytest.fixture(scope="module", autouse=True)
def _events_table():
    asyncio.run(_with_fresh_clickhouse_client(lambda client: migrate(client, _MIGRATIONS_DIR)))
    yield
    asyncio.run(_with_fresh_clickhouse_client(drop_event_schema))


@pytest.fixture(autouse=True)
def _relaxed_rate_limit() -> Iterator[None]:
    """The parity sweep issues hundreds of queries for one org, far past the
    default per-org limit. The limiter has its own tests (test_query_rate_limit)."""
    settings = get_settings()
    original = settings.query_rate_limit_max_queries
    settings.query_rate_limit_max_queries = 1_000_000
    yield
    settings.query_rate_limit_max_queries = original


async def _create_org_and_project(timezone: str = "UTC") -> tuple[uuid.UUID, uuid.UUID]:
    async with session_scope() as session:
        user = User(
            email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="not-a-real-hash",
            name="Owner",
        )
        session.add(user)
        await session.commit()
    org = await orgs_service.create_organization(
        "Rollup Test Org", f"rollup-org-{uuid.uuid4().hex[:8]}", user.id
    )
    project = await projects_service.create_project(org.id, "Web", "web", timezone, user.id)
    return org.id, project.id


def _event(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    name: str,
    user_id: str,
    anonymous_id: str,
    at: datetime,
) -> dict[str, object]:
    event = generate_fake_event(org_id, project_id, event_name=name, timestamp=at)
    event["user_id"] = user_id  # generate_fake_event turns "" into a random user
    event["anonymous_id"] = anonymous_id
    return event


def _dataset(org_id: uuid.UUID, project_id: uuid.UUID) -> list[dict[str, object]]:
    """~6,000 events over Jan 1 - Apr 12 2026, built to be hard for a rollup:
    identified users on several devices, anonymous-only users, hours with many
    events, and events placed exactly on (and a millisecond either side of)
    local midnights in every whole-hour timezone under test, including the days
    the clocks change."""
    rng = random.Random(20260921)
    names = ["view", "click", "buy"]
    start = datetime(2026, 1, 1, tzinfo=UTC)
    span_ms = 102 * 86_400_000
    events: list[dict[str, object]] = []

    def add(at: datetime) -> None:
        name = rng.choice(names)
        if rng.random() < 0.6:  # identified, on any of several devices
            user, anon = f"u{rng.randrange(60)}", f"device-{rng.randrange(200)}"
        else:  # anonymous only
            user, anon = "", f"anon-{rng.randrange(80)}"
        events.append(_event(org_id, project_id, name, user, anon, at))

    for _ in range(5000):
        add(start + timedelta(milliseconds=rng.randrange(span_ms)))

    for _ in range(40):  # bursts: many users inside one hour
        hour = start + timedelta(hours=rng.randrange(102 * 24))
        for _ in range(25):
            add(hour + timedelta(milliseconds=rng.randrange(3_600_000)))

    boundary_dates = [
        (2026, 1, 5),  # a Monday
        (2026, 1, 31),
        (2026, 2, 1),
        (2026, 2, 28),
        (2026, 3, 1),
        (2026, 3, 8),  # New York clocks go forward
        (2026, 3, 9),
        (2026, 3, 29),  # London clocks go forward
        (2026, 3, 30),
        (2026, 4, 5),  # Auckland clocks go back
        (2026, 4, 6),
    ]
    for tz_name in WHOLE_HOUR_TIMEZONES:
        tz = ZoneInfo(tz_name)
        for year, month, day in boundary_dates:
            midnight = datetime(year, month, day, tzinfo=tz).astimezone(UTC)
            for delta in (timedelta(milliseconds=-1), timedelta(0), timedelta(milliseconds=1)):
                add(midnight + delta)
    return events


def _trend_spec(**overrides: Any) -> TrendSpec:
    payload: dict[str, Any] = {
        "kind": "trend",
        "events": ["view"],
        "measure": "count",
        "range": dict(_RANGE),
        "granularity": "day",
    }
    payload.update(overrides)
    return TrendSpec.model_validate(payload)


async def _raw(spec: TrendSpec, org_id: uuid.UUID, project_id: uuid.UUID):
    """The same query with the rollup switched off -- the ground truth."""
    settings = get_settings()
    settings.query_rollups_enabled = False
    try:
        return await query_service.run_trend(spec, org_id, project_id, refresh=True)
    finally:
        settings.query_rollups_enabled = True


async def _rollup(spec: TrendSpec, org_id: uuid.UUID, project_id: uuid.UUID):
    return await query_service.run_trend(spec, org_id, project_id, refresh=True)


async def _from_the_sketch(spec: TrendSpec, org_id: uuid.UUID, project_id: uuid.UUID):
    """Unique users answered from the rollup's approximate sketch regardless of
    window size (threshold 0) -- what large windows get in production."""
    settings = get_settings()
    original = settings.query_rollup_unique_min_events
    settings.query_rollup_unique_min_events = 0
    try:
        return await query_service.run_trend(spec, org_id, project_id, refresh=True)
    finally:
        settings.query_rollup_unique_min_events = original


SKETCH_TOLERANCE = (
    0.05  # measured worst case is ~3.5% (a narrow band near 80k users); a real bug would blow this
)


def assert_close(
    sketch: list[dict[str, Any]], exact: list[dict[str, Any]], label: str
) -> list[str]:
    """Same buckets, and every value equal or within tolerance."""
    if [r["bucket"] for r in sketch] != [r["bucket"] for r in exact]:
        return [f"{label}: buckets differ"]
    return [
        f"{label}: {r['bucket']} sketch {r['value']} vs exact {e['value']}"
        for r, e in zip(sketch, exact, strict=True)
        if abs(r["value"] - e["value"]) > max(1, SKETCH_TOLERANCE * e["value"])
    ]


async def test_rollup_matches_raw_across_timezones_granularities_and_measures() -> None:
    """DoD: rollup vs raw parity. Counts must match *exactly*; unique users must
    match within the sketch's tolerance when forced onto it, and exactly when
    routed normally (small windows stay on raw). Every whole-hour timezone must
    actually be served by the rollup, or the sweep proves nothing."""
    org_id, project_id = await _create_org_and_project()
    await insert_events(await get_clickhouse_client(), _dataset(org_id, project_id))

    event_sets = [["view"], ["view", "click"], ["view", "click", "buy"]]
    problems: list[str] = []
    compared = rows_compared = 0

    for tz_name in WHOLE_HOUR_TIMEZONES + FRACTIONAL_TIMEZONES:
        rollup_ok = tz_name in WHOLE_HOUR_TIMEZONES
        for granularity in ("hour", "day", "week", "month"):
            for measure in ("count", "unique_users"):
                for events in event_sets:
                    spec = _trend_spec(
                        events=events,
                        measure=measure,
                        granularity=granularity,
                        range={**_RANGE, "tz": tz_name},
                    )
                    label = f"{tz_name} {granularity} {measure} {events}"
                    truth = await _raw(spec, org_id, project_id)
                    routed = await _rollup(spec, org_id, project_id)
                    compared += 1
                    rows_compared += len(truth.results)
                    if truth.source != "raw" or truth.approximate:
                        problems.append(f"{label}: ground truth wasn't exact raw")

                    if measure == "count":
                        want_source = "rollup" if rollup_ok else "raw"
                        if (routed.source, routed.approximate) != (want_source, False):
                            problems.append(f"{label}: routed {routed.source}/{routed.approximate}")
                        if routed.results != truth.results:
                            problems.append(f"{label}: counts differ")
                        continue

                    # unique users: a small window stays on raw and is exact...
                    if (routed.source, routed.approximate) != ("raw", False):
                        problems.append(f"{label}: small window should stay on raw")
                    if routed.results != truth.results:
                        problems.append(f"{label}: raw-routed uniques differ")
                    # ...and the sketch, when a large window forces it, is close.
                    sketch = await _from_the_sketch(spec, org_id, project_id)
                    want = ("rollup", True) if rollup_ok else ("raw", False)
                    if (sketch.source, sketch.approximate) != want:
                        problems.append(
                            f"{label}: sketch routed {sketch.source}/{sketch.approximate}"
                        )
                    problems += assert_close(sketch.results, truth.results, label)

    assert not problems, f"{len(problems)} of {compared} combinations failed:\n" + "\n".join(
        problems[:20]
    )
    # Guard against a vacuous pass (e.g. every query returning nothing).
    assert rows_compared > 10_000, rows_compared


async def test_a_half_hour_zone_uses_the_rollup_for_a_whole_hour_range() -> None:
    """Lord Howe is +11:00 all through January, so for that range the rollup is
    exact for counts and should be used; the fallback is per range, not per zone."""
    org_id, project_id = await _create_org_and_project()
    await insert_events(await get_clickhouse_client(), _dataset(org_id, project_id))
    for granularity in ("hour", "day", "week", "month"):
        for measure in ("count", "unique_users"):
            spec = _trend_spec(
                measure=measure,
                granularity=granularity,
                events=["view", "click"],
                range={"from": "2026-01-05", "to": "2026-01-25", "tz": "Australia/Lord_Howe"},
            )
            truth = await _raw(spec, org_id, project_id)
            assert truth.results, "vacuous comparison"
            if measure == "count":
                got = await _rollup(spec, org_id, project_id)
                assert (got.source, got.results) == ("rollup", truth.results), granularity
            else:
                sketch = await _from_the_sketch(spec, org_id, project_id)
                assert sketch.source == "rollup", granularity
                assert not assert_close(sketch.results, truth.results, granularity)


async def test_a_whole_hour_timezone_change_mid_range_is_still_exact() -> None:
    """A range that spans a clock change has 23- or 25-hour days; the local
    midnights around it must land in the right bucket."""
    org_id, project_id = await _create_org_and_project()
    ny = ZoneInfo("America/New_York")
    ch = await get_clickhouse_client()
    # 2026-03-08 in New York is a 23-hour day (02:00 EST jumps to 03:00 EDT).
    day_start = datetime(2026, 3, 8, tzinfo=ny).astimezone(UTC)
    next_start = datetime(2026, 3, 9, tzinfo=ny).astimezone(UTC)
    assert next_start - day_start == timedelta(hours=23)
    await insert_events(
        ch,
        [
            _event(org_id, project_id, "view", "a", "d1", day_start - timedelta(milliseconds=1)),
            _event(org_id, project_id, "view", "b", "d2", day_start),
            _event(org_id, project_id, "view", "c", "d3", next_start - timedelta(milliseconds=1)),
            _event(org_id, project_id, "view", "d", "d4", next_start),
        ],
    )
    spec = _trend_spec(range={"from": "2026-03-07", "to": "2026-03-09", "tz": "America/New_York"})
    got, truth = await _rollup(spec, org_id, project_id), await _raw(spec, org_id, project_id)
    assert got.source == "rollup"
    assert got.results == truth.results
    assert [row["value"] for row in got.results] == [1, 2, 1]  # Mar 7, Mar 8 (23h), Mar 9


async def test_unique_users_use_the_same_identity_as_raw() -> None:
    """The sketch counts the engine's identity (user_id once identified, else
    anonymous_id), not `user_id` alone -- which would count every anonymous user
    (user_id '') as one person."""
    org_id, project_id = await _create_org_and_project()
    at = datetime(2026, 2, 10, 12, 0, tzinfo=UTC)
    await insert_events(
        await get_clickhouse_client(),
        [
            # three different anonymous visitors -> 3 people, not 1
            _event(org_id, project_id, "view", "", "anon-1", at),
            _event(org_id, project_id, "view", "", "anon-2", at),
            _event(org_id, project_id, "view", "", "anon-3", at),
            # one identified user on two devices, and twice on one -> 1 person
            _event(org_id, project_id, "view", "alice", "phone", at),
            _event(org_id, project_id, "view", "alice", "laptop", at),
            _event(org_id, project_id, "view", "alice", "laptop", at + timedelta(minutes=5)),
            # the same anonymous visitor twice -> still 1
            _event(org_id, project_id, "view", "", "anon-1", at + timedelta(minutes=9)),
        ],
    )
    spec = _trend_spec(measure="unique_users", range={"from": "2026-02-10", "to": "2026-02-10"})
    sketch = await _from_the_sketch(spec, org_id, project_id)
    truth = await _raw(spec, org_id, project_id)
    assert sketch.source == "rollup"
    assert [row["value"] for row in sketch.results] == [4]  # anon-1, anon-2, anon-3, alice
    assert sketch.results == truth.results  # small sets are counted exactly

    count_spec = _trend_spec(measure="count", range={"from": "2026-02-10", "to": "2026-02-10"})
    assert [row["value"] for row in (await _rollup(count_spec, org_id, project_id)).results] == [7]


async def test_multi_event_unique_users_is_the_union_not_the_sum() -> None:
    org_id, project_id = await _create_org_and_project()
    at = datetime(2026, 2, 10, 9, 0, tzinfo=UTC)
    await insert_events(
        await get_clickhouse_client(),
        [
            _event(org_id, project_id, "view", "alice", "d", at),
            _event(org_id, project_id, "click", "alice", "d", at),
            _event(org_id, project_id, "click", "bob", "d", at),
        ],
    )
    spec = _trend_spec(
        events=["view", "click"],
        measure="unique_users",
        range={"from": "2026-02-10", "to": "2026-02-10"},
    )
    got = await _from_the_sketch(spec, org_id, project_id)
    assert got.source == "rollup"
    assert [row["value"] for row in got.results] == [2]  # alice once, not twice


async def test_the_rollup_stays_correct_after_background_merges() -> None:
    """In an AggregatingMergeTree a plain column silently keeps one arbitrary
    row's value when parts merge. Insert in several parts, force a full merge,
    and confirm nothing about the answers changes."""
    org_id, project_id = await _create_org_and_project()
    ch = await get_clickhouse_client()
    events = _dataset(org_id, project_id)
    for start in range(0, len(events), 700):  # ~9 separate parts
        await insert_events(ch, events[start : start + 700])

    specs = [
        _trend_spec(measure=measure, granularity=granularity, events=["view", "click"])
        for measure in ("count", "unique_users")
        for granularity in ("hour", "day", "week")
    ]
    before = [(await _from_the_sketch(spec, org_id, project_id)).results for spec in specs]
    parts_before = (
        await ch.query("SELECT count() FROM system.parts WHERE table = 'event_hourly' AND active")
    ).result_rows[0][0]

    await ch.command("OPTIMIZE TABLE event_hourly FINAL")

    parts_after = (
        await ch.query("SELECT count() FROM system.parts WHERE table = 'event_hourly' AND active")
    ).result_rows[0][0]
    assert parts_after < parts_before, "the forced merge should have combined parts"
    for spec, earlier in zip(specs, before, strict=True):
        after = await _from_the_sketch(spec, org_id, project_id)
        truth = (await _raw(spec, org_id, project_id)).results
        assert after.results == earlier  # merging sketches changes nothing
        if spec.measure == "count":
            assert after.results == truth  # and counts are exact
        else:
            assert not assert_close(after.results, truth, spec.granularity)


async def test_new_events_reach_the_rollup_without_any_rebuild() -> None:
    org_id, project_id = await _create_org_and_project()
    ch = await get_clickhouse_client()
    at = datetime(2026, 2, 10, 12, 0, tzinfo=UTC)
    spec = _trend_spec(range={"from": "2026-02-10", "to": "2026-02-10"})

    await insert_events(ch, [_event(org_id, project_id, "view", "a", "d", at)])
    assert [r["value"] for r in (await _rollup(spec, org_id, project_id)).results] == [1]

    await insert_events(ch, [_event(org_id, project_id, "view", "b", "d", at + timedelta(hours=3))])
    got = await _rollup(spec, org_id, project_id)
    assert [r["value"] for r in got.results] == [2]
    assert got.results == (await _raw(spec, org_id, project_id)).results


async def test_rollup_never_leaks_across_tenants() -> None:
    """Mandatory tenant-leakage test (CLAUDE.md #4) for the rollup path."""
    org_a, project_a = await _create_org_and_project()
    org_b, project_b = await _create_org_and_project()
    ch = await get_clickhouse_client()
    at = datetime(2026, 2, 10, 12, 0, tzinfo=UTC)
    await insert_events(
        ch,
        [
            _event(org_a, project_a, "view", "a1", "d", at),
            _event(org_a, project_a, "view", "a2", "d", at),
            _event(org_b, project_b, "view", "b1", "d", at),
            _event(org_b, project_b, "secret event", "b1", "d", at),
        ],
    )
    unique = _trend_spec(measure="unique_users", range={"from": "2026-02-10", "to": "2026-02-10"})
    a = await _from_the_sketch(unique, org_a, project_a)
    assert a.source == "rollup"
    assert [row["value"] for row in a.results] == [2]  # not 3

    only_in_b = _trend_spec(
        events=["secret event"], range={"from": "2026-02-10", "to": "2026-02-10"}
    )
    assert (await _rollup(only_in_b, org_a, project_a)).results == []
    assert [r["value"] for r in (await _rollup(only_in_b, org_b, project_b)).results] == [1]

    # The volume estimate that decides raw-vs-sketch is tenant-scoped too.
    b_only_unique = _trend_spec(events=["secret event"], measure="unique_users")
    assert (await _from_the_sketch(b_only_unique, org_a, project_a)).results == []


async def test_shapes_the_rollup_cannot_answer_fall_back_to_raw() -> None:
    org_id, project_id = await _create_org_and_project()
    await insert_events(await get_clickhouse_client(), _dataset(org_id, project_id))

    plain = _trend_spec()
    assert (await _rollup(plain, org_id, project_id)).source == "rollup"

    not_servable = {
        "a property filter": {"filters": [{"key": "platform", "op": "eq", "value": "web"}]},
        "a breakdown": {"breakdown": "platform"},
        "a property sum": {"measure": "property_sum:amount"},
        "a property average": {"measure": "property_avg:amount"},
    }
    for label, overrides in not_servable.items():
        result = await _from_the_sketch(_trend_spec(**overrides), org_id, project_id)
        assert (result.source, result.approximate) == ("raw", False), label


async def test_unique_users_use_the_sketch_only_once_the_window_is_large() -> None:
    """The routing rule, at its boundary: exact raw below the threshold, the
    sketch at or above it, where the threshold counts events in the window."""
    org_id, project_id = await _create_org_and_project()
    at = datetime(2026, 2, 10, 12, 0, tzinfo=UTC)
    await insert_events(
        await get_clickhouse_client(),
        [_event(org_id, project_id, "view", f"u{i}", f"d{i}", at) for i in range(30)],
    )
    spec = _trend_spec(measure="unique_users", range={"from": "2026-02-10", "to": "2026-02-10"})
    settings = get_settings()
    original = settings.query_rollup_unique_min_events
    try:
        for threshold, want in (
            (31, ("raw", False)),
            (30, ("rollup", True)),
            (1, ("rollup", True)),
        ):
            settings.query_rollup_unique_min_events = threshold
            got = await query_service.run_trend(spec, org_id, project_id, refresh=True)
            assert (got.source, got.approximate) == want, threshold
            assert [r["value"] for r in got.results] == [30]
    finally:
        settings.query_rollup_unique_min_events = original


async def test_counts_are_never_approximate_whatever_the_threshold() -> None:
    org_id, project_id = await _create_org_and_project()
    await insert_events(await get_clickhouse_client(), _dataset(org_id, project_id)[:300])
    settings = get_settings()
    original = settings.query_rollup_unique_min_events
    try:
        for threshold in (0, 1, 10**9):
            settings.query_rollup_unique_min_events = threshold
            got = await query_service.run_trend(_trend_spec(), org_id, project_id, refresh=True)
            assert (got.source, got.approximate) == ("rollup", False), threshold
    finally:
        settings.query_rollup_unique_min_events = original


@pytest.mark.parametrize("audience", [5_000, 40_000, 80_000])
async def test_the_sketch_stays_within_its_documented_error(audience: int) -> None:
    """Small sets are counted exactly, so the accuracy claim has to be tested where
    the sketch actually approximates. 80,000 is deliberately the worst-measured
    size for precision 15 (a narrow band where the sketch changes mode)."""
    org_id, project_id = await _create_org_and_project()
    start = datetime(2026, 2, 10, 12, 0, tzinfo=UTC)
    events = [
        _event(org_id, project_id, "view", f"u{i}", f"d{i}", start + timedelta(milliseconds=i))
        for i in range(audience)
    ]
    await insert_events(await get_clickhouse_client(), events)
    spec = _trend_spec(measure="unique_users", range={"from": "2026-02-10", "to": "2026-02-10"})
    truth = await _raw(spec, org_id, project_id)
    assert [r["value"] for r in truth.results] == [audience]

    sketch = await _from_the_sketch(spec, org_id, project_id)
    assert (sketch.source, sketch.approximate) == ("rollup", True)
    (bucket,) = sketch.results
    assert abs(bucket["value"] - audience) <= SKETCH_TOLERANCE * audience, bucket["value"]


def test_the_sketch_precision_in_python_matches_the_migration() -> None:
    """The precision lives in two places (the SQL file and rollup.SKETCH_PRECISION);
    a mismatch would make every merge fail or, worse, silently mis-merge."""
    sql = (_MIGRATIONS_DIR / "0002_event_hourly_rollup.sql").read_text()
    assert sql.count(f"uniqCombined64State({rollup.SKETCH_PRECISION})") == 2  # view + backfill
    assert f"AggregateFunction(uniqCombined64({rollup.SKETCH_PRECISION}), String)" in sql


async def test_turning_the_rollup_off_routes_every_query_to_raw() -> None:
    org_id, project_id = await _create_org_and_project()
    await insert_events(await get_clickhouse_client(), _dataset(org_id, project_id)[:200])
    settings = get_settings()
    settings.query_rollups_enabled = False
    try:
        result = await query_service.run_trend(_trend_spec(), org_id, project_id, refresh=True)
    finally:
        settings.query_rollups_enabled = True
    assert (result.source, result.approximate) == ("raw", False)


async def test_source_and_approximate_survive_a_cache_hit() -> None:
    org_id, project_id = await _create_org_and_project()
    await insert_events(await get_clickhouse_client(), _dataset(org_id, project_id)[:200])
    settings = get_settings()
    original = settings.query_rollup_unique_min_events
    settings.query_rollup_unique_min_events = 0
    try:
        for measure, want in (("count", ("rollup", False)), ("unique_users", ("rollup", True))):
            spec = _trend_spec(measure=measure)
            miss = await query_service.run_trend(spec, org_id, project_id, refresh=True)
            hit = await query_service.run_trend(spec, org_id, project_id)
            assert (miss.cached, hit.cached) == (False, True)
            assert (miss.source, miss.approximate) == want == (hit.source, hit.approximate)
            assert miss.results == hit.results
    finally:
        settings.query_rollup_unique_min_events = original


async def test_a_query_that_hits_the_row_cap_is_a_clear_error_and_is_not_cached() -> None:
    org_id, project_id = await _create_org_and_project()
    await insert_events(await get_clickhouse_client(), _dataset(org_id, project_id)[:200])
    settings = get_settings()
    original = settings.query_max_rows_to_read
    settings.query_max_rows_to_read = 5
    # A property filter forces the raw path, which has to scan more than 5 rows.
    spec = _trend_spec(filters=[{"key": "platform", "op": "eq", "value": "web"}])
    try:
        with pytest.raises(query_service.QueryTooExpensive) as caught:
            await query_service.run_trend(spec, org_id, project_id, refresh=True)
    finally:
        settings.query_max_rows_to_read = original
    assert "Narrow the date range" in str(caught.value)
    key = cache.cache_key(spec, org_id, project_id)
    assert await cache.get_cached(get_redis_client(), key) is None


class TestEligibility:
    """Pure rules, no ClickHouse."""

    def test_whole_hour_zones_are_eligible_including_across_a_dst_change(self) -> None:
        for tz_name in WHOLE_HOUR_TIMEZONES:
            assert rollup.is_rollup_eligible(_trend_spec(range={**_RANGE, "tz": tz_name}), "UTC")

    def test_fractional_offset_zones_are_not(self) -> None:
        for tz_name in FRACTIONAL_TIMEZONES:
            assert not rollup.is_rollup_eligible(
                _trend_spec(range={**_RANGE, "tz": tz_name}), "UTC"
            ), tz_name

    def test_the_project_timezone_is_used_when_the_spec_defers_to_it(self) -> None:
        spec = _trend_spec()  # range.tz defaults to "project"
        assert rollup.is_rollup_eligible(spec, "Asia/Tokyo")
        assert not rollup.is_rollup_eligible(spec, "Asia/Kolkata")

    def test_eligibility_depends_on_the_offsets_in_the_range_not_just_the_zone(self) -> None:
        # Lord Howe is +11:00 in summer (a whole hour) but +10:30 in winter, and
        # its clocks go back on 2026-04-05.
        def lord_howe(start: str, end: str) -> TrendSpec:
            return _trend_spec(range={"from": start, "to": end, "tz": "Australia/Lord_Howe"})

        assert rollup.is_rollup_eligible(lord_howe("2026-01-05", "2026-01-25"), "UTC")
        assert not rollup.is_rollup_eligible(lord_howe("2026-06-01", "2026-06-30"), "UTC")
        assert not rollup.is_rollup_eligible(lord_howe("2026-03-20", "2026-04-20"), "UTC")

    def test_only_count_and_unique_users_with_no_filters_or_breakdown(self) -> None:
        assert rollup.is_rollup_eligible(_trend_spec(measure="count"), "UTC")
        assert rollup.is_rollup_eligible(_trend_spec(measure="unique_users"), "UTC")
        assert not rollup.is_rollup_eligible(_trend_spec(measure="property_sum:x"), "UTC")
        assert not rollup.is_rollup_eligible(_trend_spec(breakdown="platform"), "UTC")
        assert not rollup.is_rollup_eligible(
            _trend_spec(filters=[{"key": "k", "op": "eq", "value": "v"}]), "UTC"
        )


class TestRollupSql:
    def _built(self, **overrides: Any):
        return rollup.build_trend_rollup_query(
            _trend_spec(**overrides), uuid.uuid4(), uuid.uuid4(), "UTC", get_settings()
        )

    def _total(self, **overrides: Any):
        return rollup.build_event_total_query(
            _trend_spec(**overrides), uuid.uuid4(), uuid.uuid4(), "UTC", get_settings()
        )

    def test_tenant_scope_and_caps_are_always_present(self) -> None:
        for built in (self._built(), self._total()):
            assert "org_id = {org_id:UUID}" in built.sql
            assert "project_id = {project_id:UUID}" in built.sql
            assert {"max_execution_time", "max_rows_to_read"} <= set(built.settings)
        assert "LIMIT {result_limit:UInt32}" in self._built().sql

    def test_client_input_is_only_ever_a_bound_parameter(self) -> None:
        hostile = "x'; DROP TABLE event_hourly; --"
        for built in (self._built(events=[hostile]), self._total(events=[hostile])):
            assert hostile not in built.sql
            assert built.parameters["events"] == [hostile]

    def test_it_always_re_aggregates(self) -> None:
        # A plain column read would double-count until parts merge.
        assert "sum(events)" in self._built(measure="count").sql
        assert "uniqCombined64Merge(15)(users_state)" in self._built(measure="unique_users").sql

    def test_the_volume_estimate_never_reads_the_user_sketch(self) -> None:
        # That is what keeps it cheap and flat; reading the sketch would defeat it.
        assert "users_state" not in self._total().sql
