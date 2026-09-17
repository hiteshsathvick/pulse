"""Applies pending ClickHouse migrations. `python -m pulse.clickhouse_migrations`
-- the same one-line ergonomics as `alembic upgrade head`, for the other store."""

import asyncio
import logging
from pathlib import Path

from pulse.clickhouse_migrations.runner import migrate
from pulse.repositories import clickhouse

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("pulse.clickhouse_migrations")

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


async def main() -> None:
    client = await clickhouse.get_client()
    try:
        applied = await migrate(client, _MIGRATIONS_DIR)
    finally:
        await clickhouse.close()

    if applied:
        logger.info("Applied versions: %s", applied)
    else:
        logger.info("Already up to date.")


if __name__ == "__main__":
    asyncio.run(main())
