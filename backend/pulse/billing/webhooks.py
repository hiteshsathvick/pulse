"""Stripe webhook event handling (POST /api/v1/webhooks/stripe,
pulse/api/billing.py). The verified signature IS the boundary here -- like
a write key on /ingest, not RBAC: Stripe calls this endpoint directly, with
no Pulse-issued credential at all. See SPEC.md #6.17."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from pulse.models import Subscription, SubscriptionPlan, SubscriptionStatus
from pulse.repositories.postgres import session_scope

logger = logging.getLogger("pulse.billing.webhooks")

_STRIPE_STATUS_MAP = {
    "active": SubscriptionStatus.ACTIVE,
    "trialing": SubscriptionStatus.ACTIVE,
    "past_due": SubscriptionStatus.PAST_DUE,
    "canceled": SubscriptionStatus.CANCELED,
    "incomplete": SubscriptionStatus.INCOMPLETE,
    "incomplete_expired": SubscriptionStatus.CANCELED,
    "unpaid": SubscriptionStatus.PAST_DUE,
}

_SUBSCRIPTION_EVENTS = {"customer.subscription.created", "customer.subscription.updated"}


def _org_id_from_metadata(data: dict[str, Any]) -> uuid.UUID | None:
    raw = (data.get("metadata") or {}).get("org_id")
    if raw is None:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        logger.warning("stripe event carried a malformed org_id in metadata: %r", raw)
        return None


async def _get_subscription(org_id: uuid.UUID) -> Subscription | None:
    async with session_scope(org_id=org_id) as session:
        subscription: Subscription | None = await session.scalar(
            select(Subscription).where(Subscription.org_id == org_id)
        )
        return subscription


def _unix_to_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), tz=UTC)  # type: ignore[arg-type]


async def _handle_subscription_upsert(data: dict[str, Any]) -> None:
    org_id = _org_id_from_metadata(data)
    if org_id is None:
        logger.warning(
            "stripe subscription event without an org_id in metadata: %s", data.get("id")
        )
        return

    subscription = await _get_subscription(org_id)
    if subscription is None:
        logger.warning(
            "stripe subscription event for an org with no local subscription: %s", org_id
        )
        return

    status = _STRIPE_STATUS_MAP.get(data.get("status", ""), SubscriptionStatus.INCOMPLETE)
    async with session_scope(org_id=org_id) as session:
        row = await session.get(Subscription, subscription.id)
        assert row is not None
        row.stripe_subscription_id = data.get("id")
        row.status = status
        # Only a truly active/trialing Stripe subscription counts as Pro --
        # past_due/incomplete keeps whatever plan the row already had rather
        # than silently downgrading on a payment hiccup Stripe itself is
        # still trying to resolve.
        if status == SubscriptionStatus.ACTIVE:
            row.plan = SubscriptionPlan.PRO
        row.current_period_start = _unix_to_datetime(data.get("current_period_start"))
        row.current_period_end = _unix_to_datetime(data.get("current_period_end"))
        await session.commit()


async def _handle_subscription_deleted(data: dict[str, Any]) -> None:
    org_id = _org_id_from_metadata(data)
    if org_id is None:
        return
    subscription = await _get_subscription(org_id)
    if subscription is None:
        return
    async with session_scope(org_id=org_id) as session:
        row = await session.get(Subscription, subscription.id)
        assert row is not None
        row.status = SubscriptionStatus.CANCELED
        row.plan = SubscriptionPlan.FREE
        await session.commit()


async def handle_event(event: dict[str, Any]) -> None:
    event_type = event["type"]
    data = event["data"]["object"]

    if event_type in _SUBSCRIPTION_EVENTS:
        await _handle_subscription_upsert(data)
    elif event_type == "customer.subscription.deleted":
        await _handle_subscription_deleted(data)
    # invoice.paid / invoice.payment_failed: acknowledged, no local state
    # change needed beyond what a subscription.updated event already
    # captures -- invoices themselves are read live from Stripe
    # (GET .../billing/invoices), never mirrored locally.
    else:
        logger.info("unhandled stripe webhook event type: %s", event_type)
