import argparse
import asyncio
import random
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
from pulse.models import User
from pulse.query import service as query_service
from pulse.query.spec import TrendSpec
from pulse.repositories.clickhouse import get_client as get_clickhouse_client
from pulse.repositories.postgres import session_scope
from pulse.rollups import __main__ as rollups_cli
from pulse.rollups import maintenance
from pulse.services import orgs as orgs_service
from pulse.services import projects as projects_service
from tests.clickhouse_schema import drop_event_schema

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_MIGRATIONS_DIR = _BACKEND_ROOT / "pulse" / "clickhouse_migrations" / "migrations"


def _alembic_config() -> Config:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return config


@pytest.fixture(scope="module", autouse=True)
def _control_plane_schema():
    config = _alembic_config()
    command.upgrade(config, "head")
    yield
    command.downgrade(config, "base")


async def _with_fresh_clickhouse_client(body) -> None:
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
def _events_table():
    asyncio.run(_with_fresh_clickhouse_client(lambda client: migrate(client, _MIGRATIONS_DIR)))
    yield
    asyncio.run(_with_fresh_clickhouse_client(drop_event_schema))


@pytest.fixture(autouse=True)
def _relaxed_rate_limit():
    settings = get_settings()
    original = settings.query_rate_limit_max_queries
    settings.query_rate_limit_max_queries = 1_000_000
    yield
    settings.query_rate_limit_max_queries = original


async def _create_org_and_project() -> tuple[uuid.UUID, uuid.UUID]:
    async with session_scope() as session:
        user = User(
            email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="not-a-real-hash",
            name="Owner",
        )
        session.add(user)
        await session.commit()
    org = await orgs_service.create_organization(
        "Maintenance Org", f"maint-org-{uuid.uuid4().hex[:8]}", user.id
    )
    project = await projects_service.create_project(org.id, "Web", "web", "UTC", user.id)
    return org.id, project.id


def _events(org_id: uuid.UUID, project_id: uuid.UUID, count: int = 300) -> list[dict[str, Any]]:
    rng = random.Random(7)
    start = datetime(2026, 2, 1, tzinfo=UTC)
    out = []
    for _ in range(count):
        event = generate_fake_event(
            org_id,
            project_id,
            event_name=rng.choice(["view", "click"]),
            timestamp=start + timedelta(minutes=rng.randrange(20 * 24 * 60)),
        )
        event["user_id"] = rng.choice(["", f"u{rng.randrange(25)}"])
        event["anonymous_id"] = f"anon-{rng.randrange(40)}"
        out.append(event)
    return out


async def test_verify_reports_no_drift_for_a_healthy_rollup() -> None:
    org_id, project_id = await _create_org_and_project()
    client = await get_clickhouse_client()
    await insert_events(client, _events(org_id, project_id))

    report = await maintenance.verify(client, project_id=project_id)
    assert report.ok and report.total_mismatched_hours == 0 and report.sample == []


async def test_verify_finds_drift_in_both_directions() -> None:
    org_id, project_id = await _create_org_and_project()
    client = await get_clickhouse_client()
    await insert_events(client, _events(org_id, project_id))

    # (a) an orphan: the rollup claims something `events` never saw.
    await client.command(
        f"""
        INSERT INTO event_hourly
        SELECT toUUID('{org_id}'), toUUID('{project_id}'), 'ghost',
               toDateTime('2026-02-10 10:00:00', 'UTC'), 5, uniqCombined64State(15)('nobody')
        """
    )
    # (b) a hole: a real rollup row that has gone missing.
    real = (
        await client.query(
            "SELECT event_name, hour FROM event_hourly "
            "WHERE project_id = {p:UUID} AND event_name != 'ghost' ORDER BY hour LIMIT 1",
            parameters={"p": str(project_id)},
        )
    ).result_rows[0]
    await client.command(
        "ALTER TABLE event_hourly DELETE WHERE project_id = {p:UUID} AND event_name = {e:String} "
        "AND hour = {h:DateTime('UTC')} SETTINGS mutations_sync = 2",
        parameters={"p": str(project_id), "e": real[0], "h": real[1]},
    )

    report = await maintenance.verify(client, project_id=project_id)
    assert not report.ok
    assert report.total_mismatched_hours == 2
    by_name = {m.event_name: m for m in report.sample}
    orphan, hole = by_name["ghost"], by_name[real[0]]
    assert (orphan.raw_events, orphan.rollup_events) == (0, 5)
    assert hole.raw_events > 0 and hole.rollup_events == 0


async def test_rebuild_repairs_the_drift_and_the_answers_match_raw_again() -> None:
    org_id, project_id = await _create_org_and_project()
    client = await get_clickhouse_client()
    await insert_events(client, _events(org_id, project_id))
    await client.command(
        f"""
        INSERT INTO event_hourly
        SELECT toUUID('{org_id}'), toUUID('{project_id}'), 'view',
               toDateTime('2026-02-10 10:00:00', 'UTC'), 999, uniqCombined64State(15)('phantom')
        """
    )
    assert not (await maintenance.verify(client, project_id=project_id)).ok

    rows = await maintenance.rebuild(client)
    assert rows > 0
    assert (await maintenance.verify(client)).ok

    spec = TrendSpec.model_validate(
        {
            "kind": "trend",
            "events": ["view", "click"],
            "measure": "count",
            "range": {"from": "2026-02-01", "to": "2026-02-28"},
            "granularity": "day",
        }
    )
    via_rollup = await query_service.run_trend(spec, org_id, project_id, refresh=True)
    settings = get_settings()
    settings.query_rollups_enabled = False
    try:
        via_raw = await query_service.run_trend(spec, org_id, project_id, refresh=True)
    finally:
        settings.query_rollups_enabled = True
    assert via_rollup.source == "rollup" and via_raw.source == "raw"
    assert via_rollup.results == via_raw.results and via_rollup.results

    # The view is back: a new event still reaches the rollup.
    await insert_events(
        client,
        [
            generate_fake_event(
                org_id,
                project_id,
                event_name="view",
                timestamp=datetime(2026, 2, 27, 8, tzinfo=UTC),
            )
        ],
    )
    assert (await maintenance.verify(client, project_id=project_id)).ok


async def test_verify_can_be_scoped_to_one_project() -> None:
    org_a, project_a = await _create_org_and_project()
    org_b, project_b = await _create_org_and_project()
    client = await get_clickhouse_client()
    await insert_events(client, _events(org_a, project_a, 50) + _events(org_b, project_b, 50))
    await client.command(
        f"""
        INSERT INTO event_hourly
        SELECT toUUID('{org_b}'), toUUID('{project_b}'), 'ghost',
               toDateTime('2026-02-10 10:00:00', 'UTC'), 1, uniqCombined64State(15)('x')
        """
    )
    assert (await maintenance.verify(client, project_id=project_a)).ok
    assert not (await maintenance.verify(client, project_id=project_b)).ok
    await maintenance.rebuild(client)  # leave the shared table clean for other tests


async def test_the_migration_backfills_events_that_already_exist() -> None:
    """The deploy case: `events` already holds data when the rollup is first
    created. Undo the rollup, write events with no view watching, re-apply."""
    org_id, project_id = await _create_org_and_project()
    client = await get_clickhouse_client()
    await client.command("DROP VIEW IF EXISTS mv_event_hourly")
    await client.command("DROP TABLE IF EXISTS event_hourly")
    await client.command(
        "ALTER TABLE schema_migrations DELETE WHERE version = 2 SETTINGS mutations_sync = 2"
    )
    await insert_events(client, _events(org_id, project_id))
    assert (
        await client.query(
            "SELECT count() FROM events WHERE project_id = {p:UUID}",
            parameters={"p": str(project_id)},
        )
    ).result_rows[0][0] == 300

    assert await migrate(client, _MIGRATIONS_DIR) == [2]

    report = await maintenance.verify(client, project_id=project_id)
    assert report.ok, report.sample
    filled = (
        await client.query(
            "SELECT sum(events) FROM event_hourly WHERE project_id = {p:UUID}",
            parameters={"p": str(project_id)},
        )
    ).result_rows[0][0]
    assert filled == 300


async def test_the_command_line_exits_non_zero_on_drift() -> None:
    org_id, project_id = await _create_org_and_project()
    client = await get_clickhouse_client()
    await insert_events(client, _events(org_id, project_id, 40))

    healthy = argparse.Namespace(command="verify", project=str(project_id))
    assert await rollups_cli._run(healthy) == 0

    client = await get_clickhouse_client()
    await client.command(
        f"""
        INSERT INTO event_hourly
        SELECT toUUID('{org_id}'), toUUID('{project_id}'), 'ghost',
               toDateTime('2026-02-10 10:00:00', 'UTC'), 3, uniqCombined64State(15)('x')
        """
    )
    assert await rollups_cli._run(healthy) == 1

    client = await get_clickhouse_client()
    assert await rollups_cli._run(argparse.Namespace(command="rebuild", project=None)) == 0
    client = await get_clickhouse_client()
    assert (await maintenance.verify(client, project_id=project_id)).ok
