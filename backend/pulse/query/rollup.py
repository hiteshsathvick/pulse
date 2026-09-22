"""Answering trends from the hourly rollup (`event_hourly`, migration 0002)
instead of scanning raw events. See SPEC.md 5.2 and 6.14.

Counts from the rollup are *identical* to raw. Unique users are not: the rollup
holds an approximate sketch, because an exact one measured slower than scanning
raw. The engine (pulse/query/service.py) only uses the sketch when the window is
large enough that exact raw would be slow or refused; this module builds the SQL
and decides which query *shapes* the rollup can answer at all."""

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pulse.core.config import Settings
from pulse.query.builder import (
    BuiltQuery,
    _bucket_expr,
    _utc_bounds,
    parse_measure,
    resolve_timezone,
)
from pulse.query.spec import TrendSpec

_OFFSET_SAMPLE_STEP = timedelta(hours=6)

# Precision of the rollup's unique-user sketch, uniqCombined64(SKETCH_PRECISION).
# Must match migration 0002 (a test enforces it). Measured trade-off, 50M events:
# 14 is ~100 ms but has a 4% error spot near 40k users; 15 is ~200 ms with ~3.5%
# worst case near 80k; 16 is ~400 ms; 17 is ~1 s. See docs/PERFORMANCE.md.
SKETCH_PRECISION = 15


def has_whole_hour_offsets(tz_name: str, range_start: datetime, range_end: datetime) -> bool:
    """True if the timezone's UTC offset is a whole number of hours at every
    point in (and just around) the range.

    That is what lets an *hour* bucket be attributed to exactly one local day,
    week or month: a local midnight then always falls on an hour boundary.
    Half-hour and quarter-hour zones (India +5:30, Nepal +5:45, Newfoundland,
    Lord Howe) put midnight in the middle of a UTC hour, so an hour bucket can
    straddle two local days and a rollup can't say which one it belongs to --
    those fall back to raw. Sampled every few hours, padded by a day each side,
    so a DST change that alters the offset mid-range is caught."""
    tz = ZoneInfo(tz_name)
    moment = range_start - timedelta(days=1)
    stop = range_end + timedelta(days=1)
    while moment <= stop:
        offset = moment.astimezone(tz).utcoffset()
        if offset is None or offset.total_seconds() % 3600 != 0:
            return False
        moment += _OFFSET_SAMPLE_STEP
    return True


def is_rollup_eligible(spec: TrendSpec, project_timezone: str) -> bool:
    """A trend is answerable from the rollup only if the rollup holds everything
    it needs: it stores per-(event, hour) counts and unique users and nothing
    else, so no property filters, no breakdown, and only the count and
    unique_users measures. Funnels and retention need per-user event order and
    never come through here."""
    kind, _ = parse_measure(spec.measure)
    if kind not in ("count", "unique_users") or spec.filters or spec.breakdown is not None:
        return False
    tz_name = resolve_timezone(spec.range.tz, project_timezone)
    range_start, range_end = _utc_bounds(spec.range.from_, spec.range.to, tz_name)
    return has_whole_hour_offsets(tz_name, range_start, range_end)


def build_trend_rollup_query(
    spec: TrendSpec,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    project_timezone: str,
    settings: Settings,
) -> BuiltQuery:
    """Same tenant scope, same range, same caps and the same bucketing
    functions as build_trend_query; only the table and the aggregate differ.
    Always re-aggregates (`sum` / `uniqCombined64Merge`) -- the table may hold
    several rows per key until background merges combine them."""
    kind, _ = parse_measure(spec.measure)
    tz_name = resolve_timezone(spec.range.tz, project_timezone)
    range_start, range_end = _utc_bounds(spec.range.from_, spec.range.to, tz_name)

    parameters: dict[str, object] = {
        "tz": tz_name,
        "result_limit": settings.query_result_limit,
        "org_id": str(org_id),
        "project_id": str(project_id),
        "events": list(spec.events),
        "range_start": range_start,
        "range_end": range_end,
    }
    value_expr = (
        "sum(events)"
        if kind == "count"
        else f"uniqCombined64Merge({SKETCH_PRECISION})(users_state)"
    )

    sql = f"""
        SELECT {_bucket_expr(spec.granularity, "hour")} AS bucket, {value_expr} AS value
        FROM event_hourly
        WHERE org_id = {{org_id:UUID}}
          AND project_id = {{project_id:UUID}}
          AND event_name IN {{events:Array(String)}}
          AND hour >= {{range_start:DateTime64(3)}}
          AND hour < {{range_end:DateTime64(3)}}
        GROUP BY bucket
        ORDER BY bucket
        LIMIT {{result_limit:UInt32}}
    """
    query_settings: dict[str, object] = {
        "max_execution_time": settings.query_max_execution_time_seconds,
        "max_rows_to_read": settings.query_max_rows_to_read,
    }
    return BuiltQuery(sql=sql, parameters=parameters, settings=query_settings)


def build_event_total_query(
    spec: TrendSpec,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    project_timezone: str,
    settings: Settings,
) -> BuiltQuery:
    """How many raw events fall in this spec's window, read from the rollup's
    exact `events` column. Cheap and flat (it never touches the user sketch), and
    it is the size that decides whether an exact raw unique-user query would be
    fast enough."""
    tz_name = resolve_timezone(spec.range.tz, project_timezone)
    range_start, range_end = _utc_bounds(spec.range.from_, spec.range.to, tz_name)
    sql = """
        SELECT sum(events) AS total
        FROM event_hourly
        WHERE org_id = {org_id:UUID}
          AND project_id = {project_id:UUID}
          AND event_name IN {events:Array(String)}
          AND hour >= {range_start:DateTime64(3)}
          AND hour < {range_end:DateTime64(3)}
    """
    parameters: dict[str, object] = {
        "org_id": str(org_id),
        "project_id": str(project_id),
        "events": list(spec.events),
        "range_start": range_start,
        "range_end": range_end,
    }
    query_settings: dict[str, object] = {
        "max_execution_time": settings.query_max_execution_time_seconds,
        "max_rows_to_read": settings.query_max_rows_to_read,
    }
    return BuiltQuery(sql=sql, parameters=parameters, settings=query_settings)
