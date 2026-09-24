import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import clickhouse_connect
import pytest

from alembic import command
from alembic.config import Config
from pulse.clickhouse_migrations.runner import migrate
from pulse.core.config import get_settings
from pulse.events.repository import query_events
from pulse.models import PropertyType, User
from pulse.registry.service import list_events, list_properties
from pulse.repositories import object_storage
from pulse.repositories.clickhouse import get_client as get_clickhouse_client
from pulse.repositories.postgres import session_scope
from pulse.repositories.redis import get_client as get_redis_client
from pulse.services import orgs as orgs_service
from pulse.services import projects as projects_service
from pulse.worker.consumer import ensure_consumer_group, read_batch
from pulse.worker.processing import process_batch
from tests.clickhouse_schema import drop_event_schema

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_MIGRATIONS_DIR = _BACKEND_ROOT / "pulse" / "clickhouse_migrations" / "migrations"


def _alembic_config() -> Config:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return config


@pytest.fixture(scope="module", autouse=True)
def _control_plane_schema():
    config = _alembic_config()
    command.upgrade(config, "head")
    yield
    command.downgrade(config, "base")


async def _create_org_and_project() -> tuple[uuid.UUID, uuid.UUID]:
    async with session_scope() as session:
        user = User(
            email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="not-a-real-hash",
            name="Owner",
        )
        session.add(user)
        await session.commit()

    org = await orgs_service.create_organization(
        "Worker Registry Org", f"worker-registry-{uuid.uuid4().hex[:8]}", user.id
    )
    project = await projects_service.create_project(org.id, "Web", "web", "UTC", user.id)
    return org.id, project.id


async def _with_fresh_clickhouse_client(body) -> None:
    """Same short-lived-client pattern as test_events_clickhouse.py /
    test_ingest_api.py -- a module-scoped fixture runs on a different event
    loop than the per-test-function ones, so it must not touch the shared
    get_client() singleton."""
    settings = get_settings()
    client = await clickhouse_connect.get_async_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        database=settings.clickhouse_database,
        secure=settings.clickhouse_secure,
    )
    try:
        await body(client)
    finally:
        await client.close()


@pytest.fixture(scope="module", autouse=True)
def _events_table():
    asyncio.run(_with_fresh_clickhouse_client(lambda client: migrate(client, _MIGRATIONS_DIR)))
    yield

    async def _teardown(client) -> None:
        await drop_event_schema(client)

    asyncio.run(_with_fresh_clickhouse_client(_teardown))


@pytest.fixture(scope="module", autouse=True)
def _bucket():
    """Minio's client is sync (no event-loop binding), so unlike the
    ClickHouse/Postgres fixtures above, ensure_bucket() is safe to call
    module-scoped without a cross-loop hazard."""
    asyncio.run(object_storage.ensure_bucket())


def _stream_key() -> str:
    return f"test:worker:stream:{uuid.uuid4().hex[:8]}"


def _group() -> str:
    return f"test-group-{uuid.uuid4().hex[:8]}"


def _raw_fields(**overrides: str) -> dict[str, str]:
    now = datetime.now(UTC).isoformat()
    fields = {
        "org_id": str(uuid.uuid4()),
        "project_id": str(uuid.uuid4()),
        "event_id": str(uuid.uuid4()),
        "event_name": "button clicked",
        "user_id": "user_1",
        "anonymous_id": "",
        "timestamp": now,
        "received_at": now,
        "properties": json.dumps({"platform": "web"}),
    }
    fields.update(overrides)
    return fields


async def _process(stream_key: str, group: str, entries) -> object:
    settings = get_settings()
    return await process_batch(
        redis_client=get_redis_client(),
        clickhouse_client=await get_clickhouse_client(),
        entries=entries,
        stream_key=stream_key,
        group=group,
        dlq_stream_key=f"{stream_key}:dlq",
        dedup_ttl_seconds=settings.worker_dedup_ttl_seconds,
    )


async def test_idempotent_reprocessing_does_not_double_count() -> None:
    """DoD: idempotent re-consume -- the same physical entry processed twice
    (as a redelivery after a crash would produce) must not double-count."""
    stream_key, group = _stream_key(), _group()
    redis_client = get_redis_client()
    await ensure_consumer_group(redis_client, stream_key, group)

    org_id, project_id = uuid.uuid4(), uuid.uuid4()
    fields = _raw_fields(org_id=str(org_id), project_id=str(project_id))
    entry = (b"1-1", {k.encode(): v.encode() for k, v in fields.items()})

    for _ in range(2):
        await _process(stream_key, group, [entry])

    ch_client = await get_clickhouse_client()
    await ch_client.command("OPTIMIZE TABLE events FINAL")
    rows = await query_events(ch_client, org_id, project_id)
    assert len(rows) == 1


async def test_poison_event_goes_to_dlq_and_is_acked_not_dropped() -> None:
    """DoD: poison -> DLQ, never a silent drop."""
    stream_key, group = _stream_key(), _group()
    dlq_key = f"{stream_key}:dlq"
    redis_client = get_redis_client()
    await ensure_consumer_group(redis_client, stream_key, group)

    await redis_client.xadd(stream_key, _raw_fields(properties="not valid json"))
    entries = await read_batch(redis_client, stream_key, group, "c1", count=10, block_ms=100)
    assert len(entries) == 1

    result = await _process(stream_key, group, entries)

    assert result.poisoned == 1
    assert result.processed == 0

    dlq_entries = await redis_client.xrange(dlq_key)
    assert len(dlq_entries) == 1
    _, dlq_fields = dlq_entries[0]
    assert b"error" in dlq_fields
    assert b"failed_at" in dlq_fields

    pending = await redis_client.xpending(stream_key, group)
    assert pending["pending"] == 0


async def test_worker_crash_mid_batch_is_recovered_without_loss_or_dup() -> None:
    """DoD: worker crash mid-batch -> no loss, no dup. Simulates a crash by
    reading via XREADGROUP directly and never acking -- the entry sits in
    the consumer's PEL exactly as it would after a real process death."""
    stream_key, group = _stream_key(), _group()
    redis_client = get_redis_client()
    settings = get_settings()
    await ensure_consumer_group(redis_client, stream_key, group)

    org_id, project_id = uuid.uuid4(), uuid.uuid4()
    await redis_client.xadd(stream_key, _raw_fields(org_id=str(org_id), project_id=str(project_id)))

    crashed_read = await redis_client.xreadgroup(
        group, settings.worker_consumer_name, {stream_key: ">"}, count=10
    )
    assert crashed_read, "the 'crashed' worker must have actually read the entry once"

    # Restart: the real read cycle reclaims this consumer's own pending
    # entries before looking for anything new.
    entries = await read_batch(
        redis_client, stream_key, group, settings.worker_consumer_name, count=10, block_ms=100
    )
    assert len(entries) == 1

    result = await _process(stream_key, group, entries)
    assert result.processed == 1
    assert result.acked == 1

    pending = await redis_client.xpending(stream_key, group)
    assert pending["pending"] == 0

    ch_client = await get_clickhouse_client()
    rows = await query_events(ch_client, org_id, project_id)
    assert len(rows) == 1


async def test_clickhouse_failure_leaves_batch_pending_not_lost() -> None:
    """DoD: backpressure -- a ClickHouse outage must not lose or ack data;
    the batch stays pending and succeeds once the store recovers."""
    stream_key, group = _stream_key(), _group()
    dlq_key = f"{stream_key}:dlq"
    redis_client = get_redis_client()
    settings = get_settings()
    await ensure_consumer_group(redis_client, stream_key, group)

    org_id, project_id = uuid.uuid4(), uuid.uuid4()
    await redis_client.xadd(stream_key, _raw_fields(org_id=str(org_id), project_id=str(project_id)))
    entries = await read_batch(redis_client, stream_key, group, "c1", count=10, block_ms=100)

    class _BrokenClickHouse:
        async def insert(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("simulated ClickHouse outage")

    with pytest.raises(RuntimeError):
        await process_batch(
            redis_client=redis_client,
            clickhouse_client=_BrokenClickHouse(),  # type: ignore[arg-type]
            entries=entries,
            stream_key=stream_key,
            group=group,
            dlq_stream_key=dlq_key,
            dedup_ttl_seconds=settings.worker_dedup_ttl_seconds,
        )

    pending = await redis_client.xpending(stream_key, group)
    assert pending["pending"] == 1, "a failed batch must not be acked"

    entries_again = await read_batch(redis_client, stream_key, group, "c1", count=10, block_ms=100)
    result = await _process(stream_key, group, entries_again)
    assert result.processed == 1

    ch_client = await get_clickhouse_client()
    rows = await query_events(ch_client, org_id, project_id)
    assert len(rows) == 1, "recovery must not have also double-inserted"


async def test_batch_shares_one_ingest_batch_id_and_a_new_cycle_gets_a_new_one() -> None:
    stream_key, group = _stream_key(), _group()
    redis_client = get_redis_client()
    await ensure_consumer_group(redis_client, stream_key, group)

    org_id, project_id = uuid.uuid4(), uuid.uuid4()
    for _ in range(2):
        await redis_client.xadd(
            stream_key, _raw_fields(org_id=str(org_id), project_id=str(project_id))
        )
    entries = await read_batch(redis_client, stream_key, group, "c1", count=10, block_ms=100)
    assert len(entries) == 2

    result = await _process(stream_key, group, entries)

    ch_client = await get_clickhouse_client()
    rows = await query_events(ch_client, org_id, project_id)
    assert len(rows) == 2
    assert {row["_ingest_batch"] for row in rows} == {result.ingest_batch}

    await redis_client.xadd(stream_key, _raw_fields(org_id=str(org_id), project_id=str(project_id)))
    more_entries = await read_batch(redis_client, stream_key, group, "c1", count=10, block_ms=100)
    result2 = await _process(stream_key, group, more_entries)
    assert result2.ingest_batch != result.ingest_batch


async def test_batch_is_archived_to_object_storage() -> None:
    stream_key, group = _stream_key(), _group()
    redis_client = get_redis_client()
    settings = get_settings()
    await ensure_consumer_group(redis_client, stream_key, group)
    await object_storage.ensure_bucket()

    event_id = uuid.uuid4()
    await redis_client.xadd(stream_key, _raw_fields(event_id=str(event_id)))
    entries = await read_batch(redis_client, stream_key, group, "c1", count=10, block_ms=100)

    result = await _process(stream_key, group, entries)

    key = f"raw/{datetime.now(UTC):%Y/%m/%d}/{result.ingest_batch}.json"
    client = object_storage.get_client()
    response = await asyncio.to_thread(client.get_object, settings.s3_bucket, key)
    try:
        content = json.loads(response.read())
    finally:
        response.close()
        response.release_conn()

    assert len(content) == 1
    assert content[0]["event_id"] == str(event_id)


async def test_worker_auto_registers_a_new_event_through_the_real_pipeline() -> None:
    """DoD: new event auto-registers -- exercised through the actual worker
    path (XADD -> read_batch -> process_batch), not just the registry
    service directly, against a real org/project so the FK-backed insert
    genuinely succeeds."""
    stream_key, group = _stream_key(), _group()
    redis_client = get_redis_client()
    await ensure_consumer_group(redis_client, stream_key, group)

    org_id, project_id = await _create_org_and_project()
    await redis_client.xadd(
        stream_key,
        _raw_fields(
            org_id=str(org_id),
            project_id=str(project_id),
            event_name="checkout completed",
            properties=json.dumps({"revenue": 42.5, "platform": "web"}),
        ),
    )
    entries = await read_batch(redis_client, stream_key, group, "c1", count=10, block_ms=100)

    result = await _process(stream_key, group, entries)
    assert result.processed == 1

    events = await list_events(org_id, project_id)
    assert len(events) == 1
    assert events[0].event_name == "checkout completed"

    properties = {p.key: p.inferred_type for p in await list_properties(org_id, events[0].id)}
    assert properties["revenue"] == PropertyType.NUMBER
    assert properties["platform"] == PropertyType.STRING


async def test_broken_registry_does_not_block_insert_archive_or_ack() -> None:
    """The new risk Phase 9 introduces: a registry write failure (here, a
    genuine Postgres FK violation -- org_id/project_id were never created as
    real control-plane rows) must never cost the ClickHouse insert, the
    archive, or the ack. Best-effort is only meaningful if it holds under a
    real failure, not just in the happy path."""
    stream_key, group = _stream_key(), _group()
    redis_client = get_redis_client()
    await ensure_consumer_group(redis_client, stream_key, group)

    org_id, project_id = uuid.uuid4(), uuid.uuid4()  # deliberately not real rows
    await redis_client.xadd(stream_key, _raw_fields(org_id=str(org_id), project_id=str(project_id)))
    entries = await read_batch(redis_client, stream_key, group, "c1", count=10, block_ms=100)

    result = await _process(stream_key, group, entries)
    assert result.processed == 1
    assert result.acked == 1

    ch_client = await get_clickhouse_client()
    rows = await query_events(ch_client, org_id, project_id)
    assert len(rows) == 1, "ClickHouse insert must succeed even though the registry write failed"

    pending = await redis_client.xpending(stream_key, group)
    assert pending["pending"] == 0


async def test_acked_entries_are_deleted_so_the_ingest_stream_does_not_grow_without_bound() -> None:
    """Found by Phase 23's stream-length gauge: XACK alone leaves every
    processed entry in the stream forever (5,000+ entries, zero pending, zero
    lag on a dev Redis). With the deployed `noeviction` policy that is a slow
    outage -- so an acked entry must be gone, and an *unacked* one must not be
    (deleting early would lose an un-landed event)."""
    stream_key, group = _stream_key(), _group()
    redis_client = get_redis_client()
    await ensure_consumer_group(redis_client, stream_key, group)

    org_id, project_id = uuid.uuid4(), uuid.uuid4()
    for _ in range(3):
        await redis_client.xadd(
            stream_key,
            _raw_fields(org_id=str(org_id), project_id=str(project_id), event_id=str(uuid.uuid4())),
        )
    entries = await read_batch(redis_client, stream_key, group, "c1", count=10, block_ms=10)
    assert await redis_client.xlen(stream_key) == 3  # delivered, not yet acked: still there

    await _process(stream_key, group, entries)

    assert await redis_client.xlen(stream_key) == 0
    assert (await redis_client.xinfo_groups(stream_key))[0]["pending"] == 0
