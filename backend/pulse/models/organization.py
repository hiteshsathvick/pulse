from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from pulse.models.base import Base, IdMixin, SoftDeleteMixin, TimestampMixin


class Organization(IdMixin, TimestampMixin, SoftDeleteMixin, Base):
    """The tenant itself. Not RLS-protected: RLS scopes rows that reference a
    tenant via org_id, and Organization doesn't reference one -- it is one."""

    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String)
    slug: Mapped[str] = mapped_column(String, unique=True, index=True)
    retention_days: Mapped[int] = mapped_column(Integer, default=365)
