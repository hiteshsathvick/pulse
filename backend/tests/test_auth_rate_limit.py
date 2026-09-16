from pathlib import Path

import httpx
import pytest

from alembic import command
from alembic.config import Config
from pulse.core.config import get_settings
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


async def test_login_is_rate_limited_after_max_attempts() -> None:
    """DoD: brute-force limit. Every attempt below the threshold still gets a
    normal 401 (wrong credentials) -- it's only once the limit is exceeded
    that the endpoint starts refusing outright with 429."""
    settings = get_settings()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(settings.auth_rate_limit_max_attempts):
            response = await client.post(
                "/api/v1/auth/login",
                json={"email": "nobody@example.com", "password": "wrong"},
            )
            assert response.status_code == 401

        limited_response = await client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "wrong"},
        )

    assert limited_response.status_code == 429
    assert limited_response.json()["error"]["code"] == "rate_limited"
