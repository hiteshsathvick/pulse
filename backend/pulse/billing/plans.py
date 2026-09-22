"""Plan config (SPEC.md #6.17). Not a database table -- Stripe is the
source of truth for pricing; the only thing Pulse itself needs per plan is
its monthly event quota, which never depends on Stripe being configured at
all (the free plan works with zero Stripe setup)."""

from __future__ import annotations

from dataclasses import dataclass

from pulse.models import SubscriptionPlan


@dataclass(frozen=True)
class Plan:
    id: SubscriptionPlan
    name: str
    quota_events_per_month: int


PLANS: dict[SubscriptionPlan, Plan] = {
    SubscriptionPlan.FREE: Plan(SubscriptionPlan.FREE, "Free", 10_000),
    SubscriptionPlan.PRO: Plan(SubscriptionPlan.PRO, "Pro", 1_000_000),
}
