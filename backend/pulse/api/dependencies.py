import uuid

from fastapi import Depends, HTTPException, status
from sqlalchemy import select

from pulse.core.security import get_current_user
from pulse.models import Membership, MembershipRole, User
from pulse.repositories.postgres import session_scope

_ELEVATED_ROLES = (MembershipRole.OWNER, MembershipRole.ADMIN)


async def get_org_membership(
    org_id: uuid.UUID, current_user: User = Depends(get_current_user)
) -> Membership:
    """Resolves tenant context for a request: is the caller actually a
    member of this org? 404 either way (not 403) -- a non-member shouldn't
    be able to distinguish "org exists, you're just not in it" from "org
    doesn't exist" from the response."""
    async with session_scope(org_id=org_id) as session:
        membership = await session.scalar(
            select(Membership).where(Membership.user_id == current_user.id)
        )
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    return membership


def require_elevated_role(membership: Membership) -> None:
    """Minimal role gating for destructive/elevating actions (delete org or
    project, change a member's role, remove a member) -- not Phase 5's
    systematic, declarative permission-dependency framework applied to
    every endpoint, just enough that these specific actions aren't left
    open to any member regardless of role in the meantime."""
    if membership.role not in _ELEVATED_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires an owner or admin role in this organization",
        )
