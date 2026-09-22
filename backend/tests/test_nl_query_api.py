import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from alembic import command
from alembic.config import Config
from pulse.ai import translator as ai_translator
from pulse.core.config import get_settings
from pulse.main import app
from pulse.repositories.redis import get_client as get_redis_client

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


async def _org_and_project(client: httpx.AsyncClient, name: str = "NL Org") -> dict[str, Any]:
    email = f"nl-{uuid.uuid4().hex[:8]}@example.com"
    await client.post(
        "/api/v1/auth/register", json={"email": email, "password": _PASSWORD, "name": "N"}
    )
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    org = await client.post(
        "/api/v1/orgs", json={"name": name, "slug": f"nl-{uuid.uuid4().hex[:8]}"}, headers=headers
    )
    org_id = org.json()["id"]
    project = await client.post(
        f"/api/v1/orgs/{org_id}/projects", json={"name": "Web", "slug": "web"}, headers=headers
    )
    return {
        "org_id": org_id,
        "project_id": project.json()["id"],
        "headers": headers,
        "base": f"/api/v1/orgs/{org_id}/projects/{project.json()['id']}/query",
    }


async def _nl(client: httpx.AsyncClient, ctx: dict[str, Any], question: str) -> httpx.Response:
    return await client.post(
        f"{ctx['base']}/nl", json={"question": question}, headers=ctx["headers"]
    )


@pytest.fixture
def ai_limit(monkeypatch: pytest.MonkeyPatch):
    def _set(max_requests: int, window_seconds: int = 60) -> None:
        settings = get_settings()
        monkeypatch.setattr(settings, "ai_rate_limit_max_requests", max_requests)
        monkeypatch.setattr(settings, "ai_rate_limit_window_seconds", window_seconds)

    return _set


async def test_a_known_phrasing_returns_an_interpreted_spec_without_running_it() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        response = await _nl(client, ctx, "how many times did checkout completed happen today")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["spec"]["kind"] == "trend"
        assert body["spec"]["events"] == ["checkout completed"]
        assert body["message"] is None


async def test_an_unanswerable_question_asks_for_clarification() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        response = await _nl(client, ctx, "what's the weather like today")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "clarify"
        assert body["spec"] is None
        assert body["message"]


async def test_the_endpoint_requires_authentication() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        response = await client.post(f"{ctx['base']}/nl", json={"question": "checkouts today"})
        assert response.status_code == 401


async def test_a_read_api_key_can_call_it() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        key_response = await client.post(
            f"/api/v1/orgs/{ctx['org_id']}/projects/{ctx['project_id']}/keys",
            json={"type": "read"},
            headers=ctx["headers"],
        )
        assert key_response.status_code == 201
        read_key = key_response.json()["key"]

        response = await client.post(
            f"{ctx['base']}/nl",
            json={"question": "how many times did checkout completed happen today"},
            headers={"X-API-Key": read_key},
        )
        assert response.status_code == 200


async def test_a_write_key_cannot_call_it() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        key_response = await client.post(
            f"/api/v1/orgs/{ctx['org_id']}/projects/{ctx['project_id']}/keys",
            json={"type": "write"},
            headers=ctx["headers"],
        )
        write_key = key_response.json()["key"]

        response = await client.post(
            f"{ctx['base']}/nl",
            json={"question": "checkouts today"},
            headers={"X-API-Key": write_key},
        )
        assert response.status_code == 401


async def test_a_member_of_another_org_cannot_reach_this_projects_nl_endpoint() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client, "Org A")
        outsider_email = f"outsider-{uuid.uuid4().hex[:8]}@example.com"
        await client.post(
            "/api/v1/auth/register",
            json={"email": outsider_email, "password": _PASSWORD, "name": "O"},
        )
        outsider_login = await client.post(
            "/api/v1/auth/login", json={"email": outsider_email, "password": _PASSWORD}
        )
        outsider_headers = {"Authorization": f"Bearer {outsider_login.json()['access_token']}"}

        response = await client.post(
            f"{ctx['base']}/nl", json={"question": "checkouts today"}, headers=outsider_headers
        )
        assert response.status_code == 404


async def test_grounding_never_pulls_in_another_orgs_event_taxonomy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tenant-leakage test: even though grounding reads this project's event
    registry to build the prompt, it must never be able to see another org's
    events. Patches the mock provider to echo back whatever grounding it saw,
    so the test can assert on it directly rather than trusting the prompt
    text alone."""
    from pulse.ai import provider as ai_provider

    captured_systems: list[str] = []

    class _EchoProvider(ai_provider.LLMProvider):
        name = "echo"

        async def translate(self, *, system: str, question: str) -> dict[str, Any]:
            captured_systems.append(system)
            return {"status": "clarify", "message": "echo"}

    monkeypatch.setattr(ai_translator, "get_provider", lambda settings: _EchoProvider())

    async with _client() as client:
        ctx_a = await _org_and_project(client, "Org A")
        ctx_b = await _org_and_project(client, "Org B")

        # Register a distinctive event in org B only, via the registry service
        # directly (no need to go through ingestion for this test).
        from pulse.registry.service import RegistryObservation, register_batch

        await register_batch(
            [
                RegistryObservation(
                    org_id=uuid.UUID(ctx_b["org_id"]),
                    project_id=uuid.UUID(ctx_b["project_id"]),
                    event_name="org-b-secret-event",
                    raw_properties={},
                )
            ]
        )

        await _nl(client, ctx_a, "anything")
        assert "org-b-secret-event" not in captured_systems[-1]


async def test_translations_past_the_limit_get_429_with_a_retry_after(ai_limit) -> None:
    ai_limit(2)
    async with _client() as client:
        ctx = await _org_and_project(client)
        for _ in range(2):
            assert (await _nl(client, ctx, "checkouts today")).status_code == 200

        limited = await _nl(client, ctx, "checkouts today")
        assert limited.status_code == 429
        assert 1 <= int(limited.headers["Retry-After"]) <= 60
        assert limited.json()["error"]["code"] == "rate_limited"


async def test_each_organization_has_its_own_ai_budget(ai_limit) -> None:
    ai_limit(1)
    async with _client() as client:
        noisy = await _org_and_project(client, "Noisy")
        quiet = await _org_and_project(client, "Quiet")
        assert (await _nl(client, noisy, "checkouts today")).status_code == 200
        assert (await _nl(client, noisy, "checkouts today")).status_code == 429
        assert (await _nl(client, quiet, "checkouts today")).status_code == 200


async def test_a_counter_left_without_an_expiry_cannot_lock_an_org_out_forever(ai_limit) -> None:
    ai_limit(2)
    async with _client() as client:
        ctx = await _org_and_project(client)
        key = f"ai:translations:{ctx['org_id']}"
        redis = get_redis_client()
        await redis.set(key, 10_000)
        assert await redis.ttl(key) == -1

        limited = await _nl(client, ctx, "checkouts today")
        assert limited.status_code == 429
        assert int(limited.headers["Retry-After"]) == get_settings().ai_rate_limit_window_seconds
        assert await redis.ttl(key) > 0


async def test_a_translation_failure_is_a_clean_422_not_a_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise ai_translator.TranslationFailed("The model produced an invalid insight spec.")

    monkeypatch.setattr(ai_translator, "translate_question", _boom)

    async with _client() as client:
        ctx = await _org_and_project(client)
        response = await _nl(client, ctx, "checkouts today")
        assert response.status_code == 422
        assert "invalid insight spec" in response.json()["error"]["message"]
