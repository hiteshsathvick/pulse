import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from clickhouse_connect.driver.asyncclient import AsyncClient
from clickhouse_connect.driver.exceptions import DatabaseError
from clickhouse_connect.driver.query import QueryResult

from pulse.core.config import Settings, get_settings
from pulse.core.rate_limit import check_query_rate_limit
from pulse.query import builder, cache, rollup
from pulse.query.builder import BuiltQuery, parse_measure
from pulse.query.spec import FunnelSpec, RetentionPeriod, RetentionSpec, TrendSpec
from pulse.repositories import clickhouse
from pulse.repositories.redis import get_client as get_redis_client
from pulse.services import projects as projects_service


class ProjectNotFound(Exception):
    pass


class QueryTooExpensive(Exception):
    """ClickHouse stopped the query at one of its caps (rows read, time, memory).
    The message is safe to show the caller."""


# TOO_MANY_ROWS, TIMEOUT_EXCEEDED, MEMORY_LIMIT_EXCEEDED
_CAP_ERROR_CODES = {158, 159, 241}
_ERROR_CODE = re.compile(r"code: (\d+)")


async def _query(ch_client: AsyncClient, built: BuiltQuery) -> QueryResult:
    """Every query goes through here so hitting a cap is a clear, catchable
    condition rather than a generic 500."""
    try:
        return await ch_client.query(
            built.sql, parameters=built.parameters, settings=built.settings
        )
    except DatabaseError as exc:
        match = _ERROR_CODE.search(str(exc))
        if match and int(match.group(1)) in _CAP_ERROR_CODES:
            raise QueryTooExpensive(
                "This query would read too much data. Narrow the date range, or add "
                "a filter, and try again."
            ) from exc
        raise


RESULT_SOURCE_RAW = "raw"
RESULT_SOURCE_ROLLUP = "rollup"


@dataclass
class TrendResult:
    results: list[dict[str, object]]
    cached: bool
    # Where the answer comes from ("rollup" or "raw").
    source: str = RESULT_SOURCE_RAW
    # True when a unique-user count came from the rollup's approximate sketch
    # (see Settings.query_rollup_unique_min_events). Counts are never approximate.
    approximate: bool = False


async def run_trend(
    spec: TrendSpec, org_id: uuid.UUID, project_id: uuid.UUID, *, refresh: bool = False
) -> TrendResult:
    project = await projects_service.get_project(org_id, project_id)
    if project is None:
        raise ProjectNotFound()

    settings = get_settings()
    redis_client = get_redis_client()
    key = cache.cache_key(spec, org_id, project_id)

    # `refresh` skips only the read: the fresh result is still written back below,
    # so the next ordinary call is served the up-to-date value, not a stale one.
    cached = None if refresh else await cache.get_cached(redis_client, key)
    if cached is not None:
        return TrendResult(
            results=cached.results,
            cached=True,
            source=cached.source,
            approximate=cached.approximate,
        )

    await check_query_rate_limit(str(org_id))
    ch_client = await clickhouse.get_client()
    source, approximate, built = await _plan_trend(
        ch_client, spec, org_id, project_id, project.timezone, settings
    )
    query_result = await _query(ch_client, built)

    columns = query_result.column_names
    rows = [
        _normalize_row(dict(zip(columns, row, strict=True))) for row in query_result.result_rows
    ]

    await cache.set_cached(
        redis_client,
        key,
        rows,
        settings.query_cache_ttl_seconds,
        source=source,
        approximate=approximate,
    )
    return TrendResult(results=rows, cached=False, source=source, approximate=approximate)


async def _plan_trend(
    ch_client: AsyncClient,
    spec: TrendSpec,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    project_timezone: str,
    settings: Settings,
) -> tuple[str, bool, BuiltQuery]:
    """Chooses where a trend is answered from: (source, approximate, query).

    Counts the rollup can answer come from it, exactly. Unique users are exact
    from raw events while the window is small -- raw is fast there -- and come
    from the rollup's approximate sketch only once the window holds enough events
    that an exact raw scan would be slow or hit the row cap. The size is read
    from the rollup's exact event count, which costs one cheap query."""
    raw = (RESULT_SOURCE_RAW, False, builder.build_trend_query)
    if not (settings.query_rollups_enabled and rollup.is_rollup_eligible(spec, project_timezone)):
        source, approximate, build = raw
    elif parse_measure(spec.measure)[0] == "count":
        source, approximate, build = RESULT_SOURCE_ROLLUP, False, rollup.build_trend_rollup_query
    else:
        total_query = rollup.build_event_total_query(
            spec, org_id, project_id, project_timezone, settings
        )
        total = (await _query(ch_client, total_query)).result_rows[0][0] or 0
        if total >= settings.query_rollup_unique_min_events:
            source, approximate, build = (
                RESULT_SOURCE_ROLLUP,
                True,
                rollup.build_trend_rollup_query,
            )
        else:
            source, approximate, build = raw
    return source, approximate, build(spec, org_id, project_id, project_timezone, settings)


def _normalize_row(row: dict[str, object]) -> dict[str, object]:
    """Datetimes become ISO8601 strings immediately, not left as native
    objects -- a value round-tripped through the Redis cache (JSON has no
    datetime type) would otherwise come back in a different string format
    than one FastAPI serializes fresh, making a cache hit's response
    subtly different from a cache miss's for the exact same query."""
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in row.items()
    }


@dataclass
class FunnelResult:
    results: list[dict[str, object]]
    cached: bool


async def run_funnel(
    spec: FunnelSpec, org_id: uuid.UUID, project_id: uuid.UUID, *, refresh: bool = False
) -> FunnelResult:
    project = await projects_service.get_project(org_id, project_id)
    if project is None:
        raise ProjectNotFound()

    settings = get_settings()
    redis_client = get_redis_client()
    key = cache.cache_key(spec, org_id, project_id)

    # `refresh` skips only the read: the fresh result is still written back below,
    # so the next ordinary call is served the up-to-date value, not a stale one.
    cached = None if refresh else await cache.get_cached(redis_client, key)
    if cached is not None:
        return FunnelResult(results=cached.results, cached=True)

    await check_query_rate_limit(str(org_id))

    built = builder.build_funnel_query(spec, org_id, project_id, project.timezone, settings)
    ch_client = await clickhouse.get_client()
    query_result = await _query(ch_client, built)
    columns = query_result.column_names
    level_rows = [dict(zip(columns, row, strict=True)) for row in query_result.result_rows]

    rows = _funnel_step_results(spec, level_rows)
    await cache.set_cached(redis_client, key, rows, settings.query_cache_ttl_seconds)
    return FunnelResult(results=rows, cached=False)


def _funnel_step_results(
    spec: FunnelSpec, level_rows: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Turns the builder's raw (level[, breakdown], users) histogram into
    per-step cumulative counts, conversion % (relative to step 1), and
    drop-off from the previous step. Reaching windowFunnel level >= k means
    step k was completed in order, so a step's user count is the sum of
    every level at or above it, not just that exact level."""
    has_breakdown = spec.breakdown is not None
    groups: dict[object, dict[int, int]] = {}
    for row in level_rows:
        breakdown_value = row.get("breakdown") if has_breakdown else None
        level_counts = groups.setdefault(breakdown_value, {})
        level_counts[int(row["level"])] = int(row["users"])  # type: ignore[call-overload]

    step_count = len(spec.steps)
    results: list[dict[str, object]] = []
    for breakdown_value in sorted(groups, key=lambda v: "" if v is None else str(v)):
        level_counts = groups[breakdown_value]
        cumulative = [
            sum(count for level, count in level_counts.items() if level >= step)
            for step in range(1, step_count + 1)
        ]
        started = cumulative[0]
        previous = started
        for index, step in enumerate(spec.steps):
            users = cumulative[index]
            step_row: dict[str, object] = {
                "step": step.event,
                "users": users,
                "conversion_pct": (users / started * 100) if started else 0.0,
                "drop_off": 0 if index == 0 else previous - users,
            }
            if has_breakdown:
                step_row["breakdown"] = breakdown_value
            results.append(step_row)
            previous = users
    return results


def _as_utc_datetime(value: object) -> datetime:
    """ClickHouse's `toStartOfWeek` returns `Date` while `toStartOfDay`
    returns `DateTime`, so the same bucket_expr call yields a plain
    `date` for week periods and a `datetime` for day periods -- a real
    asymmetry in ClickHouse's own function library, not a client-driver
    quirk. Coerced to `datetime` here so cohort_period/activity_period are
    always the same shape in the API response, regardless of which period
    was requested."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    raise TypeError(f"expected date or datetime from ClickHouse, got {type(value)!r}")


@dataclass
class RetentionResult:
    results: list[dict[str, object]]
    cached: bool


async def run_retention(
    spec: RetentionSpec, org_id: uuid.UUID, project_id: uuid.UUID, *, refresh: bool = False
) -> RetentionResult:
    project = await projects_service.get_project(org_id, project_id)
    if project is None:
        raise ProjectNotFound()

    settings = get_settings()
    redis_client = get_redis_client()
    key = cache.cache_key(spec, org_id, project_id)

    # `refresh` skips only the read: the fresh result is still written back below,
    # so the next ordinary call is served the up-to-date value, not a stale one.
    cached = None if refresh else await cache.get_cached(redis_client, key)
    if cached is not None:
        return RetentionResult(results=cached.results, cached=True)

    await check_query_rate_limit(str(org_id))

    ch_client = await clickhouse.get_client()

    cohort_built = builder.build_retention_cohort_query(
        spec, org_id, project_id, project.timezone, settings
    )
    cohort_result = await _query(ch_client, cohort_built)
    cohort_columns = cohort_result.column_names
    cohort_by_uid: dict[str, datetime] = {}
    for row in cohort_result.result_rows:
        record = dict(zip(cohort_columns, row, strict=True))
        cohort_by_uid[str(record["uid"])] = _as_utc_datetime(record["cohort_period"])

    activity_built = builder.build_retention_activity_query(
        spec, org_id, project_id, project.timezone, settings
    )
    activity_result = await _query(ch_client, activity_built)
    activity_columns = activity_result.column_names
    activity_by_uid: dict[str, set[datetime]] = {}
    for row in activity_result.result_rows:
        record = dict(zip(activity_columns, row, strict=True))
        activity_by_uid.setdefault(str(record["uid"]), set()).add(
            _as_utc_datetime(record["activity_period"])
        )

    rows = _retention_grid_rows(spec, cohort_by_uid, activity_by_uid)
    await cache.set_cached(redis_client, key, rows, settings.query_cache_ttl_seconds)
    return RetentionResult(results=rows, cached=False)


def _retention_grid_rows(
    spec: RetentionSpec,
    cohort_by_uid: dict[str, datetime],
    activity_by_uid: dict[str, set[datetime]],
) -> list[dict[str, object]]:
    """Turns per-user cohort assignment + activity-period sets into a flat
    cohort-period x offset grid: for each cohort period, how many users
    were assigned to it, and how many were still active exactly `offset`
    periods later, for every offset in [0, periods)."""
    period_length = timedelta(days=1) if spec.period == RetentionPeriod.DAY else timedelta(weeks=1)

    cohort_users: dict[datetime, list[str]] = {}
    for uid, cohort_period in cohort_by_uid.items():
        cohort_users.setdefault(cohort_period, []).append(uid)

    results: list[dict[str, object]] = []
    for cohort_period in sorted(cohort_users):
        uids = cohort_users[cohort_period]
        cohort_size = len(uids)
        for offset in range(spec.periods):
            target_period = cohort_period + period_length * offset
            retained = sum(1 for uid in uids if target_period in activity_by_uid.get(uid, set()))
            results.append(
                {
                    "cohort_period": cohort_period.isoformat(),
                    "cohort_size": cohort_size,
                    "period_offset": offset,
                    "retained": retained,
                    "retention_pct": (retained / cohort_size * 100) if cohort_size else 0.0,
                }
            )
    return results
