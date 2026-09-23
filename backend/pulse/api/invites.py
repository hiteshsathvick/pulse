import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from pulse.api.dependencies import require_role
from pulse.core.security import get_current_user
from pulse.models import Invite, Membership, MembershipRole, User
from pulse.services import invites as invites_service

org_invites_router = APIRouter(prefix="/api/v1/orgs/{org_id}/invites", tags=["invites"])
invites_router = APIRouter(prefix="/api/v1/invites", tags=["invites"])


class CreateInviteRequest(BaseModel):
    email: str = Field(max_length=320)
    role: MembershipRole = MembershipRole.MEMBER


class InviteResponse(BaseModel):
    id: uuid.UUID
    email: str
    role: MembershipRole
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None


class InviteCreatedResponse(InviteResponse):
    # Stand-in for actually emailing this -- no email provider is in
    # SPEC.md #2's tech stack yet. See pulse/services/invites.py.
    token: str


class AcceptInviteRequest(BaseModel):
    token: str = Field(max_length=512)


class MembershipResponse(BaseModel):
    org_id: uuid.UUID
    role: MembershipRole


def _invite_response(invite: Invite) -> InviteResponse:
    return InviteResponse(
        id=invite.id,
        email=invite.email,
        role=invite.role,
        expires_at=invite.expires_at,
        accepted_at=invite.accepted_at,
        revoked_at=invite.revoked_at,
    )


@org_invites_router.post(
    "", response_model=InviteCreatedResponse, status_code=status.HTTP_201_CREATED
)
async def create_invite(
    body: CreateInviteRequest,
    membership: Membership = Depends(require_role(MembershipRole.ADMIN)),
    current_user: User = Depends(get_current_user),
) -> InviteCreatedResponse:
    if body.role == MembershipRole.OWNER and membership.role != MembershipRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an owner can invite someone as an owner",
        )
    try:
        invite, token = await invites_service.create_invite(
            membership.org_id, body.email, body.role, current_user.id
        )
    except invites_service.InviteAlreadyPending as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An invite is already pending for this email",
        ) from exc
    return InviteCreatedResponse(**_invite_response(invite).model_dump(), token=token)


@org_invites_router.get("", response_model=list[InviteResponse])
async def list_invites(
    membership: Membership = Depends(require_role(MembershipRole.ADMIN)),
) -> list[InviteResponse]:
    invites = await invites_service.list_invites(membership.org_id)
    return [_invite_response(i) for i in invites]


@org_invites_router.delete("/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite(
    invite_id: uuid.UUID,
    membership: Membership = Depends(require_role(MembershipRole.ADMIN)),
) -> None:
    revoked = await invites_service.revoke_invite(membership.org_id, invite_id, membership.user_id)
    if not revoked:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found")


@invites_router.post("/accept", response_model=MembershipResponse)
async def accept_invite(
    body: AcceptInviteRequest, current_user: User = Depends(get_current_user)
) -> MembershipResponse:
    try:
        membership = await invites_service.accept_invite(
            body.token, current_user.id, current_user.email
        )
    except invites_service.InvalidInvite as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired invite"
        ) from exc
    except invites_service.InviteEmailMismatch as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This invite was issued for a different email address",
        ) from exc
    return MembershipResponse(org_id=membership.org_id, role=membership.role)
