import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from clickhouse_connect.driver.asyncclient import AsyncClient
from redis.asyncio import Redis

from pulse.events.repository import insert_events
from pulse.repositories import object_storage
from pulse.worker.consumer import StreamEntry, ack

PropertyValue = str | float | bool | None


class PoisonEvent(Exception):
    """A stream entry that fails to parse -- corrupted, or produced by
    something other than Phase 7's own validated /ingest (defense-in-depth;
    this should never happen from our own producer, but a poison event must
    still be routed to the DLQ rather than crashing the worker or being
    silently dropped, per SPEC.md invariant #5)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class ParsedEvent:
    org_id: uuid.UUID
    project_id: uuid.UUID
    event_id: uuid.UUID
    event_name: str
    user_id: str
    anonymous_id: str
    timestamp: datetime
    received_at: datetime
    properties: dict[str, str]


@dataclass
class BatchResult:
    processed: int = 0
    duplicates: int = 0
    poisoned: int = 0
    acked: int = 0
    ingest_batch: uuid.UUID | None = None


def _decode(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _decode_fields(fields: dict[bytes, bytes]) -> dict[str, str]:
    return {_decode(k): _decode(v) for k, v in fields.items()}


def _stringify_properties(properties: dict[str, PropertyValue]) -> dict[str, str]:
    """Matches Phase 6's fixture convention exactly (events/fixtures.py):
    plain str() for numbers, lowercase true/false for bool (Python's own
    str(True) == "True" would silently break a later `= 'true'`-style
    query), and a property with a null value is dropped -- Map(String,String)
    has no null representation, so an absent key is the natural encoding."""
    result: dict[str, str] = {}
    for key, value in properties.items():
        if value is None:
            continue
        if isinstance(value, bool):
            result[key] = "true" if value else "false"
        else:
            result[key] = str(value)
    return result


def parse_stream_entry(fields: dict[bytes, bytes]) -> ParsedEvent:
    try:
        decoded = _decode_fields(fields)
        raw_properties = json.loads(decoded["properties"]) if decoded.get("properties") else {}
        return ParsedEvent(
            org_id=uuid.UUID(decoded["org_id"]),
            project_id=uuid.UUID(decoded["project_id"]),
            event_id=uuid.UUID(decoded["event_id"]),
            event_name=decoded["event_name"],
            user_id=decoded.get("user_id", ""),
            anonymous_id=decoded.get("anonymous_id", ""),
            timestamp=datetime.fromisoformat(decoded["timestamp"]),
            received_at=datetime.fromisoformat(decoded["received_at"]),
            properties=_stringify_properties(raw_properties),
        )
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise PoisonEvent(str(exc)) from exc


def to_clickhouse_row(parsed: ParsedEvent, ingest_batch: uuid.UUID) -> dict[str, object]:
    return {
        "org_id": parsed.org_id,
        "project_id": parsed.project_id,
        "event_id": parsed.event_id,
        "event_name": parsed.event_name,
        "user_id": parsed.user_id,
        "anonymous_id": parsed.anonymous_id,
        "timestamp": parsed.timestamp,
        "received_at": parsed.received_at,
        "properties": parsed.properties,
        "_ingest_batch": ingest_batch,
    }


def _dedup_key(event_id: uuid.UUID) -> str:
    return f"ingest:seen:{event_id}"


async def is_duplicate(redis_client: Redis, event_id: uuid.UUID) -> bool:
    return bool(await redis_client.exists(_dedup_key(event_id)))


async def mark_seen(redis_client: Redis, event_ids: list[uuid.UUID], ttl_seconds: int) -> None:
    """Marked only *after* a successful insert (see process_batch) -- marking
    first would risk silently skipping a genuinely-not-yet-inserted event if
    the worker crashed in between. Being late here means an unlucky crash can
    cause one duplicate ClickHouse row instead of a lost event; Phase 6's
    ReplacingMergeTree is the documented backstop for exactly that."""
    if not event_ids:
        return
    pipeline = redis_client.pipeline(transaction=False)
    for event_id in event_ids:
        pipeline.set(_dedup_key(event_id), "1", ex=ttl_seconds)
    await pipeline.execute()


async def push_to_dlq(
    redis_client: Redis, dlq_stream_key: str, fields: dict[bytes, bytes], reason: str
) -> None:
    entry = _decode_fields(fields)
    entry["error"] = reason
    entry["failed_at"] = datetime.now(UTC).isoformat()
    await redis_client.xadd(dlq_stream_key, entry)  # type: ignore[arg-type]


async def archive_batch(ingest_batch: uuid.UUID, raw_events: list[dict[str, str]]) -> None:
    """Archives the *entire* raw batch -- good and poison entries alike --
    as one JSON object, per SPEC.md #6.7's "raw event archive (replay/backfill
    source of truth)". Separate from the DLQ, which exists for operator
    visibility into bad entries specifically, not as the archive itself."""
    if not raw_events:
        return
    payload = json.dumps(raw_events).encode()
    key = f"raw/{datetime.now(UTC):%Y/%m/%d}/{ingest_batch}.json"
    await object_storage.put_object(key, payload)


async def process_batch(
    *,
    redis_client: Redis,
    clickhouse_client: AsyncClient,
    entries: list[StreamEntry],
    stream_key: str,
    group: str,
    dlq_stream_key: str,
    dedup_ttl_seconds: int,
) -> BatchResult:
    """One worker batch, per SPEC.md #6.4-adjacent Phase 8 design: parse (a
    parse failure -> DLQ) -> dedup-check (read-only) to filter -> bulk
    ClickHouse insert of the survivors -> archive the raw batch -> mark the
    freshly-inserted ids seen -> ack everything. If the insert raises, this
    function raises too, *before* marking-seen or acking -- the caller (the
    main loop) logs it and leaves the batch pending for the next cycle. That
    is the backpressure behavior: nothing is lost, nothing is acked early."""
    result = BatchResult()
    if not entries:
        return result

    ingest_batch = uuid.uuid4()
    result.ingest_batch = ingest_batch

    good_rows: list[dict[str, object]] = []
    fresh_event_ids: list[uuid.UUID] = []
    ack_ids: list[bytes] = []
    raw_archive: list[dict[str, str]] = []

    for entry_id, fields in entries:
        ack_ids.append(entry_id)
        raw_archive.append(_decode_fields(fields))

        try:
            parsed = parse_stream_entry(fields)
        except PoisonEvent as exc:
            await push_to_dlq(redis_client, dlq_stream_key, fields, exc.reason)
            result.poisoned += 1
            continue

        if await is_duplicate(redis_client, parsed.event_id):
            result.duplicates += 1
            continue

        good_rows.append(to_clickhouse_row(parsed, ingest_batch))
        fresh_event_ids.append(parsed.event_id)

    if good_rows:
        await insert_events(clickhouse_client, good_rows)
        result.processed = len(good_rows)

    await archive_batch(ingest_batch, raw_archive)
    await mark_seen(redis_client, fresh_event_ids, dedup_ttl_seconds)
    await ack(redis_client, stream_key, group, ack_ids)
    result.acked = len(ack_ids)
    return result
