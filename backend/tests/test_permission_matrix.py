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
def _control_plane_schema():
    config = _alembic_config()
    command.upgrade(config, "head")
    yield
    command.downgrade(config, "base")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _register_and_login(client: httpx.AsyncClient, email: str) -> dict[str, Any]:
    await client.post(
        "/api/v1/auth/register", json={"email": email, "password": _PASSWORD, "name": email}
    )
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    return login.json()


def _auth(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _build_org_with_all_roles(client: httpx.AsyncClient) -> dict[str, Any]:
    """One org, one project, and a real user seated at each of the four
    roles via the actual invite-accept flow -- not seeded directly into the
    DB, so this also re-proves the invite lifecycle end to end."""
    owner_email = f"owner-{uuid.uuid4().hex[:8]}@example.com"
    owner_tokens = await _register_and_login(client, owner_email)
    owner_headers = _auth(owner_tokens)

    org = await client.post(
        "/api/v1/orgs",
        json={"name": "Matrix Org", "slug": f"matrix-{uuid.uuid4().hex[:8]}"},
        headers=owner_headers,
    )
    org_id = org.json()["id"]

    project = await client.post(
        f"/api/v1/orgs/{org_id}/projects",
        json={"name": "Web", "slug": "web"},
        headers=owner_headers,
    )
    project_id = project.json()["id"]

    role_headers = {"owner": owner_headers}
    for role in ("viewer", "member", "admin"):
        email = f"{role}-{uuid.uuid4().hex[:8]}@example.com"
        invite = await client.post(
            f"/api/v1/orgs/{org_id}/invites",
            json={"email": email, "role": role},
            headers=owner_headers,
        )
        token = invite.json()["token"]
        tokens = await _register_and_login(client, email)
        headers = _auth(tokens)
        accept = await client.post("/api/v1/invites/accept", json={"token": token}, headers=headers)
        assert accept.status_code == 200
        role_headers[role] = headers

    return {"org_id": org_id, "project_id": project_id, "headers": role_headers}


async def test_viewer_can_read_but_not_write() -> None:
    async with _client() as client:
        ctx = await _build_org_with_all_roles(client)
        org_id, project_id, h = ctx["org_id"], ctx["project_id"], ctx["headers"]["viewer"]

        assert (await client.get(f"/api/v1/orgs/{org_id}", headers=h)).status_code == 200
        assert (await client.get(f"/api/v1/orgs/{org_id}/members", headers=h)).status_code == 200
        assert (
            await client.get(f"/api/v1/orgs/{org_id}/projects/{project_id}", headers=h)
        ).status_code == 200

        assert (
            await client.post(
                f"/api/v1/orgs/{org_id}/projects", json={"name": "X", "slug": "x"}, headers=h
            )
        ).status_code == 403
        assert (
            await client.patch(f"/api/v1/orgs/{org_id}", json={"name": "Renamed"}, headers=h)
        ).status_code == 403
        assert (
            await client.post(
                f"/api/v1/orgs/{org_id}/invites",
                json={"email": "x@example.com", "role": "member"},
                headers=h,
            )
        ).status_code == 403
        assert (await client.delete(f"/api/v1/orgs/{org_id}", headers=h)).status_code == 403


async def test_member_can_create_projects_but_not_manage_org() -> None:
    async with _client() as client:
        ctx = await _build_org_with_all_roles(client)
        org_id, h = ctx["org_id"], ctx["headers"]["member"]

        create = await client.post(
            f"/api/v1/orgs/{org_id}/projects",
            json={"name": "Member's Project", "slug": "member-project"},
            headers=h,
        )
        assert create.status_code == 201
        project_id = create.json()["id"]

        assert (
            await client.delete(f"/api/v1/orgs/{org_id}/projects/{project_id}", headers=h)
        ).status_code == 403
        assert (
            await client.patch(f"/api/v1/orgs/{org_id}", json={"name": "Renamed"}, headers=h)
        ).status_code == 403
        assert (
            await client.post(
                f"/api/v1/orgs/{org_id}/invites",
                json={"email": "x@example.com", "role": "member"},
                headers=h,
            )
        ).status_code == 403


async def test_admin_can_manage_but_not_delete_org_or_grant_owner() -> None:
    async with _client() as client:
        ctx = await _build_org_with_all_roles(client)
        org_id, project_id, h = ctx["org_id"], ctx["project_id"], ctx["headers"]["admin"]

        assert (
            await client.patch(f"/api/v1/orgs/{org_id}", json={"name": "Renamed"}, headers=h)
        ).status_code == 200
        assert (
            await client.delete(f"/api/v1/orgs/{org_id}/projects/{project_id}", headers=h)
        ).status_code == 204

        invite = await client.post(
            f"/api/v1/orgs/{org_id}/invites",
            json={"email": f"friend-{uuid.uuid4().hex[:8]}@example.com", "role": "member"},
            headers=h,
        )
        assert invite.status_code == 201

        # an admin cannot invite someone directly as owner...
        owner_invite = await client.post(
            f"/api/v1/orgs/{org_id}/invites",
            json={"email": f"wannabe-{uuid.uuid4().hex[:8]}@example.com", "role": "owner"},
            headers=h,
        )
        assert owner_invite.status_code == 403

        # ...nor promote an existing member to owner...
        member_id = (await client.get("/api/v1/auth/me", headers=ctx["headers"]["member"])).json()[
            "id"
        ]
        promote = await client.patch(
            f"/api/v1/orgs/{org_id}/members/{member_id}",
            json={"role": "owner"},
            headers=h,
        )
        assert promote.status_code == 403

        # ...nor delete the org outright (owner-only).
        assert (await client.delete(f"/api/v1/orgs/{org_id}", headers=h)).status_code == 403


async def test_owner_can_do_everything_including_delete_org() -> None:
    async with _client() as client:
        ctx = await _build_org_with_all_roles(client)
        org_id, h = ctx["org_id"], ctx["headers"]["owner"]

        member_id = (await client.get("/api/v1/auth/me", headers=ctx["headers"]["member"])).json()[
            "id"
        ]
        promote = await client.patch(
            f"/api/v1/orgs/{org_id}/members/{member_id}", json={"role": "owner"}, headers=h
        )
        assert promote.status_code == 200
        assert promote.json()["role"] == "owner"

        assert (await client.delete(f"/api/v1/orgs/{org_id}", headers=h)).status_code == 204


async def test_only_admin_or_above_can_manage_api_keys() -> None:
    async with _client() as client:
        ctx = await _build_org_with_all_roles(client)
        org_id, project_id = ctx["org_id"], ctx["project_id"]
        base = f"/api/v1/orgs/{org_id}/projects/{project_id}/keys"

        for role in ("viewer", "member"):
            response = await client.post(base, json={"type": "write"}, headers=ctx["headers"][role])
            assert response.status_code == 403, f"{role} should not be able to create a key"

        admin_create = await client.post(
            base, json={"type": "read"}, headers=ctx["headers"]["admin"]
        )
        assert admin_create.status_code == 201
