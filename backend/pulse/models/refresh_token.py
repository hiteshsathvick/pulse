import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from pulse.models.base import Base, IdMixin, TimestampMixin


class RefreshToken(IdMixin, TimestampMixin, Base):
    """A refresh token is deliberately not a JWT: it's an opaque random string,
    stored here hashed (never plaintext), so it can actually be revoked and
    rotated. Access tokens are the stateless, short-lived JWTs -- verified by
    signature only, never persisted. Not RLS-protected: like User, this has no
    org_id -- auth is per-user, not per-org."""

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    token_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
