import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, model_validator

from pulse.alerts import evaluate as alerts_evaluate
from pulse.alerts import service as alerts_service
from pulse.alerts.rules import AlertChannels, DiscriminatedAlertRule
from pulse.api.dependencies import require_role
from pulse.insights import service as insights_service
from pulse.models import Alert, AlertEvent, Membership, MembershipRole
from pulse.query import service as query_service
from pulse.services import projects as projects_service

router = APIRouter(prefix="/api/v1/orgs/{org_id}/projects/{project_id}/alerts", tags=["alerts"])


class CreateAlertRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    insight_id: uuid.UUID
    rule: DiscriminatedAlertRule
    channels: AlertChannels = AlertChannels()


class UpdateAlertRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    rule: DiscriminatedAlertRule | None = None
    channels: AlertChannels | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def _require_a_change(self) -> "UpdateAlertRequest":
        if (
            self.name is None
            and self.rule is None
            and self.channels is None
            and self.enabled is None
        ):
            raise ValueError("provide at least one field to change")
        return self


class AlertResponse(BaseModel):
    id: uuid.UUID
    name: str
    insight_id: uuid.UUID
    rule: dict[str, object]
    channels: dict[str, object]
    enabled: bool
    is_breaching: bool
    last_evaluated_at: datetime | None
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


class AlertEventResponse(BaseModel):
    id: uuid.UUID
    alert_id: uuid.UUID
    triggered_at: datetime
    value: float
    message: str
    delivered: dict[str, object]
    acknowledged_at: datetime | None


class EvaluateNowResponse(BaseModel):
    fired: bool
    recovered: bool
    value: float | None


def _response(alert: Alert) -> AlertResponse:
    return AlertResponse(
        id=alert.id,
        name=alert.name,
        insight_id=alert.insight_id,
        rule=alert.rule,
        channels=alert.channels,
        enabled=alert.enabled,
        is_breaching=alert.is_breaching,
        last_evaluated_at=alert.last_evaluated_at,
        created_by=alert.created_by,
        created_at=alert.created_at,
        updated_at=alert.updated_at,
    )


def _event_response(event: AlertEvent) -> AlertEventResponse:
    return AlertEventResponse(
        id=event.id,
        alert_id=event.alert_id,
        triggered_at=event.triggered_at,
        value=event.value,
        message=event.message,
        delivered=event.delivered,
        acknowledged_at=event.acknowledged_at,
    )


async def _require_project_in_org(org_id: uuid.UUID, project_id: uuid.UUID) -> None:
    if await projects_service.get_project(org_id, project_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")


def _anomaly_requires_trend() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail="An anomaly rule requires a trend insight",
    )


@router.get("", response_model=list[AlertResponse])
async def list_alerts(
    project_id: uuid.UUID, membership: Membership = Depends(require_role(MembershipRole.VIEWER))
) -> list[AlertResponse]:
    await _require_project_in_org(membership.org_id, project_id)
    alerts = await alerts_service.list_alerts(membership.org_id, project_id)
    return [_response(a) for a in alerts]


@router.post("", response_model=AlertResponse, status_code=status.HTTP_201_CREATED)
async def create_alert(
    project_id: uuid.UUID,
    body: CreateAlertRequest,
    membership: Membership = Depends(require_role(MembershipRole.MEMBER)),
) -> AlertResponse:
    await _require_project_in_org(membership.org_id, project_id)
    try:
        alert = await alerts_service.create_alert(
            membership.org_id,
            project_id,
            body.insight_id,
            body.name,
            body.rule,
            body.channels,
            membership.user_id,
        )
    except alerts_service.InsightNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Insight not found"
        ) from exc
    except alerts_service.AnomalyRequiresTrend as exc:
        raise _anomaly_requires_trend() from exc
    return _response(alert)


# Registered before /{alert_id} so "events" is never mistaken for an alert id.
@router.get("/events", response_model=list[AlertEventResponse])
async def list_events(
    project_id: uuid.UUID,
    alert_id: uuid.UUID | None = None,
    unacknowledged_only: bool = False,
    membership: Membership = Depends(require_role(MembershipRole.VIEWER)),
) -> list[AlertEventResponse]:
    await _require_project_in_org(membership.org_id, project_id)
    events = await alerts_service.list_events(
        membership.org_id, project_id, alert_id, unacknowledged_only=unacknowledged_only
    )
    return [_event_response(e) for e in events]


@router.post("/events/{event_id}/ack", response_model=AlertEventResponse)
async def acknowledge_event(
    project_id: uuid.UUID,
    event_id: uuid.UUID,
    membership: Membership = Depends(require_role(MembershipRole.VIEWER)),
) -> AlertEventResponse:
    await _require_project_in_org(membership.org_id, project_id)
    event = await alerts_service.acknowledge_event(membership.org_id, project_id, event_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert event not found")
    return _event_response(event)


@router.get("/{alert_id}", response_model=AlertResponse)
async def get_alert(
    project_id: uuid.UUID,
    alert_id: uuid.UUID,
    membership: Membership = Depends(require_role(MembershipRole.VIEWER)),
) -> AlertResponse:
    await _require_project_in_org(membership.org_id, project_id)
    alert = await alerts_service.get_alert(membership.org_id, project_id, alert_id)
    if alert is None:
        raise _not_found()
    return _response(alert)


@router.patch("/{alert_id}", response_model=AlertResponse)
async def update_alert(
    project_id: uuid.UUID,
    alert_id: uuid.UUID,
    body: UpdateAlertRequest,
    membership: Membership = Depends(require_role(MembershipRole.MEMBER)),
) -> AlertResponse:
    await _require_project_in_org(membership.org_id, project_id)
    try:
        alert = await alerts_service.update_alert(
            membership.org_id,
            project_id,
            alert_id,
            membership.user_id,
            name=body.name,
            rule=body.rule,
            channels=body.channels,
            enabled=body.enabled,
        )
    except alerts_service.AnomalyRequiresTrend as exc:
        raise _anomaly_requires_trend() from exc
    if alert is None:
        raise _not_found()
    return _response(alert)


@router.delete("/{alert_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_alert(
    project_id: uuid.UUID,
    alert_id: uuid.UUID,
    membership: Membership = Depends(require_role(MembershipRole.MEMBER)),
) -> Response:
    await _require_project_in_org(membership.org_id, project_id)
    deleted = await alerts_service.delete_alert(
        membership.org_id, project_id, alert_id, membership.user_id
    )
    if not deleted:
        raise _not_found()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{alert_id}/evaluate-now", response_model=EvaluateNowResponse)
async def evaluate_now(
    project_id: uuid.UUID,
    alert_id: uuid.UUID,
    membership: Membership = Depends(require_role(MembershipRole.MEMBER)),
) -> EvaluateNowResponse:
    """On-demand evaluation for testing/demoing a rule without waiting for
    the alert-worker's next cycle -- the exact same evaluate_alert() path,
    not a second, lighter-weight implementation."""
    await _require_project_in_org(membership.org_id, project_id)
    alert = await alerts_service.get_alert(membership.org_id, project_id, alert_id)
    if alert is None:
        raise _not_found()

    insight = await insights_service.get_insight(membership.org_id, project_id, alert.insight_id)
    if insight is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Insight not found")

    try:
        outcome = await alerts_evaluate.evaluate_alert(alert, insight)
    except alerts_evaluate.InsufficientData as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except query_service.ProjectNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        ) from exc
    return EvaluateNowResponse(
        fired=outcome.fired, recovered=outcome.recovered, value=outcome.value
    )
