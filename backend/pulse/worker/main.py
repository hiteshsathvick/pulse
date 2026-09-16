import asyncio
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pulse.worker")


async def main() -> None:
    """Phase 0 placeholder: proves the ingest-worker service boots and stays up.
    Real Redis Streams consumption lands in Phase 8."""
    logger.info("ingest-worker starting (no consumer logic yet)")
    while True:
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
