import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from alembic import command
from alembic.config import Config
from pulse.models import AuditLog, User
from pulse.repositories.postgres import session_scope
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


async def _create_user() -> uuid.UUID:
    async with session_scope() as session:
        user = User(
            email=f"user-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="not-a-real-hash",
            name="Test User",
        )
        session.add(user)
        await session.commit()
        return user.id


async def _actions(org_id: uuid.UUID) -> list[str]:
    async with session_scope(org_id=org_id) as session:
        result = await session.execute(select(AuditLog.action).where(AuditLog.org_id == org_id))
        return list(result.scalars().all())


async def test_org_and_project_mutations_write_audit_entries() -> None:
    owner_id = await _create_user()

    org = await orgs_service.create_organization(
        "Audit Test Org", f"audit-org-{uuid.uuid4().hex[:8]}", owner_id
    )
    assert "org.created" in await _actions(org.id)

    await orgs_service.update_organization(org.id, owner_id, name="Renamed")
    assert "org.updated" in await _actions(org.id)

    project = await projects_service.create_project(org.id, "Web", "web", "UTC", owner_id)
    assert "project.created" in await _actions(org.id)

    await projects_service.delete_project(org.id, project.id, owner_id)
    assert "project.deleted" in await _actions(org.id)

    await orgs_service.delete_organization(org.id, owner_id)
    assert "org.deleted" in await _actions(org.id)
