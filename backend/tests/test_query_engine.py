import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import clickhouse_connect
import httpx
import pytest

from alembic import command
from alembic.config import Config
from pulse.clickhouse_migrations.runner import migrate
from pulse.core.config import get_settings
from pulse.events.fixtures import generate_fake_event
from pulse.events.repository import insert_events
from pulse.main import app
from pulse.models import User
from pulse.query import service as query_service
from pulse.query.cache import cache_key
from pulse.query.spec import FunnelSpec, RetentionSpec, TrendSpec
from pulse.repositories.clickhouse import get_client as get_clickhouse_client
from pulse.repositories.postgres import session_scope
from pulse.repositories.redis import get_client as get_redis_client
from pulse.services import orgs as orgs_service
from pulse.services import projects as projects_service

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

    async def _teardown(client) -> None:
        await client.command("DROP TABLE IF EXISTS events")
        await client.command("ALTER TABLE schema_migrations DELETE WHERE version = 1")

    asyncio.run(_with_fresh_clickhouse_client(_teardown))


async def _create_org_and_project(timezone: str = "UTC") -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with session_scope() as session:
        user = User(
            email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="not-a-real-hash",
            name="Owner",
        )
        session.add(user)
        await session.commit()

    org = await orgs_service.create_organization(
        "Query Test Org", f"query-org-{uuid.uuid4().hex[:8]}", user.id
    )
    project = await projects_service.create_project(org.id, "Web", "web", timezone, user.id)
    return org.id, project.id, user.id


def _spec(**overrides: Any) -> TrendSpec:
    payload: dict[str, Any] = {
        "kind": "trend",
        "events": ["checkout completed"],
        "measure": "count",
        "range": {"from": "2026-08-15", "to": "2026-08-15"},
        "granularity": "day",
    }
    payload.update(overrides)
    return TrendSpec.model_validate(payload)


_FIXED_TIMESTAMP = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


async def test_query_never_leaks_across_tenants() -> None:
    """DoD: tenant-leakage test."""
    org_a, project_a, _ = await _create_org_and_project()
    org_b, project_b, _ = await _create_org_and_project()

    ch_client = await get_clickhouse_client()
    await insert_events(
        ch_client,
        [
            generate_fake_event(
                org_a, project_a, event_name="checkout completed", timestamp=_FIXED_TIMESTAMP
            )
            for _ in range(3)
        ],
    )
    await insert_events(
        ch_client,
        [
            generate_fake_event(
                org_b, project_b, event_name="checkout completed", timestamp=_FIXED_TIMESTAMP
            )
            for _ in range(5)
        ],
    )

    result_a = await query_service.run_trend(_spec(), org_a, project_a)
    result_b = await query_service.run_trend(_spec(), org_b, project_b)

    assert result_a.results[0]["value"] == 3
    assert result_b.results[0]["value"] == 5


async def test_measures_match_hand_computed_values() -> None:
    org_id, project_id, _ = await _create_org_and_project()
    ch_client = await get_clickhouse_client()
    await insert_events(
        ch_client,
        [
            generate_fake_event(
                org_id,
                project_id,
                event_name="checkout completed",
                user_id="u1",
                timestamp=_FIXED_TIMESTAMP,
                properties={"revenue": "10"},
            ),
            generate_fake_event(
                org_id,
                project_id,
                event_name="checkout completed",
                user_id="u1",
                timestamp=_FIXED_TIMESTAMP,
                properties={"revenue": "20"},
            ),
            generate_fake_event(
                org_id,
                project_id,
                event_name="checkout completed",
                user_id="u2",
                timestamp=_FIXED_TIMESTAMP,
                properties={"revenue": "30"},
            ),
        ],
    )

    count = await query_service.run_trend(_spec(measure="count"), org_id, project_id)
    assert count.results[0]["value"] == 3

    unique_users = await query_service.run_trend(_spec(measure="unique_users"), org_id, project_id)
    assert unique_users.results[0]["value"] == 2

    total = await query_service.run_trend(_spec(measure="property_sum:revenue"), org_id, project_id)
    assert total.results[0]["value"] == 60

    average = await query_service.run_trend(
        _spec(measure="property_avg:revenue"), org_id, project_id
    )
    assert average.results[0]["value"] == 20


async def test_timezone_bucketing_uses_the_project_timezone_not_utc() -> None:
    """DoD: tz-correct bucketing. 2026-08-01 20:00 UTC is 2026-08-02 01:30
    in Asia/Kolkata (UTC+5:30, no DST) -- a day *later* locally. A query for
    the local date 2026-08-02 must include it; a query for 2026-08-01 (the
    UTC date) must not."""
    org_id, project_id, _ = await _create_org_and_project(timezone="Asia/Kolkata")
    ch_client = await get_clickhouse_client()
    straddling_timestamp = datetime(2026, 8, 1, 20, 0, 0, tzinfo=UTC)
    await insert_events(
        ch_client,
        [
            generate_fake_event(
                org_id,
                project_id,
                event_name="checkout completed",
                timestamp=straddling_timestamp,
            )
        ],
    )

    on_local_day = await query_service.run_trend(
        _spec(range={"from": "2026-08-02", "to": "2026-08-02"}), org_id, project_id
    )
    assert sum(row["value"] for row in on_local_day.results) == 1

    on_utc_day = await query_service.run_trend(
        _spec(range={"from": "2026-08-01", "to": "2026-08-01"}), org_id, project_id
    )
    assert sum(row["value"] for row in on_utc_day.results) == 0


async def test_cache_hit_serves_the_stale_result_without_requerying_clickhouse() -> None:
    """DoD: cache hit/miss."""
    org_id, project_id, _ = await _create_org_and_project()
    ch_client = await get_clickhouse_client()
    await insert_events(
        ch_client,
        [
            generate_fake_event(
                org_id, project_id, event_name="checkout completed", timestamp=_FIXED_TIMESTAMP
            )
        ],
    )

    spec = _spec()
    first = await query_service.run_trend(spec, org_id, project_id)
    assert first.cached is False
    assert first.results[0]["value"] == 1
    assert isinstance(first.results[0]["bucket"], str), (
        "bucket must already be a plain string, not a native datetime -- "
        "otherwise a cache hit (JSON round-tripped) and a cache miss would "
        "format the same value differently"
    )

    redis_client = get_redis_client()
    assert await redis_client.get(cache_key(spec, org_id, project_id)) is not None

    # A second event lands after the first query -- if the second run
    # genuinely hits the cache, it must NOT reflect this new event.
    await insert_events(
        ch_client,
        [
            generate_fake_event(
                org_id, project_id, event_name="checkout completed", timestamp=_FIXED_TIMESTAMP
            )
        ],
    )

    second = await query_service.run_trend(spec, org_id, project_id)
    assert second.cached is True
    assert second.results[0]["value"] == 1  # stale on purpose -- proves it's cached, not recomputed
    assert second.results[0]["bucket"] == first.results[0]["bucket"]


def _funnel_spec(**overrides: Any) -> FunnelSpec:
    payload: dict[str, Any] = {
        "kind": "funnel",
        "steps": [
            {"event": "signed up"},
            {"event": "added to cart"},
            {"event": "checkout completed"},
        ],
        "window": {"value": 1, "unit": "day"},
        "range": {"from": "2026-08-15", "to": "2026-08-18"},
    }
    payload.update(overrides)
    return FunnelSpec.model_validate(payload)


async def test_funnel_ordering_and_window_boundaries_match_hand_computed_counts() -> None:
    """DoD: ordering enforced; window boundaries; hand-computed fixture
    funnel matches exactly (CLAUDE.md #4: funnel math must be verified
    against a fixture with a provably correct answer, not just 'ran without
    error'). Seven users, each isolating one thing the engine must get
    right:
      u1 completes all 3 steps, in order, well inside the window -> level 3
      u2 completes steps 1-2 only                                -> level 2
      u3 completes step 1 only                                   -> level 1
      u4 never does step 1 at all (only step 2)                  -> level 0
      u5 does steps 1 and 3, skipping step 2                     -> level 1
      u6 does steps 1-2, but step 2 lands 2 days after step 1,
         outside the 1-day conversion window                     -> level 1
      u7 does step 2 *before* step 1 -- wrong order               -> level 1
    Hand-computed cumulative counts (reaching level >= k means step k was
    completed): step1 = 6 (everyone but u4), step2 = 2 (u1, u2),
    step3 = 1 (u1)."""
    org_id, project_id, _ = await _create_org_and_project()
    ch_client = await get_clickhouse_client()
    t = datetime(2026, 8, 15, 0, 0, 0, tzinfo=UTC)

    def _e(event_name: str, user_id: str, offset: timedelta) -> dict[str, object]:
        return generate_fake_event(
            org_id, project_id, event_name=event_name, user_id=user_id, timestamp=t + offset
        )

    zero = timedelta()
    await insert_events(
        ch_client,
        [
            _e("signed up", "u1", zero),
            _e("added to cart", "u1", timedelta(hours=1)),
            _e("checkout completed", "u1", timedelta(hours=2)),
            _e("signed up", "u2", zero),
            _e("added to cart", "u2", timedelta(hours=1)),
            _e("signed up", "u3", zero),
            _e("added to cart", "u4", zero),
            _e("signed up", "u5", zero),
            _e("checkout completed", "u5", timedelta(hours=1)),
            _e("signed up", "u6", zero),
            _e("added to cart", "u6", timedelta(days=2)),
            _e("added to cart", "u7", zero),
            _e("signed up", "u7", timedelta(hours=1)),
        ],
    )

    result = await query_service.run_funnel(_funnel_spec(), org_id, project_id)

    assert result.results == [
        {"step": "signed up", "users": 6, "conversion_pct": 100.0, "drop_off": 0},
        {
            "step": "added to cart",
            "users": 2,
            "conversion_pct": pytest.approx(33.3333, rel=1e-4),
            "drop_off": 4,
        },
        {
            "step": "checkout completed",
            "users": 1,
            "conversion_pct": pytest.approx(16.6666, rel=1e-4),
            "drop_off": 1,
        },
    ]


async def test_funnel_never_leaks_across_tenants() -> None:
    """DoD: tenant-leakage test, mandatory per CLAUDE.md #4 for every query type."""
    org_a, project_a, _ = await _create_org_and_project()
    org_b, project_b, _ = await _create_org_and_project()
    ch_client = await get_clickhouse_client()
    t = datetime(2026, 8, 15, 0, 0, 0, tzinfo=UTC)

    await insert_events(
        ch_client,
        [
            generate_fake_event(
                org_a, project_a, event_name="signed up", user_id="a1", timestamp=t
            ),
            generate_fake_event(
                org_a, project_a, event_name="added to cart", user_id="a1", timestamp=t
            ),
        ],
    )
    await insert_events(
        ch_client,
        [
            generate_fake_event(
                org_b, project_b, event_name="signed up", user_id=f"b{i}", timestamp=t
            )
            for i in range(3)
        ],
    )

    result_a = await query_service.run_funnel(_funnel_spec(), org_a, project_a)
    result_b = await query_service.run_funnel(_funnel_spec(), org_b, project_b)

    assert result_a.results[0]["users"] == 1
    assert result_a.results[1]["users"] == 1
    assert result_b.results[0]["users"] == 3
    assert result_b.results[1]["users"] == 0


async def test_funnel_breakdown_splits_results_per_dimension_value() -> None:
    org_id, project_id, _ = await _create_org_and_project()
    ch_client = await get_clickhouse_client()
    t = datetime(2026, 8, 15, 0, 0, 0, tzinfo=UTC)

    def _e(user_id: str, event_name: str, platform: str) -> dict[str, object]:
        return generate_fake_event(
            org_id,
            project_id,
            event_name=event_name,
            user_id=user_id,
            timestamp=t,
            properties={"platform": platform},
        )

    await insert_events(
        ch_client,
        [
            _e("ios1", "signed up", "ios"),
            _e("ios1", "added to cart", "ios"),
            _e("web1", "signed up", "web"),
        ],
    )

    result = await query_service.run_funnel(_funnel_spec(breakdown="platform"), org_id, project_id)

    by_breakdown = {(row["breakdown"], row["step"]): row["users"] for row in result.results}
    assert by_breakdown[("ios", "signed up")] == 1
    assert by_breakdown[("ios", "added to cart")] == 1
    assert by_breakdown[("web", "signed up")] == 1
    assert by_breakdown[("web", "added to cart")] == 0


def _retention_spec(**overrides: Any) -> RetentionSpec:
    payload: dict[str, Any] = {
        "kind": "retention",
        "born_event": "signed up",
        "return_event": "checkout completed",
        "period": "week",
        "periods": 3,
        "range": {"from": "2026-08-03", "to": "2026-08-16"},
    }
    payload.update(overrides)
    return RetentionSpec.model_validate(payload)


async def test_retention_cohort_assignment_and_week_bucketing_match_hand_computed_grid() -> None:
    """DoD: cohort assignment; day/week bucketing (week half); hand-computed
    retention fixture matches exactly (CLAUDE.md #4). Two ISO weeks of born
    events (Mon 2026-08-03 and Mon 2026-08-10), each its own cohort:

    Cohort week 1 (Aug 3-9), 3 users:
      c1: returns in week 1 (offset 0) and week 2 (offset 1), not week 3
      c2: returns in week 1 (offset 0) only
      c3: never returns
      -> offset0 2/3, offset1 1/3, offset2 0/3

    Cohort week 2 (Aug 10-16), 2 users:
      c4: returns in week 2 (offset 0) and week 4 relative to its own
          cohort (offset 2), skipping offset 1 -- retention need not be
          monotonic, each offset is checked independently
      c5: never returns
      -> offset0 1/2, offset1 0/2, offset2 1/2

    The offset-2 activity for c4 (2026-08-26) falls *after* the query
    range's own `to` (2026-08-16), proving the activity window widening
    actually works, not just that it compiles."""
    org_id, project_id, _ = await _create_org_and_project()
    ch_client = await get_clickhouse_client()

    def _e(event_name: str, user_id: str, when: datetime) -> dict[str, object]:
        return generate_fake_event(
            org_id, project_id, event_name=event_name, user_id=user_id, timestamp=when
        )

    await insert_events(
        ch_client,
        [
            _e("signed up", "c1", datetime(2026, 8, 3, 9, 0, tzinfo=UTC)),
            _e("checkout completed", "c1", datetime(2026, 8, 4, tzinfo=UTC)),
            _e("checkout completed", "c1", datetime(2026, 8, 11, tzinfo=UTC)),
            _e("signed up", "c2", datetime(2026, 8, 5, tzinfo=UTC)),
            _e("checkout completed", "c2", datetime(2026, 8, 5, 12, 0, tzinfo=UTC)),
            _e("signed up", "c3", datetime(2026, 8, 6, tzinfo=UTC)),
            _e("signed up", "c4", datetime(2026, 8, 11, tzinfo=UTC)),
            _e("checkout completed", "c4", datetime(2026, 8, 12, tzinfo=UTC)),
            _e("checkout completed", "c4", datetime(2026, 8, 26, tzinfo=UTC)),
            _e("signed up", "c5", datetime(2026, 8, 14, tzinfo=UTC)),
        ],
    )

    result = await query_service.run_retention(_retention_spec(), org_id, project_id)

    by_cohort_offset = {
        (row["cohort_period"], row["period_offset"]): (row["cohort_size"], row["retained"])
        for row in result.results
    }
    week1 = "2026-08-03T00:00:00+00:00"
    week2 = "2026-08-10T00:00:00+00:00"
    assert by_cohort_offset[(week1, 0)] == (3, 2)
    assert by_cohort_offset[(week1, 1)] == (3, 1)
    assert by_cohort_offset[(week1, 2)] == (3, 0)
    assert by_cohort_offset[(week2, 0)] == (2, 1)
    assert by_cohort_offset[(week2, 1)] == (2, 0)
    assert by_cohort_offset[(week2, 2)] == (2, 1)


async def test_retention_day_bucketing_matches_hand_computed_grid() -> None:
    """DoD: day/week bucketing (day half). One cohort day, one user retained
    the next day but not two days later."""
    org_id, project_id, _ = await _create_org_and_project()
    ch_client = await get_clickhouse_client()

    def _e(event_name: str, user_id: str, when: datetime) -> dict[str, object]:
        return generate_fake_event(
            org_id, project_id, event_name=event_name, user_id=user_id, timestamp=when
        )

    await insert_events(
        ch_client,
        [
            _e("signed up", "d1", datetime(2026, 8, 3, 9, 0, tzinfo=UTC)),
            _e("checkout completed", "d1", datetime(2026, 8, 4, 10, 0, tzinfo=UTC)),
            _e("signed up", "d2", datetime(2026, 8, 3, 15, 0, tzinfo=UTC)),
        ],
    )

    spec = _retention_spec(
        period="day", periods=3, range={"from": "2026-08-03", "to": "2026-08-03"}
    )
    result = await query_service.run_retention(spec, org_id, project_id)

    by_offset = {row["period_offset"]: row["retained"] for row in result.results}
    assert result.results[0]["cohort_size"] == 2
    assert by_offset[0] == 0
    assert by_offset[1] == 1
    assert by_offset[2] == 0


async def test_retention_when_return_event_equals_born_event_period_zero_is_full() -> None:
    """SPEC.md #4.2: return_event may equal born_event, for "came back at
    all" -- period 0 is then trivially 100%, since the born event itself
    is activity in its own cohort period."""
    org_id, project_id, _ = await _create_org_and_project()
    ch_client = await get_clickhouse_client()
    t = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
    await insert_events(
        ch_client,
        [
            generate_fake_event(
                org_id, project_id, event_name="signed up", user_id="r1", timestamp=t
            )
        ],
    )

    spec = _retention_spec(born_event="signed up", return_event="signed up", periods=1)
    result = await query_service.run_retention(spec, org_id, project_id)

    assert result.results[0]["cohort_size"] == 1
    assert result.results[0]["retained"] == 1
    assert result.results[0]["retention_pct"] == 100.0


async def test_retention_never_leaks_across_tenants() -> None:
    """DoD: tenant-leakage test, mandatory per CLAUDE.md #4 for every query type."""
    org_a, project_a, _ = await _create_org_and_project()
    org_b, project_b, _ = await _create_org_and_project()
    ch_client = await get_clickhouse_client()
    t = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)

    await insert_events(
        ch_client,
        [generate_fake_event(org_a, project_a, event_name="signed up", user_id="a1", timestamp=t)],
    )
    await insert_events(
        ch_client,
        [
            generate_fake_event(
                org_b, project_b, event_name="signed up", user_id=f"b{i}", timestamp=t
            )
            for i in range(4)
        ],
    )

    result_a = await query_service.run_retention(_retention_spec(periods=1), org_a, project_a)
    result_b = await query_service.run_retention(_retention_spec(periods=1), org_b, project_b)

    assert result_a.results[0]["cohort_size"] == 1
    assert result_b.results[0]["cohort_size"] == 4


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _register_and_login(client: httpx.AsyncClient, email: str) -> dict[str, Any]:
    await client.post(
        "/api/v1/auth/register", json={"email": email, "password": _PASSWORD, "name": email}
    )
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    result: dict[str, Any] = login.json()
    return result


async def test_query_auth_accepts_jwt_or_read_key_and_rejects_everything_else() -> None:
    async with _client() as client:
        email = f"owner-{uuid.uuid4().hex[:8]}@example.com"
        tokens = await _register_and_login(client, email)
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}

        org = await client.post(
            "/api/v1/orgs",
            json={"name": "Query Auth Org", "slug": f"query-auth-{uuid.uuid4().hex[:8]}"},
            headers=headers,
        )
        org_id = org.json()["id"]
        project = await client.post(
            f"/api/v1/orgs/{org_id}/projects", json={"name": "Web", "slug": "web"}, headers=headers
        )
        project_id = project.json()["id"]

        other_project = await client.post(
            f"/api/v1/orgs/{org_id}/projects",
            json={"name": "Other", "slug": "other"},
            headers=headers,
        )
        other_project_id = other_project.json()["id"]

        read_key_resp = await client.post(
            f"/api/v1/orgs/{org_id}/projects/{project_id}/keys",
            json={"type": "read"},
            headers=headers,
        )
        read_key = read_key_resp.json()["key"]

        write_key_resp = await client.post(
            f"/api/v1/orgs/{org_id}/projects/{project_id}/keys",
            json={"type": "write"},
            headers=headers,
        )
        write_key = write_key_resp.json()["key"]

        body = _spec().model_dump(by_alias=True, mode="json")
        url = f"/api/v1/orgs/{org_id}/projects/{project_id}/query/trend"

        assert (await client.post(url, json=body, headers=headers)).status_code == 200
        assert (
            await client.post(url, json=body, headers={"X-API-Key": read_key})
        ).status_code == 200
        assert (
            await client.post(url, json=body, headers={"X-API-Key": write_key})
        ).status_code == 401
        assert (await client.post(url, json=body)).status_code == 401

        # A read key scoped to a *different* project in the same org must not work here.
        other_url = f"/api/v1/orgs/{org_id}/projects/{other_project_id}/query/trend"
        assert (
            await client.post(other_url, json=body, headers={"X-API-Key": read_key})
        ).status_code == 404


async def test_refresh_bypasses_the_cache_but_repopulates_it() -> None:
    """Phase 16 DoD: refresh correctness. A dashboard's Refresh button must show
    current data (not the up-to-60s-stale cached value), and the fresh value
    must be what the next ordinary call is served -- not left behind the old one."""
    org_id, project_id, _ = await _create_org_and_project()
    ch_client = await get_clickhouse_client()

    def event() -> dict[str, object]:
        return generate_fake_event(
            org_id, project_id, event_name="checkout completed", timestamp=_FIXED_TIMESTAMP
        )

    await insert_events(ch_client, [event()])
    spec = _spec()
    first = await query_service.run_trend(spec, org_id, project_id)
    assert (first.cached, first.results[0]["value"]) == (False, 1)

    await insert_events(ch_client, [event()])

    stale = await query_service.run_trend(spec, org_id, project_id)
    assert (stale.cached, stale.results[0]["value"]) == (True, 1)

    refreshed = await query_service.run_trend(spec, org_id, project_id, refresh=True)
    assert (refreshed.cached, refreshed.results[0]["value"]) == (False, 2)

    after = await query_service.run_trend(spec, org_id, project_id)
    assert (after.cached, after.results[0]["value"]) == (True, 2)


@pytest.mark.parametrize("kind", ["funnel", "retention"])
async def test_refresh_is_honoured_by_every_insight_kind(kind: str) -> None:
    org_id, project_id, _ = await _create_org_and_project()
    run = (
        (
            lambda refresh: query_service.run_funnel(
                _funnel_spec(), org_id, project_id, refresh=refresh
            )
        )
        if kind == "funnel"
        else (
            lambda refresh: query_service.run_retention(
                _retention_spec(), org_id, project_id, refresh=refresh
            )
        )
    )

    assert (await run(False)).cached is False
    assert (await run(False)).cached is True
    assert (await run(True)).cached is False
    assert (await run(False)).cached is True


async def test_refresh_query_parameter_reaches_the_query_endpoints() -> None:
    """The HTTP surface: `?refresh=true` is what the dashboard sends, so prove it
    is actually wired to the service for all three endpoints, not just accepted."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        email = f"refresh-{uuid.uuid4().hex[:8]}@example.com"
        await client.post(
            "/api/v1/auth/register", json={"email": email, "password": _PASSWORD, "name": "R"}
        )
        login = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": _PASSWORD}
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        org = await client.post(
            "/api/v1/orgs",
            json={"name": "Refresh Org", "slug": f"refresh-{uuid.uuid4().hex[:8]}"},
            headers=headers,
        )
        org_id = org.json()["id"]
        project = await client.post(
            f"/api/v1/orgs/{org_id}/projects", json={"name": "Web", "slug": "web"}, headers=headers
        )
        base = f"/api/v1/orgs/{org_id}/projects/{project.json()['id']}/query"

        bodies = {
            "trend": _spec().model_dump(mode="json", by_alias=True),
            "funnel": _funnel_spec().model_dump(mode="json", by_alias=True),
            "retention": _retention_spec().model_dump(mode="json", by_alias=True),
        }
        for kind, body in bodies.items():
            url = f"{base}/{kind}"
            assert (await client.post(url, json=body, headers=headers)).json()["cached"] is False
            assert (await client.post(url, json=body, headers=headers)).json()["cached"] is True
            refreshed = await client.post(f"{url}?refresh=true", json=body, headers=headers)
            assert refreshed.status_code == 200
            assert refreshed.json()["cached"] is False
