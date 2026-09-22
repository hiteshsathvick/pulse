import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from pulse.models.base import Base, IdMixin, TimestampMixin


class Alert(IdMixin, TimestampMixin, Base):
    """RLS-protected like Dashboard/Insight. `rule` is a validated
    ThresholdRule|AnomalyRule (pulse.alerts.rules) serialized to JSON, the
    same store-as-JSON-validate-in-the-service-layer pattern Insight.spec
    uses; `channels` is a validated AlertChannels the same way. `is_breaching`
    and `last_evaluated_at` are the evaluator's own state, not user input --
    they're what makes an alert fire once per breach episode (set True on the
    OK->breach transition, cleared on recovery) instead of re-firing every
    evaluation cycle while a breach persists. See SPEC.md #6.16."""

    __tablename__ = "alerts"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    insight_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("insights.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String)
    rule: Mapped[dict[str, object]] = mapped_column(JSONB)
    channels: Mapped[dict[str, object]] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_breaching: Mapped[bool] = mapped_column(Boolean, default=False)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )


class AlertEvent(IdMixin, Base):
    """Immutable fire history, like AuditLog -- no updated_at, no soft
    delete; an alert's fire record that could be edited after the fact isn't
    one. `acknowledged_at` is the one mutable field, set later when a user
    dismisses it in-app -- a plain column, not a new row, since "seen" isn't
    itself a fact worth its own history."""

    __tablename__ = "alert_events"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    alert_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("alerts.id", ondelete="CASCADE"), index=True
    )
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    value: Mapped[float] = mapped_column(Float)
    message: Mapped[str] = mapped_column(String)
    delivered: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
