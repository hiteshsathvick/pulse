"""Org-wide usage metering from the event_hourly rollup (SPEC.md #6.14),
never raw events -- consistent with "router prefers rollups" for anything
that scans a whole billing period. Unlike the query engine's own per-spec
rollup queries (pulse/query/rollup.py), which are always scoped to one
project and one spec's event list, this is org-wide across every project
and event name -- that's what a billing total actually needs. See
SPEC.md #6.17."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from pulse.query.rollup import SKETCH_PRECISION
from pulse.repositories.clickhouse import get_client


@dataclass(frozen=True)
class UsageTotals:
    events_ingested: int
    mtu: int


def month_bounds(period: date) -> tuple[datetime, datetime]:
    start = datetime(period.year, period.month, 1, tzinfo=UTC)
    end = (
        datetime(period.year + 1, 1, 1, tzinfo=UTC)
        if period.month == 12
        else datetime(period.year, period.month + 1, 1, tzinfo=UTC)
    )
    return start, end


async def compute_org_usage(org_id: uuid.UUID, period: date) -> UsageTotals:
    """`events` is exact (a plain sum); `mtu` merges the rollup's per-
    (project, event_name, hour) uniqCombined64 sketch states org-wide --
    the same approximate-but-mergeable sketch pulse/query/rollup.py already
    uses for a single trend's unique-user count, just merged across
    everything instead of one spec's event list."""
    start, end = month_bounds(period)
    client = await get_client()
    result = await client.query(
        f"""
        SELECT sum(events) AS events_ingested,
               uniqCombined64Merge({SKETCH_PRECISION})(users_state) AS mtu
        FROM event_hourly
        WHERE org_id = {{org_id:UUID}}
          AND hour >= {{start:DateTime64(3)}}
          AND hour < {{end:DateTime64(3)}}
        """,
        parameters={"org_id": str(org_id), "start": start, "end": end},
    )
    if not result.result_rows:
        return UsageTotals(events_ingested=0, mtu=0)
    events_ingested, mtu = result.result_rows[0]
    return UsageTotals(events_ingested=int(events_ingested or 0), mtu=int(mtu or 0))
