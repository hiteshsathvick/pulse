import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from pulse.models import Invite, Membership, MembershipRole
from pulse.repositories.postgres import session_scope, set_session_scope
from pulse.services import audit

_INVITE_TTL = timedelta(days=7)


class InviteAlreadyPending(Exception):
    pass


class InvalidInvite(Exception):
    """Unknown token, already accepted, revoked, or expired -- deliberately
    one error for all of these so a caller can't distinguish which."""


class InviteEmailMismatch(Exception):
    pass


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_invite(
    org_id: uuid.UUID, email: str, role: MembershipRole, invited_by: uuid.UUID
) -> tuple[Invite, str]:
    """Returns the invite record and the raw token. No email provider is in
    SPEC.md #2's tech stack yet, so returning the raw token here is an
    explicit stand-in for actually sending it -- wiring real delivery is
    deferred until a provider is chosen."""
    # invites aren't RLS-protected, but audit_logs is -- org-scope the
    # session so the audit write below satisfies its WITH CHECK.
    async with session_scope(org_id=org_id) as session:
        existing = await session.scalar(
            select(Invite).where(
                Invite.org_id == org_id,
                Invite.email == email,
                Invite.accepted_at.is_(None),
                Invite.revoked_at.is_(None),
            )
        )
        if existing is not None and existing.expires_at > datetime.now(UTC):
            raise InviteAlreadyPending()

        token = secrets.token_urlsafe(32)
        invite = Invite(
            org_id=org_id,
            email=email,
            role=role,
            invited_by=invited_by,
            token_hash=_hash_token(token),
            expires_at=datetime.now(UTC) + _INVITE_TTL,
        )
        session.add(invite)
        await session.flush()
        audit.record(
            session,
            org_id=org_id,
            actor_id=invited_by,
            action="invite.created",
            target=str(invite.id),
            metadata={"email": email, "role": role.value},
        )
        await session.commit()
        return invite, token


async def list_invites(org_id: uuid.UUID) -> list[Invite]:
    async with session_scope() as session:
        result = await session.execute(select(Invite).where(Invite.org_id == org_id))
        return list(result.scalars().all())


async def revoke_invite(org_id: uuid.UUID, invite_id: uuid.UUID, actor_id: uuid.UUID) -> bool:
    async with session_scope(org_id=org_id) as session:  # see create_invite on why
        invite = await session.get(Invite, invite_id)
        if invite is None or invite.org_id != org_id:
            return False
        invite.revoked_at = datetime.now(UTC)
        audit.record(
            session,
            org_id=org_id,
            actor_id=actor_id,
            action="invite.revoked",
            target=str(invite_id),
        )
        await session.commit()
        return True


async def accept_invite(
    token: str, accepting_user_id: uuid.UUID, accepting_email: str
) -> Membership:
    """Idempotent if the user is already a member of the invite's org (e.g. a
    double-submitted accept): returns the existing membership rather than
    erroring on the unique (org_id, user_id) constraint."""
    token_hash = _hash_token(token)
    async with session_scope() as session:
        invite = await session.scalar(select(Invite).where(Invite.token_hash == token_hash))
        now = datetime.now(UTC)
        if (
            invite is None
            or invite.accepted_at is not None
            or invite.revoked_at is not None
            or invite.expires_at < now
        ):
            raise InvalidInvite()
        if invite.email.lower() != accepting_email.lower():
            raise InviteEmailMismatch()

        invite.accepted_at = now
        await set_session_scope(session, org_id=invite.org_id)

        existing_membership = await session.scalar(
            select(Membership).where(Membership.user_id == accepting_user_id)
        )
        if existing_membership is not None:
            await session.commit()
            return existing_membership

        membership = Membership(org_id=invite.org_id, user_id=accepting_user_id, role=invite.role)
        session.add(membership)
        audit.record(
            session,
            org_id=invite.org_id,
            actor_id=accepting_user_id,
            action="invite.accepted",
            target=str(invite.id),
        )
        await session.commit()
        return membership
