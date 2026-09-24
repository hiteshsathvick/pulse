from redis.asyncio import Redis
from redis.exceptions import ResponseError

StreamEntry = tuple[bytes, dict[bytes, bytes]]


async def ensure_consumer_group(client: Redis, stream_key: str, group: str) -> None:
    """Idempotent, like alembic/env.py's _ensure_app_role_exists. id="0" (not
    "$") so a freshly-created group processes the stream's existing backlog
    too -- events already sitting in the stream from before this worker ever
    ran must still become queryable, not be silently skipped."""
    try:
        await client.xgroup_create(stream_key, group, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def read_batch(
    client: Redis, stream_key: str, group: str, consumer: str, *, count: int, block_ms: int
) -> list[StreamEntry]:
    """Reclaims this consumer's own still-pending (unacked) entries first --
    the crash-recovery path: a worker that died after XREADGROUP but before
    XACK left entries in its own pending-entries list, and a restart using
    the same fixed consumer name (SPEC.md #6.4-adjacent design) sees them
    again here before anything new. Only reads new entries (id=">"), with
    BLOCK+COUNT giving both the size and time trigger in one call, once the
    pending backlog for this consumer is empty."""
    pending = await client.xreadgroup(group, consumer, {stream_key: "0"}, count=count)
    entries = _flatten(pending)
    if entries:
        return entries

    fresh = await client.xreadgroup(group, consumer, {stream_key: ">"}, count=count, block=block_ms)
    return _flatten(fresh)


def _flatten(result: object) -> list[StreamEntry]:
    if not result:
        return []
    # redis-py: [(stream_name, [(entry_id, fields), ...])]
    _, entries = result[0]  # type: ignore[index]
    return list(entries)


async def ack(client: Redis, stream_key: str, group: str, entry_ids: list[bytes]) -> None:
    """Acknowledges AND deletes. XACK alone only clears the entry from the
    group's pending list -- the entry itself stays in the stream forever, so
    an un-trimmed ingest stream grows without bound (found by Phase 23's
    stream-length gauge: 5,000+ entries with zero pending and zero lag). That
    matters because the deployed Redis runs `noeviction` (the stream is the
    only copy of an un-landed event, so it must never be evicted): once memory
    filled, /ingest would fail permanently.

    Deleting on ack is safe because an acked entry is, by construction,
    already landed in ClickHouse (or routed to the DLQ) and in the raw
    archive -- process_batch only acks after all of that. It assumes a single
    consumer group, which is the design (one group, many consumers): a second
    group would need its own retention story. One MULTI/EXEC round trip, so
    an entry is never acked-but-undeleted or deleted-but-unacked."""
    if not entry_ids:
        return
    pipeline = client.pipeline(transaction=True)
    pipeline.xack(stream_key, group, *entry_ids)
    pipeline.xdel(stream_key, *entry_ids)
    await pipeline.execute()
