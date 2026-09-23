import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from pulse.alerts.delivery import send_webhook_with_result
from pulse.api.dependencies import require_role
from pulse.models import Membership, MembershipRole
from pulse.services import projects as projects_service

router = APIRouter(prefix="/api/v1/orgs/{org_id}/projects/{project_id}/webhooks", tags=["webhooks"])


class TestWebhookRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


class TestWebhookResponse(BaseModel):
    delivered: bool
    status_code: int | None


@router.post("/test", response_model=TestWebhookResponse)
async def test_webhook(
    project_id: uuid.UUID,
    body: TestWebhookRequest,
    membership: Membership = Depends(require_role(MembershipRole.ADMIN)),
) -> TestWebhookResponse:
    """Sends one synthetic, signed payload to `url` so a user can confirm
    their endpoint and secret work before wiring the URL into a real alert
    (`AlertChannels.webhook_url`) -- this saves someone from only finding
    out their receiver is misconfigured the first time a real alert fires.
    Admin+, not Member+: unlike creating an alert, this makes an outbound
    network call to an arbitrary URL the caller supplies, so it gets the
    same elevated bar as other actions with an external side effect."""
    if await projects_service.get_project(membership.org_id, project_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    result = await send_webhook_with_result(
        body.url,
        {"test": True, "sent_at": datetime.now(UTC).isoformat()},
    )
    return TestWebhookResponse(delivered=result.delivered, status_code=result.status_code)
