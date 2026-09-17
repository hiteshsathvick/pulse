import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from pulse.models import Membership, MembershipRole, Organization
from pulse.repositories.postgres import session_scope, set_session_scope
from pulse.services import audit


class SlugAlreadyTaken(Exception):
    pass


async def create_organization(name: str, slug: str, creator_user_id: uuid.UUID) -> Organization:
    """Atomic: creates the org (unscoped -- Organization carries no org_id,
    so RLS doesn't apply to it) and its owner Membership (RLS-protected) in
    one transaction, re-scoping the GUC mid-transaction once the org's id
    exists, rather than two commits that could leave an ownerless org on a
    crash in between."""
    async with session_scope() as session:
        existing = await session.scalar(select(Organization).where(Organization.slug == slug))
        if existing is not None:
            raise SlugAlreadyTaken()

        org = Organization(name=name, slug=slug)
        session.add(org)
        await session.flush()  # populates org.id via RETURNING; not committed yet

        await set_session_scope(session, org_id=org.id)
        session.add(Membership(org_id=org.id, user_id=creator_user_id, role=MembershipRole.OWNER))
        audit.record(
            session,
            org_id=org.id,
            actor_id=creator_user_id,
            action="org.created",
            target=str(org.id),
        )

        await session.commit()
        return org


async def list_my_organizations(
    user_id: uuid.UUID,
) -> list[tuple[Organization, MembershipRole]]:
    """Membership is normally only visible within one org's scope; this is
    the one place that's inherently cross-org, hence user_id scoping instead
    of org_id (see SPEC.md #4.1 / migration 0004)."""
    async with session_scope(user_id=user_id) as session:
        result = await session.execute(
            select(Organization, Membership.role)
            .join(Membership, Membership.org_id == Organization.id)
            .where(Membership.user_id == user_id)
        )
        return list(result.tuples().all())


async def get_organization(org_id: uuid.UUID) -> Organization | None:
    async with session_scope(org_id=org_id) as session:
        return await session.get(Organization, org_id)


async def update_organization(
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    name: str | None = None,
    retention_days: int | None = None,
) -> Organization | None:
    async with session_scope(org_id=org_id) as session:
        org = await session.get(Organization, org_id)
        if org is None:
            return None
        if name is not None:
            org.name = name
        if retention_days is not None:
            org.retention_days = retention_days
        audit.record(
            session, org_id=org_id, actor_id=actor_id, action="org.updated", target=str(org_id)
        )
        await session.commit()
        return org


async def delete_organization(org_id: uuid.UUID, actor_id: uuid.UUID) -> bool:
    async with session_scope(org_id=org_id) as session:
        org = await session.get(Organization, org_id)
        if org is None:
            return False
        org.deleted_at = datetime.now(UTC)
        audit.record(
            session, org_id=org_id, actor_id=actor_id, action="org.deleted", target=str(org_id)
        )
        await session.commit()
        return True
