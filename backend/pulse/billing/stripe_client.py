"""A thin wrapper around the (synchronous) Stripe SDK, mirroring
pulse/repositories/object_storage.py's shape for a sync-only third-party
client: construction is cheap and non-blocking, actual I/O goes through
asyncio.to_thread at the call site. Every call is gated by
Settings.stripe_secret_key -- unset (the default, and true until a real
Stripe test-mode account is connected -- confirmed with the user first,
"build now, connect later") raises StripeNotConfigured rather than ever
attempting a network call with no key. See SPEC.md #6.17."""

from __future__ import annotations

import asyncio
from typing import Any

import stripe

from pulse.core.config import Settings, get_settings

_client: stripe.StripeClient | None = None


class StripeNotConfigured(Exception):
    pass


def _get_client(settings: Settings) -> stripe.StripeClient:
    global _client
    if not settings.stripe_secret_key:
        raise StripeNotConfigured("STRIPE_SECRET_KEY is not configured")
    if _client is None:
        _client = stripe.StripeClient(api_key=settings.stripe_secret_key)
    return _client


async def create_customer(*, org_id: str, org_name: str, email: str | None) -> str:
    settings = get_settings()
    client = _get_client(settings)
    params: dict[str, Any] = {"name": org_name, "metadata": {"org_id": org_id}}
    if email is not None:
        params["email"] = email
    customer = await asyncio.to_thread(client.customers.create, params=params)  # type: ignore[arg-type]
    return customer.id


async def create_checkout_session(*, customer_id: str, org_id: str) -> str:
    settings = get_settings()
    client = _get_client(settings)
    if not settings.stripe_pro_price_id:
        raise StripeNotConfigured("STRIPE_PRO_PRICE_ID is not configured")
    session = await asyncio.to_thread(
        client.checkout.sessions.create,
        params={
            "customer": customer_id,
            "mode": "subscription",
            "line_items": [{"price": settings.stripe_pro_price_id, "quantity": 1}],
            "success_url": settings.billing_checkout_success_url,
            "cancel_url": settings.billing_checkout_cancel_url,
            # Copied onto the resulting Subscription object (not just this
            # Session), so the webhook handler can recover org_id from a
            # later customer.subscription.* event without a second lookup.
            "subscription_data": {"metadata": {"org_id": org_id}},
        },
    )
    if session.url is None:
        raise StripeNotConfigured("Stripe did not return a checkout URL")
    return session.url


async def create_portal_session(*, customer_id: str) -> str:
    settings = get_settings()
    client = _get_client(settings)
    session = await asyncio.to_thread(
        client.billing_portal.sessions.create,
        params={"customer": customer_id, "return_url": settings.billing_portal_return_url},
    )
    return session.url


async def list_invoices(*, customer_id: str, limit: int = 20) -> list[dict[str, Any]]:
    settings = get_settings()
    client = _get_client(settings)
    invoices = await asyncio.to_thread(
        client.invoices.list, params={"customer": customer_id, "limit": limit}
    )
    return [
        {
            "id": invoice.id,
            "status": invoice.status,
            "amount_due": invoice.amount_due,
            "currency": invoice.currency,
            "hosted_invoice_url": invoice.hosted_invoice_url,
            "created": invoice.created,
        }
        for invoice in invoices.data
    ]


def construct_webhook_event(payload: bytes, sig_header: str) -> stripe.Event:
    settings = get_settings()
    if not settings.stripe_webhook_secret:
        raise StripeNotConfigured("STRIPE_WEBHOOK_SECRET is not configured")
    # stripe.Webhook.construct_event's own type stubs are untyped/Any --
    # nothing to narrow here, just annotate the boundary explicitly.
    event: stripe.Event = stripe.Webhook.construct_event(  # type: ignore[no-untyped-call]
        payload, sig_header, settings.stripe_webhook_secret
    )
    return event


def close() -> None:
    global _client
    _client = None
