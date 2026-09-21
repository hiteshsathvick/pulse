import enum
import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from pulse.models.base import Base, IdMixin, TimestampMixin


class DashboardScope(enum.StrEnum):
    PRIVATE = "private"
    ORG = "org"


class Dashboard(IdMixin, TimestampMixin, Base):
    """RLS-protected like Insight. `shared_scope` decides who can *see* it
    (PRIVATE: only `created_by`; ORG: every member of the org); who can *edit*
    is a separate rule enforced in pulse/dashboards/service.py. `layout` holds
    grid configuration (just the column count today); each tile's own placement
    lives on its DashboardItem. `default_range` is a validated DashboardRange
    (relative "last N days" or absolute dates) serialized to JSON."""

    __tablename__ = "dashboards"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String)
    layout: Mapped[dict[str, object]] = mapped_column(JSONB)
    default_range: Mapped[dict[str, object]] = mapped_column(JSONB)
    shared_scope: Mapped[DashboardScope] = mapped_column(
        ENUM(DashboardScope, name="dashboard_scope"), default=DashboardScope.PRIVATE
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )


class DashboardItem(IdMixin, TimestampMixin, Base):
    """One saved insight placed on a dashboard. org_id is denormalized (also
    derivable via dashboard_id) so RLS scopes this table directly, like every
    other RLS table here -- never via a join. Deleting either the dashboard or
    the insight removes the item: an insight that no longer exists simply
    disappears from the dashboards that showed it."""

    __tablename__ = "dashboard_items"
    __table_args__ = (
        UniqueConstraint("dashboard_id", "insight_id", name="uq_dashboard_items_dashboard_insight"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    dashboard_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dashboards.id", ondelete="CASCADE"), index=True
    )
    insight_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("insights.id", ondelete="CASCADE")
    )
    position: Mapped[dict[str, object]] = mapped_column(JSONB)
