import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column

from pulse.models.base import Base, IdMixin, TimestampMixin


class SchemaStatus(enum.StrEnum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    HIDDEN = "hidden"


class PropertyType(enum.StrEnum):
    STRING = "string"
    NUMBER = "number"
    BOOL = "bool"
    DATETIME = "datetime"


class EventSchema(IdMixin, TimestampMixin, Base):
    """RLS-protected like Project/AuditLog: carries org_id, reached via a
    browse-this-project's-taxonomy pattern, not token redemption. Auto-created
    by the ingestion worker (Phase 8/9) the first time an event name is seen
    for a project; never blocks ingestion if this write fails (Phase 9's
    registry is best-effort, see pulse/registry/service.py)."""

    __tablename__ = "event_schemas"
    __table_args__ = (
        UniqueConstraint("project_id", "event_name", name="uq_event_schemas_project_event"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE")
    )
    event_name: Mapped[str] = mapped_column(String)
    status: Mapped[SchemaStatus] = mapped_column(
        ENUM(SchemaStatus, name="schema_status"), default=SchemaStatus.ACTIVE
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    volume_estimate: Mapped[int] = mapped_column(Integer, default=0)


class PropertySchema(IdMixin, TimestampMixin, Base):
    """org_id is denormalized here (also derivable via event_schema_id's own
    org_id) so RLS can scope this table directly, matching every other
    RLS-protected table in this codebase -- never via a join.

    event_schema_id is nullable per SPEC.md #4 ("nullable = event-agnostic"),
    for a future project-wide property catalog entry not tied to one event
    name. Phase 9 does not populate that path -- every property it registers
    is tied to a specific event."""

    __tablename__ = "property_schemas"
    __table_args__ = (
        UniqueConstraint("event_schema_id", "key", name="uq_property_schemas_event_key"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    event_schema_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("event_schemas.id", ondelete="CASCADE")
    )
    key: Mapped[str] = mapped_column(String)
    # Set once at first sighting, never auto-changed -- a later observation of
    # a different type flags type_conflict_detected_at instead of overwriting
    # this, so the originally-registered contract stays visible.
    inferred_type: Mapped[PropertyType] = mapped_column(ENUM(PropertyType, name="property_type"))
    is_pii: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[SchemaStatus] = mapped_column(
        ENUM(SchemaStatus, name="schema_status"), default=SchemaStatus.ACTIVE
    )
    type_conflict_detected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
