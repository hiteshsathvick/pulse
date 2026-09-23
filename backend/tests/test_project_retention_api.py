"""PATCH .../projects/{id}'s retention_days: a real three-state field
(unset/omitted, explicitly null, explicitly a value) which plain `if x is
not None` PATCH semantics can't distinguish -- pulse/services/projects.py's
UNSET sentinel is what makes "clear my override" different from "I didn't
mention retention at all"."""

import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from alembic import command
from alembic.config import Config
from pulse.main import app

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
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


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _org_and_project(client: httpx.AsyncClient) -> dict[str, Any]:
    owner_email = f"retention-owner-{uuid.uuid4().hex[:8]}@example.com"
    await client.post(
        "/api/v1/auth/register",
        json={"email": owner_email, "password": _PASSWORD, "name": owner_email},
    )
    login = await client.post(
        "/api/v1/auth/login", json={"email": owner_email, "password": _PASSWORD}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    org = await client.post(
        "/api/v1/orgs",
        json={"name": "Retention Org", "slug": f"retention-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    org_id = org.json()["id"]
    project = await client.post(
        f"/api/v1/orgs/{org_id}/projects", json={"name": "Web", "slug": "web"}, headers=headers
    )
    return {
        "headers": headers,
        "url": f"/api/v1/orgs/{org_id}/projects/{project.json()['id']}",
        "retention_days": project.json()["retention_days"],
    }


async def test_a_new_project_has_no_retention_override_by_default() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        assert ctx["retention_days"] is None


async def test_setting_a_value_creates_an_override() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        response = await client.patch(
            ctx["url"], json={"retention_days": 30}, headers=ctx["headers"]
        )
        assert response.status_code == 200
        assert response.json()["retention_days"] == 30


async def test_omitting_the_field_leaves_an_existing_override_untouched() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        await client.patch(ctx["url"], json={"retention_days": 30}, headers=ctx["headers"])

        response = await client.patch(ctx["url"], json={"name": "Web v2"}, headers=ctx["headers"])
        assert response.status_code == 200
        assert response.json()["retention_days"] == 30


async def test_explicit_null_clears_an_existing_override() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        await client.patch(ctx["url"], json={"retention_days": 30}, headers=ctx["headers"])

        response = await client.patch(
            ctx["url"], json={"retention_days": None}, headers=ctx["headers"]
        )
        assert response.status_code == 200
        assert response.json()["retention_days"] is None


async def test_a_non_positive_value_is_rejected() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        response = await client.patch(
            ctx["url"], json={"retention_days": 0}, headers=ctx["headers"]
        )
        assert response.status_code == 422
