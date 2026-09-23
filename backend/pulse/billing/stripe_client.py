"""The real payment provider (SPEC.md #6.18) -- an optional swap-in behind
pulse/billing/providers.py's PaymentProvider interface, gated entirely by
Settings.payment_provider="stripe" plus Settings.stripe_secret_key. Mirrors
pulse/repositories/object_storage.py's shape for a sync-only third-party
client: construction is cheap and non-blocking, actual I/O goes through
asyncio.to_thread at the call site. Not the default and not exercised by
CI -- confirmed with the user first, see pulse/billing/providers.py's own
docstring for why."""

from __future__ import annotations

import asyncio
from typing import Any

import stripe

from pulse.billing.providers import Invoice, PaymentProvider
from pulse.core.config import Settings

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


class StripePaymentProvider(PaymentProvider):
    name = "stripe"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def create_customer(self, *, org_id: str, org_name: str, email: str | None) -> str:
        client = _get_client(self._settings)
        params: dict[str, Any] = {"name": org_name, "metadata": {"org_id": org_id}}
        if email is not None:
            params["email"] = email
        customer = await asyncio.to_thread(client.customers.create, params=params)  # type: ignore[arg-type]
        return customer.id

    async def create_checkout_session(self, *, customer_id: str, org_id: str) -> str:
        settings = self._settings
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
                # Copied onto the resulting Subscription object (not just
                # this Session), so the webhook handler can recover org_id
                # from a later customer.subscription.* event without a
                # second lookup.
                "subscription_data": {"metadata": {"org_id": org_id}},
            },
        )
        if session.url is None:
            raise StripeNotConfigured("Stripe did not return a checkout URL")
        return session.url

    async def create_portal_session(self, *, customer_id: str, org_id: str) -> str:
        settings = self._settings
        client = _get_client(settings)
        session = await asyncio.to_thread(
            client.billing_portal.sessions.create,
            params={"customer": customer_id, "return_url": settings.billing_portal_return_url},
        )
        return session.url

    async def list_invoices(self, *, customer_id: str, limit: int = 20) -> list[Invoice]:
        client = _get_client(self._settings)
        invoices = await asyncio.to_thread(
            client.invoices.list, params={"customer": customer_id, "limit": limit}
        )
        return [
            Invoice(
                # Stripe's own stubs type `id` as optional (some expand
                # states omit it); a real, listed invoice always has one.
                id=invoice.id or "",
                status=invoice.status,
                amount_due=invoice.amount_due,
                currency=invoice.currency,
                hosted_invoice_url=invoice.hosted_invoice_url,
                created=invoice.created,
            )
            for invoice in invoices.data
        ]


def construct_webhook_event(payload: bytes, sig_header: str, settings: Settings) -> stripe.Event:
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
