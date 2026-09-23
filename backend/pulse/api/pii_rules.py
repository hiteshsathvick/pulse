import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from pulse.api.dependencies import require_role
from pulse.models import Membership, MembershipRole, PiiAction, PiiRule
from pulse.services import pii_rules as pii_rules_service
from pulse.services import projects as projects_service

router = APIRouter(prefix="/api/v1/orgs/{org_id}/projects/{project_id}/pii-rules", tags=["pii"])


class CreatePiiRuleRequest(BaseModel):
    property_key: str = Field(min_length=1, max_length=200)
    action: PiiAction


class PiiRuleResponse(BaseModel):
    id: uuid.UUID
    property_key: str
    action: PiiAction
    created_at: datetime


def _rule_response(rule: PiiRule) -> PiiRuleResponse:
    return PiiRuleResponse(
        id=rule.id, property_key=rule.property_key, action=rule.action, created_at=rule.created_at
    )


async def _require_project_in_org(org_id: uuid.UUID, project_id: uuid.UUID) -> None:
    if await projects_service.get_project(org_id, project_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")


@router.post("", response_model=PiiRuleResponse, status_code=status.HTTP_201_CREATED)
async def create_pii_rule(
    project_id: uuid.UUID,
    body: CreatePiiRuleRequest,
    membership: Membership = Depends(require_role(MembershipRole.ADMIN)),
) -> PiiRuleResponse:
    await _require_project_in_org(membership.org_id, project_id)
    try:
        rule = await pii_rules_service.create_pii_rule(
            membership.org_id, project_id, body.property_key, body.action, membership.user_id
        )
    except pii_rules_service.DuplicatePiiRule as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A rule for this property already exists on this project",
        ) from exc
    return _rule_response(rule)


@router.get("", response_model=list[PiiRuleResponse])
async def list_pii_rules(
    project_id: uuid.UUID,
    membership: Membership = Depends(require_role(MembershipRole.ADMIN)),
) -> list[PiiRuleResponse]:
    await _require_project_in_org(membership.org_id, project_id)
    rules = await pii_rules_service.list_pii_rules(membership.org_id, project_id)
    return [_rule_response(r) for r in rules]


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pii_rule(
    project_id: uuid.UUID,
    rule_id: uuid.UUID,
    membership: Membership = Depends(require_role(MembershipRole.ADMIN)),
) -> None:
    await _require_project_in_org(membership.org_id, project_id)
    deleted = await pii_rules_service.delete_pii_rule(
        membership.org_id, project_id, rule_id, membership.user_id
    )
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PII rule not found")
