import uuid
from collections.abc import Callable, Coroutine

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select

from pulse.core.security import get_current_user
from pulse.models import ApiKey, ApiKeyType, Membership, MembershipRole, User
from pulse.repositories.postgres import session_scope
from pulse.services import api_keys as api_keys_service

# OWNER > ADMIN > MEMBER > VIEWER (SPEC.md #4 / PULSE_PROJECT_GUIDE.md).
_ROLE_RANK: dict[MembershipRole, int] = {
    MembershipRole.VIEWER: 0,
    MembershipRole.MEMBER: 1,
    MembershipRole.ADMIN: 2,
    MembershipRole.OWNER: 3,
}


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


def require_role(
    minimum: MembershipRole,
) -> Callable[[Membership], Coroutine[None, None, Membership]]:
    """Dependency factory: the caller's role in this org must be at least
    `minimum`. Use as `Depends(require_role(MembershipRole.ADMIN))` directly
    in a route signature -- this is Phase 5's "permission checks as
    dependencies", replacing Phase 4's single inline elevated-or-not check
    with the full Owner > Admin > Member > Viewer matrix (SPEC.md #4.1)."""

    async def _check(membership: Membership = Depends(get_org_membership)) -> Membership:
        if _ROLE_RANK[membership.role] < _ROLE_RANK[minimum]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires at least the {minimum.value} role in this organization",
            )
        return membership

    return _check


async def require_write_key(x_api_key: str = Header(...)) -> ApiKey:
    """For Phase 7's /ingest to depend on -- built now so the DoD's "write
    keys can only ingest, read keys can only query" is real and tested
    ahead of there being an actual ingest endpoint to attach it to."""
    try:
        return await api_keys_service.resolve_api_key(x_api_key, ApiKeyType.WRITE)
    except api_keys_service.InvalidApiKey as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or revoked write key"
        ) from exc


async def require_read_key(x_api_key: str = Header(...)) -> ApiKey:
    """For Phase 11's query endpoints to depend on -- see require_write_key."""
    try:
        return await api_keys_service.resolve_api_key(x_api_key, ApiKeyType.READ)
    except api_keys_service.InvalidApiKey as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or revoked read key"
        ) from exc
