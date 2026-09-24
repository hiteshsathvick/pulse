"""Phase 22: the retention-worker process. Mirrors pulse/alerts/main.py's
shape exactly: its own entrypoint, its own docker-compose service, a sleep
loop between sweeps rather than blocking on anything (there's no queue to
drain here, same reasoning as alert-worker)."""

import asyncio
import logging

from pulse.core.config import get_settings
from pulse.observability.setup import setup_observability
from pulse.repositories import clickhouse
from pulse.retention.service import sweep_all_projects

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pulse.retention.worker")


async def run_cycle() -> None:
    clickhouse_client = await clickhouse.get_client()
    outcomes = await sweep_all_projects(clickhouse_client)
    if outcomes:
        logger.info("retention sweep: %d project(s) swept", len(outcomes))


async def main() -> None:
    settings = get_settings()
    setup_observability("pulse-retention-worker")
    logger.info(
        "retention-worker starting (interval=%ds)", settings.retention_sweep_interval_seconds
    )
    while True:
        try:
            await run_cycle()
        except Exception:
            # One bad cycle (a transient ClickHouse/Postgres blip) must not
            # kill the loop -- same reasoning as alert-worker: there's no
            # pending work to lose here, just a wait for the next cycle.
            logger.exception("retention sweep cycle failed")
        await asyncio.sleep(settings.retention_sweep_interval_seconds)


if __name__ == "__main__":
    asyncio.run(main())
