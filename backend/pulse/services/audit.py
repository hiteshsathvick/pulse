import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from pulse.models import AuditLog


def record(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    action: str,
    target: str,
    metadata: dict[str, object] | None = None,
) -> None:
    """Adds an audit log entry to the given session, in the same transaction
    as the mutation it's recording -- if that transaction rolls back, the
    audit entry never existed either, per CLAUDE.md/SPEC.md's standing rule
    that every mutating action writes one. Caller still has to commit."""
    session.add(
        AuditLog(
            org_id=org_id,
            actor_id=actor_id,
            action=action,
            target=target,
            log_metadata=metadata or {},
            created_at=datetime.now(UTC),
        )
    )
