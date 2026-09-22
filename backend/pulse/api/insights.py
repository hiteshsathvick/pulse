import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, model_validator

from pulse.api.dependencies import require_role
from pulse.insights import service as insights_service
from pulse.models import Insight, InsightKind, Membership, MembershipRole
from pulse.query.spec import DiscriminatedInsightSpec as DiscriminatedSpec
from pulse.services import projects as projects_service

router = APIRouter(prefix="/api/v1/orgs/{org_id}/projects/{project_id}/insights", tags=["insights"])


class CreateInsightRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    spec: DiscriminatedSpec


class UpdateInsightRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    spec: DiscriminatedSpec | None = None

    @model_validator(mode="after")
    def _require_a_change(self) -> "UpdateInsightRequest":
        if self.name is None and self.spec is None:
            raise ValueError("provide at least one of name or spec")
        return self


class InsightResponse(BaseModel):
    id: uuid.UUID
    name: str
    kind: InsightKind
    spec: dict[str, object]
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


def _response(insight: Insight) -> InsightResponse:
    return InsightResponse(
        id=insight.id,
        name=insight.name,
        kind=insight.kind,
        spec=insight.spec,
        created_by=insight.created_by,
        created_at=insight.created_at,
        updated_at=insight.updated_at,
    )


async def _require_project_in_org(org_id: uuid.UUID, project_id: uuid.UUID) -> None:
    if await projects_service.get_project(org_id, project_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Insight not found")


@router.get("", response_model=list[InsightResponse])
async def list_insights(
    project_id: uuid.UUID,
    membership: Membership = Depends(require_role(MembershipRole.VIEWER)),
) -> list[InsightResponse]:
    await _require_project_in_org(membership.org_id, project_id)
    insights = await insights_service.list_insights(membership.org_id, project_id)
    return [_response(i) for i in insights]


@router.post("", response_model=InsightResponse, status_code=status.HTTP_201_CREATED)
async def create_insight(
    project_id: uuid.UUID,
    body: CreateInsightRequest,
    membership: Membership = Depends(require_role(MembershipRole.MEMBER)),
) -> InsightResponse:
    await _require_project_in_org(membership.org_id, project_id)
    insight = await insights_service.create_insight(
        membership.org_id, project_id, body.name, body.spec, membership.user_id
    )
    return _response(insight)


@router.get("/{insight_id}", response_model=InsightResponse)
async def get_insight(
    project_id: uuid.UUID,
    insight_id: uuid.UUID,
    membership: Membership = Depends(require_role(MembershipRole.VIEWER)),
) -> InsightResponse:
    await _require_project_in_org(membership.org_id, project_id)
    insight = await insights_service.get_insight(membership.org_id, project_id, insight_id)
    if insight is None:
        raise _not_found()
    return _response(insight)


@router.patch("/{insight_id}", response_model=InsightResponse)
async def update_insight(
    project_id: uuid.UUID,
    insight_id: uuid.UUID,
    body: UpdateInsightRequest,
    membership: Membership = Depends(require_role(MembershipRole.MEMBER)),
) -> InsightResponse:
    await _require_project_in_org(membership.org_id, project_id)
    insight = await insights_service.update_insight(
        membership.org_id,
        project_id,
        insight_id,
        membership.user_id,
        name=body.name,
        spec=body.spec,
    )
    if insight is None:
        raise _not_found()
    return _response(insight)


@router.delete("/{insight_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_insight(
    project_id: uuid.UUID,
    insight_id: uuid.UUID,
    membership: Membership = Depends(require_role(MembershipRole.MEMBER)),
) -> Response:
    await _require_project_in_org(membership.org_id, project_id)
    deleted = await insights_service.delete_insight(
        membership.org_id, project_id, insight_id, membership.user_id
    )
    if not deleted:
        raise _not_found()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
