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
    assert login.status_code == 200
    return login.json()


def _auth(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def test_signup_org_project_invite_and_isolation() -> None:
    """The DoD's full acceptance sequence: a user creates an org and a
    project, invites another, each sees only their org's data."""
    async with _client() as client:
        email_a = f"owner-{uuid.uuid4().hex[:8]}@example.com"
        email_b = f"invitee-{uuid.uuid4().hex[:8]}@example.com"
        email_outsider = f"outsider-{uuid.uuid4().hex[:8]}@example.com"

        tokens_a = await _register_and_login(client, email_a)
        user_a_id = (await client.get("/api/v1/auth/me", headers=_auth(tokens_a))).json()["id"]

        create_org = await client.post(
            "/api/v1/orgs",
            json={"name": "Acme", "slug": f"acme-{uuid.uuid4().hex[:8]}"},
            headers=_auth(tokens_a),
        )
        assert create_org.status_code == 201
        org_id = create_org.json()["id"]

        my_orgs = await client.get("/api/v1/orgs", headers=_auth(tokens_a))
        assert any(o["id"] == org_id and o["role"] == "owner" for o in my_orgs.json())

        create_project = await client.post(
            f"/api/v1/orgs/{org_id}/projects",
            json={"name": "Web App", "slug": "web-app"},
            headers=_auth(tokens_a),
        )
        assert create_project.status_code == 201
        project_id = create_project.json()["id"]

        create_invite = await client.post(
            f"/api/v1/orgs/{org_id}/invites",
            json={"email": email_b, "role": "member"},
            headers=_auth(tokens_a),
        )
        assert create_invite.status_code == 201
        invite_token = create_invite.json()["token"]

        tokens_b = await _register_and_login(client, email_b)
        me_b = await client.get("/api/v1/auth/me", headers=_auth(tokens_b))
        user_b_id = me_b.json()["id"]

        accept = await client.post(
            "/api/v1/invites/accept", json={"token": invite_token}, headers=_auth(tokens_b)
        )
        assert accept.status_code == 200
        assert accept.json() == {"org_id": org_id, "role": "member"}

        # rotation-like property: the token is dead once used
        reuse = await client.post(
            "/api/v1/invites/accept", json={"token": invite_token}, headers=_auth(tokens_b)
        )
        assert reuse.status_code == 401

        # user B now sees the org and its project
        b_orgs = await client.get("/api/v1/orgs", headers=_auth(tokens_b))
        assert any(o["id"] == org_id for o in b_orgs.json())
        b_sees_project = await client.get(
            f"/api/v1/orgs/{org_id}/projects/{project_id}", headers=_auth(tokens_b)
        )
        assert b_sees_project.status_code == 200

        # an unrelated third user sees none of this org's data
        tokens_outsider = await _register_and_login(client, email_outsider)
        outsider_org = await client.get(f"/api/v1/orgs/{org_id}", headers=_auth(tokens_outsider))
        assert outsider_org.status_code == 404
        outsider_project = await client.get(
            f"/api/v1/orgs/{org_id}/projects/{project_id}", headers=_auth(tokens_outsider)
        )
        assert outsider_project.status_code == 404
        outsider_orgs = await client.get("/api/v1/orgs", headers=_auth(tokens_outsider))
        assert all(o["id"] != org_id for o in outsider_orgs.json())

        # a plain member (not owner/admin) cannot delete the org or remove members
        member_delete_org = await client.delete(f"/api/v1/orgs/{org_id}", headers=_auth(tokens_b))
        assert member_delete_org.status_code == 403

        # ...but the owner can remove that member
        owner_removes_member = await client.delete(
            f"/api/v1/orgs/{org_id}/members/{user_b_id}", headers=_auth(tokens_a)
        )
        assert owner_removes_member.status_code == 204

        # and the org's sole owner cannot be removed (would orphan the org)
        remove_last_owner = await client.delete(
            f"/api/v1/orgs/{org_id}/members/{user_a_id}", headers=_auth(tokens_a)
        )
        assert remove_last_owner.status_code == 409
