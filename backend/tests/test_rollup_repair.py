"""Phase 24: deleting a subject must also remove their contribution to the hourly
rollup (`event_hourly`), whose unique-users sketch can't have one user subtracted.
`repair_buckets` recomputes just the touched buckets from `events`. Real
ClickHouse, including a deterministic simulation of an event landing mid-repair."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import clickhouse_connect
import pytest

from alembic import command
from alembic.config import Config
from pulse.clickhouse_migrations.runner import migrate
from pulse.core.config import get_settings
from pulse.events.fixtures import generate_fake_event
from pulse.events.repository import insert_events
from pulse.query.rollup import SKETCH_PRECISION
from pulse.repositories.clickhouse import get_client as get_clickhouse_client
from pulse.rollups import maintenance
from pulse.services import deletion as deletion_service
from tests.clickhouse_schema import drop_event_schema
from tests.test_deletion import _create_org_and_project

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_MIGRATIONS_DIR = _BACKEND_ROOT / "pulse" / "clickhouse_migrations" / "migrations"


def _alembic_config() -> Config:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return config


@pytest.fixture(scope="module", autouse=True)
def _control_plane_schema() -> Any:
    config = _alembic_config()
    command.upgrade(config, "head")
    yield
    command.downgrade(config, "base")


async def _with_fresh_clickhouse_client(body: Any) -> None:
    settings = get_settings()
    client = await clickhouse_connect.get_async_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        database=settings.clickhouse_database,
        secure=settings.clickhouse_secure,
    )
    try:
        await body(client)
    finally:
        await client.close()


@pytest.fixture(scope="module", autouse=True)
def _events_table() -> Any:
    asyncio.run(_with_fresh_clickhouse_client(lambda client: migrate(client, _MIGRATIONS_DIR)))
    yield
    asyncio.run(_with_fresh_clickhouse_client(drop_event_schema))


def _hour(hours_ago: int) -> datetime:
    return datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=hours_ago)


def _event(org: uuid.UUID, project: uuid.UUID, user: str, name: str, hour: datetime):
    return generate_fake_event(
        org, project, user_id=user, event_name=name, timestamp=hour + timedelta(minutes=17)
    )


async def _rollup(
    client: Any, org: uuid.UUID, project: uuid.UUID
) -> dict[tuple[str, datetime], tuple[int, int]]:
    """(event name, hour) -> (events, unique users), read the way the query engine
    reads it: summed / sketch-merged across whatever parts exist."""
    result = await client.query(
        "SELECT event_name, hour, sum(events), "
        f"uniqCombined64Merge({SKETCH_PRECISION})(users_state) "
        "FROM event_hourly WHERE org_id = {org:UUID} AND project_id = {project:UUID} "
        "GROUP BY event_name, hour",
        parameters={"org": str(org), "project": str(project)},
    )
    # clickhouse-connect hands DateTime('UTC') back naive; the tests use aware UTC.
    return {(r[0], r[1].replace(tzinfo=UTC)): (int(r[2]), int(r[3])) for r in result.result_rows}


async def test_the_rollup_no_longer_counts_a_deleted_subject() -> None:
    org, project, actor = await _create_org_and_project()
    client = await get_clickhouse_client()
    h0, h1 = _hour(3), _hour(2)
    await insert_events(
        client,
        [
            _event(org, project, "alice", "page view", h0),
            _event(org, project, "alice", "page view", h0),
            _event(org, project, "bob", "page view", h0),
            _event(org, project, "alice", "signup", h0),  # a bucket only alice is in
            _event(org, project, "bob", "page view", h1),  # an hour alice never touched
        ],
    )
    before = await _rollup(client, org, project)
    assert before[("page view", h0)] == (3, 2)
    assert before[("signup", h0)] == (1, 1)

    report = await deletion_service.delete_subject(client, org, project, actor, user_id="alice")

    after = await _rollup(client, org, project)
    assert after[("page view", h0)] == (1, 1)  # alice's two events and her user are gone
    assert ("signup", h0) not in after  # the bucket only she was in is gone entirely
    assert after[("page view", h1)] == (1, 1)  # untouched hour unchanged
    assert report.rollup_verified is True
    assert report.rollup_buckets_recomputed == 2 * 1  # 2 event names x 1 hour touched
    # And the rollup as a whole agrees with events again.
    assert (await maintenance.verify(client, project_id=project)).ok


async def test_another_projects_rollup_is_left_alone() -> None:
    org_a, project_a, actor = await _create_org_and_project()
    org_b, project_b, _ = await _create_org_and_project()
    client = await get_clickhouse_client()
    hour = _hour(4)
    await insert_events(
        client,
        [
            _event(org_a, project_a, "shared", "page view", hour),
            _event(org_b, project_b, "shared", "page view", hour),
            _event(org_b, project_b, "shared", "page view", hour),
        ],
    )

    await deletion_service.delete_subject(client, org_a, project_a, actor, user_id="shared")

    assert await _rollup(client, org_b, project_b) == {("page view", hour): (2, 1)}


class _RacingClient:
    """Wraps the real client; runs `before_insert` once, just before the first
    recompute INSERT -- i.e. after the rollup rows were deleted and before they
    are rebuilt, the exact window in which live ingestion could land an event."""

    def __init__(self, real: Any, before_insert: Any) -> None:
        self._real = real
        self._before_insert = before_insert
        self._fired = False
        self.recompute_inserts = 0

    async def command(self, sql: str, **kwargs: Any) -> Any:
        if sql.startswith("INSERT INTO event_hourly"):
            self.recompute_inserts += 1
            if not self._fired:
                self._fired = True
                await self._before_insert()
        return await self._real.command(sql, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


async def test_an_event_landing_mid_repair_is_detected_and_the_repair_converges() -> None:
    """The materialized view counts the late event when it lands, and the recompute
    counts it again from `events` -- a double count. Event counts are exact, so the
    scoped verify catches it and the (idempotent) repair runs a second time."""
    org, project, actor = await _create_org_and_project()
    real = await get_clickhouse_client()
    hour = _hour(5)
    await insert_events(
        real,
        [
            _event(org, project, "alice", "page view", hour),
            _event(org, project, "bob", "page view", hour),
        ],
    )

    async def late_event_lands() -> None:
        await insert_events(real, [_event(org, project, "carol", "page view", hour)])

    racing = _RacingClient(real, late_event_lands)
    report = await deletion_service.delete_subject(racing, org, project, actor, user_id="alice")  # type: ignore[arg-type]

    assert report.rollup_verified is True
    # Two recompute passes: the first double-counted the late event, was caught,
    # and was redone. One pass would mean the race never happened or went unseen.
    assert racing.recompute_inserts == 2
    assert (await _rollup(real, org, project))[("page view", hour)] == (2, 2)  # bob + carol
    assert (await maintenance.verify(real, project_id=project)).ok


class _AlwaysDoubleCounting:
    """Makes every recompute INSERT run twice, so no attempt can ever verify."""

    def __init__(self, real: Any) -> None:
        self._real = real

    async def command(self, sql: str, **kwargs: Any) -> Any:
        if sql.startswith("INSERT INTO event_hourly"):
            await self._real.command(sql, **kwargs)
        return await self._real.command(sql, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


async def test_a_repair_that_cannot_converge_raises_instead_of_claiming_success() -> None:
    org, project, _ = await _create_org_and_project()
    real = await get_clickhouse_client()
    hour = _hour(6)
    await insert_events(real, [_event(org, project, "bob", "page view", hour)])

    with pytest.raises(maintenance.RollupRepairFailed):
        await maintenance.repair_buckets(
            _AlwaysDoubleCounting(real),  # type: ignore[arg-type]
            org,
            project,
            ["page view"],
            [hour],
        )


async def test_repairing_nothing_is_a_verified_no_op() -> None:
    org, project, _ = await _create_org_and_project()
    client = await get_clickhouse_client()
    report = await maintenance.repair_buckets(client, org, project, [], [])
    assert (report.buckets_recomputed, report.verified, report.attempts) == (0, True, 0)
