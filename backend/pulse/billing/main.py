"""Phase 20: the billing-worker process. Mirrors pulse/alerts/main.py's
shape exactly -- its own entrypoint, its own docker-compose service, a
sleep loop, and the same unscoped-Organization-then-per-org-scoped sweep
Phase 19 established (an unscoped Postgres session default-denies every
RLS-protected table; Organization is the one exception, since it carries no
org_id -- it IS the tenant)."""

import asyncio
import logging

from sqlalchemy import select

from pulse.billing.service import compute_and_store_usage, current_period
from pulse.core.config import get_settings
from pulse.models import Organization
from pulse.repositories.postgres import session_scope

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pulse.billing.worker")


async def run_cycle() -> None:
    period = current_period()
    async with session_scope() as session:
        org_ids = list(await session.scalars(select(Organization.id)))

    for org_id in org_ids:
        try:
            await compute_and_store_usage(org_id, period)
        except Exception:
            logger.exception("usage computation failed for org %s", org_id)


async def main() -> None:
    settings = get_settings()
    logger.info("billing-worker starting (interval=%ds)", settings.billing_usage_interval_seconds)
    while True:
        try:
            await run_cycle()
        except Exception:
            logger.exception("billing usage cycle failed")
        await asyncio.sleep(settings.billing_usage_interval_seconds)


if __name__ == "__main__":
    asyncio.run(main())
