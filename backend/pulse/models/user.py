from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from pulse.models.base import Base, IdMixin, SoftDeleteMixin, TimestampMixin


class User(IdMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A user's identity is global, not tenant-scoped -- which org(s) they
    belong to, and with what role, lives in Membership. Not RLS-protected:
    it carries no org_id."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
