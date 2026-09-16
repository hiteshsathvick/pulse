import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from alembic import command
from alembic.config import Config
from pulse.models import Membership, MembershipRole, Organization, Project, User
from pulse.repositories.postgres import session_scope

_BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _alembic_config() -> Config:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return config


@pytest.fixture(scope="module", autouse=True)
def _control_plane_schema():
    """This module owns the schema lifecycle for its tests rather than
    assuming some other test file left it upgraded. Module-scoped since
    every test here creates its own fresh orgs/users -- no need to pay for
    a migration round-trip per test."""
    config = _alembic_config()
    command.upgrade(config, "head")
    yield
    command.downgrade(config, "base")


async def _create_two_orgs() -> tuple[uuid.UUID, uuid.UUID]:
    async with session_scope() as session:
        org_a = Organization(name="Org A", slug=f"org-a-{uuid.uuid4().hex[:8]}")
        org_b = Organization(name="Org B", slug=f"org-b-{uuid.uuid4().hex[:8]}")
        session.add_all([org_a, org_b])
        await session.commit()
        return org_a.id, org_b.id


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


async def test_projects_are_isolated_by_org() -> None:
    org_a_id, org_b_id = await _create_two_orgs()

    async with session_scope(org_id=org_a_id) as session:
        session.add(Project(org_id=org_a_id, name="A's project", slug="proj", timezone="UTC"))
        await session.commit()

    async with session_scope(org_id=org_b_id) as session:
        session.add(Project(org_id=org_b_id, name="B's project", slug="proj", timezone="UTC"))
        await session.commit()

    # A session scoped to org A sees only org A's project.
    async with session_scope(org_id=org_a_id) as session:
        rows = (await session.execute(select(Project))).scalars().all()
        assert [row.org_id for row in rows] == [org_a_id]

    # ...and a session scoped to org B sees only its own.
    async with session_scope(org_id=org_b_id) as session:
        rows = (await session.execute(select(Project))).scalars().all()
        assert [row.org_id for row in rows] == [org_b_id]

    # An unscoped session sees nothing -- default-deny, not "happened to filter".
    async with session_scope() as session:
        rows = (await session.execute(select(Project))).scalars().all()
        assert rows == []

    # A session scoped to org A cannot insert a row claiming to belong to org B:
    # enforced by the WITH CHECK clause, not by the app remembering to validate.
    async with session_scope(org_id=org_a_id) as session:
        session.add(Project(org_id=org_b_id, name="sneaky", slug="sneaky", timezone="UTC"))
        with pytest.raises(DBAPIError):
            await session.commit()


async def test_memberships_are_isolated_by_org() -> None:
    org_a_id, org_b_id = await _create_two_orgs()
    user_id = await _create_user()

    async with session_scope(org_id=org_a_id) as session:
        session.add(Membership(org_id=org_a_id, user_id=user_id, role=MembershipRole.OWNER))
        await session.commit()

    async with session_scope(org_id=org_b_id) as session:
        session.add(Membership(org_id=org_b_id, user_id=user_id, role=MembershipRole.MEMBER))
        await session.commit()

    async with session_scope(org_id=org_a_id) as session:
        rows = (await session.execute(select(Membership))).scalars().all()
        assert [row.org_id for row in rows] == [org_a_id]

    async with session_scope() as session:
        rows = (await session.execute(select(Membership))).scalars().all()
        assert rows == []
