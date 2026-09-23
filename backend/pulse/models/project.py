import uuid

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from pulse.models.base import Base, IdMixin, SoftDeleteMixin, TimestampMixin


class Project(IdMixin, TimestampMixin, SoftDeleteMixin, Base):
    """The analytics unit and the write-key boundary (SPEC.md #4). RLS-protected:
    carries org_id. Slug is unique per-org, not globally -- two orgs can each
    have a project called "web-app"."""

    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("org_id", "slug", name="uq_projects_org_slug"),)

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String)
    slug: Mapped[str] = mapped_column(String)
    timezone: Mapped[str] = mapped_column(String, default="UTC")
    # Phase 22: null means "use Organization.retention_days" -- an explicit
    # override is only stored here when a project actually diverges from its
    # org's default, so the org-level default still applies for anyone who's
    # never touched this setting.
    retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
