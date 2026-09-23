"""RBAC and CRUD for pulse/api/pii_rules.py."""

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


async def _register_and_login(client: httpx.AsyncClient, email: str) -> dict[str, str]:
    await client.post(
        "/api/v1/auth/register", json={"email": email, "password": _PASSWORD, "name": email}
    )
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _org_and_project(client: httpx.AsyncClient) -> dict[str, Any]:
    owner_email = f"pii-owner-{uuid.uuid4().hex[:8]}@example.com"
    headers = await _register_and_login(client, owner_email)
    org = await client.post(
        "/api/v1/orgs",
        json={"name": "PII Org", "slug": f"pii-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    org_id = org.json()["id"]
    project = await client.post(
        f"/api/v1/orgs/{org_id}/projects", json={"name": "Web", "slug": "web"}, headers=headers
    )
    project_id = project.json()["id"]
    return {
        "org_id": org_id,
        "project_id": project_id,
        "headers": headers,
        "base": f"/api/v1/orgs/{org_id}/projects/{project_id}/pii-rules",
    }


async def test_create_list_and_delete_a_pii_rule() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        created = await client.post(
            ctx["base"],
            json={"property_key": "email", "action": "hash"},
            headers=ctx["headers"],
        )
        assert created.status_code == 201
        rule_id = created.json()["id"]
        assert created.json()["property_key"] == "email"
        assert created.json()["action"] == "hash"

        listed = await client.get(ctx["base"], headers=ctx["headers"])
        assert listed.status_code == 200
        assert [r["id"] for r in listed.json()] == [rule_id]

        deleted = await client.delete(f"{ctx['base']}/{rule_id}", headers=ctx["headers"])
        assert deleted.status_code == 204

        listed_after = await client.get(ctx["base"], headers=ctx["headers"])
        assert listed_after.json() == []


async def test_a_duplicate_property_key_on_the_same_project_is_409() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        await client.post(
            ctx["base"],
            json={"property_key": "email", "action": "hash"},
            headers=ctx["headers"],
        )
        duplicate = await client.post(
            ctx["base"],
            json={"property_key": "email", "action": "drop"},
            headers=ctx["headers"],
        )
        assert duplicate.status_code == 409


async def test_a_member_cannot_manage_pii_rules_only_an_admin_can() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        member_email = f"member-{uuid.uuid4().hex[:8]}@example.com"
        member_headers = await _register_and_login(client, member_email)
        invite = await client.post(
            f"/api/v1/orgs/{ctx['org_id']}/invites",
            json={"email": member_email, "role": "member"},
            headers=ctx["headers"],
        )
        await client.post(
            "/api/v1/invites/accept",
            json={"token": invite.json()["token"]},
            headers=member_headers,
        )

        response = await client.post(
            ctx["base"], json={"property_key": "email", "action": "hash"}, headers=member_headers
        )
        assert response.status_code == 403


async def test_a_member_of_another_org_gets_404_not_403() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        outsider_headers = await _register_and_login(
            client, f"outsider-{uuid.uuid4().hex[:8]}@example.com"
        )
        response = await client.get(ctx["base"], headers=outsider_headers)
        assert response.status_code == 404


async def test_deleting_an_unknown_rule_is_404() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        response = await client.delete(f"{ctx['base']}/{uuid.uuid4()}", headers=ctx["headers"])
        assert response.status_code == 404


async def test_rules_never_leak_across_projects() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        other_project = await client.post(
            f"/api/v1/orgs/{ctx['org_id']}/projects",
            json={"name": "Other", "slug": "other"},
            headers=ctx["headers"],
        )
        other_project_id = other_project.json()["id"]

        await client.post(
            ctx["base"], json={"property_key": "email", "action": "hash"}, headers=ctx["headers"]
        )
        other_base = f"/api/v1/orgs/{ctx['org_id']}/projects/{other_project_id}/pii-rules"
        listed = await client.get(other_base, headers=ctx["headers"])
        assert listed.json() == []
