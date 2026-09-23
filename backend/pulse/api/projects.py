import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from pulse.api.dependencies import get_org_membership, require_role
from pulse.models import Membership, MembershipRole, Project
from pulse.services import projects as projects_service

router = APIRouter(prefix="/api/v1/orgs/{org_id}/projects", tags=["projects"])


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=63)
    timezone: str = Field(default="UTC", max_length=64)


class UpdateProjectRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    timezone: str | None = Field(default=None, max_length=64)
    # Phase 22: null explicitly clears an override back to the org default
    # (Organization.retention_days) -- distinguished from "field omitted
    # entirely" via model_fields_set at the call site, not by this default.
    retention_days: int | None = Field(default=None, ge=1)


class ProjectResponse(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    slug: str
    timezone: str
    retention_days: int | None


def _project_response(project: Project) -> ProjectResponse:
    return ProjectResponse(
        id=project.id,
        org_id=project.org_id,
        name=project.name,
        slug=project.slug,
        timezone=project.timezone,
        retention_days=project.retention_days,
    )


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: CreateProjectRequest,
    membership: Membership = Depends(require_role(MembershipRole.MEMBER)),
) -> ProjectResponse:
    try:
        project = await projects_service.create_project(
            membership.org_id, body.name, body.slug, body.timezone, membership.user_id
        )
    except projects_service.ProjectSlugAlreadyTaken as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Slug already taken"
        ) from exc
    return _project_response(project)


@router.get("", response_model=list[ProjectResponse])
async def list_projects(
    membership: Membership = Depends(get_org_membership),
) -> list[ProjectResponse]:
    projects = await projects_service.list_projects(membership.org_id)
    return [_project_response(p) for p in projects]


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: uuid.UUID, membership: Membership = Depends(get_org_membership)
) -> ProjectResponse:
    project = await projects_service.get_project(membership.org_id, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return _project_response(project)


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: uuid.UUID,
    body: UpdateProjectRequest,
    membership: Membership = Depends(require_role(MembershipRole.MEMBER)),
) -> ProjectResponse:
    project = await projects_service.update_project(
        membership.org_id,
        project_id,
        membership.user_id,
        name=body.name,
        timezone=body.timezone,
        retention_days=body.retention_days
        if "retention_days" in body.model_fields_set
        else projects_service.UNSET,
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return _project_response(project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: uuid.UUID,
    membership: Membership = Depends(require_role(MembershipRole.ADMIN)),
) -> None:
    deleted = await projects_service.delete_project(
        membership.org_id, project_id, membership.user_id
    )
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
