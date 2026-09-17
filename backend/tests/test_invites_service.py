import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from alembic import command
from alembic.config import Config
from pulse.models import Invite, MembershipRole, User
from pulse.repositories.postgres import session_scope
from pulse.services import invites as invites_service
from pulse.services import orgs as orgs_service

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


async def _create_org_with_owner() -> tuple[uuid.UUID, uuid.UUID]:
    async with session_scope() as session:
        owner = User(
            email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="not-a-real-hash",
            name="Owner",
        )
        session.add(owner)
        await session.commit()

    org = await orgs_service.create_organization(
        "Test Org", f"org-{uuid.uuid4().hex[:8]}", owner.id
    )
    return org.id, owner.id


async def test_accept_rejects_an_expired_invite() -> None:
    org_id, owner_id = await _create_org_with_owner()
    invite, token = await invites_service.create_invite(
        org_id, "invitee@example.com", MembershipRole.MEMBER, owner_id
    )

    # Force expiry directly rather than waiting out the service's fixed TTL.
    async with session_scope() as session:
        db_invite = await session.get(Invite, invite.id)
        assert db_invite is not None
        db_invite.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    with pytest.raises(invites_service.InvalidInvite):
        await invites_service.accept_invite(token, uuid.uuid4(), "invitee@example.com")


async def test_accept_rejects_a_mismatched_email() -> None:
    org_id, owner_id = await _create_org_with_owner()
    _, token = await invites_service.create_invite(
        org_id, "invitee@example.com", MembershipRole.MEMBER, owner_id
    )

    with pytest.raises(invites_service.InviteEmailMismatch):
        await invites_service.accept_invite(token, uuid.uuid4(), "someone-else@example.com")


async def test_accept_rejects_a_revoked_invite() -> None:
    org_id, owner_id = await _create_org_with_owner()
    invite, token = await invites_service.create_invite(
        org_id, "invitee@example.com", MembershipRole.MEMBER, owner_id
    )
    assert await invites_service.revoke_invite(org_id, invite.id, owner_id)

    with pytest.raises(invites_service.InvalidInvite):
        await invites_service.accept_invite(token, uuid.uuid4(), "invitee@example.com")


async def test_a_second_invite_for_an_already_pending_email_is_rejected() -> None:
    org_id, owner_id = await _create_org_with_owner()
    await invites_service.create_invite(org_id, "dup@example.com", MembershipRole.MEMBER, owner_id)

    with pytest.raises(invites_service.InviteAlreadyPending):
        await invites_service.create_invite(
            org_id, "dup@example.com", MembershipRole.MEMBER, owner_id
        )
