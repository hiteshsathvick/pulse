import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pulse.core.config import Settings
from pulse.query.spec import FilterOp, Granularity, TrendSpec


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


def _bucket_expr(granularity: Granularity) -> str:
    if granularity == Granularity.HOUR:
        return "toStartOfHour(timestamp, {tz:String})"
    if granularity == Granularity.DAY:
        return "toStartOfDay(timestamp, {tz:String})"
    if granularity == Granularity.WEEK:
        # mode=1: Monday-start ISO week, the common product-analytics default.
        return "toStartOfWeek(timestamp, 1, {tz:String})"
    if granularity == Granularity.MONTH:
        return "toStartOfMonth(timestamp, {tz:String})"
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

    parameters: dict[str, object] = {
        "org_id": str(org_id),
        "project_id": str(project_id),
        "events": list(spec.events),
        "range_start": range_start,
        "range_end": range_end,
        "tz": tz_name,
        "result_limit": settings.query_result_limit,
    }

    where_clauses = [
        "org_id = {org_id:UUID}",
        "project_id = {project_id:UUID}",
        "event_name IN {events:Array(String)}",
        "timestamp >= {range_start:DateTime64(3)}",
        "timestamp < {range_end:DateTime64(3)}",
    ]
    for index, filter_ in enumerate(spec.filters):
        parameters[f"filter_{index}_key"] = filter_.key
        parameters[f"filter_{index}_value"] = filter_.value
        where_clauses.append(_filter_clause(index, filter_.op, parameters))

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
