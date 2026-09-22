"""Alert rule + channel shapes (SPEC.md #6.16), the same
validated-JSON-in/validated-JSON-out discipline pulse.query.spec already
established for insights: a stored `Alert.rule`/`Alert.channels` is always
one these models accept, never hand-built dict access at a call site."""

from __future__ import annotations

import enum
from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

Comparator = Literal["gt", "lt", "gte", "lte"]

_COMPARATORS: dict[Comparator, Callable[[float, float], bool]] = {
    "gt": lambda value, threshold: value > threshold,
    "lt": lambda value, threshold: value < threshold,
    "gte": lambda value, threshold: value >= threshold,
    "lte": lambda value, threshold: value <= threshold,
}


class ThresholdRule(BaseModel):
    """Works against any insight kind -- trend's latest bucket, funnel's
    final-step conversion %, or retention's period-1 retention % (see
    pulse.alerts.evaluate). `comparator="gt"` catches a spike, `"lt"` catches
    a drop -- SPEC.md's "anomalously drops/spikes" covers both directions,
    and a threshold rule is deliberately not limited to one."""

    kind: Literal["threshold"] = "threshold"
    version: Literal[1] = 1
    comparator: Comparator
    value: float

    def breached(self, current: float) -> bool:
        return _COMPARATORS[self.comparator](current, self.value)


class AnomalyMethod(enum.StrEnum):
    ZSCORE = "zscore"
    SEASONAL_ZSCORE = "seasonal_zscore"


class AnomalyRule(BaseModel):
    """Trend insights only (pulse.alerts.service.create_alert rejects one
    attached to a funnel/retention insight with a 422) -- a bucketed time
    series is the one thing this rule needs that only a trend query already
    produces. `window` is how many trailing buckets form the baseline;
    `sensitivity` is the z-score threshold (number of standard deviations)
    past which a point counts as anomalous -- 3.0 is the traditional
    "3-sigma" default. See pulse.alerts.anomaly for the actual math."""

    kind: Literal["anomaly"] = "anomaly"
    version: Literal[1] = 1
    method: AnomalyMethod = AnomalyMethod.ZSCORE
    window: int = Field(default=14, ge=4, le=90)
    sensitivity: float = Field(default=3.0, gt=0)


AlertRule = ThresholdRule | AnomalyRule
DiscriminatedAlertRule = Annotated[AlertRule, Field(discriminator="kind")]


class AlertChannels(BaseModel):
    """At least one channel must be enabled, or an alert can never notify
    anyone -- validated the same way TrendSpec validates its measure.
    `email`/`webhook_url` delivery is pulse.alerts.delivery's job;
    `in_app=True` needs no delivery step at all -- the AlertEvent row a fire
    writes IS the in-app notification (SPEC.md #6.16)."""

    email: list[str] = Field(default_factory=list)
    webhook_url: str | None = None
    in_app: bool = True

    @model_validator(mode="after")
    def _at_least_one_channel(self) -> AlertChannels:
        if not self.email and not self.webhook_url and not self.in_app:
            raise ValueError("at least one delivery channel must be enabled")
        return self
