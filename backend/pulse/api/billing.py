import uuid
from datetime import UTC, date, datetime, timedelta

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel

from pulse.api.dependencies import require_role
from pulse.billing import service as billing_service
from pulse.billing import stripe_client
from pulse.billing import webhooks as billing_webhooks
from pulse.billing.plans import PLANS
from pulse.billing.providers import get_payment_provider
from pulse.core.config import get_settings
from pulse.core.security import get_current_user
from pulse.models import Membership, MembershipRole, SubscriptionPlan, SubscriptionStatus, User
from pulse.services import orgs as orgs_service

router = APIRouter(prefix="/api/v1/orgs/{org_id}/billing", tags=["billing"])
# Not org-scoped: a real payment provider calls this directly with no
# Pulse-issued credential at all. The verified signature is the only
# boundary (see pulse/billing/webhooks.py). Unused while payment_provider
# is "mock" (the default) -- nothing external ever calls it in that mode.
webhook_router = APIRouter(prefix="/api/v1/webhooks", tags=["billing"])


class SubscriptionResponse(BaseModel):
    plan: SubscriptionPlan
    status: SubscriptionStatus
    quota_events_per_month: int
    current_period_start: datetime | None
    current_period_end: datetime | None


class UsageResponse(BaseModel):
    period: date
    events_ingested: int
    mtu: int
    quota_events_per_month: int


class CheckoutResponse(BaseModel):
    url: str


class InvoiceResponse(BaseModel):
    id: str
    status: str | None
    amount_due: int
    currency: str
    hosted_invoice_url: str | None
    created: int


class MockActionResponse(BaseModel):
    plan: SubscriptionPlan
    status: SubscriptionStatus


def _not_configured() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Billing is not configured on this server yet",
    )


def _no_subscription() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No subscription found")


def _mock_actions_disabled() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Mock billing actions are disabled while a real payment provider is configured",
    )


@router.get("/subscription", response_model=SubscriptionResponse)
async def get_subscription(
    membership: Membership = Depends(require_role(MembershipRole.ADMIN)),
) -> SubscriptionResponse:
    subscription = await billing_service.get_subscription(membership.org_id)
    if subscription is None:
        raise _no_subscription()
    return SubscriptionResponse(
        plan=subscription.plan,
        status=subscription.status,
        quota_events_per_month=PLANS[subscription.plan].quota_events_per_month,
        current_period_start=subscription.current_period_start,
        current_period_end=subscription.current_period_end,
    )


@router.get("/usage", response_model=UsageResponse)
async def get_usage(
    membership: Membership = Depends(require_role(MembershipRole.ADMIN)),
) -> UsageResponse:
    subscription = await billing_service.get_subscription(membership.org_id)
    plan = subscription.plan if subscription is not None else SubscriptionPlan.FREE
    usage = await billing_service.get_usage(membership.org_id)
    period = usage.period if usage is not None else billing_service.current_period()
    return UsageResponse(
        period=period,
        events_ingested=usage.events_ingested if usage is not None else 0,
        mtu=usage.mtu if usage is not None else 0,
        quota_events_per_month=PLANS[plan].quota_events_per_month,
    )


@router.post("/checkout", response_model=CheckoutResponse)
async def create_checkout(
    membership: Membership = Depends(require_role(MembershipRole.ADMIN)),
    current_user: User = Depends(get_current_user),
) -> CheckoutResponse:
    subscription = await billing_service.get_subscription(membership.org_id)
    if subscription is None:
        raise _no_subscription()
    org = await orgs_service.get_organization(membership.org_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    provider = get_payment_provider(get_settings())
    try:
        customer_id = subscription.payment_customer_id
        if customer_id is None:
            customer_id = await provider.create_customer(
                org_id=str(membership.org_id), org_name=org.name, email=current_user.email
            )
            await billing_service.set_payment_customer_id(membership.org_id, customer_id)
        url = await provider.create_checkout_session(
            customer_id=customer_id, org_id=str(membership.org_id)
        )
    except stripe_client.StripeNotConfigured as exc:
        raise _not_configured() from exc
    return CheckoutResponse(url=url)


@router.post("/portal", response_model=CheckoutResponse)
async def create_portal(
    membership: Membership = Depends(require_role(MembershipRole.ADMIN)),
) -> CheckoutResponse:
    subscription = await billing_service.get_subscription(membership.org_id)
    if subscription is None or subscription.payment_customer_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No billing account yet -- upgrade first"
        )
    provider = get_payment_provider(get_settings())
    try:
        url = await provider.create_portal_session(
            customer_id=subscription.payment_customer_id, org_id=str(membership.org_id)
        )
    except stripe_client.StripeNotConfigured as exc:
        raise _not_configured() from exc
    return CheckoutResponse(url=url)


@router.get("/invoices", response_model=list[InvoiceResponse])
async def list_invoices(
    membership: Membership = Depends(require_role(MembershipRole.ADMIN)),
) -> list[InvoiceResponse]:
    subscription = await billing_service.get_subscription(membership.org_id)
    # Nothing to list before a real checkout completed -- a customer id
    # alone (created the moment "Upgrade" is first clicked) isn't proof of
    # a paid period yet.
    if (
        subscription is None
        or subscription.payment_customer_id is None
        or subscription.plan != SubscriptionPlan.PRO
    ):
        return []
    provider = get_payment_provider(get_settings())
    try:
        invoices = await provider.list_invoices(customer_id=subscription.payment_customer_id)
    except stripe_client.StripeNotConfigured as exc:
        raise _not_configured() from exc
    return [
        InvoiceResponse(
            id=invoice.id,
            status=invoice.status,
            amount_due=invoice.amount_due,
            currency=invoice.currency,
            hosted_invoice_url=invoice.hosted_invoice_url,
            created=invoice.created,
        )
        for invoice in invoices
    ]


def _require_mock_provider() -> None:
    if get_settings().payment_provider != "mock":
        raise _mock_actions_disabled()


@router.post("/mock/subscribe", response_model=MockActionResponse)
async def mock_subscribe(
    membership: Membership = Depends(require_role(MembershipRole.ADMIN)),
) -> MockActionResponse:
    """Simulates the event a real payment provider's webhook would send on a
    completed checkout -- builds the same event shape
    pulse/billing/webhooks.py already knows how to apply and runs it
    through the identical handler, so "how does a subscription change get
    applied" has exactly one implementation regardless of whether it was
    triggered by a real webhook or this mock action."""
    _require_mock_provider()
    subscription = await billing_service.get_subscription(membership.org_id)
    if subscription is None:
        raise _no_subscription()

    now = datetime.now(UTC)
    event = {
        "id": f"evt_mock_{uuid.uuid4().hex}",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": f"mock_sub_{uuid.uuid4().hex[:16]}",
                "status": "active",
                "metadata": {"org_id": str(membership.org_id)},
                "current_period_start": int(now.timestamp()),
                "current_period_end": int((now + timedelta(days=30)).timestamp()),
            }
        },
    }
    await billing_webhooks.handle_event(event)

    updated = await billing_service.get_subscription(membership.org_id)
    assert updated is not None
    return MockActionResponse(plan=updated.plan, status=updated.status)


@router.post("/mock/cancel", response_model=MockActionResponse)
async def mock_cancel(
    membership: Membership = Depends(require_role(MembershipRole.ADMIN)),
) -> MockActionResponse:
    _require_mock_provider()
    subscription = await billing_service.get_subscription(membership.org_id)
    if subscription is None:
        raise _no_subscription()

    event = {
        "id": f"evt_mock_{uuid.uuid4().hex}",
        "type": "customer.subscription.deleted",
        "data": {
            "object": {
                "id": subscription.payment_subscription_id or f"mock_sub_{uuid.uuid4().hex[:16]}",
                "metadata": {"org_id": str(membership.org_id)},
            }
        },
    }
    await billing_webhooks.handle_event(event)

    updated = await billing_service.get_subscription(membership.org_id)
    assert updated is not None
    return MockActionResponse(plan=updated.plan, status=updated.status)


@webhook_router.post("/stripe")
async def stripe_webhook(
    request: Request, stripe_signature: str = Header(alias="Stripe-Signature")
) -> dict[str, bool]:
    payload = await request.body()
    settings = get_settings()
    try:
        event = stripe_client.construct_webhook_event(payload, stripe_signature, settings)
    except stripe_client.StripeNotConfigured as exc:
        raise _not_configured() from exc
    except stripe.SignatureVerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid webhook signature"
        ) from exc

    await billing_webhooks.handle_event(event)
    return {"received": True}
