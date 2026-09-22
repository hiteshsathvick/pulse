"""SPEC.md Phase 20 DoD: "metering accuracy vs ingested volume." Inserts a
known number of real events directly into ClickHouse (the same fixture
helpers every other integration test in this repo uses) and asserts the
computed usage matches exactly -- event counts are exact by construction
(a plain sum), so "accuracy" here means "exactly right", not "close"."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import clickhouse_connect
import pytest

from alembic import command
from alembic.config import Config
from pulse.billing import service as billing_service
from pulse.billing.usage import compute_org_usage
from pulse.clickhouse_migrations.runner import migrate
from pulse.core.config import get_settings
from pulse.events.fixtures import generate_fake_event
from pulse.events.repository import insert_events
from pulse.models import User
from pulse.repositories.clickhouse import get_client as get_clickhouse_client
from pulse.repositories.postgres import session_scope
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


async def _create_org_and_project() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with session_scope() as session:
        user = User(
            email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="not-a-real-hash",
            name="Owner",
        )
        session.add(user)
        await session.commit()

    org = await orgs_service.create_organization(
        "Billing Test Org", f"billing-org-{uuid.uuid4().hex[:8]}", user.id
    )
    project = await projects_service.create_project(org.id, "Web", "web", "UTC", user.id)
    return org.id, project.id, user.id


async def test_org_creation_auto_creates_a_free_subscription() -> None:
    org_id, _project_id, _user_id = await _create_org_and_project()
    subscription = await billing_service.get_subscription(org_id)
    assert subscription is not None
    assert subscription.plan.value == "free"
    assert subscription.status.value == "active"
    assert subscription.stripe_customer_id is None


async def test_event_count_is_exact() -> None:
    org_id, project_id, _user_id = await _create_org_and_project()
    now = datetime.now(UTC)
    ch_client = await get_clickhouse_client()

    await insert_events(
        ch_client,
        [
            generate_fake_event(org_id, project_id, event_name="checkout completed", timestamp=now)
            for _ in range(37)
        ],
    )

    period = now.date().replace(day=1)
    totals = await compute_org_usage(org_id, period)
    assert totals.events_ingested == 37


async def test_usage_is_org_wide_across_projects_and_event_names() -> None:
    org_id, project_a, user_id = await _create_org_and_project()
    project_b = await projects_service.create_project(org_id, "Mobile", "mobile", "UTC", user_id)
    now = datetime.now(UTC)
    ch_client = await get_clickhouse_client()

    await insert_events(
        ch_client,
        [
            generate_fake_event(org_id, project_a, event_name="checkout completed", timestamp=now)
            for _ in range(5)
        ]
        + [
            generate_fake_event(org_id, project_a, event_name="signed up", timestamp=now)
            for _ in range(3)
        ]
        + [
            generate_fake_event(org_id, project_b.id, event_name="page viewed", timestamp=now)
            for _ in range(4)
        ],
    )

    period = now.date().replace(day=1)
    totals = await compute_org_usage(org_id, period)
    assert totals.events_ingested == 12  # 5 + 3 + 4, across two projects and three event names


async def test_events_outside_the_period_are_not_counted() -> None:
    org_id, project_id, _user_id = await _create_org_and_project()
    now = datetime.now(UTC)
    this_month_start = now.date().replace(day=1)
    last_month = (this_month_start - timedelta(days=1)).replace(day=1)
    ch_client = await get_clickhouse_client()

    await insert_events(
        ch_client,
        [
            generate_fake_event(org_id, project_id, event_name="checkout completed", timestamp=now)
            for _ in range(4)
        ]
        + [
            generate_fake_event(
                org_id,
                project_id,
                event_name="checkout completed",
                timestamp=datetime(last_month.year, last_month.month, 15, tzinfo=UTC),
            )
            for _ in range(9)
        ],
    )

    totals = await compute_org_usage(org_id, this_month_start)
    assert totals.events_ingested == 4


async def test_mtu_counts_distinct_users_not_events() -> None:
    org_id, project_id, _user_id = await _create_org_and_project()
    now = datetime.now(UTC)
    ch_client = await get_clickhouse_client()

    # 3 distinct users, 2 events each -- 6 events, 3 unique users.
    events = []
    for i in range(3):
        events += [
            generate_fake_event(
                org_id, project_id, event_name="page viewed", user_id=f"user_{i}", timestamp=now
            )
            for _ in range(2)
        ]
    await insert_events(ch_client, events)

    period = now.date().replace(day=1)
    totals = await compute_org_usage(org_id, period)
    assert totals.events_ingested == 6
    assert totals.mtu == 3


async def test_compute_and_store_usage_upserts_the_same_period_row() -> None:
    org_id, project_id, _user_id = await _create_org_and_project()
    now = datetime.now(UTC)
    period = now.date().replace(day=1)
    ch_client = await get_clickhouse_client()

    await insert_events(
        ch_client,
        [
            generate_fake_event(org_id, project_id, event_name="checkout completed", timestamp=now)
            for _ in range(2)
        ],
    )
    first = await billing_service.compute_and_store_usage(org_id, period)
    assert first.events_ingested == 2

    await insert_events(
        ch_client,
        [
            generate_fake_event(org_id, project_id, event_name="checkout completed", timestamp=now)
            for _ in range(3)
        ],
    )
    second = await billing_service.compute_and_store_usage(org_id, period)
    assert second.events_ingested == 5  # re-summed from ClickHouse, not incremented
    assert second.id == first.id  # same row, upserted -- not a new one per cycle

    stored = await billing_service.get_usage(org_id, period)
    assert stored is not None
    assert stored.events_ingested == 5
