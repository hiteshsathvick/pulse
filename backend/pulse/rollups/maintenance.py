"""Checking and repairing the hourly rollup (`event_hourly`).

The materialized view keeps the rollup current for every insert it sees, but
it can drift from `events` if the view was ever missing while events were
written (a partial deploy, a manual `DROP`), or if rows are deleted from
`events` behind its back. `verify` finds that; `rebuild` fixes it."""

import uuid
from dataclasses import dataclass
from pathlib import Path

from clickhouse_connect.driver.asyncclient import AsyncClient
from clickhouse_connect.driver.client import Client as SyncClient

from pulse.clickhouse_migrations.runner import _split_statements
from pulse.query.rollup import SKETCH_PRECISION

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "clickhouse_migrations"
    / "migrations"
    / "0002_event_hourly_rollup.sql"
)

# The one definition of the rollup lives in the migration. Rebuilding re-runs
# its statements rather than restating them here, so the two can't disagree.
_CREATE_VIEW_INDEX, _BACKFILL_INDEX = 1, 2


@dataclass(frozen=True)
class Mismatch:
    org_id: uuid.UUID
    project_id: uuid.UUID
    event_name: str
    hour: str
    raw_events: int
    raw_users: int
    rollup_events: int
    rollup_users: int


@dataclass(frozen=True)
class VerifyReport:
    total_mismatched_hours: int
    sample: list[Mismatch]

    @property
    def ok(self) -> bool:
        return self.total_mismatched_hours == 0


# The rollup's unique-user sketch is approximate (0.4% mean, ~3.5% worst measured),
# so user counts are compared with a tolerance; event counts are exact and compared exactly.
USER_TOLERANCE = 0.05
_HOUR = "toStartOfHour(toDateTime(timestamp, 'UTC'), 'UTC')"
_IDENTITY = "if(user_id != '', user_id, anonymous_id)"


def _comparison(where_events: str, where_rollup: str) -> str:
    return f"""
        SELECT org_id, project_id, event_name, hour,
               raw_events, raw_users, roll_events, roll_users
        FROM (
            SELECT org_id, project_id, event_name, {_HOUR} AS hour,
                   count() AS raw_events, uniqExact({_IDENTITY}) AS raw_users
            FROM events {where_events}
            GROUP BY org_id, project_id, event_name, hour
        ) AS r
        FULL OUTER JOIN (
            SELECT org_id, project_id, event_name, hour,
                   sum(events) AS roll_events,
                   uniqCombined64Merge({SKETCH_PRECISION})(users_state) AS roll_users
            FROM event_hourly {where_rollup}
            GROUP BY org_id, project_id, event_name, hour
        ) AS a USING (org_id, project_id, event_name, hour)
        WHERE raw_events != roll_events
           OR abs(toInt64(raw_users) - toInt64(roll_users))
              > greatest(1, raw_users * {USER_TOLERANCE})
    """


async def verify(
    client: AsyncClient, *, project_id: uuid.UUID | None = None, sample_size: int = 20
) -> VerifyReport:
    """Compares, hour by hour and event by event, what `events` says with what
    `event_hourly` says: event counts exactly, unique users within a small
    tolerance (the rollup holds an approximate sketch). A FULL join, so an hour
    present on only one side (a missing or an orphaned rollup row) is a
    mismatch too."""
    where = "WHERE project_id = {project_id:UUID}" if project_id else ""
    parameters: dict[str, object] = {"project_id": str(project_id)} if project_id else {}
    body = _comparison(where, where)

    total = await client.query(f"SELECT count() FROM ({body})", parameters=parameters)
    sample_rows = await client.query(
        f"{body} ORDER BY org_id, project_id, event_name, hour LIMIT {int(sample_size)}",
        parameters=parameters,
    )
    sample = [
        Mismatch(
            org_id=row[0],
            project_id=row[1],
            event_name=row[2],
            hour=row[3].isoformat(),
            raw_events=row[4],
            raw_users=row[5],
            rollup_events=row[6],
            rollup_users=row[7],
        )
        for row in sample_rows.result_rows
    ]
    return VerifyReport(total_mismatched_hours=int(total.result_rows[0][0]), sample=sample)


def _view_and_backfill_statements() -> tuple[str, str]:
    statements = _split_statements(_MIGRATION.read_text())
    create_view, backfill = statements[_CREATE_VIEW_INDEX], statements[_BACKFILL_INDEX]
    if not create_view.startswith("CREATE MATERIALIZED VIEW") or not backfill.startswith(
        "INSERT INTO event_hourly"
    ):
        raise RuntimeError(
            "migration 0002 no longer has the expected layout (table, view, backfill); "
            "update pulse/rollups/maintenance.py"
        )
    return create_view, backfill


def recreate_view(client: SyncClient) -> None:
    """Puts the materialized view back without touching its data. For restoring
    it after it was dropped on purpose (the load-test harness drops it to measure
    what the rollup costs ingestion). Takes a synchronous client."""
    create_view, _ = _view_and_backfill_statements()
    client.command("DROP VIEW IF EXISTS mv_event_hourly")
    client.command(create_view)


async def rebuild(client: AsyncClient) -> int:
    """Empties the rollup and recomputes it from `events`. Returns the number
    of rollup rows afterwards.

    Run this with the ingestion workers stopped. The view is dropped while the
    backfill runs and recreated after it, so an event inserted in between would
    be missing from the rollup (and one inserted while the view existed *and*
    the backfill ran would be counted twice). Run `verify` afterwards."""
    create_view, backfill = _view_and_backfill_statements()

    await client.command("DROP VIEW IF EXISTS mv_event_hourly")
    await client.command("TRUNCATE TABLE event_hourly")
    await client.command(backfill)
    await client.command(create_view)

    result = await client.query("SELECT count() FROM event_hourly")
    return int(result.result_rows[0][0])
