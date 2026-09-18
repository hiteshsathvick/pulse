import asyncio
import logging

from pulse.core.config import get_settings
from pulse.repositories import clickhouse, object_storage
from pulse.repositories import redis as redis_repo
from pulse.worker.consumer import ensure_consumer_group, read_batch
from pulse.worker.processing import process_batch

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pulse.worker")


async def run_cycle() -> None:
    settings = get_settings()
    redis_client = redis_repo.get_client()
    clickhouse_client = await clickhouse.get_client()

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


async def main() -> None:
    settings = get_settings()
    redis_client = redis_repo.get_client()

    await ensure_consumer_group(
        redis_client, settings.ingest_stream_key, settings.worker_consumer_group
    )
    await object_storage.ensure_bucket()

    logger.info("ingest-worker starting (consumer=%s)", settings.worker_consumer_name)
    while True:
        await run_cycle()


if __name__ == "__main__":
    asyncio.run(main())
