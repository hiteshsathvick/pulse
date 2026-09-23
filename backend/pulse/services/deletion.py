"""Phase 22: GDPR-style subject deletion. A "subject" here is an arbitrary
end-user identifier the customer's own app assigns via the ingestion SDK
(events.user_id / events.anonymous_id) -- not a Pulse console User, which
is an entirely separate identity (pulse/models/user.py). Scoped to
ClickHouse `events` only: `event_hourly`'s aggregated sketches retain the
subject's contribution until an operator next runs the existing
`python -m pulse.rollups rebuild` (a global, ingestion-must-be-stopped
operation -- not something this call can safely trigger inline), and the
raw batch archive in object storage is batch-shaped, not per-subject-
editable without rewriting archive files. Both are documented, deliberate
gaps, not silently ignored. See SPEC.md #6.20."""

import uuid

from clickhouse_connect.driver.asyncclient import AsyncClient

from pulse.repositories.postgres import session_scope
from pulse.services import audit


class NoIdentifierGiven(Exception):
    """Neither user_id nor anonymous_id was provided -- deleting "everyone"
    isn't what a subject-deletion request means."""


async def delete_subject(
    clickhouse_client: AsyncClient,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    user_id: str | None = None,
    anonymous_id: str | None = None,
) -> None:
    if not user_id and not anonymous_id:
        raise NoIdentifierGiven()

    where = ["org_id = {org_id:UUID}", "project_id = {project_id:UUID}"]
    parameters: dict[str, object] = {"org_id": str(org_id), "project_id": str(project_id)}
    identity_clauses = []
    if user_id:
        identity_clauses.append("user_id = {user_id:String}")
        parameters["user_id"] = user_id
    if anonymous_id:
        identity_clauses.append("anonymous_id = {anonymous_id:String}")
        parameters["anonymous_id"] = anonymous_id
    where.append(f"({' OR '.join(identity_clauses)})")

    await clickhouse_client.command(
        f"ALTER TABLE events DELETE WHERE {' AND '.join(where)}",
        parameters=parameters,
        # Waits for this mutation to actually finish -- someone calling
        # "delete my data" expects it done by the time this returns, not
        # merely queued.
        settings={"mutations_sync": 1},
    )

    # A best-effort audit trail, not atomic with the ClickHouse mutation
    # above (they're two different databases) -- but "every mutating action
    # writes an audit log" (SPEC.md #6) still applies, and a deletion this
    # destructive should never go unrecorded even if it can't be made
    # transactionally atomic with the delete itself.
    async with session_scope(org_id=org_id) as session:
        audit.record(
            session,
            org_id=org_id,
            actor_id=actor_id,
            action="subject.deleted",
            target=user_id or anonymous_id or "",
            metadata={
                "project_id": str(project_id),
                "user_id": user_id,
                "anonymous_id": anonymous_id,
            },
        )
        await session.commit()
