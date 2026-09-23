"""Reads/writes for Subscription + UsageRecord, and the ingest-path quota
check (pulse/ingest/router.py). Mirrors pulse/alerts/service.py's
session_scope discipline."""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select

from pulse.billing.plans import PLANS
from pulse.billing.usage import compute_org_usage
from pulse.core.config import get_settings
from pulse.models import Subscription, SubscriptionPlan, UsageRecord
from pulse.repositories.postgres import session_scope


def current_period(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).date().replace(day=1)


async def get_subscription(org_id: uuid.UUID) -> Subscription | None:
    async with session_scope(org_id=org_id) as session:
        subscription: Subscription | None = await session.scalar(
            select(Subscription).where(Subscription.org_id == org_id)
        )
        return subscription


async def set_payment_customer_id(org_id: uuid.UUID, payment_customer_id: str) -> None:
    async with session_scope(org_id=org_id) as session:
        subscription = await session.scalar(
            select(Subscription).where(Subscription.org_id == org_id)
        )
        if subscription is not None:
            subscription.payment_customer_id = payment_customer_id
            await session.commit()


async def get_usage(org_id: uuid.UUID, period: date | None = None) -> UsageRecord | None:
    period = period or current_period()
    async with session_scope(org_id=org_id) as session:
        usage: UsageRecord | None = await session.scalar(
            select(UsageRecord).where(UsageRecord.org_id == org_id, UsageRecord.period == period)
        )
        return usage


async def compute_and_store_usage(org_id: uuid.UUID, period: date | None = None) -> UsageRecord:
    """The billing-worker's (pulse/billing/main.py) per-org, per-cycle unit
    of work -- also callable directly by tests without needing the worker
    loop running, the same "same path, different trigger" shape
    pulse/alerts/api.py's evaluate-now endpoint uses for alerts."""
    period = period or current_period()
    totals = await compute_org_usage(org_id, period)
    async with session_scope(org_id=org_id) as session:
        existing = await session.scalar(
            select(UsageRecord).where(UsageRecord.org_id == org_id, UsageRecord.period == period)
        )
        if existing is None:
            existing = UsageRecord(org_id=org_id, period=period)
            session.add(existing)
        existing.events_ingested = totals.events_ingested
        existing.mtu = totals.mtu
        await session.flush()
        await session.refresh(existing)
        await session.commit()
        return existing


class QuotaLevel(enum.StrEnum):
    OK = "ok"
    SOFT = "soft"
    HARD = "hard"


@dataclass(frozen=True)
class QuotaStatus:
    level: QuotaLevel
    events_ingested: int
    quota: int
    message: str | None = None


async def check_ingest_quota(org_id: uuid.UUID) -> QuotaStatus:
    """Reads the last-computed UsageRecord (upserted by the billing-worker
    every billing_usage_interval_seconds) rather than a live ClickHouse
    query on every /ingest call -- quota freshness lags by at most one
    worker cycle, an acceptable trade-off given the buffer, not this check,
    is the ingestion path's real durability guarantee."""
    subscription = await get_subscription(org_id)
    plan_id = subscription.plan if subscription is not None else SubscriptionPlan.FREE
    quota = PLANS[plan_id].quota_events_per_month

    usage = await get_usage(org_id)
    events_ingested = usage.events_ingested if usage is not None else 0

    settings = get_settings()
    if events_ingested >= quota:
        return QuotaStatus(
            QuotaLevel.HARD,
            events_ingested,
            quota,
            f"Monthly quota of {quota} events exceeded ({events_ingested} ingested this period). "
            "Upgrade your plan to continue ingesting.",
        )
    if events_ingested >= quota * settings.billing_soft_limit_ratio:
        return QuotaStatus(
            QuotaLevel.SOFT,
            events_ingested,
            quota,
            f"{events_ingested} of {quota} monthly events used.",
        )
    return QuotaStatus(QuotaLevel.OK, events_ingested, quota)
