import asyncio
import uuid
from pathlib import Path

import clickhouse_connect
import pytest

from pulse.clickhouse_migrations.runner import migrate
from pulse.core.config import get_settings
from pulse.events.fixtures import generate_fake_event, generate_fake_events
from pulse.events.repository import insert_events, query_events
from pulse.repositories.clickhouse import get_client

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parent.parent / "pulse" / "clickhouse_migrations" / "migrations"
)


async def _with_fresh_client(body) -> None:
    """A short-lived client, created and closed entirely within one
    asyncio.run() call -- deliberately not the shared get_client() singleton,
    which is scoped to whichever event loop a given test function runs in.
    A module-scoped fixture's setup/teardown don't share a loop with the
    per-test-function ones pytest-asyncio creates, so touching the shared
    singleton here would bind it to a loop that's gone by the time the first
    test runs (the same class of cross-loop issue solved elsewhere in this
    suite by using plain `def` fixtures with asyncio.run() for Alembic)."""
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
    """Applies the real product migration (not a throwaway fixture dir, like
    Phase 1's runner test) once for this module, and cleans up afterward --
    ClickHouse migrations are forward-only (no downgrade), so tearing down
    means dropping the table *and* its tracking-table entry, or a real
    `python -m pulse.clickhouse_migrations` run afterward would think
    version 1 is already applied when the table no longer exists."""
    asyncio.run(_with_fresh_client(lambda client: migrate(client, _MIGRATIONS_DIR)))
    yield

    async def _teardown(client) -> None:
        await client.command("DROP TABLE IF EXISTS events")
        await client.command("ALTER TABLE schema_migrations DELETE WHERE version = 1")

    asyncio.run(_with_fresh_client(_teardown))


async def test_insert_and_read_round_trip_with_typed_property_casting() -> None:
    org_id, project_id = uuid.uuid4(), uuid.uuid4()
    client = await get_client()

    event = generate_fake_event(
        org_id,
        project_id,
        event_name="checkout completed",
        properties={"platform": "ios", "revenue": "9.99"},
    )
    await insert_events(client, [event])

    rows = await query_events(client, org_id, project_id)
    assert len(rows) == 1
    assert rows[0]["event_name"] == "checkout completed"
    assert rows[0]["properties"] == {"platform": "ios", "revenue": "9.99"}

    # Typed reads via casting, per SPEC.md #5.1 -- the column is a raw
    # Map(String, String), so numeric comparisons need an explicit cast.
    result = await client.query(
        "SELECT CAST(properties['revenue'] AS Float64) FROM events "
        "WHERE event_id = {event_id:UUID}",
        parameters={"event_id": str(event["event_id"])},
    )
    assert result.result_rows[0][0] == pytest.approx(9.99)


async def test_org_and_project_columns_always_scope_the_query() -> None:
    """Two different (org, project) pairs; querying one must never return
    the other's rows -- SPEC.md #3's tenant-scoping invariant, at the
    ClickHouse layer (no RLS safety net here, unlike Postgres)."""
    org_a, project_a = uuid.uuid4(), uuid.uuid4()
    org_b, project_b = uuid.uuid4(), uuid.uuid4()
    client = await get_client()

    await insert_events(client, generate_fake_events(org_a, project_a, count=3))
    await insert_events(client, generate_fake_events(org_b, project_b, count=2))

    rows_a = await query_events(client, org_a, project_a)
    rows_b = await query_events(client, org_b, project_b)

    assert len(rows_a) == 3
    assert len(rows_b) == 2
    assert all(row["org_id"] == org_a for row in rows_a)
    assert all(row["org_id"] == org_b for row in rows_b)


async def test_partition_and_sort_keys_match_spec() -> None:
    """A structural check that the DDL was actually applied as intended, not
    just that inserts happen to work."""
    client = await get_client()
    result = await client.query(
        "SELECT partition_key, sorting_key FROM system.tables "
        "WHERE database = currentDatabase() AND name = 'events'"
    )
    partition_key, sorting_key = result.result_rows[0]
    assert partition_key == "(org_id, toYYYYMM(timestamp))"
    assert sorting_key == "org_id, project_id, event_name, timestamp, event_id"


async def test_replacing_merge_tree_collapses_true_duplicates_but_not_lookalikes() -> None:
    """The dedup-engine decision documented in SPEC.md #5.1: a genuine
    duplicate (identical event_id, and therefore identical sort key) is
    collapsed by a merge; two distinct events that happen to share every
    other sort column but differ by event_id are not."""
    org_id, project_id = uuid.uuid4(), uuid.uuid4()
    client = await get_client()
    shared_timestamp = generate_fake_event(org_id, project_id)["timestamp"]

    duplicate_id = uuid.uuid4()
    await insert_events(
        client,
        [
            generate_fake_event(
                org_id,
                project_id,
                event_id=duplicate_id,
                event_name="checkout completed",
                timestamp=shared_timestamp,
            )
        ],
    )
    # A second insert of the *same* event_id, as a worker retry would produce.
    await insert_events(
        client,
        [
            generate_fake_event(
                org_id,
                project_id,
                event_id=duplicate_id,
                event_name="checkout completed",
                timestamp=shared_timestamp,
            )
        ],
    )
    # A genuinely different event sharing every other sort column.
    await insert_events(
        client,
        [
            generate_fake_event(
                org_id,
                project_id,
                event_name="checkout completed",
                timestamp=shared_timestamp,
            )
        ],
    )

    await client.command("OPTIMIZE TABLE events FINAL")

    rows = await query_events(client, org_id, project_id, limit=10)
    event_ids = [row["event_id"] for row in rows]
    assert event_ids.count(duplicate_id) == 1, "the true duplicate must collapse to one row"
    assert len(rows) == 2, "the distinct event (different event_id) must survive"
