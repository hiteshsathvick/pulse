import uuid
from pathlib import Path

import httpx
import pytest

from alembic import command
from alembic.config import Config
from pulse.main import app

_BACKEND_ROOT = Path(__file__).resolve().parent.parent


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


async def test_signup_login_protected_route_refresh_rotation_and_logout() -> None:
    """The DoD's full acceptance sequence in one flow: signup -> login ->
    protected route -> refresh (with rotation) -> logout (with revocation)."""
    email = f"user-{uuid.uuid4().hex[:8]}@example.com"
    password = "correct horse battery staple"

    async with _client() as client:
        register_response = await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "name": "Test User"},
        )
        assert register_response.status_code == 201
        assert register_response.json()["email"] == email

        duplicate_response = await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "name": "Test User"},
        )
        assert duplicate_response.status_code == 409

        login_response = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
        assert login_response.status_code == 200
        tokens = login_response.json()

        wrong_password_response = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": "wrong password"}
        )
        assert wrong_password_response.status_code == 401

        me_response = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        assert me_response.status_code == 200
        assert me_response.json()["email"] == email

        anonymous_response = await client.get("/api/v1/auth/me")
        assert anonymous_response.status_code == 401

        refresh_response = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert refresh_response.status_code == 200
        rotated_tokens = refresh_response.json()
        assert rotated_tokens["refresh_token"] != tokens["refresh_token"]

        # rotation: the old refresh token cannot be reused
        reuse_response = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert reuse_response.status_code == 401

        logout_response = await client.post(
            "/api/v1/auth/logout", json={"refresh_token": rotated_tokens["refresh_token"]}
        )
        assert logout_response.status_code == 204

        # revocation: a logged-out refresh token is dead too
        after_logout_response = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": rotated_tokens["refresh_token"]}
        )
        assert after_logout_response.status_code == 401


async def test_register_rejects_a_too_short_password() -> None:
    async with _client() as client:
        response = await client.post(
            "/api/v1/auth/register",
            json={"email": "short@example.com", "password": "short", "name": "Test"},
        )
    assert response.status_code == 422
