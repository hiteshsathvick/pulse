"""SPEC.md Phase 21 DoD: key scoping and export completeness for
GET .../export/events (streamed CSV/NDJSON, raw events)."""

import asyncio
import csv
import io
import json
import uuid
from datetime import UTC, datetime
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
from pulse.repositories.clickhouse import get_client as get_clickhouse_client
from tests.clickhouse_schema import drop_event_schema

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_MIGRATIONS_DIR = _BACKEND_ROOT / "pulse" / "clickhouse_migrations" / "migrations"
_PASSWORD = "correct horse battery staple"
_FIXED_TIMESTAMP = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


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


async def _org_and_project(client: httpx.AsyncClient) -> dict[str, Any]:
    owner_email = f"export-owner-{uuid.uuid4().hex[:8]}@example.com"
    headers = await _register_and_login(client, owner_email)
    org = await client.post(
        "/api/v1/orgs",
        json={"name": "Export Org", "slug": f"export-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    org_id = org.json()["id"]
    project = await client.post(
        f"/api/v1/orgs/{org_id}/projects", json={"name": "Web", "slug": "web"}, headers=headers
    )
    project_id = project.json()["id"]
    read_key = await client.post(
        f"/api/v1/orgs/{org_id}/projects/{project_id}/keys",
        json={"type": "read"},
        headers=headers,
    )
    return {
        "org_id": org_id,
        "project_id": project_id,
        "headers": headers,
        "read_key": read_key.json()["key"],
        "base": f"/api/v1/orgs/{org_id}/projects/{project_id}/export",
    }


def _range_params(event_name: str | None = None, **overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {"from": "2026-08-15", "to": "2026-08-15"}
    if event_name is not None:
        params["event_name"] = event_name
    params.update(overrides)
    return params


async def test_export_ndjson_includes_every_seeded_event_with_its_properties() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        ch_client = await get_clickhouse_client()
        await insert_events(
            ch_client,
            [
                generate_fake_event(
                    ctx["org_id"],
                    ctx["project_id"],
                    event_name="checkout completed",
                    timestamp=_FIXED_TIMESTAMP,
                    properties={"plan": "pro"},
                )
                for _ in range(4)
            ],
        )

        response = await client.get(
            f"{ctx['base']}/events", params=_range_params(), headers=ctx["headers"]
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")

        lines = [line for line in response.text.splitlines() if line]
        assert len(lines) == 4
        rows = [json.loads(line) for line in lines]
        assert all(row["event_name"] == "checkout completed" for row in rows)
        assert all(row["properties"] == {"plan": "pro"} for row in rows)


async def test_export_csv_has_a_header_and_flattens_properties_to_json() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        ch_client = await get_clickhouse_client()
        await insert_events(
            ch_client,
            [
                generate_fake_event(
                    ctx["org_id"],
                    ctx["project_id"],
                    event_name="signup",
                    timestamp=_FIXED_TIMESTAMP,
                    properties={"source": "ad"},
                )
            ],
        )

        response = await client.get(
            f"{ctx['base']}/events",
            params=_range_params(format="csv"),
            headers=ctx["headers"],
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")

        reader = csv.DictReader(io.StringIO(response.text))
        rows = list(reader)
        assert reader.fieldnames == [
            "event_id",
            "event_name",
            "user_id",
            "anonymous_id",
            "timestamp",
            "received_at",
            "properties",
        ]
        assert len(rows) == 1
        assert rows[0]["event_name"] == "signup"
        assert json.loads(rows[0]["properties"]) == {"source": "ad"}


async def test_event_name_filter_excludes_other_event_names() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        ch_client = await get_clickhouse_client()
        await insert_events(
            ch_client,
            [
                generate_fake_event(
                    ctx["org_id"], ctx["project_id"], event_name="a", timestamp=_FIXED_TIMESTAMP
                ),
                generate_fake_event(
                    ctx["org_id"], ctx["project_id"], event_name="b", timestamp=_FIXED_TIMESTAMP
                ),
            ],
        )

        response = await client.get(
            f"{ctx['base']}/events",
            params=_range_params(event_name="a"),
            headers=ctx["headers"],
        )
        lines = [line for line in response.text.splitlines() if line]
        assert len(lines) == 1
        assert json.loads(lines[0])["event_name"] == "a"


async def test_an_empty_range_streams_zero_rows_not_an_error() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        response = await client.get(
            f"{ctx['base']}/events", params=_range_params(), headers=ctx["headers"]
        )
        assert response.status_code == 200
        assert response.text == ""


async def test_export_never_leaks_across_tenants() -> None:
    async with _client() as client:
        ctx_a = await _org_and_project(client)
        ctx_b = await _org_and_project(client)
        ch_client = await get_clickhouse_client()
        await insert_events(
            ch_client,
            [
                generate_fake_event(
                    ctx_a["org_id"],
                    ctx_a["project_id"],
                    event_name="checkout completed",
                    timestamp=_FIXED_TIMESTAMP,
                )
            ],
        )
        await insert_events(
            ch_client,
            [
                generate_fake_event(
                    ctx_b["org_id"],
                    ctx_b["project_id"],
                    event_name="checkout completed",
                    timestamp=_FIXED_TIMESTAMP,
                )
                for _ in range(3)
            ],
        )

        response_a = await client.get(
            f"{ctx_a['base']}/events", params=_range_params(), headers=ctx_a["headers"]
        )
        response_b = await client.get(
            f"{ctx_b['base']}/events", params=_range_params(), headers=ctx_b["headers"]
        )
        assert len([line for line in response_a.text.splitlines() if line]) == 1
        assert len([line for line in response_b.text.splitlines() if line]) == 3


async def test_a_read_key_scoped_to_a_different_project_is_rejected_with_404() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        other_project = await client.post(
            f"/api/v1/orgs/{ctx['org_id']}/projects",
            json={"name": "Other", "slug": "other"},
            headers=ctx["headers"],
        )
        other_project_id = other_project.json()["id"]

        url = f"/api/v1/orgs/{ctx['org_id']}/projects/{other_project_id}/export/events"
        response = await client.get(
            url, params=_range_params(), headers={"X-API-Key": ctx["read_key"]}
        )
        assert response.status_code == 404


async def test_a_read_key_for_the_right_project_can_export() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        ch_client = await get_clickhouse_client()
        await insert_events(
            ch_client,
            [
                generate_fake_event(
                    ctx["org_id"],
                    ctx["project_id"],
                    event_name="checkout completed",
                    timestamp=_FIXED_TIMESTAMP,
                )
            ],
        )

        response = await client.get(
            f"{ctx['base']}/events",
            params=_range_params(),
            headers={"X-API-Key": ctx["read_key"]},
        )
        assert response.status_code == 200
        assert len([line for line in response.text.splitlines() if line]) == 1


async def test_no_auth_at_all_is_rejected() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        response = await client.get(f"{ctx['base']}/events", params=_range_params())
        assert response.status_code == 401


async def test_the_row_cap_truncates_rather_than_reading_unbounded_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "export_max_rows", 2)
    async with _client() as client:
        ctx = await _org_and_project(client)
        ch_client = await get_clickhouse_client()
        await insert_events(
            ch_client,
            [
                generate_fake_event(
                    ctx["org_id"],
                    ctx["project_id"],
                    event_name="checkout completed",
                    timestamp=_FIXED_TIMESTAMP,
                )
                for _ in range(5)
            ],
        )

        response = await client.get(
            f"{ctx['base']}/events", params=_range_params(), headers=ctx["headers"]
        )
        assert response.status_code == 200
        assert len([line for line in response.text.splitlines() if line]) == 2


async def test_an_unknown_project_is_404() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        url = f"/api/v1/orgs/{ctx['org_id']}/projects/{uuid.uuid4()}/export/events"
        response = await client.get(url, params=_range_params(), headers=ctx["headers"])
        assert response.status_code == 404
