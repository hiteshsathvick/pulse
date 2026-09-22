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
from tests.clickhouse_schema import drop_event_schema

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_MIGRATIONS_DIR = _BACKEND_ROOT / "pulse" / "clickhouse_migrations" / "migrations"
_PASSWORD = "correct horse battery staple"


def _alembic_config() -> Config:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return config


@pytest.fixture(scope="module", autouse=True)
def _control_plane_schema() -> Any:
    config = _alembic_config()
    command.upgrade(config, "head")
    yield
    command.downgrade(config, "base")


async def _with_fresh_clickhouse_client(body: Any) -> None:
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
def _events_table() -> Any:
    # Empty, but real -- evaluate-now needs the ClickHouse events/rollup
    # tables to exist so an alert with no matching events yet exercises the
    # real "insufficient data" path, not a table-not-found error.
    asyncio.run(_with_fresh_clickhouse_client(lambda client: migrate(client, _MIGRATIONS_DIR)))
    yield
    asyncio.run(_with_fresh_clickhouse_client(drop_event_schema))


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _register_and_login(client: httpx.AsyncClient, email: str) -> dict[str, str]:
    await client.post(
        "/api/v1/auth/register", json={"email": email, "password": _PASSWORD, "name": email}
    )
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


_TREND_SPEC = {
    "kind": "trend",
    "events": ["checkout completed"],
    "measure": "count",
    "range": {"from": "2026-01-01", "to": "2026-01-31"},
}
_FUNNEL_SPEC = {
    "kind": "funnel",
    "steps": [{"event": "a"}, {"event": "b"}],
    "window": {"value": 1, "unit": "day"},
    "range": {"from": "2026-01-01", "to": "2026-01-31"},
}


async def _org_project_and_insight(
    client: httpx.AsyncClient, *, spec: dict[str, Any] = _TREND_SPEC
) -> dict[str, Any]:
    owner_email = f"alerts-owner-{uuid.uuid4().hex[:8]}@example.com"
    headers = await _register_and_login(client, owner_email)
    org = await client.post(
        "/api/v1/orgs",
        json={"name": "Alerts Org", "slug": f"alerts-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    org_id = org.json()["id"]
    project = await client.post(
        f"/api/v1/orgs/{org_id}/projects", json={"name": "Web", "slug": "web"}, headers=headers
    )
    project_id = project.json()["id"]
    insight = await client.post(
        f"/api/v1/orgs/{org_id}/projects/{project_id}/insights",
        json={"name": "Checkouts", "spec": spec},
        headers=headers,
    )
    return {
        "org_id": org_id,
        "project_id": project_id,
        "insight_id": insight.json()["id"],
        "headers": headers,
        "base": f"/api/v1/orgs/{org_id}/projects/{project_id}/alerts",
    }


def _create_body(ctx: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Too many checkouts",
        "insight_id": ctx["insight_id"],
        "rule": {"kind": "threshold", "comparator": "gt", "value": 10},
        "channels": {"in_app": True},
    }
    body.update(overrides)
    return body


async def test_create_list_get_alert() -> None:
    async with _client() as client:
        ctx = await _org_project_and_insight(client)
        created = await client.post(ctx["base"], json=_create_body(ctx), headers=ctx["headers"])
        assert created.status_code == 201
        alert_id = created.json()["id"]

        listed = await client.get(ctx["base"], headers=ctx["headers"])
        assert listed.status_code == 200
        assert [a["id"] for a in listed.json()] == [alert_id]

        got = await client.get(f"{ctx['base']}/{alert_id}", headers=ctx["headers"])
        assert got.status_code == 200
        assert got.json()["name"] == "Too many checkouts"


async def test_create_with_an_unknown_insight_is_404() -> None:
    async with _client() as client:
        ctx = await _org_project_and_insight(client)
        body = _create_body(ctx, insight_id=str(uuid.uuid4()))
        response = await client.post(ctx["base"], json=body, headers=ctx["headers"])
        assert response.status_code == 404


async def test_an_anomaly_rule_on_a_funnel_insight_is_a_clean_422() -> None:
    async with _client() as client:
        ctx = await _org_project_and_insight(client, spec=_FUNNEL_SPEC)
        body = _create_body(
            ctx, rule={"kind": "anomaly", "method": "zscore", "window": 14, "sensitivity": 3.0}
        )
        response = await client.post(ctx["base"], json=body, headers=ctx["headers"])
        assert response.status_code == 422
        assert "trend insight" in response.json()["error"]["message"]


async def test_a_channel_set_with_nothing_enabled_is_rejected_at_the_request_level() -> None:
    async with _client() as client:
        ctx = await _org_project_and_insight(client)
        body = _create_body(ctx, channels={"in_app": False, "email": [], "webhook_url": None})
        response = await client.post(ctx["base"], json=body, headers=ctx["headers"])
        assert response.status_code == 422


async def test_update_and_delete_alert() -> None:
    async with _client() as client:
        ctx = await _org_project_and_insight(client)
        created = await client.post(ctx["base"], json=_create_body(ctx), headers=ctx["headers"])
        alert_id = created.json()["id"]

        updated = await client.patch(
            f"{ctx['base']}/{alert_id}", json={"enabled": False}, headers=ctx["headers"]
        )
        assert updated.status_code == 200
        assert updated.json()["enabled"] is False

        deleted = await client.delete(f"{ctx['base']}/{alert_id}", headers=ctx["headers"])
        assert deleted.status_code == 204
        refetch = await client.get(f"{ctx['base']}/{alert_id}", headers=ctx["headers"])
        assert refetch.status_code == 404


async def test_update_requires_at_least_one_field() -> None:
    async with _client() as client:
        ctx = await _org_project_and_insight(client)
        created = await client.post(ctx["base"], json=_create_body(ctx), headers=ctx["headers"])
        alert_id = created.json()["id"]
        response = await client.patch(f"{ctx['base']}/{alert_id}", json={}, headers=ctx["headers"])
        assert response.status_code == 422


async def test_a_viewer_cannot_create_an_alert_but_can_list_them() -> None:
    async with _client() as client:
        ctx = await _org_project_and_insight(client)
        await client.post(ctx["base"], json=_create_body(ctx), headers=ctx["headers"])

        viewer_email = f"viewer-{uuid.uuid4().hex[:8]}@example.com"
        viewer_headers = await _register_and_login(client, viewer_email)
        invite = await client.post(
            f"/api/v1/orgs/{ctx['org_id']}/invites",
            json={"email": viewer_email, "role": "viewer"},
            headers=ctx["headers"],
        )
        accept = await client.post(
            "/api/v1/invites/accept",
            json={"token": invite.json()["token"]},
            headers=viewer_headers,
        )
        assert accept.status_code == 200

        listed = await client.get(ctx["base"], headers=viewer_headers)
        assert listed.status_code == 200
        assert len(listed.json()) == 1

        forbidden = await client.post(ctx["base"], json=_create_body(ctx), headers=viewer_headers)
        assert forbidden.status_code == 403


async def test_a_member_of_another_org_gets_404_not_403() -> None:
    async with _client() as client:
        ctx = await _org_project_and_insight(client)
        created = await client.post(ctx["base"], json=_create_body(ctx), headers=ctx["headers"])
        alert_id = created.json()["id"]

        outsider_headers = await _register_and_login(
            client, f"outsider-{uuid.uuid4().hex[:8]}@example.com"
        )
        response = await client.get(f"{ctx['base']}/{alert_id}", headers=outsider_headers)
        assert response.status_code == 404


async def test_alert_events_list_and_acknowledge_via_the_api() -> None:
    async with _client() as client:
        ctx = await _org_project_and_insight(client)
        created = await client.post(ctx["base"], json=_create_body(ctx), headers=ctx["headers"])
        alert_id = created.json()["id"]

        # No ClickHouse events exist yet, so evaluating won't find enough
        # data to fire -- this exercises the 422 "insufficient data" path,
        # not a real fire (that's covered by test_alert_evaluation.py,
        # which does insert real ClickHouse fixture data).
        evaluated = await client.post(
            f"{ctx['base']}/{alert_id}/evaluate-now", headers=ctx["headers"]
        )
        assert evaluated.status_code == 422

        events = await client.get(f"{ctx['base']}/events", headers=ctx["headers"])
        assert events.status_code == 200
        assert events.json() == []


async def test_evaluate_now_on_an_unknown_alert_is_404() -> None:
    async with _client() as client:
        ctx = await _org_project_and_insight(client)
        response = await client.post(
            f"{ctx['base']}/{uuid.uuid4()}/evaluate-now", headers=ctx["headers"]
        )
        assert response.status_code == 404
