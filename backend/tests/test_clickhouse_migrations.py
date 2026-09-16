import random
from pathlib import Path

from pulse.clickhouse_migrations.runner import migrate
from pulse.repositories.clickhouse import get_client


async def test_migrate_applies_pending_files_and_is_idempotent(tmp_path: Path) -> None:
    # A random version avoids colliding with the shared, persistent
    # `schema_migrations` tracking table across repeated local/CI test runs.
    version = random.randint(100_000, 999_999)
    table_name = f"_pulse_test_migration_{version}"
    (tmp_path / f"{version}_create_test_table.sql").write_text(
        f"CREATE TABLE {table_name} (id UInt32) ENGINE = MergeTree ORDER BY id"
    )

    client = await get_client()

    try:
        first_run = await migrate(client, tmp_path)
        assert first_run == [version]

        exists = await client.query(f"EXISTS TABLE {table_name}")
        assert exists.result_rows[0][0] == 1

        second_run = await migrate(client, tmp_path)
        assert second_run == [], "an already-applied migration must not be re-run"
    finally:
        await client.command(f"DROP TABLE IF EXISTS {table_name}")
        await client.command(f"ALTER TABLE schema_migrations DELETE WHERE version = {version}")
