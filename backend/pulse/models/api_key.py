import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column

from pulse.models.base import Base, IdMixin, TimestampMixin


class ApiKeyType(enum.StrEnum):
    WRITE = "write"
    READ = "read"


class ApiKey(IdMixin, TimestampMixin, Base):
    """Not RLS-protected, despite carrying org_id/project_id -- like
    RefreshToken/Invite, this is a token-redemption table: validating a key
    on every future ingest/query call (Phase 7/11) means looking it up by an
    opaque secret before any org context is known. Management endpoints
    (list/create/revoke) filter by org_id/project_id explicitly instead,
    reached only once the caller's membership has already been confirmed by
    require_role. See SPEC.md #4.1."""

    __tablename__ = "api_keys"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE")
    )
    type: Mapped[ApiKeyType] = mapped_column(ENUM(ApiKeyType, name="api_key_type"))
    # Non-secret, e.g. "pulse_write_a1b2c3d4" -- shown in a key list so a key
    # can be identified without ever seeing the full secret again.
    key_prefix: Mapped[str] = mapped_column(String)
    key_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
