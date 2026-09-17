import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update

from pulse.models import EventSchema, PropertySchema, PropertyType, SchemaStatus
from pulse.repositories.postgres import session_scope
from pulse.services import audit

PropertyValue = str | float | bool | None

# Process-lifetime caches -- avoid a Postgres round trip for every event in a
# 500-event worker batch when the (project, event) or (event, property) pair
# has already been registered this run. A unique DB constraint on each is the
# real safety net; there's no cross-process race to worry about since Phase 8
# already committed to a single worker process.
_event_cache: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
_property_cache: dict[tuple[uuid.UUID, str], tuple[uuid.UUID, PropertyType]] = {}
_conflict_flagged: set[uuid.UUID] = set()


@dataclass
class RegistryObservation:
    org_id: uuid.UUID
    project_id: uuid.UUID
    event_name: str
    raw_properties: dict[str, PropertyValue]


def _infer_type(value: PropertyValue) -> PropertyType:
    # bool before (int, float): bool is a subclass of int in Python, so the
    # numeric check would otherwise misclassify every boolean as a number.
    if isinstance(value, bool):
        return PropertyType.BOOL
    if isinstance(value, int | float):
        return PropertyType.NUMBER
    try:
        datetime.fromisoformat(value)  # type: ignore[arg-type]
        return PropertyType.DATETIME
    except ValueError:
        return PropertyType.STRING


async def register_batch(observations: list[RegistryObservation]) -> None:
    """Called once per worker batch, after a successful ClickHouse insert
    (SPEC.md #6.6) -- never before it, and never allowed to affect whether
    that insert's entries get acked. Bumps volume_estimate once per distinct
    event name in this batch (grouped), not once per event."""
    if not observations:
        return

    volume_by_event: dict[tuple[uuid.UUID, uuid.UUID, str], int] = {}

    for obs in observations:
        key = (obs.org_id, obs.project_id, obs.event_name)
        volume_by_event[key] = volume_by_event.get(key, 0) + 1

        event_schema_id = await _get_or_create_event_schema(
            obs.org_id, obs.project_id, obs.event_name
        )
        for prop_key, value in obs.raw_properties.items():
            if value is None:
                continue
            await _get_or_create_property_schema(
                obs.org_id, event_schema_id, prop_key, _infer_type(value)
            )

    for (org_id, project_id, event_name), count in volume_by_event.items():
        event_schema_id = _event_cache[(project_id, event_name)]
        await _bump_volume(org_id, event_schema_id, count)


async def _get_or_create_event_schema(
    org_id: uuid.UUID, project_id: uuid.UUID, event_name: str
) -> uuid.UUID:
    cache_key = (project_id, event_name)
    cached = _event_cache.get(cache_key)
    if cached is not None:
        return cached

    async with session_scope(org_id=org_id) as session:
        existing = await session.scalar(
            select(EventSchema).where(
                EventSchema.project_id == project_id, EventSchema.event_name == event_name
            )
        )
        if existing is None:
            existing = EventSchema(
                org_id=org_id,
                project_id=project_id,
                event_name=event_name,
                first_seen_at=datetime.now(UTC),
            )
            session.add(existing)
            await session.flush()
        await session.commit()
        event_schema_id = existing.id

    _event_cache[cache_key] = event_schema_id
    return event_schema_id


async def _get_or_create_property_schema(
    org_id: uuid.UUID, event_schema_id: uuid.UUID, key: str, inferred_type: PropertyType
) -> None:
    cache_key = (event_schema_id, key)
    cached = _property_cache.get(cache_key)

    if cached is not None:
        property_schema_id, registered_type = cached
        if registered_type != inferred_type and property_schema_id not in _conflict_flagged:
            await _flag_conflict(org_id, property_schema_id)
        return

    async with session_scope(org_id=org_id) as session:
        existing = await session.scalar(
            select(PropertySchema).where(
                PropertySchema.event_schema_id == event_schema_id, PropertySchema.key == key
            )
        )
        if existing is None:
            existing = PropertySchema(
                org_id=org_id,
                event_schema_id=event_schema_id,
                key=key,
                inferred_type=inferred_type,
            )
            session.add(existing)
            await session.flush()
        elif existing.inferred_type != inferred_type and existing.type_conflict_detected_at is None:
            existing.type_conflict_detected_at = datetime.now(UTC)
            _conflict_flagged.add(existing.id)
        await session.commit()
        registered = (existing.id, existing.inferred_type)

    _property_cache[cache_key] = registered


async def _flag_conflict(org_id: uuid.UUID, property_schema_id: uuid.UUID) -> None:
    async with session_scope(org_id=org_id) as session:
        prop = await session.get(PropertySchema, property_schema_id)
        if prop is not None and prop.type_conflict_detected_at is None:
            prop.type_conflict_detected_at = datetime.now(UTC)
            await session.commit()
    _conflict_flagged.add(property_schema_id)


async def _bump_volume(org_id: uuid.UUID, event_schema_id: uuid.UUID, count: int) -> None:
    async with session_scope(org_id=org_id) as session:
        await session.execute(
            update(EventSchema)
            .where(EventSchema.id == event_schema_id)
            .values(volume_estimate=EventSchema.volume_estimate + count)
        )
        await session.commit()


# -- API-facing reads/writes (pulse/api/schema_registry.py) --


async def list_events(org_id: uuid.UUID, project_id: uuid.UUID) -> list[EventSchema]:
    async with session_scope(org_id=org_id) as session:
        result = await session.execute(
            select(EventSchema)
            .where(EventSchema.project_id == project_id)
            .order_by(EventSchema.event_name)
        )
        return list(result.scalars().all())


async def list_properties(org_id: uuid.UUID, event_schema_id: uuid.UUID) -> list[PropertySchema]:
    async with session_scope(org_id=org_id) as session:
        result = await session.execute(
            select(PropertySchema)
            .where(PropertySchema.event_schema_id == event_schema_id)
            .order_by(PropertySchema.key)
        )
        return list(result.scalars().all())


async def get_event(org_id: uuid.UUID, event_schema_id: uuid.UUID) -> EventSchema | None:
    async with session_scope(org_id=org_id) as session:
        event = await session.get(EventSchema, event_schema_id)
        if event is None or event.org_id != org_id:
            return None
        return event


async def get_property(org_id: uuid.UUID, property_schema_id: uuid.UUID) -> PropertySchema | None:
    async with session_scope(org_id=org_id) as session:
        prop = await session.get(PropertySchema, property_schema_id)
        if prop is None or prop.org_id != org_id:
            return None
        return prop


async def update_event_status(
    org_id: uuid.UUID, event_schema_id: uuid.UUID, status: SchemaStatus, actor_id: uuid.UUID
) -> EventSchema | None:
    async with session_scope(org_id=org_id) as session:
        event = await session.get(EventSchema, event_schema_id)
        if event is None or event.org_id != org_id:
            return None
        event.status = status
        audit.record(
            session,
            org_id=org_id,
            actor_id=actor_id,
            action="event_schema.status_updated",
            target=str(event_schema_id),
            metadata={"status": status.value},
        )
        await session.commit()
        return event


async def update_property(
    org_id: uuid.UUID,
    property_schema_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    status: SchemaStatus | None = None,
    is_pii: bool | None = None,
) -> PropertySchema | None:
    async with session_scope(org_id=org_id) as session:
        prop = await session.get(PropertySchema, property_schema_id)
        if prop is None or prop.org_id != org_id:
            return None
        if status is not None:
            prop.status = status
        if is_pii is not None:
            prop.is_pii = is_pii
        audit.record(
            session,
            org_id=org_id,
            actor_id=actor_id,
            action="property_schema.updated",
            target=str(property_schema_id),
            metadata={"status": status.value if status else None, "is_pii": is_pii},
        )
        await session.commit()
        return prop
