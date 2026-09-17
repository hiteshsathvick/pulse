import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from pulse.api.dependencies import require_role
from pulse.models import (
    EventSchema,
    Membership,
    MembershipRole,
    PropertySchema,
    PropertyType,
    SchemaStatus,
)
from pulse.registry import service as registry_service
from pulse.services import projects as projects_service

router = APIRouter(
    prefix="/api/v1/orgs/{org_id}/projects/{project_id}/schema", tags=["schema-registry"]
)


class EventSchemaResponse(BaseModel):
    id: uuid.UUID
    event_name: str
    status: SchemaStatus
    first_seen_at: datetime
    volume_estimate: int


class PropertySchemaResponse(BaseModel):
    id: uuid.UUID
    event_schema_id: uuid.UUID | None
    key: str
    inferred_type: PropertyType
    is_pii: bool
    status: SchemaStatus
    type_conflict_detected_at: datetime | None


class UpdateEventStatusRequest(BaseModel):
    status: SchemaStatus


class UpdatePropertyRequest(BaseModel):
    status: SchemaStatus | None = None
    is_pii: bool | None = None


def _event_response(event: EventSchema) -> EventSchemaResponse:
    return EventSchemaResponse(
        id=event.id,
        event_name=event.event_name,
        status=event.status,
        first_seen_at=event.first_seen_at,
        volume_estimate=event.volume_estimate,
    )


def _property_response(prop: PropertySchema) -> PropertySchemaResponse:
    return PropertySchemaResponse(
        id=prop.id,
        event_schema_id=prop.event_schema_id,
        key=prop.key,
        inferred_type=prop.inferred_type,
        is_pii=prop.is_pii,
        status=prop.status,
        type_conflict_detected_at=prop.type_conflict_detected_at,
    )


async def _require_project_in_org(org_id: uuid.UUID, project_id: uuid.UUID) -> None:
    if await projects_service.get_project(org_id, project_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")


async def _require_event_in_project(
    org_id: uuid.UUID, project_id: uuid.UUID, event_schema_id: uuid.UUID
) -> EventSchema:
    event = await registry_service.get_event(org_id, event_schema_id)
    if event is None or event.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
    return event


@router.get("/events", response_model=list[EventSchemaResponse])
async def list_events(
    project_id: uuid.UUID,
    membership: Membership = Depends(require_role(MembershipRole.VIEWER)),
) -> list[EventSchemaResponse]:
    await _require_project_in_org(membership.org_id, project_id)
    events = await registry_service.list_events(membership.org_id, project_id)
    return [_event_response(e) for e in events]


@router.get("/events/{event_schema_id}/properties", response_model=list[PropertySchemaResponse])
async def list_properties(
    project_id: uuid.UUID,
    event_schema_id: uuid.UUID,
    membership: Membership = Depends(require_role(MembershipRole.VIEWER)),
) -> list[PropertySchemaResponse]:
    await _require_project_in_org(membership.org_id, project_id)
    await _require_event_in_project(membership.org_id, project_id, event_schema_id)
    properties = await registry_service.list_properties(membership.org_id, event_schema_id)
    return [_property_response(p) for p in properties]


@router.patch("/events/{event_schema_id}", response_model=EventSchemaResponse)
async def update_event_status(
    project_id: uuid.UUID,
    event_schema_id: uuid.UUID,
    body: UpdateEventStatusRequest,
    membership: Membership = Depends(require_role(MembershipRole.ADMIN)),
) -> EventSchemaResponse:
    await _require_project_in_org(membership.org_id, project_id)
    await _require_event_in_project(membership.org_id, project_id, event_schema_id)

    event = await registry_service.update_event_status(
        membership.org_id, event_schema_id, body.status, membership.user_id
    )
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")

    return _event_response(event)


@router.patch("/properties/{property_schema_id}", response_model=PropertySchemaResponse)
async def update_property(
    project_id: uuid.UUID,
    property_schema_id: uuid.UUID,
    body: UpdatePropertyRequest,
    membership: Membership = Depends(require_role(MembershipRole.ADMIN)),
) -> PropertySchemaResponse:
    await _require_project_in_org(membership.org_id, project_id)

    prop = await registry_service.get_property(membership.org_id, property_schema_id)
    if prop is None or prop.event_schema_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property not found")
    await _require_event_in_project(membership.org_id, project_id, prop.event_schema_id)

    updated = await registry_service.update_property(
        membership.org_id,
        property_schema_id,
        membership.user_id,
        status=body.status,
        is_pii=body.is_pii,
    )
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property not found")

    return _property_response(updated)
