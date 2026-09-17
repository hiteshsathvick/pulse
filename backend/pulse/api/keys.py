import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from pulse.api.dependencies import require_role
from pulse.models import ApiKey, ApiKeyType, Membership, MembershipRole
from pulse.services import api_keys as api_keys_service
from pulse.services import projects as projects_service

router = APIRouter(prefix="/api/v1/orgs/{org_id}/projects/{project_id}/keys", tags=["api-keys"])


class CreateApiKeyRequest(BaseModel):
    type: ApiKeyType


class ApiKeyResponse(BaseModel):
    id: uuid.UUID
    type: ApiKeyType
    key_prefix: str
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreatedResponse(ApiKeyResponse):
    # Shown exactly once -- only key_hash is ever persisted. See
    # pulse/services/api_keys.py.
    key: str


def _key_response(key: ApiKey) -> ApiKeyResponse:
    return ApiKeyResponse(
        id=key.id,
        type=key.type,
        key_prefix=key.key_prefix,
        last_used_at=key.last_used_at,
        revoked_at=key.revoked_at,
    )


async def _require_project_in_org(org_id: uuid.UUID, project_id: uuid.UUID) -> None:
    if await projects_service.get_project(org_id, project_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")


@router.post("", response_model=ApiKeyCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    project_id: uuid.UUID,
    body: CreateApiKeyRequest,
    membership: Membership = Depends(require_role(MembershipRole.ADMIN)),
) -> ApiKeyCreatedResponse:
    await _require_project_in_org(membership.org_id, project_id)
    key, raw_key = await api_keys_service.create_api_key(
        membership.org_id, project_id, body.type, membership.user_id
    )
    return ApiKeyCreatedResponse(**_key_response(key).model_dump(), key=raw_key)


@router.get("", response_model=list[ApiKeyResponse])
async def list_api_keys(
    project_id: uuid.UUID,
    membership: Membership = Depends(require_role(MembershipRole.ADMIN)),
) -> list[ApiKeyResponse]:
    await _require_project_in_org(membership.org_id, project_id)
    keys = await api_keys_service.list_api_keys(membership.org_id, project_id)
    return [_key_response(k) for k in keys]


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    project_id: uuid.UUID,
    key_id: uuid.UUID,
    membership: Membership = Depends(require_role(MembershipRole.ADMIN)),
) -> None:
    await _require_project_in_org(membership.org_id, project_id)
    revoked = await api_keys_service.revoke_api_key(membership.org_id, key_id, membership.user_id)
    if not revoked:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
