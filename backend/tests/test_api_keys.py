import uuid
from pathlib import Path

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


async def _owner_setup(client: httpx.AsyncClient) -> tuple[dict, str, str]:
    email = f"owner-{uuid.uuid4().hex[:8]}@example.com"
    await client.post(
        "/api/v1/auth/register", json={"email": email, "password": _PASSWORD, "name": email}
    )
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    org = await client.post(
        "/api/v1/orgs",
        json={"name": "Key Org", "slug": f"key-org-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    org_id = org.json()["id"]

    project = await client.post(
        f"/api/v1/orgs/{org_id}/projects",
        json={"name": "Web", "slug": "web"},
        headers=headers,
    )
    project_id = project.json()["id"]
    return headers, org_id, project_id


async def test_create_list_and_revoke_api_key() -> None:
    async with _client() as client:
        headers, org_id, project_id = await _owner_setup(client)
        base = f"/api/v1/orgs/{org_id}/projects/{project_id}/keys"

        create = await client.post(base, json={"type": "write"}, headers=headers)
        assert create.status_code == 201
        body = create.json()
        assert body["type"] == "write"
        assert body["key"].startswith("pulse_write_")
        assert body["key_prefix"] in body["key"]
        key_id = body["id"]

        listing = await client.get(base, headers=headers)
        assert listing.status_code == 200
        keys = listing.json()
        assert len(keys) == 1
        assert "key" not in keys[0]  # the raw secret is never returned again
        assert keys[0]["revoked_at"] is None

        revoke = await client.delete(f"{base}/{key_id}", headers=headers)
        assert revoke.status_code == 204

        listing_after = await client.get(base, headers=headers)
        assert listing_after.json()[0]["revoked_at"] is not None

        revoke_again = await client.delete(f"{base}/{uuid.uuid4()}", headers=headers)
        assert revoke_again.status_code == 404
