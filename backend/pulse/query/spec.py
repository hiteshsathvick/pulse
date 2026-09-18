import enum
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Granularity(enum.StrEnum):
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class FilterOp(enum.StrEnum):
    EQ = "eq"
    NEQ = "neq"
    CONTAINS = "contains"


class Filter(BaseModel):
    key: str = Field(min_length=1)
    op: FilterOp
    value: str


class DateRange(BaseModel):
    """`tz: "project"` (the default) resolves to the queried project's own
    Project.timezone column at query time; any other value is treated as an
    explicit IANA timezone override. `from`/`to` are calendar dates in that
    timezone, not UTC -- SPEC.md #5.3: "never bucket on raw UTC" applies to
    the range boundary too, not just the GROUP BY bucketing."""

    from_: date = Field(alias="from")
    to: date
    tz: str = "project"

    model_config = {"populate_by_name": True}


# "count" | "unique_users" | "property_sum:<key>" | "property_avg:<key>" (SPEC.md #4.2)
# -- not a clean enum since two of the four carry a client-supplied property
# key; kept as a validated string and parsed by the query builder.
_SIMPLE_MEASURES = {"count", "unique_users"}
_PROPERTY_MEASURE_KINDS = {"property_sum", "property_avg"}


class TrendSpec(BaseModel):
    kind: Literal["trend"] = "trend"
    version: Literal[1] = 1
    events: list[str] = Field(min_length=1)
    measure: str
    filters: list[Filter] = Field(default_factory=list)
    breakdown: str | None = None
    range: DateRange
    granularity: Granularity = Granularity.DAY

    @model_validator(mode="after")
    def _validate_measure(self) -> "TrendSpec":
        if self.measure in _SIMPLE_MEASURES:
            return self
        kind, sep, key = self.measure.partition(":")
        if sep and kind in _PROPERTY_MEASURE_KINDS and key:
            return self
        raise ValueError(
            f"invalid measure {self.measure!r}: expected one of {sorted(_SIMPLE_MEASURES)} "
            f"or 'property_sum:<key>' / 'property_avg:<key>'"
        )

    @model_validator(mode="after")
    def _validate_range(self) -> "TrendSpec":
        if self.range.from_ > self.range.to:
            raise ValueError("range.from must not be after range.to")
        return self


class FunnelStep(BaseModel):
    event: str = Field(min_length=1)


class FunnelWindow(BaseModel):
    """The conversion window: a user must complete every step within
    `value` `unit`s of their *first* matching step (SPEC.md #4.2). Only
    hour/day are exposed -- a funnel spanning weeks/months isn't a
    meaningful "did this happen in one session/journey" question."""

    value: int = Field(gt=0)
    unit: Literal["hour", "day"] = "day"


class FunnelSpec(BaseModel):
    kind: Literal["funnel"] = "funnel"
    version: Literal[1] = 1
    steps: list[FunnelStep] = Field(min_length=2)
    window: FunnelWindow
    filters: list[Filter] = Field(default_factory=list)
    breakdown: str | None = None
    range: DateRange

    @model_validator(mode="after")
    def _validate_range(self) -> "FunnelSpec":
        if self.range.from_ > self.range.to:
            raise ValueError("range.from must not be after range.to")
        return self


# The only two insight kinds the query engine can compile so far (Phase
# 11/12) -- retention (Phase 13) joins this once it has a builder too.
InsightSpec = TrendSpec | FunnelSpec
