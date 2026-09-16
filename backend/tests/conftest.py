from collections.abc import AsyncIterator

import pytest

from pulse.repositories import clickhouse, postgres
from pulse.repositories import redis as redis_repo


@pytest.fixture(autouse=True)
async def _close_store_clients() -> AsyncIterator[None]:
    """httpx's ASGITransport doesn't run FastAPI's lifespan, so the module-level
    store clients created by a test are never closed by the app itself. Close them
    here instead of leaking connections across tests."""
    yield
    await clickhouse.close()
    await redis_repo.close()
    await postgres.close()
