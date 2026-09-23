"""RBAC and the real happy/failure paths for pulse/api/webhooks.py's
test-send endpoint -- lets a user confirm a webhook URL works before wiring
it into a real alert. Never touches a real network: httpx.AsyncClient is
patched to a mock transport, the same pattern test_alert_delivery.py uses."""

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
    owner_email = f"webhooks-owner-{uuid.uuid4().hex[:8]}@example.com"
    headers = await _register_and_login(client, owner_email)
    org = await client.post(
        "/api/v1/orgs",
        json={"name": "Webhooks Org", "slug": f"webhooks-{uuid.uuid4().hex[:8]}"},
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
        "base": f"/api/v1/orgs/{org_id}/projects/{project_id}/webhooks",
    }


def _patch_transport(monkeypatch: pytest.MonkeyPatch, transport: httpx.BaseTransport) -> None:
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


async def test_owner_can_send_a_test_webhook_and_gets_a_successful_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        # Patched only now, not before -- patching httpx.AsyncClient.__init__
        # earlier would also hijack this test client's own ASGI transport,
        # since it's an httpx.AsyncClient too.
        _patch_transport(monkeypatch, httpx.MockTransport(lambda request: httpx.Response(200)))
        response = await client.post(
            f"{ctx['base']}/test",
            json={"url": "https://example.com/hook"},
            headers=ctx["headers"],
        )
        assert response.status_code == 200
        assert response.json() == {"delivered": True, "status_code": 200}


async def test_a_failing_url_reports_delivered_false_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        # A 4xx isn't retried at all (see test_alert_delivery.py), so this
        # stays fast without needing to shrink the retry backoff.
        _patch_transport(monkeypatch, httpx.MockTransport(lambda request: httpx.Response(400)))
        response = await client.post(
            f"{ctx['base']}/test",
            json={"url": "https://example.com/hook"},
            headers=ctx["headers"],
        )
        assert response.status_code == 200
        assert response.json() == {"delivered": False, "status_code": 400}


async def test_a_member_cannot_send_a_test_webhook_only_an_admin_can() -> None:
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
            f"{ctx['base']}/test",
            json={"url": "https://example.com/hook"},
            headers=member_headers,
        )
        assert response.status_code == 403


async def test_a_member_of_another_org_gets_404_not_403() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        outsider_headers = await _register_and_login(
            client, f"outsider-{uuid.uuid4().hex[:8]}@example.com"
        )
        response = await client.post(
            f"{ctx['base']}/test",
            json={"url": "https://example.com/hook"},
            headers=outsider_headers,
        )
        assert response.status_code == 404


async def test_an_unknown_project_in_the_caller_s_own_org_is_404() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        response = await client.post(
            f"/api/v1/orgs/{ctx['org_id']}/projects/{uuid.uuid4()}/webhooks/test",
            json={"url": "https://example.com/hook"},
            headers=ctx["headers"],
        )
        assert response.status_code == 404
