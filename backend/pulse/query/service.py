import uuid
from dataclasses import dataclass
from datetime import datetime

from pulse.core.config import get_settings
from pulse.query import builder, cache
from pulse.query.spec import FunnelSpec, TrendSpec
from pulse.repositories import clickhouse
from pulse.repositories.redis import get_client as get_redis_client
from pulse.services import projects as projects_service


class ProjectNotFound(Exception):
    pass


@dataclass
class TrendResult:
    results: list[dict[str, object]]
    cached: bool


async def run_trend(spec: TrendSpec, org_id: uuid.UUID, project_id: uuid.UUID) -> TrendResult:
    project = await projects_service.get_project(org_id, project_id)
    if project is None:
        raise ProjectNotFound()

    settings = get_settings()
    redis_client = get_redis_client()
    key = cache.cache_key(spec, org_id, project_id)

    cached = await cache.get_cached(redis_client, key)
    if cached is not None:
        return TrendResult(results=cached, cached=True)

    built = builder.build_trend_query(spec, org_id, project_id, project.timezone, settings)
    ch_client = await clickhouse.get_client()
    query_result = await ch_client.query(
        built.sql, parameters=built.parameters, settings=built.settings
    )

    columns = query_result.column_names
    rows = [
        _normalize_row(dict(zip(columns, row, strict=True))) for row in query_result.result_rows
    ]

    await cache.set_cached(redis_client, key, rows, settings.query_cache_ttl_seconds)
    return TrendResult(results=rows, cached=False)


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


async def run_funnel(spec: FunnelSpec, org_id: uuid.UUID, project_id: uuid.UUID) -> FunnelResult:
    project = await projects_service.get_project(org_id, project_id)
    if project is None:
        raise ProjectNotFound()

    settings = get_settings()
    redis_client = get_redis_client()
    key = cache.cache_key(spec, org_id, project_id)

    cached = await cache.get_cached(redis_client, key)
    if cached is not None:
        return FunnelResult(results=cached, cached=True)

    built = builder.build_funnel_query(spec, org_id, project_id, project.timezone, settings)
    ch_client = await clickhouse.get_client()
    query_result = await ch_client.query(
        built.sql, parameters=built.parameters, settings=built.settings
    )
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
