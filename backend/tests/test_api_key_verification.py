import uuid
from pathlib import Path

import pytest

from alembic import command
from alembic.config import Config
from pulse.api.dependencies import require_read_key, require_write_key
from pulse.models import ApiKeyType, User
from pulse.repositories.postgres import session_scope
from pulse.services import api_keys as api_keys_service
from pulse.services import orgs as orgs_service
from pulse.services import projects as projects_service

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


async def _create_org_and_project() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with session_scope() as session:
        user = User(
            email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="not-a-real-hash",
            name="Owner",
        )
        session.add(user)
        await session.commit()

    org = await orgs_service.create_organization(
        "Key Test Org", f"key-org-{uuid.uuid4().hex[:8]}", user.id
    )
    project = await projects_service.create_project(org.id, "Web", "web", "UTC", user.id)
    return org.id, project.id, user.id


async def test_write_key_passes_write_check_and_fails_read_check() -> None:
    """DoD: write keys can only ingest (i.e. only pass require_write_key)."""
    org_id, project_id, actor_id = await _create_org_and_project()
    _, raw_key = await api_keys_service.create_api_key(
        org_id, project_id, ApiKeyType.WRITE, actor_id
    )

    resolved = await api_keys_service.resolve_api_key(raw_key, ApiKeyType.WRITE)
    assert resolved.type == ApiKeyType.WRITE

    with pytest.raises(api_keys_service.InvalidApiKey):
        await api_keys_service.resolve_api_key(raw_key, ApiKeyType.READ)


async def test_read_key_passes_read_check_and_fails_write_check() -> None:
    """DoD: read keys can only query (i.e. only pass require_read_key)."""
    org_id, project_id, actor_id = await _create_org_and_project()
    _, raw_key = await api_keys_service.create_api_key(
        org_id, project_id, ApiKeyType.READ, actor_id
    )

    resolved = await api_keys_service.resolve_api_key(raw_key, ApiKeyType.READ)
    assert resolved.type == ApiKeyType.READ

    with pytest.raises(api_keys_service.InvalidApiKey):
        await api_keys_service.resolve_api_key(raw_key, ApiKeyType.WRITE)


async def test_revoked_key_fails_regardless_of_type() -> None:
    org_id, project_id, actor_id = await _create_org_and_project()
    key, raw_key = await api_keys_service.create_api_key(
        org_id, project_id, ApiKeyType.WRITE, actor_id
    )
    assert await api_keys_service.revoke_api_key(org_id, key.id, actor_id)

    with pytest.raises(api_keys_service.InvalidApiKey):
        await api_keys_service.resolve_api_key(raw_key, ApiKeyType.WRITE)


async def test_unknown_key_is_rejected() -> None:
    with pytest.raises(api_keys_service.InvalidApiKey):
        await api_keys_service.resolve_api_key("pulse_write_not-a-real-key", ApiKeyType.WRITE)


async def test_fastapi_dependencies_map_type_mismatch_to_401() -> None:
    """Exercises the actual FastAPI dependency Phase 7/11 will use, not just
    the service layer underneath it."""
    from fastapi import HTTPException

    org_id, project_id, actor_id = await _create_org_and_project()
    _, write_key = await api_keys_service.create_api_key(
        org_id, project_id, ApiKeyType.WRITE, actor_id
    )

    resolved = await require_write_key(x_api_key=write_key)
    assert resolved.type == ApiKeyType.WRITE

    with pytest.raises(HTTPException) as exc_info:
        await require_read_key(x_api_key=write_key)
    assert exc_info.value.status_code == 401
