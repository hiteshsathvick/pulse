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


@pytest.fixture(autouse=True)
async def _reset_auth_rate_limit() -> AsyncIterator[None]:
    """Every test client shares the same fake "IP" under httpx's ASGITransport,
    so without this, one test's login attempts bleed into another's counter.
    Declared after _close_store_clients so its teardown (LIFO) runs first,
    while the redis client is still open."""
    client = redis_repo.get_client()
    async for key in client.scan_iter("auth:login_attempts:*"):
        await client.delete(key)
    yield
    async for key in client.scan_iter("auth:login_attempts:*"):
        await client.delete(key)
