from clickhouse_connect.driver.asyncclient import AsyncClient


async def drop_event_schema(client: AsyncClient) -> None:
    """Undoes every ClickHouse migration the tests apply. Migrations are
    forward-only, so a test module that applied them has to remove both the
    objects *and* their tracking rows, or a later `migrate()` would think they
    still exist. Order matters: the materialized view depends on `events`.
    `mutations_sync = 2` waits for the tracking-row delete, which is otherwise
    an asynchronous mutation that a following `migrate()` could race."""
    await client.command("DROP VIEW IF EXISTS mv_event_hourly")
    await client.command("DROP TABLE IF EXISTS event_hourly")
    await client.command("DROP TABLE IF EXISTS events")
    await client.command(
        "ALTER TABLE schema_migrations DELETE WHERE version IN (1, 2) SETTINGS mutations_sync = 2"
    )
