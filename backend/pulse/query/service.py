import uuid
from dataclasses import dataclass
from datetime import datetime

from pulse.core.config import get_settings
from pulse.query import builder, cache
from pulse.query.spec import TrendSpec
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
