import asyncio
import logging

from pulse.core.config import get_settings
from pulse.observability.metrics import WORKER_CYCLE_FAILURES, update_stream_gauges
from pulse.observability.setup import setup_observability
from pulse.repositories import clickhouse, object_storage
from pulse.repositories import redis as redis_repo
from pulse.services.pii_rules import get_rules_for_project
from pulse.worker.consumer import ensure_consumer_group, read_batch
from pulse.worker.processing import process_batch

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pulse.worker")


async def run_cycle() -> None:
    settings = get_settings()
    redis_client = redis_repo.get_client()
    clickhouse_client = await clickhouse.get_client()

    await update_stream_gauges(
        redis_client,
        settings.ingest_stream_key,
        settings.worker_consumer_group,
        settings.worker_dlq_stream_key,
    )
    entries = await read_batch(
        redis_client,
        settings.ingest_stream_key,
        settings.worker_consumer_group,
        settings.worker_consumer_name,
        count=settings.worker_batch_size,
        block_ms=settings.worker_block_ms,
    )
    if not entries:
        return

    try:
        result = await process_batch(
            redis_client=redis_client,
            clickhouse_client=clickhouse_client,
            entries=entries,
            stream_key=settings.ingest_stream_key,
            group=settings.worker_consumer_group,
            dlq_stream_key=settings.worker_dlq_stream_key,
            dedup_ttl_seconds=settings.worker_dedup_ttl_seconds,
            pii_rules_fetcher=get_rules_for_project,
        )
    except Exception:
        # Not acked -- entries stay pending and are reclaimed next cycle.
        # This is the backpressure behavior: a ClickHouse (or object-storage)
        # outage backs the stream up instead of losing or dropping data.
        logger.exception("batch processing failed, %d entries remain pending", len(entries))
        return

    logger.info(
        "processed batch %s: %d inserted, %d duplicates, %d poisoned, %d acked",
        result.ingest_batch,
        result.processed,
        result.duplicates,
        result.poisoned,
        result.acked,
    )


async def run_cycle_with_retry() -> None:
    """One trip through run_cycle(), never raising. A ClickHouse/object-storage
    failure *during* processing is already handled inside run_cycle -- the
    batch stays pending, not lost. This is the broader net around the whole
    cycle: a Redis error from read_batch/update_stream_gauges (confirmed live
    -- a Redis restart mid-session took the whole worker process down, needing
    a manual restart to notice and recover) used to propagate straight out of
    the main loop uncaught. redis-py's own connection pool recovers on its own
    on the next call; nothing here ever gave it one. Split out from main()'s
    `while True` so a test can drive exactly one iteration without looping
    forever."""
    try:
        await run_cycle()
    except Exception:
        WORKER_CYCLE_FAILURES.inc()
        logger.exception("worker cycle failed, retrying after backoff")
        await asyncio.sleep(get_settings().worker_cycle_retry_backoff_seconds)


async def main() -> None:
    settings = get_settings()
    setup_observability("pulse-ingest-worker")
    redis_client = redis_repo.get_client()

    await ensure_consumer_group(
        redis_client, settings.ingest_stream_key, settings.worker_consumer_group
    )
    await object_storage.ensure_bucket()

    logger.info("ingest-worker starting (consumer=%s)", settings.worker_consumer_name)
    while True:
        await run_cycle_with_retry()


if __name__ == "__main__":
    asyncio.run(main())
