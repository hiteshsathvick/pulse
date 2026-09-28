"""GDPR-style subject deletion. A "subject" here is an arbitrary end-user
identifier the customer's own app assigns via the ingestion SDK
(events.user_id / events.anonymous_id) -- not a Pulse console User, which is an
entirely separate identity (pulse/models/user.py).

Phase 22 covered ClickHouse `events` only. Phase 24 closed two of the three
gaps it documented, so a deletion reaches every store that holds the subject:

  1. `events`            -- the rows themselves (ALTER TABLE ... DELETE).
  2. `event_hourly`      -- the rollup keeps a per-hour unique-users sketch that
                            still contains the subject and can't be edited, so the
                            (event x hour) buckets the subject touched are
                            recomputed from what remains (rollups.repair_buckets).
                            Scoped, so it does NOT need the global, ingestion-
                            stopped `rebuild`.
  3. raw object archive  -- rewritten without the subject's entries
                            (archive.erase_subject).

Phase 25 closes the third: an event the API had already accepted into the Redis
stream, but the worker had not yet landed, used to reappear after this call
returned -- reintroducing exactly the data that was just erased. `suppress()`
below is called FIRST, before anything else, so any such event racing in during
the rest of this function is caught too; the worker checks it
(pulse/worker/processing.py, pulse/suppression.py) before landing an event.

Remaining, deliberate scope limits (docs/THREAT_MODEL.md): suppression is
bounded, not permanent (pulse/suppression.py); archive entries whose org/project
couldn't be parsed can't be attributed to a tenant; backups are out of scope.
See SPEC.md #6.20 / #6.22 / #6.25."""

import uuid
from dataclasses import dataclass

from clickhouse_connect.driver.asyncclient import AsyncClient
from redis.asyncio import Redis

from pulse import archive, suppression
from pulse.core.config import get_settings
from pulse.repositories.postgres import session_scope
from pulse.rollups import maintenance as rollup_maintenance
from pulse.services import audit


class NoIdentifierGiven(Exception):
    """Neither user_id nor anonymous_id was provided -- deleting "everyone"
    isn't what a subject-deletion request means."""


@dataclass(frozen=True)
class DeletionReport:
    rollup_buckets_recomputed: int
    rollup_verified: bool
    archive: archive.ErasureReport


async def delete_subject(
    clickhouse_client: AsyncClient,
    redis_client: Redis,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    user_id: str | None = None,
    anonymous_id: str | None = None,
) -> DeletionReport:
    if not user_id and not anonymous_id:
        raise NoIdentifierGiven()

    # First, before anything else: any event for this identity that lands in
    # the stream from here on is dropped by the worker, not just the events
    # already sitting there when this call started.
    await suppression.suppress(
        redis_client,
        org_id,
        project_id,
        user_id=user_id,
        anonymous_id=anonymous_id,
        ttl_seconds=get_settings().subject_suppression_ttl_seconds,
    )

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

    # Which rollup buckets the subject contributes to must be read BEFORE their
    # rows are deleted -- afterwards there is nothing left to ask.
    touched = await clickhouse_client.query(
        "SELECT DISTINCT event_name, toStartOfHour(toDateTime(timestamp, 'UTC'), 'UTC') "
        f"FROM events WHERE {' AND '.join(where)}",
        parameters=parameters,
    )
    event_names = [str(row[0]) for row in touched.result_rows]
    hours = [row[1] for row in touched.result_rows]

    await clickhouse_client.command(
        f"ALTER TABLE events DELETE WHERE {' AND '.join(where)}",
        parameters=parameters,
        # Waits for this mutation to actually finish -- someone calling
        # "delete my data" expects it done by the time this returns, not
        # merely queued.
        settings={"mutations_sync": 1},
    )

    repair = await rollup_maintenance.repair_buckets(
        clickhouse_client, org_id, project_id, event_names, hours
    )
    archive_report = await archive.erase_subject(
        org_id, project_id, user_id=user_id, anonymous_id=anonymous_id
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
                "rollup_buckets_recomputed": repair.buckets_recomputed,
                "archive_entries_removed": archive_report.entries_removed,
                "archive_unreadable_objects": archive_report.unreadable,
            },
        )
        await session.commit()

    return DeletionReport(
        rollup_buckets_recomputed=repair.buckets_recomputed,
        rollup_verified=repair.verified,
        archive=archive_report,
    )
