"""Phase 22 DoD: "TTL actually expires old data." Real end-to-end tests
against ClickHouse: seeded old and new events, sweep, confirm only the new
ones remain -- for both a project-level override and an org-default
fallback."""

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
from pulse.events.repository import insert_events, query_events
from pulse.models import Organization, Project, User
from pulse.repositories.clickhouse import get_client as get_clickhouse_client
from pulse.repositories.postgres import session_scope
from pulse.retention.service import effective_retention_days, sweep_all_projects, sweep_project
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


async def _create_org_and_project(
    *, org_retention_days: int = 365, project_retention_days: int | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    async with session_scope() as session:
        user = User(
            email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="not-a-real-hash",
            name="Owner",
        )
        session.add(user)
        await session.commit()

    org = await orgs_service.create_organization(
        "Retention Org", f"retention-org-{uuid.uuid4().hex[:8]}", user.id
    )
    async with session_scope(org_id=org.id) as session:
        organization = await session.get(Organization, org.id)
        assert organization is not None
        organization.retention_days = org_retention_days
        await session.commit()

    project = await projects_service.create_project(org.id, "Web", "web", "UTC", user.id)
    if project_retention_days is not None:
        await projects_service.update_project(
            org.id, project.id, user.id, retention_days=project_retention_days
        )
    return org.id, project.id


def test_effective_retention_days_prefers_the_project_override() -> None:
    org = Organization(id=uuid.uuid4(), name="o", slug="o", retention_days=365)
    project_with_override = Project(
        id=uuid.uuid4(), org_id=org.id, name="p", slug="p", timezone="UTC", retention_days=7
    )
    project_without = Project(
        id=uuid.uuid4(), org_id=org.id, name="p2", slug="p2", timezone="UTC", retention_days=None
    )
    assert effective_retention_days(project_with_override, org) == 7
    assert effective_retention_days(project_without, org) == 365


async def test_sweep_deletes_events_older_than_the_project_s_own_retention() -> None:
    org_id, project_id = await _create_org_and_project(
        org_retention_days=365, project_retention_days=1
    )
    ch_client = await get_clickhouse_client()
    old_event = generate_fake_event(
        org_id, project_id, timestamp=datetime.now(UTC) - timedelta(days=5)
    )
    new_event = generate_fake_event(
        org_id, project_id, timestamp=datetime.now(UTC) - timedelta(hours=1)
    )
    await insert_events(ch_client, [old_event, new_event])

    async with session_scope(org_id=org_id) as session:
        project = await session.get(Project, project_id)
        organization = await session.get(Organization, org_id)
        assert project is not None
        assert organization is not None

    await sweep_project(ch_client, project, organization)
    await ch_client.command("OPTIMIZE TABLE events FINAL")

    remaining = await query_events(ch_client, org_id, project_id, limit=100)
    assert len(remaining) == 1
    assert remaining[0]["event_id"] == uuid.UUID(str(new_event["event_id"]))


async def test_sweep_falls_back_to_the_org_default_when_the_project_has_no_override() -> None:
    org_id, project_id = await _create_org_and_project(
        org_retention_days=1, project_retention_days=None
    )
    ch_client = await get_clickhouse_client()
    old_event = generate_fake_event(
        org_id, project_id, timestamp=datetime.now(UTC) - timedelta(days=5)
    )
    new_event = generate_fake_event(
        org_id, project_id, timestamp=datetime.now(UTC) - timedelta(hours=1)
    )
    await insert_events(ch_client, [old_event, new_event])

    async with session_scope(org_id=org_id) as session:
        project = await session.get(Project, project_id)
        organization = await session.get(Organization, org_id)
        assert project is not None
        assert organization is not None

    await sweep_project(ch_client, project, organization)
    await ch_client.command("OPTIMIZE TABLE events FINAL")

    remaining = await query_events(ch_client, org_id, project_id, limit=100)
    assert len(remaining) == 1
    assert remaining[0]["event_id"] == uuid.UUID(str(new_event["event_id"]))


async def test_a_generous_retention_window_deletes_nothing() -> None:
    org_id, project_id = await _create_org_and_project(project_retention_days=365)
    ch_client = await get_clickhouse_client()
    event = generate_fake_event(org_id, project_id, timestamp=datetime.now(UTC) - timedelta(days=5))
    await insert_events(ch_client, [event])

    async with session_scope(org_id=org_id) as session:
        project = await session.get(Project, project_id)
        organization = await session.get(Organization, org_id)
        assert project is not None
        assert organization is not None

    await sweep_project(ch_client, project, organization)
    await ch_client.command("OPTIMIZE TABLE events FINAL")

    remaining = await query_events(ch_client, org_id, project_id, limit=100)
    assert len(remaining) == 1


async def test_sweep_all_projects_never_leaks_across_tenants() -> None:
    org_a, project_a = await _create_org_and_project(project_retention_days=1)
    org_b, project_b = await _create_org_and_project(project_retention_days=365)
    ch_client = await get_clickhouse_client()
    old_ts = datetime.now(UTC) - timedelta(days=5)
    await insert_events(ch_client, [generate_fake_event(org_a, project_a, timestamp=old_ts)])
    await insert_events(ch_client, [generate_fake_event(org_b, project_b, timestamp=old_ts)])

    await sweep_all_projects(ch_client)
    await ch_client.command("OPTIMIZE TABLE events FINAL")

    assert await query_events(ch_client, org_a, project_a, limit=100) == []
    assert len(await query_events(ch_client, org_b, project_b, limit=100)) == 1
