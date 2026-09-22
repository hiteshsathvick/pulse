import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pulse.core.config import Settings
from pulse.query.spec import (
    Filter,
    FilterOp,
    FunnelSpec,
    FunnelWindow,
    Granularity,
    RetentionPeriod,
    RetentionSpec,
    TrendSpec,
)


@dataclass
class BuiltQuery:
    sql: str
    parameters: dict[str, object]
    settings: dict[str, object]


def resolve_timezone(spec_tz: str, project_timezone: str) -> str:
    return project_timezone if spec_tz == "project" else spec_tz


def _utc_bounds(range_from: date, range_to: date, tz_name: str) -> tuple[datetime, datetime]:
    """The range is inclusive of both calendar dates, in the given timezone
    -- so `to` extends through the end of that local day, not midnight at
    its start. Converted to UTC once here in Python, not left to ClickHouse,
    since the WHERE bound itself doesn't need timezone-aware SQL, only the
    GROUP BY bucketing does (SPEC.md #5.3)."""
    tz = ZoneInfo(tz_name)
    start_local = datetime(range_from.year, range_from.month, range_from.day, tzinfo=tz)
    end_local = datetime(range_to.year, range_to.month, range_to.day, tzinfo=tz) + timedelta(days=1)
    return start_local.astimezone(ZoneInfo("UTC")), end_local.astimezone(ZoneInfo("UTC"))


def parse_measure(measure: str) -> tuple[str, str | None]:
    if measure in ("count", "unique_users"):
        return measure, None
    kind, _, key = measure.partition(":")
    return kind, key


def _measure_expr(measure: str, parameters: dict[str, object]) -> str:
    kind, key = parse_measure(measure)
    if kind == "count":
        return "count()"
    if kind == "unique_users":
        # The effective identity: the known user_id once identified,
        # otherwise the anonymous_id -- matches events.user_id's own
        # documented convention ('' if anonymous), SPEC.md #5.1.
        return "uniqExact(if(user_id != '', user_id, anonymous_id))"
    if kind in ("property_sum", "property_avg"):
        parameters["measure_property_key"] = key
        agg = "sum" if kind == "property_sum" else "avg"
        # toFloat64OrZero, not toFloat64: a non-numeric property value fails
        # soft to 0 rather than erroring the whole query. Map(String,String)
        # has no schema to enforce this is actually numeric.
        return f"{agg}(toFloat64OrZero(properties[{{measure_property_key:String}}]))"
    raise ValueError(f"unknown measure kind: {kind}")


def _bucket_expr(granularity: Granularity, column: str = "timestamp") -> str:
    # `column` lets the rollup query bucket its own `hour` column with exactly
    # the same functions, so the two paths can't drift apart.
    if granularity == Granularity.HOUR:
        return f"toStartOfHour({column}, {{tz:String}})"
    if granularity == Granularity.DAY:
        return f"toStartOfDay({column}, {{tz:String}})"
    if granularity == Granularity.WEEK:
        # mode=1: Monday-start ISO week, the common product-analytics default.
        return f"toStartOfWeek({column}, 1, {{tz:String}})"
    if granularity == Granularity.MONTH:
        return f"toStartOfMonth({column}, {{tz:String}})"
    raise ValueError(f"unknown granularity: {granularity}")


def _filter_clause(index: int, op: FilterOp, parameters: dict[str, object]) -> str:
    key_param, value_param = f"filter_{index}_key", f"filter_{index}_value"
    key_expr = f"properties[{{{key_param}:String}}]"
    value_expr = f"{{{value_param}:String}}"
    if op == FilterOp.EQ:
        return f"{key_expr} = {value_expr}"
    if op == FilterOp.NEQ:
        return f"{key_expr} != {value_expr}"
    if op == FilterOp.CONTAINS:
        return f"position({key_expr}, {value_expr}) > 0"
    raise ValueError(f"unknown filter op: {op}")


def _tenant_and_range_where(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    events: list[str],
    range_start: datetime,
    range_end: datetime,
    parameters: dict[str, object],
) -> list[str]:
    """The predicates every query type shares: tenant scope always
    injected (SPEC.md #3), restricted to the event names the spec actually
    needs, bounded to the resolved UTC range."""
    parameters["org_id"] = str(org_id)
    parameters["project_id"] = str(project_id)
    parameters["events"] = events
    parameters["range_start"] = range_start
    parameters["range_end"] = range_end
    return [
        "org_id = {org_id:UUID}",
        "project_id = {project_id:UUID}",
        "event_name IN {events:Array(String)}",
        "timestamp >= {range_start:DateTime64(3)}",
        "timestamp < {range_end:DateTime64(3)}",
    ]


def _apply_filters(
    filters: list[Filter], where_clauses: list[str], parameters: dict[str, object]
) -> None:
    for index, filter_ in enumerate(filters):
        parameters[f"filter_{index}_key"] = filter_.key
        parameters[f"filter_{index}_value"] = filter_.value
        where_clauses.append(_filter_clause(index, filter_.op, parameters))


def build_trend_query(
    spec: TrendSpec,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    project_timezone: str,
    settings: Settings,
) -> BuiltQuery:
    """Compiles a validated TrendSpec into parameterized ClickHouse SQL.
    org_id/project_id are function parameters, never read off the spec --
    the spec has no field for them at all, so there's nothing for a client
    to smuggle (SPEC.md #3's "tenant scope is injected, never trusted").
    Every client-controlled value (property keys, filter values, event
    names, the timezone) goes in as a named parameter, never
    string-interpolated -- CLAUDE.md's "never hand-concatenate user input
    into SQL"."""
    tz_name = resolve_timezone(spec.range.tz, project_timezone)
    range_start, range_end = _utc_bounds(spec.range.from_, spec.range.to, tz_name)

    parameters: dict[str, object] = {"tz": tz_name, "result_limit": settings.query_result_limit}
    where_clauses = _tenant_and_range_where(
        org_id, project_id, list(spec.events), range_start, range_end, parameters
    )
    _apply_filters(spec.filters, where_clauses, parameters)

    select_columns = [f"{_bucket_expr(spec.granularity)} AS bucket"]
    group_by = ["bucket"]
    if spec.breakdown is not None:
        parameters["breakdown_key"] = spec.breakdown
        select_columns.append("properties[{breakdown_key:String}] AS breakdown")
        group_by.append("breakdown")
    select_columns.append(f"{_measure_expr(spec.measure, parameters)} AS value")

    sql = f"""
        SELECT {", ".join(select_columns)}
        FROM events
        WHERE {" AND ".join(where_clauses)}
        GROUP BY {", ".join(group_by)}
        ORDER BY bucket
        LIMIT {{result_limit:UInt32}}
    """

    query_settings: dict[str, object] = {
        "max_execution_time": settings.query_max_execution_time_seconds,
        "max_rows_to_read": settings.query_max_rows_to_read,
    }
    return BuiltQuery(sql=sql, parameters=parameters, settings=query_settings)


def _retention_bucket_granularity(period: RetentionPeriod) -> Granularity:
    return Granularity.DAY if period == RetentionPeriod.DAY else Granularity.WEEK


def _period_timedelta(period: RetentionPeriod) -> timedelta:
    return timedelta(days=1) if period == RetentionPeriod.DAY else timedelta(weeks=1)


def build_retention_cohort_query(
    spec: RetentionSpec,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    project_timezone: str,
    settings: Settings,
) -> BuiltQuery:
    """Each user's cohort period: the bucket of their *first* born_event
    within the range -- one row per user who qualifies for a cohort at
    all. Reuses trend's own `_bucket_expr` (day/week) rather than a second
    bucketing implementation."""
    tz_name = resolve_timezone(spec.range.tz, project_timezone)
    range_start, range_end = _utc_bounds(spec.range.from_, spec.range.to, tz_name)

    parameters: dict[str, object] = {"tz": tz_name}
    where_clauses = _tenant_and_range_where(
        org_id, project_id, [spec.born_event], range_start, range_end, parameters
    )

    bucket_expr = _bucket_expr(_retention_bucket_granularity(spec.period))
    sql = f"""
        SELECT
            if(user_id != '', user_id, anonymous_id) AS uid,
            min({bucket_expr}) AS cohort_period
        FROM events
        WHERE {" AND ".join(where_clauses)}
        GROUP BY uid
    """
    query_settings: dict[str, object] = {
        "max_execution_time": settings.query_max_execution_time_seconds,
        "max_rows_to_read": settings.query_max_rows_to_read,
    }
    return BuiltQuery(sql=sql, parameters=parameters, settings=query_settings)


def build_retention_activity_query(
    spec: RetentionSpec,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    project_timezone: str,
    settings: Settings,
) -> BuiltQuery:
    """Every (user, activity period) where the user did return_event,
    widened *past* range.to by periods * period-length -- a user whose
    cohort starts near the end of the range must still be checkable at
    every later offset, not cut off at the cohort range's own boundary."""
    tz_name = resolve_timezone(spec.range.tz, project_timezone)
    range_start, _ = _utc_bounds(spec.range.from_, spec.range.to, tz_name)
    activity_end_date = spec.range.to + _period_timedelta(spec.period) * spec.periods
    _, activity_range_end = _utc_bounds(activity_end_date, activity_end_date, tz_name)

    parameters: dict[str, object] = {"tz": tz_name}
    where_clauses = _tenant_and_range_where(
        org_id, project_id, [spec.return_event], range_start, activity_range_end, parameters
    )

    bucket_expr = _bucket_expr(_retention_bucket_granularity(spec.period))
    sql = f"""
        SELECT DISTINCT
            if(user_id != '', user_id, anonymous_id) AS uid,
            {bucket_expr} AS activity_period
        FROM events
        WHERE {" AND ".join(where_clauses)}
    """
    query_settings: dict[str, object] = {
        "max_execution_time": settings.query_max_execution_time_seconds,
        "max_rows_to_read": settings.query_max_rows_to_read,
    }
    return BuiltQuery(sql=sql, parameters=parameters, settings=query_settings)


_WINDOW_UNIT_SECONDS = {"hour": 3600, "day": 86400}


def window_seconds(window: FunnelWindow) -> int:
    return window.value * _WINDOW_UNIT_SECONDS[window.unit]


def build_funnel_query(
    spec: FunnelSpec,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    project_timezone: str,
    settings: Settings,
) -> BuiltQuery:
    """Compiles a validated FunnelSpec into a windowFunnel query. Per-user
    level (how many steps were completed, in order, within the conversion
    window) is computed in an inner query, then rolled up to a per-level
    histogram -- turning that histogram into cumulative per-step counts,
    conversion %, and drop-off is query/service.py's job, not SQL's; this
    function only ever emits the raw level distribution."""
    tz_name = resolve_timezone(spec.range.tz, project_timezone)
    range_start, range_end = _utc_bounds(spec.range.from_, spec.range.to, tz_name)

    step_events = [step.event for step in spec.steps]
    parameters: dict[str, object] = {"window_seconds": window_seconds(spec.window)}
    where_clauses = _tenant_and_range_where(
        org_id, project_id, step_events, range_start, range_end, parameters
    )
    _apply_filters(spec.filters, where_clauses, parameters)

    step_conditions = []
    for index, event_name in enumerate(step_events):
        param_name = f"step_{index}_event"
        parameters[param_name] = event_name
        step_conditions.append(f"event_name = {{{param_name}:String}}")

    breakdown_select, breakdown_group, breakdown_select_outer = "", "", ""
    if spec.breakdown is not None:
        parameters["breakdown_key"] = spec.breakdown
        breakdown_select = ", properties[{breakdown_key:String}] AS breakdown"
        breakdown_group = ", breakdown"
        breakdown_select_outer = ", breakdown"

    sql = f"""
        SELECT level{breakdown_select_outer}, count() AS users
        FROM (
            SELECT
                if(user_id != '', user_id, anonymous_id) AS uid{breakdown_select},
                windowFunnel({{window_seconds:UInt64}})
                    (toDateTime(timestamp), {", ".join(step_conditions)}) AS level
            FROM events
            WHERE {" AND ".join(where_clauses)}
            GROUP BY uid{breakdown_group}
        )
        GROUP BY level{breakdown_group}
        ORDER BY level{breakdown_group}
    """

    query_settings: dict[str, object] = {
        "max_execution_time": settings.query_max_execution_time_seconds,
        "max_rows_to_read": settings.query_max_rows_to_read,
    }
    return BuiltQuery(sql=sql, parameters=parameters, settings=query_settings)
