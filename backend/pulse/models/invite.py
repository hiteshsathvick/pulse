import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column

from pulse.models.base import Base, IdMixin, TimestampMixin
from pulse.models.membership import MembershipRole


class Invite(IdMixin, TimestampMixin, Base):
    """Carries org_id but is deliberately NOT RLS-protected, unlike
    Membership/Project -- accepting an invite means looking it up by an
    opaque token *before* the caller has any org context to scope a session
    by, the same structural problem RefreshToken already solves the same
    way. The token is the security boundary here, not RLS; see SPEC.md
    #4.1. Listing invites for management (org-scoped) filters by org_id
    explicitly in the query instead, reached only once the caller's
    membership in that org has already been confirmed."""

    __tablename__ = "invites"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    email: Mapped[str] = mapped_column(String)
    role: Mapped[MembershipRole] = mapped_column(
        ENUM(MembershipRole, name="membership_role", create_type=False)
    )
    invited_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    token_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
