import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from pulse.models.base import Base, IdMixin


class AuditLog(IdMixin, Base):
    """RLS-protected like Membership/Project: it carries org_id and is
    accessed via a browse-this-org's-history pattern, not a token-redemption
    one. Immutable by design -- no updated_at, no soft delete; an audit trail
    that could be edited or hidden after the fact isn't one."""

    __tablename__ = "audit_logs"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    action: Mapped[str] = mapped_column(String)
    target: Mapped[str] = mapped_column(String)
    # Python attribute is log_metadata, not metadata -- that name is reserved
    # on every SQLAlchemy declarative model (DeclarativeBase.metadata). The
    # DB column itself is still named "metadata".
    log_metadata: Mapped[dict[str, object]] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
