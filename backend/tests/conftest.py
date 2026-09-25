import asyncio
from collections.abc import AsyncIterator

import pytest
import urllib3

from pulse.repositories import clickhouse, object_storage, postgres
from pulse.repositories import redis as redis_repo


@pytest.fixture(scope="session", autouse=True)
def _archive_bucket() -> None:
    """Any test that runs a worker batch writes to the raw-event archive, so the
    bucket must exist regardless of which test file runs first. It used to be
    created only inside test_worker.py, which passed locally (the bucket persisted
    in the object store's volume) but failed on a fresh CI object store for every earlier file that
    processes a batch. Minio's client is sync, so this is safe session-scoped.

    Tolerant of the object store being unreachable: this runs for the whole session, so failing
    here would fail even the static tests that never touch storage. A test that
    genuinely needs the bucket still fails, with the real connection error."""
    try:
        asyncio.run(object_storage.ensure_bucket())
    except (OSError, urllib3.exceptions.HTTPError):
        pass


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
