import uuid
from datetime import date

import pytest

from pulse.core.config import Settings
from pulse.query.builder import (
    build_funnel_query,
    build_trend_query,
    parse_measure,
    resolve_timezone,
    window_seconds,
)
from pulse.query.spec import (
    DateRange,
    Filter,
    FilterOp,
    FunnelSpec,
    FunnelWindow,
    Granularity,
    TrendSpec,
)


def _settings() -> Settings:
    return Settings(
        query_max_execution_time_seconds=10,
        query_max_rows_to_read=1_000_000,
        query_result_limit=500,
    )


def _spec(**overrides: object) -> TrendSpec:
    payload: dict[str, object] = {
        "kind": "trend",
        "events": ["checkout completed"],
        "measure": "count",
        "range": {"from": "2026-08-01", "to": "2026-08-31"},
        "granularity": "day",
    }
    payload.update(overrides)
    return TrendSpec.model_validate(payload)


def test_resolve_timezone_defaults_to_the_project_timezone() -> None:
    assert resolve_timezone("project", "America/New_York") == "America/New_York"
    assert resolve_timezone("Europe/Berlin", "America/New_York") == "Europe/Berlin"


def test_parse_measure() -> None:
    assert parse_measure("count") == ("count", None)
    assert parse_measure("unique_users") == ("unique_users", None)
    assert parse_measure("property_sum:revenue") == ("property_sum", "revenue")
    assert parse_measure("property_avg:revenue") == ("property_avg", "revenue")


def test_org_and_project_are_always_injected_as_parameters_not_read_from_the_spec() -> None:
    """The spec has no org_id/project_id field at all -- this asserts the
    compiled query still always carries them, from the function's own
    arguments, and that the WHERE clause references them by name."""
    org_id, project_id = uuid.uuid4(), uuid.uuid4()
    built = build_trend_query(_spec(), org_id, project_id, "UTC", _settings())

    assert "org_id = {org_id:UUID}" in built.sql
    assert "project_id = {project_id:UUID}" in built.sql
    assert built.parameters["org_id"] == str(org_id)
    assert built.parameters["project_id"] == str(project_id)


def test_caps_are_passed_as_clickhouse_query_settings() -> None:
    built = build_trend_query(_spec(), uuid.uuid4(), uuid.uuid4(), "UTC", _settings())
    assert built.settings == {"max_execution_time": 10, "max_rows_to_read": 1_000_000}
    assert built.parameters["result_limit"] == 500
    assert "LIMIT {result_limit:UInt32}" in built.sql


def test_filters_are_parameterized_never_interpolated() -> None:
    spec = _spec(filters=[{"key": "platform", "op": "eq", "value": "'; DROP TABLE events; --"}])
    built = build_trend_query(spec, uuid.uuid4(), uuid.uuid4(), "UTC", _settings())

    assert "DROP TABLE" not in built.sql
    assert built.parameters["filter_0_key"] == "platform"
    assert built.parameters["filter_0_value"] == "'; DROP TABLE events; --"


@pytest.mark.parametrize(
    ("op", "expected_fragment"),
    [
        (FilterOp.EQ, "properties[{filter_0_key:String}] = {filter_0_value:String}"),
        (FilterOp.NEQ, "properties[{filter_0_key:String}] != {filter_0_value:String}"),
        (
            FilterOp.CONTAINS,
            "position(properties[{filter_0_key:String}], {filter_0_value:String}) > 0",
        ),
    ],
)
def test_each_filter_op_compiles_to_the_expected_sql_fragment(
    op: FilterOp, expected_fragment: str
) -> None:
    spec = _spec(filters=[Filter(key="platform", op=op, value="ios")])
    built = build_trend_query(spec, uuid.uuid4(), uuid.uuid4(), "UTC", _settings())
    assert expected_fragment in built.sql


def test_breakdown_adds_a_grouped_property_column() -> None:
    spec = _spec(breakdown="utm_source")
    built = build_trend_query(spec, uuid.uuid4(), uuid.uuid4(), "UTC", _settings())

    assert "properties[{breakdown_key:String}] AS breakdown" in built.sql
    assert "GROUP BY bucket, breakdown" in built.sql
    assert built.parameters["breakdown_key"] == "utm_source"


def test_property_sum_and_avg_measures_cast_with_or_zero() -> None:
    built = build_trend_query(
        _spec(measure="property_sum:revenue"), uuid.uuid4(), uuid.uuid4(), "UTC", _settings()
    )
    assert "sum(toFloat64OrZero(properties[{measure_property_key:String}]))" in built.sql
    assert built.parameters["measure_property_key"] == "revenue"

    built_avg = build_trend_query(
        _spec(measure="property_avg:revenue"), uuid.uuid4(), uuid.uuid4(), "UTC", _settings()
    )
    assert "avg(toFloat64OrZero(properties[{measure_property_key:String}]))" in built_avg.sql


@pytest.mark.parametrize(
    ("granularity", "expected_fragment"),
    [
        (Granularity.HOUR, "toStartOfHour(timestamp, {tz:String})"),
        (Granularity.DAY, "toStartOfDay(timestamp, {tz:String})"),
        (Granularity.WEEK, "toStartOfWeek(timestamp, 1, {tz:String})"),
        (Granularity.MONTH, "toStartOfMonth(timestamp, {tz:String})"),
    ],
)
def test_each_granularity_compiles_to_the_expected_bucket_function(
    granularity: Granularity, expected_fragment: str
) -> None:
    built = build_trend_query(
        _spec(granularity=granularity), uuid.uuid4(), uuid.uuid4(), "UTC", _settings()
    )
    assert expected_fragment in built.sql


def test_range_bounds_are_converted_to_utc_from_the_project_timezone() -> None:
    spec = _spec(range=DateRange(from_=date(2026, 8, 1), to=date(2026, 8, 1)))
    built = build_trend_query(spec, uuid.uuid4(), uuid.uuid4(), "America/New_York", _settings())

    # 2026-08-01 00:00 America/New_York (UTC-4 in August, DST) is 04:00 UTC;
    # the range is inclusive through the end of 2026-08-01 local, i.e. up to
    # (but excluding) 2026-08-02 04:00 UTC.
    start = built.parameters["range_start"]
    end = built.parameters["range_end"]
    assert start.isoformat() == "2026-08-01T04:00:00+00:00"  # type: ignore[union-attr]
    assert end.isoformat() == "2026-08-02T04:00:00+00:00"  # type: ignore[union-attr]


def _funnel_spec(**overrides: object) -> FunnelSpec:
    payload: dict[str, object] = {
        "kind": "funnel",
        "steps": [
            {"event": "signed up"},
            {"event": "added to cart"},
            {"event": "checkout completed"},
        ],
        "window": {"value": 7, "unit": "day"},
        "range": {"from": "2026-08-01", "to": "2026-08-31"},
    }
    payload.update(overrides)
    return FunnelSpec.model_validate(payload)


def test_window_seconds_converts_hour_and_day_units() -> None:
    assert window_seconds(FunnelWindow(value=2, unit="hour")) == 7200
    assert window_seconds(FunnelWindow(value=7, unit="day")) == 604_800


def test_funnel_org_and_project_are_always_injected_as_parameters() -> None:
    org_id, project_id = uuid.uuid4(), uuid.uuid4()
    built = build_funnel_query(_funnel_spec(), org_id, project_id, "UTC", _settings())

    assert "org_id = {org_id:UUID}" in built.sql
    assert "project_id = {project_id:UUID}" in built.sql
    assert built.parameters["org_id"] == str(org_id)
    assert built.parameters["project_id"] == str(project_id)
    assert built.parameters["events"] == ["signed up", "added to cart", "checkout completed"]


def test_funnel_steps_compile_to_windowfunnel_conditions_in_order() -> None:
    """Ordering enforced: the step conditions must appear in the same order
    as the spec's steps, as positional windowFunnel arguments -- swapping
    two conditions changes which sequence of events counts as progress."""
    built = build_funnel_query(_funnel_spec(), uuid.uuid4(), uuid.uuid4(), "UTC", _settings())

    assert "windowFunnel({window_seconds:UInt64})" in built.sql
    assert (
        "(toDateTime(timestamp), event_name = {step_0_event:String}, "
        "event_name = {step_1_event:String}, event_name = {step_2_event:String})" in built.sql
    )
    assert built.parameters["step_0_event"] == "signed up"
    assert built.parameters["step_1_event"] == "added to cart"
    assert built.parameters["step_2_event"] == "checkout completed"


def test_funnel_window_seconds_is_a_parameter_not_interpolated() -> None:
    built = build_funnel_query(
        _funnel_spec(window={"value": 2, "unit": "hour"}),
        uuid.uuid4(),
        uuid.uuid4(),
        "UTC",
        _settings(),
    )
    assert built.parameters["window_seconds"] == 7200
    assert "{window_seconds:UInt64}" in built.sql


def test_funnel_filters_are_parameterized_never_interpolated() -> None:
    spec = _funnel_spec(
        filters=[{"key": "platform", "op": "eq", "value": "'; DROP TABLE events; --"}]
    )
    built = build_funnel_query(spec, uuid.uuid4(), uuid.uuid4(), "UTC", _settings())

    assert "DROP TABLE" not in built.sql
    assert built.parameters["filter_0_value"] == "'; DROP TABLE events; --"


def test_funnel_breakdown_groups_the_inner_and_outer_query_by_it() -> None:
    spec = _funnel_spec(breakdown="utm_source")
    built = build_funnel_query(spec, uuid.uuid4(), uuid.uuid4(), "UTC", _settings())

    assert "properties[{breakdown_key:String}] AS breakdown" in built.sql
    assert "GROUP BY uid, breakdown" in built.sql
    assert "GROUP BY level, breakdown" in built.sql
    assert built.parameters["breakdown_key"] == "utm_source"


def test_funnel_caps_are_passed_as_clickhouse_query_settings() -> None:
    built = build_funnel_query(_funnel_spec(), uuid.uuid4(), uuid.uuid4(), "UTC", _settings())
    assert built.settings == {"max_execution_time": 10, "max_rows_to_read": 1_000_000}
