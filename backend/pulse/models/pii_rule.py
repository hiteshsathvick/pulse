import enum
import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column

from pulse.models.base import Base, IdMixin, TimestampMixin


class PiiAction(enum.StrEnum):
    HASH = "hash"
    DROP = "drop"


class PiiRule(IdMixin, TimestampMixin, Base):
    """A project-level, proactive PII control (Phase 22): `property_key` is
    checked at ingest (pulse/worker/processing.py) before a property is ever
    written to ClickHouse, independent of whether the schema registry has
    seen it yet -- a rule can protect "email" from the very first event
    that carries it, not just after the first (unprotected) sighting.
    RLS-protected like every other tenant table, but also scoped to one
    project (unique on project_id+property_key) since a property key's PII
    status is a project's own call, not shared org-wide by default."""

    __tablename__ = "pii_rules"
    __table_args__ = (
        UniqueConstraint("project_id", "property_key", name="uq_pii_rules_project_property"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE")
    )
    property_key: Mapped[str] = mapped_column(String)
    action: Mapped[PiiAction] = mapped_column(ENUM(PiiAction, name="pii_action"))
