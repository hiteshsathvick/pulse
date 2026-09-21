import enum
import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from pulse.models.base import Base, IdMixin, TimestampMixin


class InsightKind(enum.StrEnum):
    TREND = "trend"
    FUNNEL = "funnel"
    RETENTION = "retention"


class Insight(IdMixin, TimestampMixin, Base):
    """RLS-protected like EventSchema/Project: carries org_id and is browsed
    (listed/opened) by a member of that org, never redeemed by an opaque
    token. `spec` is a validated InsightSpec (pulse.query.spec) serialized to
    JSON -- the service layer re-validates it on every write, so a stored spec
    is always one the query engine can compile. `kind` is denormalized from
    spec["kind"] so a list can filter/label by type without parsing JSONB."""

    __tablename__ = "insights"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String)
    kind: Mapped[InsightKind] = mapped_column(ENUM(InsightKind, name="insight_kind"))
    spec: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
