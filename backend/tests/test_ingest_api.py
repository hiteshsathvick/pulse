import asyncio
import time
import uuid
from pathlib import Path

import clickhouse_connect
import httpx
import pytest

from alembic import command
from alembic.config import Config
from pulse.clickhouse_migrations.runner import migrate
from pulse.core.config import get_settings
from pulse.events.repository import query_events
from pulse.main import app
from pulse.models import ApiKeyType, User
from pulse.repositories.clickhouse import get_client as get_clickhouse_client
from pulse.repositories.postgres import session_scope
from pulse.repositories.redis import get_client as get_redis_client
from pulse.services import api_keys as api_keys_service
from pulse.services import orgs as orgs_service
from pulse.services import projects as projects_service
from pulse.services.api_keys import resolve_api_key

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_MIGRATIONS_DIR = _BACKEND_ROOT / "pulse" / "clickhouse_migrations" / "migrations"


def _alembic_config() -> Config:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return config


@pytest.fixture(scope="module", autouse=True)
def _control_plane_schema():
    config = _alembic_config()
    command.upgrade(config, "head")
    yield
    command.downgrade(config, "base")


async def _with_fresh_clickhouse_client(body) -> None:
    """A short-lived client, not the shared get_client() singleton -- see
    test_events_clickhouse.py's identical helper for why: a module-scoped
    fixture's setup/teardown run on a different event loop than the
    per-test-function ones pytest-asyncio creates, so touching the shared
    singleton here would bind it to a loop that's gone by the first test."""
    settings = get_settings()
    client = await clickhouse_connect.get_async_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        database=settings.clickhouse_database,
        secure=settings.clickhouse_secure,
    )
    try:
        await body(client)
    finally:
        await client.close()


@pytest.fixture(scope="module", autouse=True)
def _events_table():
    """Same real-migration pattern as test_events_clickhouse.py, needed here
    only to prove the DoD's "nothing hits ClickHouse synchronously" -- these
    tests never insert into it themselves."""
    asyncio.run(_with_fresh_clickhouse_client(lambda client: migrate(client, _MIGRATIONS_DIR)))
    yield

    async def _teardown(client) -> None:
        await client.command("DROP TABLE IF EXISTS events")
        await client.command("ALTER TABLE schema_migrations DELETE WHERE version = 1")

    asyncio.run(_with_fresh_clickhouse_client(_teardown))


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def _reset_ingest_rate_limit():
    """Mirrors conftest.py's _reset_auth_rate_limit -- only this one test
    needs it (it deliberately seeds the counter), so it's local rather than
    autouse."""
    client = get_redis_client()
    async for key in client.scan_iter("ingest:requests:*"):
        await client.delete(key)
    yield
    async for key in client.scan_iter("ingest:requests:*"):
        await client.delete(key)


async def _org_project_and_key(key_type: ApiKeyType = ApiKeyType.WRITE) -> tuple:
    async with session_scope() as session:
        user = User(
            email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="not-a-real-hash",
            name="Owner",
        )
        session.add(user)
        await session.commit()

    org = await orgs_service.create_organization(
        "Ingest Test Org", f"ingest-org-{uuid.uuid4().hex[:8]}", user.id
    )
    project = await projects_service.create_project(org.id, "Web", "web", "UTC", user.id)
    _, raw_key = await api_keys_service.create_api_key(org.id, project.id, key_type, user.id)
    return org.id, project.id, raw_key


def _event(**overrides) -> dict:
    event = {"event_id": str(uuid.uuid4()), "event": "button clicked", "user_id": "user_1"}
    event.update(overrides)
    return event


async def test_valid_batch_is_accepted_and_buffered_not_written_to_clickhouse() -> None:
    org_id, project_id, write_key = await _org_project_and_key()
    settings = get_settings()
    redis_client = get_redis_client()
    before = await redis_client.xlen(settings.ingest_stream_key)

    events = [_event(user_id=f"user_{i}") for i in range(5)]
    async with _client() as client:
        response = await client.post(
            "/ingest", json={"batch": events}, headers={"X-API-Key": write_key}
        )

    assert response.status_code == 202
    assert response.json() == {"accepted": 5}

    after = await redis_client.xlen(settings.ingest_stream_key)
    assert after - before == 5

    entries = await redis_client.xrevrange(settings.ingest_stream_key, count=5)
    buffered_event_ids = {fields[b"event_id"].decode() for _, fields in entries}
    assert buffered_event_ids == {e["event_id"] for e in events}
    _, sample_fields = entries[0]
    assert sample_fields[b"org_id"].decode() == str(org_id)
    assert sample_fields[b"project_id"].decode() == str(project_id)

    ch_client = await get_clickhouse_client()
    rows = await query_events(ch_client, org_id, project_id)
    assert rows == [], "the Ingest API must never write to ClickHouse synchronously"


async def test_read_key_cannot_ingest() -> None:
    """DoD: write keys can only ingest, read keys can only query -- the
    write-key-scoping half of that, exercised through the real endpoint."""
    _, _, read_key = await _org_project_and_key(ApiKeyType.READ)

    async with _client() as client:
        response = await client.post(
            "/ingest", json={"batch": [_event()]}, headers={"X-API-Key": read_key}
        )

    assert response.status_code == 401


async def test_unauthenticated_request_is_rejected() -> None:
    async with _client() as client:
        response = await client.post("/ingest", json={"batch": [_event()]})
    assert response.status_code == 422  # missing required X-API-Key header


@pytest.mark.parametrize(
    "bad_event",
    [
        {"event": "missing event_id", "user_id": "u1"},
        _event(event=""),
        {"event_id": str(uuid.uuid4()), "event": "no identity at all"},
        {"event_id": "not-a-uuid", "event": "bad uuid", "user_id": "u1"},
    ],
)
async def test_malformed_event_rejects_the_whole_batch(bad_event: dict) -> None:
    _, _, write_key = await _org_project_and_key()
    settings = get_settings()
    redis_client = get_redis_client()
    before = await redis_client.xlen(settings.ingest_stream_key)

    async with _client() as client:
        response = await client.post(
            "/ingest",
            json={"batch": [_event(), bad_event]},
            headers={"X-API-Key": write_key},
        )

    assert response.status_code == 422
    after = await redis_client.xlen(settings.ingest_stream_key)
    assert after == before, "a rejected batch must not partially land in the buffer"


async def test_empty_batch_is_rejected() -> None:
    _, _, write_key = await _org_project_and_key()
    async with _client() as client:
        response = await client.post(
            "/ingest", json={"batch": []}, headers={"X-API-Key": write_key}
        )
    assert response.status_code == 422


async def test_oversized_batch_is_rejected_cleanly() -> None:
    _, _, write_key = await _org_project_and_key()
    settings = get_settings()
    redis_client = get_redis_client()
    before = await redis_client.xlen(settings.ingest_stream_key)

    events = [_event(user_id=f"user_{i}") for i in range(settings.ingest_max_batch_size + 1)]
    async with _client() as client:
        response = await client.post(
            "/ingest", json={"batch": events}, headers={"X-API-Key": write_key}
        )

    assert response.status_code == 422
    after = await redis_client.xlen(settings.ingest_stream_key)
    assert after == before


async def test_oversized_body_is_rejected_with_413() -> None:
    _, _, write_key = await _org_project_and_key()
    settings = get_settings()

    huge_batch = [_event(properties={"blob": "x" * (settings.ingest_max_body_bytes + 10_000)})]
    async with _client() as client:
        response = await client.post(
            "/ingest", json={"batch": huge_batch}, headers={"X-API-Key": write_key}
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_ingest_rate_limit_returns_429(_reset_ingest_rate_limit) -> None:
    """Pre-seeds the fixed-window counter at the limit rather than looping
    hundreds of real requests -- same effect, without the default 600/window
    threshold making this test slow."""
    _, _, write_key = await _org_project_and_key()
    settings = get_settings()

    async with _client() as client:
        # Learn the key's id by making one accepted request, then seed the
        # counter for that same key up to the limit.
        first = await client.post(
            "/ingest", json={"batch": [_event()]}, headers={"X-API-Key": write_key}
        )
        assert first.status_code == 202

        redis_client = get_redis_client()
        # api_key.id isn't in the response -- resolve it the same way the
        # require_write_key dependency does.
        api_key = await resolve_api_key(write_key, ApiKeyType.WRITE)
        counter_key = f"ingest:requests:{api_key.id}"
        await redis_client.set(
            counter_key,
            settings.ingest_rate_limit_max_requests,
            ex=settings.ingest_rate_limit_window_seconds,
        )

        limited = await client.post(
            "/ingest", json={"batch": [_event()]}, headers={"X-API-Key": write_key}
        )

    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"


async def test_ingest_latency_sanity() -> None:
    """A loose smoke check, not a load test (that's Phase 17/23's k6/Locust
    harness) -- a moderate batch should complete comfortably fast even over
    an in-process ASGI transport with real Postgres/Redis round trips."""
    _, _, write_key = await _org_project_and_key()
    events = [_event(user_id=f"user_{i}") for i in range(200)]

    async with _client() as client:
        start = time.perf_counter()
        response = await client.post(
            "/ingest", json={"batch": events}, headers={"X-API-Key": write_key}
        )
        duration_seconds = time.perf_counter() - start

    assert response.status_code == 202
    assert duration_seconds < 2.0
