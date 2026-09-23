import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

PropertyValue = str | float | bool | None


class IngestEvent(BaseModel):
    """The wire shape a client SDK sends. Deliberately not the `events`
    table's own column shape (SPEC.md #5.1) -- typed-column coercion,
    schema-registry validation, and enrichment (geo/device/server timestamp)
    are Phase 8/9's job, applied once the event leaves the buffer. This is
    only the "lightweight validation" the Phase 7 DoD asks for: structurally
    well-formed, nothing more."""

    event_id: uuid.UUID
    event: str = Field(min_length=1, max_length=200)
    # Phase 22 input-validation sweep: these were previously fully
    # unbounded -- a legitimate (if unusual) gap for a public, write-key-only
    # endpoint meant to accept arbitrary customer app traffic.
    user_id: str | None = Field(default=None, max_length=200)
    anonymous_id: str | None = Field(default=None, max_length=200)
    timestamp: datetime | None = None
    properties: dict[str, PropertyValue] | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _requires_a_user_or_anonymous_id(self) -> "IngestEvent":
        if not self.user_id and not self.anonymous_id:
            raise ValueError("at least one of user_id or anonymous_id is required")
        return self


class IngestBatchRequest(BaseModel):
    batch: list[IngestEvent] = Field(min_length=1)


class IngestBatchResponse(BaseModel):
    accepted: int
    # Set once this org's ingested-events-this-month has crossed the soft
    # limit (Settings.billing_soft_limit_ratio) -- the batch is still
    # accepted; this is only a heads-up before the hard limit rejects one.
    quota_warning: str | None = None
