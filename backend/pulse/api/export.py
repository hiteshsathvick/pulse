"""Raw event export (SPEC.md's "GET /api/v1/export/events -- streamed
CSV/JSON, scoped"). Reuses resolve_query_scope as-is -- a read API key or a
JWT, exactly the same auth the query routes already accept, so this needed
no new auth code. Streams rows straight from ClickHouse via
query_rows_stream rather than materializing the whole export in memory,
since an export is expected to cover far more rows than an interactive
query ever would. JSON output is newline-delimited (one JSON object per
line), not a single JSON array -- an array needs the whole body buffered to
close its brackets correctly, which would defeat the point of streaming."""

from __future__ import annotations

import csv
import json
import uuid
from collections.abc import AsyncIterator
from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from starlette.responses import StreamingResponse

from pulse.api.dependencies import resolve_query_scope
from pulse.core.config import get_settings
from pulse.query.builder import _utc_bounds
from pulse.repositories.clickhouse import get_client
from pulse.services import projects as projects_service

router = APIRouter(
    prefix="/api/v1/orgs/{org_id}/projects/{project_id}/export",
    tags=["export"],
    dependencies=[Depends(resolve_query_scope)],
)

_EXPORT_COLUMNS = [
    "event_id",
    "event_name",
    "user_id",
    "anonymous_id",
    "timestamp",
    "received_at",
    "properties",
]


def _build_export_query(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    range_start: datetime,
    range_end: datetime,
    event_name: str | None,
    row_cap: int,
) -> tuple[str, dict[str, object]]:
    """Tenant scope and the row cap are always present, never trusted from
    the caller -- the same rule query/builder.py's own tests enforce for
    insight queries. `ORDER BY timestamp` costs a real sort when the range
    spans multiple event names (the table's own physical order is
    (org_id, project_id, event_name, timestamp, ...)), but a chronological
    export is what a data-ownership export is for; bounded by row_cap and
    max_execution_time either way."""
    parameters: dict[str, object] = {
        "org_id": str(org_id),
        "project_id": str(project_id),
        "range_start": range_start,
        "range_end": range_end,
        "row_cap": row_cap,
    }
    where = [
        "org_id = {org_id:UUID}",
        "project_id = {project_id:UUID}",
        "timestamp >= {range_start:DateTime64(3)}",
        "timestamp < {range_end:DateTime64(3)}",
    ]
    if event_name is not None:
        parameters["event_name"] = event_name
        where.append("event_name = {event_name:String}")

    sql = f"""
        SELECT {", ".join(_EXPORT_COLUMNS)}
        FROM events
        WHERE {" AND ".join(where)}
        ORDER BY timestamp
        LIMIT {{row_cap:UInt64}}
    """
    return sql, parameters


async def _stream_rows(
    sql: str, parameters: dict[str, object]
) -> AsyncIterator[tuple[object, ...]]:
    settings = get_settings()
    client = await get_client()
    query_settings = {"max_execution_time": settings.query_max_execution_time_seconds}
    stream = await client.query_rows_stream(sql, parameters=parameters, settings=query_settings)
    async with stream:
        async for row in stream:
            yield row


def _row_to_dict(row: tuple[object, ...]) -> dict[str, object]:
    event_id, event_name, user_id, anonymous_id, timestamp, received_at, properties = row
    assert isinstance(timestamp, datetime)
    assert isinstance(received_at, datetime)
    return {
        "event_id": str(event_id),
        "event_name": event_name,
        "user_id": user_id,
        "anonymous_id": anonymous_id,
        "timestamp": timestamp.isoformat(),
        "received_at": received_at.isoformat(),
        "properties": properties,
    }


async def _ndjson_body(rows: AsyncIterator[tuple[object, ...]]) -> AsyncIterator[bytes]:
    async for row in rows:
        yield (json.dumps(_row_to_dict(row), sort_keys=True) + "\n").encode()


class _EchoWriter:
    """A write-only, non-buffering target for csv.writer -- each writerow()
    call hands back exactly the line it produced, which is yielded straight
    into the response instead of collecting into an in-memory buffer."""

    def write(self, value: str) -> str:
        return value


async def _csv_body(rows: AsyncIterator[tuple[object, ...]]) -> AsyncIterator[bytes]:
    writer = csv.writer(_EchoWriter())
    yield writer.writerow(_EXPORT_COLUMNS).encode()
    async for row in rows:
        record = _row_to_dict(row)
        record["properties"] = json.dumps(record["properties"], sort_keys=True)
        yield writer.writerow(record[col] for col in _EXPORT_COLUMNS).encode()


@router.get("/events")
async def export_events(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    from_: date = Query(..., alias="from"),
    to: date = Query(...),
    event_name: str | None = Query(default=None),
    format: Literal["json", "csv"] = Query(default="json"),
) -> StreamingResponse:
    project = await projects_service.get_project(org_id, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    range_start, range_end = _utc_bounds(from_, to, project.timezone)
    settings = get_settings()
    sql, parameters = _build_export_query(
        org_id, project_id, range_start, range_end, event_name, settings.export_max_rows
    )
    rows = _stream_rows(sql, parameters)

    if format == "csv":
        return StreamingResponse(
            _csv_body(rows),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="events.csv"'},
        )
    return StreamingResponse(
        _ndjson_body(rows),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="events.ndjson"'},
    )
