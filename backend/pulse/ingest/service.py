import json
from datetime import UTC, datetime

from redis.asyncio import Redis

from pulse.ingest.schemas import IngestEvent
from pulse.models import ApiKey


def _stream_fields(api_key: ApiKey, event: IngestEvent, received_at: datetime) -> dict[str, str]:
    """org_id/project_id come from the resolved write key, never from the
    request body -- SPEC.md #3's "tenant scope is injected, never trusted"
    invariant, applied here even though the client-facing schema never gives
    it anything tenant-shaped to supply in the first place."""
    return {
        "org_id": str(api_key.org_id),
        "project_id": str(api_key.project_id),
        "event_id": str(event.event_id),
        "event_name": event.event,
        "user_id": event.user_id or "",
        "anonymous_id": event.anonymous_id or "",
        "timestamp": (event.timestamp or received_at).isoformat(),
        "received_at": received_at.isoformat(),
        "properties": json.dumps(event.properties or {}),
    }


async def buffer_batch(
    client: Redis, stream_key: str, api_key: ApiKey, events: list[IngestEvent]
) -> int:
    """Buffers a batch -- one Streams entry per event, not one per HTTP
    batch, so Phase 8's consumer-group workers can XREADGROUP a batch of
    entries directly. Pipelined so the whole HTTP batch is one round trip;
    this is buffering, not a transaction -- durability is the stream's job
    once XADD returns, per SPEC.md #6's "the Ingest API returns 202 the
    instant the batch is buffered"."""
    received_at = datetime.now(UTC)
    pipeline = client.pipeline(transaction=False)
    for event in events:
        # redis-py's xadd stub takes Dict[FieldT, EncodableT] (a union of
        # several scalar types) rather than Mapping -- Dict is invariant, so
        # our concrete dict[str, str] doesn't satisfy it even though every
        # value we pass is a valid EncodableT.
        pipeline.xadd(stream_key, _stream_fields(api_key, event, received_at))  # type: ignore[arg-type]
    await pipeline.execute()
    return len(events)
