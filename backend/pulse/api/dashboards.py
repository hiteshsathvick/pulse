import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, model_validator

from pulse.api.dependencies import require_role
from pulse.dashboards import service as dashboards_service
from pulse.dashboards.schemas import DashboardRange, ItemInput, Position
from pulse.dashboards.service import Actor, Bundle, Summary
from pulse.models import DashboardScope, InsightKind, Membership, MembershipRole
from pulse.services import projects as projects_service

router = APIRouter(
    prefix="/api/v1/orgs/{org_id}/projects/{project_id}/dashboards", tags=["dashboards"]
)


class CreateDashboardRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    shared_scope: DashboardScope = DashboardScope.PRIVATE
    default_range: DashboardRange | None = None


class UpdateDashboardRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    shared_scope: DashboardScope | None = None
    default_range: DashboardRange | None = None
    # When present, replaces the whole layout atomically ([] clears it).
    items: list[ItemInput] | None = None

    @model_validator(mode="after")
    def _require_a_change(self) -> "UpdateDashboardRequest":
        if (
            self.name is None
            and self.shared_scope is None
            and self.default_range is None
            and self.items is None
        ):
            raise ValueError("provide at least one field to change")
        return self


class ItemInsightResponse(BaseModel):
    id: uuid.UUID
    name: str
    kind: InsightKind
    spec: dict[str, object]


class ItemResponse(BaseModel):
    id: uuid.UUID
    insight: ItemInsightResponse
    position: Position


class DashboardSummaryResponse(BaseModel):
    id: uuid.UUID
    name: str
    shared_scope: DashboardScope
    created_by: uuid.UUID
    updated_at: datetime
    item_count: int
    can_edit: bool


class DashboardResponse(BaseModel):
    id: uuid.UUID
    name: str
    layout: dict[str, object]
    default_range: dict[str, object]
    shared_scope: DashboardScope
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    # Computed server-side so the UI never re-implements the sharing rules.
    can_edit: bool
    items: list[ItemResponse]


def _actor(membership: Membership) -> Actor:
    return Actor(user_id=membership.user_id, role=membership.role)


def _summary_response(summary: Summary) -> DashboardSummaryResponse:
    d = summary.dashboard
    return DashboardSummaryResponse(
        id=d.id,
        name=d.name,
        shared_scope=d.shared_scope,
        created_by=d.created_by,
        updated_at=d.updated_at,
        item_count=summary.item_count,
        can_edit=summary.can_edit,
    )


def _response(bundle: Bundle) -> DashboardResponse:
    d = bundle.dashboard
    return DashboardResponse(
        id=d.id,
        name=d.name,
        layout=d.layout,
        default_range=d.default_range,
        shared_scope=d.shared_scope,
        created_by=d.created_by,
        created_at=d.created_at,
        updated_at=d.updated_at,
        can_edit=bundle.can_edit,
        items=[
            ItemResponse(
                id=item.id,
                insight=ItemInsightResponse(
                    id=insight.id, name=insight.name, kind=insight.kind, spec=insight.spec
                ),
                position=Position(**item.position),  # type: ignore[arg-type]
            )
            for item, insight in bundle.items
        ],
    )


async def _require_project_in_org(org_id: uuid.UUID, project_id: uuid.UUID) -> None:
    if await projects_service.get_project(org_id, project_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dashboard not found")


def _forbidden() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You don't have permission to change this dashboard",
    )


@router.get("", response_model=list[DashboardSummaryResponse])
async def list_dashboards(
    project_id: uuid.UUID,
    membership: Membership = Depends(require_role(MembershipRole.VIEWER)),
) -> list[DashboardSummaryResponse]:
    await _require_project_in_org(membership.org_id, project_id)
    summaries = await dashboards_service.list_dashboards(
        membership.org_id, project_id, _actor(membership)
    )
    return [_summary_response(s) for s in summaries]


@router.post("", response_model=DashboardResponse, status_code=status.HTTP_201_CREATED)
async def create_dashboard(
    project_id: uuid.UUID,
    body: CreateDashboardRequest,
    membership: Membership = Depends(require_role(MembershipRole.MEMBER)),
) -> DashboardResponse:
    await _require_project_in_org(membership.org_id, project_id)
    bundle = await dashboards_service.create_dashboard(
        membership.org_id,
        project_id,
        body.name,
        body.shared_scope,
        body.default_range,
        _actor(membership),
    )
    return _response(bundle)


@router.get("/{dashboard_id}", response_model=DashboardResponse)
async def get_dashboard(
    project_id: uuid.UUID,
    dashboard_id: uuid.UUID,
    membership: Membership = Depends(require_role(MembershipRole.VIEWER)),
) -> DashboardResponse:
    await _require_project_in_org(membership.org_id, project_id)
    bundle = await dashboards_service.get_dashboard(
        membership.org_id, project_id, dashboard_id, _actor(membership)
    )
    if bundle is None:
        raise _not_found()
    return _response(bundle)


@router.patch("/{dashboard_id}", response_model=DashboardResponse)
async def update_dashboard(
    project_id: uuid.UUID,
    dashboard_id: uuid.UUID,
    body: UpdateDashboardRequest,
    membership: Membership = Depends(require_role(MembershipRole.MEMBER)),
) -> DashboardResponse:
    await _require_project_in_org(membership.org_id, project_id)
    try:
        bundle = await dashboards_service.update_dashboard(
            membership.org_id,
            project_id,
            dashboard_id,
            _actor(membership),
            name=body.name,
            shared_scope=body.shared_scope,
            default_range=body.default_range,
            items=body.items,
        )
    except dashboards_service.DashboardForbidden as exc:
        raise _forbidden() from exc
    except dashboards_service.InvalidLayout as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    if bundle is None:
        raise _not_found()
    return _response(bundle)


@router.delete("/{dashboard_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dashboard(
    project_id: uuid.UUID,
    dashboard_id: uuid.UUID,
    membership: Membership = Depends(require_role(MembershipRole.MEMBER)),
) -> Response:
    await _require_project_in_org(membership.org_id, project_id)
    try:
        deleted = await dashboards_service.delete_dashboard(
            membership.org_id, project_id, dashboard_id, _actor(membership)
        )
    except dashboards_service.DashboardForbidden as exc:
        raise _forbidden() from exc
    if not deleted:
        raise _not_found()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
