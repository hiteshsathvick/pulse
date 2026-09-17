import random
import uuid
from datetime import UTC, datetime

_EVENT_NAMES = (
    "page viewed",
    "signed up",
    "added to cart",
    "checkout completed",
    "button clicked",
)
_PLATFORMS = ("web", "ios", "android")


def generate_fake_event(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    event_id: uuid.UUID | None = None,
    event_name: str | None = None,
    user_id: str | None = None,
    timestamp: datetime | None = None,
    properties: dict[str, str] | None = None,
    ingest_batch: uuid.UUID | None = None,
) -> dict[str, object]:
    """A single realistic fake event, shaped exactly like the `events` table
    (SPEC.md #5.1). `properties` values are strings, matching the
    `Map(String, String)` column -- typed reads happen via casting at query
    time, not at write time."""
    now = datetime.now(UTC)
    return {
        "org_id": org_id,
        "project_id": project_id,
        "event_id": event_id or uuid.uuid4(),
        "event_name": event_name or random.choice(_EVENT_NAMES),
        "user_id": user_id or f"user_{random.randint(1, 1000)}",
        "anonymous_id": str(uuid.uuid4()),
        "timestamp": timestamp or now,
        "received_at": now,
        "properties": properties or {"platform": random.choice(_PLATFORMS)},
        "_ingest_batch": ingest_batch or uuid.uuid4(),
    }


def generate_fake_events(
    org_id: uuid.UUID, project_id: uuid.UUID, count: int
) -> list[dict[str, object]]:
    """A batch sharing one _ingest_batch id, like a real ingest worker's
    batch (Phase 8) would produce."""
    batch_id = uuid.uuid4()
    return [generate_fake_event(org_id, project_id, ingest_batch=batch_id) for _ in range(count)]
