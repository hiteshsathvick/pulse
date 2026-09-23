import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from pulse.api.dependencies import get_org_membership, require_role
from pulse.core.security import get_current_user
from pulse.models import Membership, MembershipRole, Organization, User
from pulse.repositories.postgres import session_scope
from pulse.services import audit
from pulse.services import orgs as orgs_service

router = APIRouter(prefix="/api/v1/orgs", tags=["organizations"])


class CreateOrgRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=63)


class UpdateOrgRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    retention_days: int | None = Field(default=None, ge=1)


class OrgResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    retention_days: int


class MyOrgResponse(OrgResponse):
    role: MembershipRole


class MemberResponse(BaseModel):
    user_id: uuid.UUID
    email: str
    name: str
    role: MembershipRole


class UpdateMemberRoleRequest(BaseModel):
    role: MembershipRole


def _org_response(org: Organization) -> OrgResponse:
    return OrgResponse(id=org.id, name=org.name, slug=org.slug, retention_days=org.retention_days)


async def _owner_count(org_id: uuid.UUID) -> int:
    async with session_scope(org_id=org_id) as session:
        count = await session.scalar(
            select(func.count())
            .select_from(Membership)
            .where(Membership.org_id == org_id, Membership.role == MembershipRole.OWNER)
        )
    return count or 0


@router.post("", response_model=OrgResponse, status_code=status.HTTP_201_CREATED)
async def create_org(
    body: CreateOrgRequest, current_user: User = Depends(get_current_user)
) -> OrgResponse:
    try:
        org = await orgs_service.create_organization(body.name, body.slug, current_user.id)
    except orgs_service.SlugAlreadyTaken as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Slug already taken"
        ) from exc
    return _org_response(org)


@router.get("", response_model=list[MyOrgResponse])
async def list_my_orgs(current_user: User = Depends(get_current_user)) -> list[MyOrgResponse]:
    rows = await orgs_service.list_my_organizations(current_user.id)
    return [
        MyOrgResponse(
            id=org.id, name=org.name, slug=org.slug, retention_days=org.retention_days, role=role
        )
        for org, role in rows
    ]


@router.get("/{org_id}", response_model=OrgResponse)
async def get_org(membership: Membership = Depends(get_org_membership)) -> OrgResponse:
    org = await orgs_service.get_organization(membership.org_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    return _org_response(org)


@router.patch("/{org_id}", response_model=OrgResponse)
async def update_org(
    body: UpdateOrgRequest,
    membership: Membership = Depends(require_role(MembershipRole.ADMIN)),
) -> OrgResponse:
    org = await orgs_service.update_organization(
        membership.org_id,
        membership.user_id,
        name=body.name,
        retention_days=body.retention_days,
    )
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    return _org_response(org)


@router.delete("/{org_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_org(membership: Membership = Depends(require_role(MembershipRole.OWNER))) -> None:
    deleted = await orgs_service.delete_organization(membership.org_id, membership.user_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")


@router.get("/{org_id}/members", response_model=list[MemberResponse])
async def list_members(
    membership: Membership = Depends(get_org_membership),
) -> list[MemberResponse]:
    async with session_scope(org_id=membership.org_id) as session:
        result = await session.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(Membership.org_id == membership.org_id)
        )
        rows = result.all()
    return [
        MemberResponse(user_id=user.id, email=user.email, name=user.name, role=member.role)
        for member, user in rows
    ]


@router.patch("/{org_id}/members/{user_id}", response_model=MemberResponse)
async def update_member_role(
    user_id: uuid.UUID,
    body: UpdateMemberRoleRequest,
    membership: Membership = Depends(require_role(MembershipRole.ADMIN)),
) -> MemberResponse:
    # Only an owner can mint another owner -- otherwise an admin could
    # self-escalate by promoting an accomplice (or themselves, via a second
    # account) to owner.
    if body.role == MembershipRole.OWNER and membership.role != MembershipRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an owner can grant the owner role",
        )

    async with session_scope(org_id=membership.org_id) as session:
        target = await session.scalar(select(Membership).where(Membership.user_id == user_id))
        if target is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")

        if target.role == MembershipRole.OWNER and body.role != MembershipRole.OWNER:
            if await _owner_count(membership.org_id) <= 1:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Cannot demote the organization's last owner",
                )

        target.role = body.role
        audit.record(
            session,
            org_id=membership.org_id,
            actor_id=membership.user_id,
            action="member.role_updated",
            target=str(user_id),
            metadata={"new_role": body.role.value},
        )
        await session.commit()
        user = await session.get(User, user_id)

    assert user is not None
    return MemberResponse(user_id=user_id, email=user.email, name=user.name, role=body.role)


@router.delete("/{org_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    user_id: uuid.UUID,
    membership: Membership = Depends(require_role(MembershipRole.ADMIN)),
) -> None:
    async with session_scope(org_id=membership.org_id) as session:
        target = await session.scalar(select(Membership).where(Membership.user_id == user_id))
        if target is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")

        if target.role == MembershipRole.OWNER and await _owner_count(membership.org_id) <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot remove the organization's last owner",
            )

        await session.delete(target)
        audit.record(
            session,
            org_id=membership.org_id,
            actor_id=membership.user_id,
            action="member.removed",
            target=str(user_id),
        )
        await session.commit()
