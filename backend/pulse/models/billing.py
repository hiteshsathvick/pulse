import enum
import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column

from pulse.models.base import Base, IdMixin, TimestampMixin


class SubscriptionPlan(enum.StrEnum):
    FREE = "free"
    PRO = "pro"


class SubscriptionStatus(enum.StrEnum):
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"


class Subscription(IdMixin, TimestampMixin, Base):
    """One row per org (unique on org_id), auto-created as plan=free,
    status=active in the same transaction as the org itself
    (pulse/services/orgs.py::create_organization) so the ingest-path quota
    check (pulse/billing/service.py) never has to handle "no subscription
    yet". RLS-protected like Project/Dashboard -- carries org_id, reached by
    a member of that org browsing their own billing, never by token
    redemption. `stripe_customer_id` is created lazily on first checkout,
    not at org-creation time -- most orgs never upgrade, and creating a
    Stripe Customer for every signup regardless would be pointless API
    traffic against a real (if test-mode) account. See SPEC.md #6.17."""

    __tablename__ = "subscriptions"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), unique=True
    )
    plan: Mapped[SubscriptionPlan] = mapped_column(
        ENUM(SubscriptionPlan, name="subscription_plan"), default=SubscriptionPlan.FREE
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        ENUM(SubscriptionStatus, name="subscription_status"), default=SubscriptionStatus.ACTIVE
    )
    stripe_customer_id: Mapped[str | None] = mapped_column(String, default=None)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String, unique=True, default=None)
    current_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )


class UsageRecord(IdMixin, TimestampMixin, Base):
    """One row per (org, billing period) -- upserted by the billing-worker
    (pulse/billing/main.py) every cycle for the CURRENT period, computed
    from real ClickHouse ingestion data (event_hourly), never estimated.
    `updated_at` (from TimestampMixin) doubles as "last computed at": unlike
    AuditLog, a period's row is expected to be overwritten in place as more
    events arrive throughout the month. RLS-protected like Subscription.
    See SPEC.md #4 and #6.17."""

    __tablename__ = "usage_records"
    __table_args__ = (UniqueConstraint("org_id", "period", name="uq_usage_records_org_period"),)

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    period: Mapped[date] = mapped_column(Date)  # first day of the billing month (UTC)
    events_ingested: Mapped[int] = mapped_column(Integer, default=0)
    mtu: Mapped[int] = mapped_column(Integer, default=0)
