import uuid

from clickhouse_connect.driver.asyncclient import AsyncClient

_COLUMNS = (
    "org_id",
    "project_id",
    "event_id",
    "event_name",
    "user_id",
    "anonymous_id",
    "timestamp",
    "received_at",
    "properties",
    "_ingest_batch",
)


async def insert_events(client: AsyncClient, events: list[dict[str, object]]) -> None:
    """Large batched inserts only, per SPEC.md #6/CLAUDE.md -- this takes a
    whole batch, never a single event. Real batching-by-size/time is Phase
    8's ingestion worker; this is the raw insert primitive underneath it."""
    if not events:
        return
    rows = [[event[column] for column in _COLUMNS] for event in events]
    await client.insert("events", rows, column_names=list(_COLUMNS))


async def query_events(
    client: AsyncClient, org_id: uuid.UUID, project_id: uuid.UUID, *, limit: int = 100
) -> list[dict[str, object]]:
    """org_id/project_id are required parameters, not optional filters --
    SPEC.md #3's "tenant scope is injected, never trusted" invariant, held
    structurally even in this preliminary round-trip helper (the real query
    engine is Phase 11). Parameterized, not string-built SQL."""
    result = await client.query(
        f"""
        SELECT {", ".join(_COLUMNS)}
        FROM events
        WHERE org_id = {{org_id:UUID}} AND project_id = {{project_id:UUID}}
        ORDER BY timestamp
        LIMIT {{limit:UInt32}}
        """,
        parameters={"org_id": str(org_id), "project_id": str(project_id), "limit": limit},
    )
    return [dict(zip(_COLUMNS, row, strict=True)) for row in result.result_rows]
