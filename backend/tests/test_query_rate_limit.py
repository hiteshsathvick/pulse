import asyncio
import uuid
from pathlib import Path
from typing import Any

import clickhouse_connect
import httpx
import pytest

from alembic import command
from alembic.config import Config
from pulse.clickhouse_migrations.runner import migrate
from pulse.core.config import get_settings
from pulse.main import app
from pulse.repositories.redis import get_client as get_redis_client
from tests.clickhouse_schema import drop_event_schema

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_MIGRATIONS_DIR = _BACKEND_ROOT / "pulse" / "clickhouse_migrations" / "migrations"
_PASSWORD = "correct horse battery staple"


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
    asyncio.run(_with_fresh_clickhouse_client(lambda client: migrate(client, _MIGRATIONS_DIR)))
    yield
    asyncio.run(_with_fresh_clickhouse_client(drop_event_schema))


@pytest.fixture
def limit(monkeypatch: pytest.MonkeyPatch):
    """Sets the per-org budget for one test."""

    def _set(max_queries: int, window_seconds: int = 60) -> None:
        settings = get_settings()
        monkeypatch.setattr(settings, "query_rate_limit_max_queries", max_queries)
        monkeypatch.setattr(settings, "query_rate_limit_window_seconds", window_seconds)

    return _set


@pytest.fixture(autouse=True)
def _three_per_minute_by_default(limit) -> None:
    limit(3)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _org_and_project(client: httpx.AsyncClient) -> dict[str, Any]:
    email = f"limit-{uuid.uuid4().hex[:8]}@example.com"
    await client.post(
        "/api/v1/auth/register", json={"email": email, "password": _PASSWORD, "name": "L"}
    )
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    org = await client.post(
        "/api/v1/orgs",
        json={"name": "Limit Org", "slug": f"limit-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    org_id = org.json()["id"]
    project = await client.post(
        f"/api/v1/orgs/{org_id}/projects", json={"name": "Web", "slug": "web"}, headers=headers
    )
    return {
        "org_id": org_id,
        "headers": headers,
        "base": f"/api/v1/orgs/{org_id}/projects/{project.json()['id']}/query",
    }


_RANGE = {"from": "2026-01-01", "to": "2026-01-31"}


def _trend(n: int = 0) -> dict[str, Any]:
    """`n` makes the spec (and so its cache key) distinct, so it must execute."""
    return {"kind": "trend", "events": [f"event {n}"], "measure": "count", "range": _RANGE}


_FUNNEL = {
    "kind": "funnel",
    "steps": [{"event": "a"}, {"event": "b"}],
    "window": {"value": 1, "unit": "day"},
    "range": _RANGE,
}
_RETENTION = {
    "kind": "retention",
    "born_event": "a",
    "return_event": "a",
    "period": "week",
    "periods": 2,
    "range": _RANGE,
}


async def _post(client, ctx, kind: str, body: dict[str, Any], suffix: str = "") -> httpx.Response:
    return await client.post(f"{ctx['base']}/{kind}{suffix}", json=body, headers=ctx["headers"])


async def test_queries_past_the_limit_get_429_with_a_retry_after() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        for n in range(3):
            assert (await _post(client, ctx, "trend", _trend(n))).status_code == 200

        limited = await _post(client, ctx, "trend", _trend(99))
        assert limited.status_code == 429
        assert 1 <= int(limited.headers["Retry-After"]) <= 60
        body = limited.json()["error"]
        assert body["code"] == "rate_limited"
        assert "try again in" in body["message"]


async def test_cache_hits_do_not_use_the_budget() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        first = await _post(client, ctx, "trend", _trend(1))
        assert first.json()["cached"] is False  # this one executed: 1 of 3

        for _ in range(25):  # far more than the budget, all served from cache
            again = await _post(client, ctx, "trend", _trend(1))
            assert again.status_code == 200 and again.json()["cached"] is True

        assert (await _post(client, ctx, "trend", _trend(2))).status_code == 200  # 2 of 3
        assert (await _post(client, ctx, "trend", _trend(3))).status_code == 200  # 3 of 3
        assert (await _post(client, ctx, "trend", _trend(4))).status_code == 429


async def test_a_refresh_uses_the_budget_because_it_runs_the_query() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        assert (await _post(client, ctx, "trend", _trend(1))).status_code == 200  # 1
        assert (
            await _post(client, ctx, "trend", _trend(1), "?refresh=true")
        ).status_code == 200  # 2
        assert (
            await _post(client, ctx, "trend", _trend(1), "?refresh=true")
        ).status_code == 200  # 3
        assert (await _post(client, ctx, "trend", _trend(1), "?refresh=true")).status_code == 429
        # ...but the cached copy is still free.
        assert (await _post(client, ctx, "trend", _trend(1))).status_code == 200


async def test_trend_funnel_and_retention_share_one_budget() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        assert (await _post(client, ctx, "trend", _trend(1))).status_code == 200
        assert (await _post(client, ctx, "funnel", _FUNNEL)).status_code == 200
        assert (await _post(client, ctx, "retention", _RETENTION)).status_code == 200
        for kind, body in (
            ("trend", _trend(2)),
            ("funnel", {**_FUNNEL, "window": {"value": 2, "unit": "day"}}),
        ):
            assert (await _post(client, ctx, kind, body)).status_code == 429, kind


async def test_each_organization_has_its_own_budget() -> None:
    """Tenant fairness: one org exhausting its allowance must not slow another."""
    async with _client() as client:
        noisy = await _org_and_project(client)
        quiet = await _org_and_project(client)
        for n in range(3):
            assert (await _post(client, noisy, "trend", _trend(n))).status_code == 200
        assert (await _post(client, noisy, "trend", _trend(50))).status_code == 429

        for n in range(3):
            assert (await _post(client, quiet, "trend", _trend(n))).status_code == 200


async def test_the_budget_comes_back_when_the_window_ends(limit) -> None:
    limit(2, window_seconds=1)
    async with _client() as client:
        ctx = await _org_and_project(client)
        assert (await _post(client, ctx, "trend", _trend(1))).status_code == 200
        assert (await _post(client, ctx, "trend", _trend(2))).status_code == 200
        assert (await _post(client, ctx, "trend", _trend(3))).status_code == 429

        await asyncio.sleep(1.3)
        assert (await _post(client, ctx, "trend", _trend(4))).status_code == 200


async def test_a_counter_left_without_an_expiry_cannot_lock_an_org_out_forever() -> None:
    """A crash between INCR and EXPIRE would leave the key immortal. The limiter
    re-arms it, and tells the caller how long to wait."""
    async with _client() as client:
        ctx = await _org_and_project(client)
        key = f"query:executions:{ctx['org_id']}"
        redis = get_redis_client()
        await redis.set(key, 10_000)  # over the limit, with no TTL
        assert await redis.ttl(key) == -1

        limited = await _post(client, ctx, "trend", _trend(1))
        assert limited.status_code == 429
        assert int(limited.headers["Retry-After"]) == get_settings().query_rate_limit_window_seconds
        assert await redis.ttl(key) > 0, "the limiter must give the stuck key an expiry"


async def test_a_limited_request_is_refused_before_it_touches_clickhouse_or_the_cache() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        for n in range(3):
            await _post(client, ctx, "trend", _trend(n))
        blocked = _trend(77)
        assert (await _post(client, ctx, "trend", blocked)).status_code == 429

        # Had it run, its (empty) result would now be cached and free to re-read.
        get_settings().query_rate_limit_max_queries = 1_000
        replay = await _post(client, ctx, "trend", blocked)
        assert replay.status_code == 200 and replay.json()["cached"] is False


async def test_trend_responses_say_where_the_answer_came_from() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        counted = (await _post(client, ctx, "trend", _trend(1))).json()
        assert (counted["source"], counted["approximate"]) == ("rollup", False)

        filtered = {**_trend(2), "filters": [{"key": "platform", "op": "eq", "value": "web"}]}
        assert (await _post(client, ctx, "trend", filtered)).json()["source"] == "raw"


async def test_a_query_that_hits_a_clickhouse_cap_is_a_422_with_advice_not_a_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pulse.query import service as query_service

    async def too_big(*args: Any, **kwargs: Any) -> Any:
        raise query_service.QueryTooExpensive("This query would read too much data. Narrow it.")

    for name in ("run_trend", "run_funnel", "run_retention"):
        monkeypatch.setattr(query_service, name, too_big)

    async with _client() as client:
        ctx = await _org_and_project(client)
        for kind, body in (("trend", _trend()), ("funnel", _FUNNEL), ("retention", _RETENTION)):
            response = await _post(client, ctx, kind, body)
            assert response.status_code == 422, kind
            assert "Narrow it" in response.json()["error"]["message"]
