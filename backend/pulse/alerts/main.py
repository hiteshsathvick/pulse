"""Phase 19: the alert-worker process. Mirrors pulse/worker/main.py's
shape -- its own entrypoint, its own docker-compose service -- but the loop
itself is different: the ingestion worker blocks on a Redis stream read;
there's nothing to block on here, so this sleeps a fixed interval between
evaluation passes instead (confirmed with the user first as the mechanism
for Phase 19, over an externally-cron'd one-shot CLI, to keep a self-hosted
deploy cron-free)."""

import asyncio
import logging

from pulse.alerts.evaluate import evaluate_all_enabled
from pulse.core.config import get_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pulse.alerts.worker")


async def run_cycle() -> None:
    outcomes = await evaluate_all_enabled()
    fired = sum(1 for outcome in outcomes if outcome.fired)
    recovered = sum(1 for outcome in outcomes if outcome.recovered)
    if fired or recovered:
        logger.info(
            "alert evaluation: %d fired, %d recovered, %d evaluated",
            fired,
            recovered,
            len(outcomes),
        )


async def main() -> None:
    settings = get_settings()
    logger.info("alert-worker starting (interval=%ds)", settings.alert_evaluation_interval_seconds)
    while True:
        try:
            await run_cycle()
        except Exception:
            # One bad cycle (a transient ClickHouse/Postgres blip) must not
            # kill the loop -- there's no "pending work" to lose here the
            # way the ingest worker has, just a wait for the next cycle.
            logger.exception("alert evaluation cycle failed")
        await asyncio.sleep(settings.alert_evaluation_interval_seconds)


if __name__ == "__main__":
    asyncio.run(main())
