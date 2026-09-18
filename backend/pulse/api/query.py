import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from pulse.api.dependencies import resolve_query_scope
from pulse.query import service as query_service
from pulse.query.spec import FunnelSpec, TrendSpec

router = APIRouter(
    prefix="/api/v1/orgs/{org_id}/projects/{project_id}/query",
    tags=["query"],
    dependencies=[Depends(resolve_query_scope)],
)


class TrendResponse(BaseModel):
    results: list[dict[str, object]]
    cached: bool


class FunnelResponse(BaseModel):
    results: list[dict[str, object]]
    cached: bool


@router.post("/trend", response_model=TrendResponse)
async def query_trend(org_id: uuid.UUID, project_id: uuid.UUID, spec: TrendSpec) -> TrendResponse:
    try:
        result = await query_service.run_trend(spec, org_id, project_id)
    except query_service.ProjectNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        ) from exc
    return TrendResponse(results=result.results, cached=result.cached)


@router.post("/funnel", response_model=FunnelResponse)
async def query_funnel(
    org_id: uuid.UUID, project_id: uuid.UUID, spec: FunnelSpec
) -> FunnelResponse:
    try:
        result = await query_service.run_funnel(spec, org_id, project_id)
    except query_service.ProjectNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        ) from exc
    return FunnelResponse(results=result.results, cached=result.cached)
