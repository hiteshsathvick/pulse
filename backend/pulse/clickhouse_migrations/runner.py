from dataclasses import dataclass
from pathlib import Path

from clickhouse_connect.driver.asyncclient import AsyncClient

_TRACKING_TABLE = "schema_migrations"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path


async def ensure_tracking_table(client: AsyncClient) -> None:
    await client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {_TRACKING_TABLE}
        (
            version   UInt32,
            name      String,
            applied_at DateTime DEFAULT now()
        )
        ENGINE = MergeTree
        ORDER BY version
        """
    )


async def applied_versions(client: AsyncClient) -> set[int]:
    result = await client.query(f"SELECT version FROM {_TRACKING_TABLE}")
    return {int(row[0]) for row in result.result_rows}


def discover_migrations(directory: Path) -> list[Migration]:
    """Migration files are named `NNNN_description.sql`. Forward-only, like
    most ClickHouse migration tooling -- there is no matching `down.sql`."""
    migrations = []
    for path in directory.glob("*.sql"):
        prefix, _, rest = path.stem.partition("_")
        migrations.append(Migration(version=int(prefix), name=rest or path.stem, path=path))
    return sorted(migrations, key=lambda m: m.version)


def _split_statements(sql: str) -> list[str]:
    return [statement.strip() for statement in sql.split(";") if statement.strip()]


async def migrate(client: AsyncClient, directory: Path) -> list[int]:
    """Applies pending versioned DDL files in order, recording each one so a
    re-run is a no-op for anything already applied. Returns the versions
    applied by this call (empty if everything was already up to date)."""
    await ensure_tracking_table(client)
    already_applied = await applied_versions(client)

    newly_applied: list[int] = []
    for migration in discover_migrations(directory):
        if migration.version in already_applied:
            continue

        for statement in _split_statements(migration.path.read_text()):
            await client.command(statement)

        await client.insert(
            _TRACKING_TABLE,
            [[migration.version, migration.name]],
            column_names=["version", "name"],
        )
        newly_applied.append(migration.version)

    return newly_applied
