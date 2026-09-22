import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import clickhouse_connect
import pytest

from alembic import command
from alembic.config import Config
from pulse.alerts import service as alerts_service
from pulse.alerts.evaluate import evaluate_alert, evaluate_all_enabled
from pulse.alerts.rules import AlertChannels, AnomalyRule, ThresholdRule
from pulse.clickhouse_migrations.runner import migrate
from pulse.core.config import get_settings
from pulse.events.fixtures import generate_fake_event
from pulse.events.repository import insert_events
from pulse.insights.service import create_insight
from pulse.models import User
from pulse.query.spec import (
    DateRange,
    FunnelSpec,
    FunnelStep,
    FunnelWindow,
    RetentionSpec,
    TrendSpec,
)
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


async def _create_org_and_project(timezone: str = "UTC") -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with session_scope() as session:
        user = User(
            email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="not-a-real-hash",
            name="Owner",
        )
        session.add(user)
        await session.commit()

    org = await orgs_service.create_organization(
        "Alert Eval Org", f"alert-eval-org-{uuid.uuid4().hex[:8]}", user.id
    )
    project = await projects_service.create_project(org.id, "Web", "web", timezone, user.id)
    return org.id, project.id, user.id


def _day_range(day: datetime, days_back: int = 1) -> DateRange:
    return DateRange.model_validate(
        {
            "from": (day - timedelta(days=days_back)).date().isoformat(),
            "to": day.date().isoformat(),
        }
    )


async def test_threshold_trend_fires_once_per_breach_episode_then_recovers() -> None:
    org_id, project_id, user_id = await _create_org_and_project()
    today = datetime.now(UTC)
    ch_client = await get_clickhouse_client()

    await insert_events(
        ch_client,
        [
            generate_fake_event(
                org_id, project_id, event_name="checkout completed", timestamp=today
            )
            for _ in range(2)
        ],
    )

    spec = TrendSpec(events=["checkout completed"], measure="count", range=_day_range(today))
    insight = await create_insight(org_id, project_id, "Checkouts", spec, user_id)
    alert = await alerts_service.create_alert(
        org_id,
        project_id,
        insight.id,
        "Low checkouts",
        ThresholdRule(comparator="lt", value=5),
        AlertChannels(in_app=True),
        user_id,
    )

    outcome = await evaluate_alert(alert, insight)
    assert outcome.fired is True
    assert outcome.value == 2

    events = await alerts_service.list_events(org_id, project_id)
    assert len(events) == 1
    assert events[0].delivered == {"in_app": "recorded"}

    # Still breaching (2 < 5) on a second evaluation -- must not fire again.
    still_breaching_alert = await alerts_service.get_alert(org_id, project_id, alert.id)
    assert still_breaching_alert is not None
    outcome_again = await evaluate_alert(still_breaching_alert, insight)
    assert outcome_again.fired is False
    assert outcome_again.recovered is False
    assert len(await alerts_service.list_events(org_id, project_id)) == 1

    # Push the count above the threshold -> recovers, no new AlertEvent.
    await insert_events(
        ch_client,
        [
            generate_fake_event(
                org_id, project_id, event_name="checkout completed", timestamp=today
            )
            for _ in range(5)
        ],
    )
    breaching_alert = await alerts_service.get_alert(org_id, project_id, alert.id)
    assert breaching_alert is not None
    outcome_recovered = await evaluate_alert(breaching_alert, insight)
    assert outcome_recovered.fired is False
    assert outcome_recovered.recovered is True
    assert len(await alerts_service.list_events(org_id, project_id)) == 1

    final_alert = await alerts_service.get_alert(org_id, project_id, alert.id)
    assert final_alert is not None
    assert final_alert.is_breaching is False


async def test_threshold_trend_does_not_fire_when_not_breached() -> None:
    org_id, project_id, user_id = await _create_org_and_project()
    today = datetime.now(UTC)
    ch_client = await get_clickhouse_client()

    await insert_events(
        ch_client,
        [
            generate_fake_event(
                org_id, project_id, event_name="checkout completed", timestamp=today
            )
            for _ in range(20)
        ],
    )

    spec = TrendSpec(events=["checkout completed"], measure="count", range=_day_range(today))
    insight = await create_insight(org_id, project_id, "Checkouts", spec, user_id)
    alert = await alerts_service.create_alert(
        org_id,
        project_id,
        insight.id,
        "Too few checkouts",
        ThresholdRule(comparator="lt", value=5),
        AlertChannels(in_app=True),
        user_id,
    )

    outcome = await evaluate_alert(alert, insight)
    assert outcome.fired is False
    assert await alerts_service.list_events(org_id, project_id) == []


async def test_threshold_funnel_uses_the_final_steps_conversion_pct() -> None:
    org_id, project_id, user_id = await _create_org_and_project()
    today = datetime.now(UTC)
    ch_client = await get_clickhouse_client()

    users = [f"user_{i}" for i in range(10)]
    events = [
        generate_fake_event(org_id, project_id, event_name="a", user_id=u, timestamp=today)
        for u in users
    ]
    events += [
        generate_fake_event(
            org_id,
            project_id,
            event_name="b",
            user_id=u,
            timestamp=today + timedelta(minutes=1),
        )
        for u in users[:3]
    ]
    await insert_events(ch_client, events)

    spec = FunnelSpec(
        steps=[FunnelStep(event="a"), FunnelStep(event="b")],
        window=FunnelWindow(value=1, unit="day"),
        range=_day_range(today),
    )
    insight = await create_insight(org_id, project_id, "A to B", spec, user_id)
    alert = await alerts_service.create_alert(
        org_id,
        project_id,
        insight.id,
        "Low conversion",
        ThresholdRule(comparator="lt", value=50.0),
        AlertChannels(in_app=True),
        user_id,
    )

    outcome = await evaluate_alert(alert, insight)
    assert outcome.fired is True
    assert outcome.value == pytest.approx(30.0)  # 3 of 10 -> 30%


async def test_threshold_retention_uses_the_latest_cohorts_period_1_value() -> None:
    org_id, project_id, user_id = await _create_org_and_project()
    now = datetime.now(UTC)
    born_day = now - timedelta(days=2)
    ch_client = await get_clickhouse_client()

    users = [f"user_{i}" for i in range(5)]
    events = [
        generate_fake_event(
            org_id, project_id, event_name="signed up", user_id=u, timestamp=born_day
        )
        for u in users
    ]
    events += [
        generate_fake_event(
            org_id,
            project_id,
            event_name="signed up",
            user_id=u,
            timestamp=born_day + timedelta(days=1),
        )
        for u in users[:2]
    ]
    await insert_events(ch_client, events)

    spec = RetentionSpec(
        born_event="signed up",
        return_event="signed up",
        period="day",
        periods=3,
        range=_day_range(now, days_back=5),
    )
    insight = await create_insight(org_id, project_id, "Signup retention", spec, user_id)
    alert = await alerts_service.create_alert(
        org_id,
        project_id,
        insight.id,
        "Low D1 retention",
        ThresholdRule(comparator="lt", value=50.0),
        AlertChannels(in_app=True),
        user_id,
    )

    outcome = await evaluate_alert(alert, insight)
    assert outcome.fired is True
    assert outcome.value == pytest.approx(40.0)  # 2 of 5 returned -> 40%


def _daily_baseline_events(
    org_id: uuid.UUID, project_id: uuid.UUID, anchor: datetime, days: int
) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for days_ago in range(days, 0, -1):
        day = anchor - timedelta(days=days_ago)
        count = 11 if days_ago % 2 else 10  # a tiny, realistic wobble around 10
        events += [
            generate_fake_event(org_id, project_id, event_name="checkout completed", timestamp=day)
            for _ in range(count)
        ]
    return events


async def test_anomaly_zscore_fires_on_a_real_spike() -> None:
    org_id, project_id, user_id = await _create_org_and_project()
    today = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    ch_client = await get_clickhouse_client()

    events = _daily_baseline_events(org_id, project_id, today, days=10)
    events += [
        generate_fake_event(org_id, project_id, event_name="checkout completed", timestamp=today)
        for _ in range(100)
    ]
    await insert_events(ch_client, events)

    spec = TrendSpec(events=["checkout completed"], measure="count", range=_day_range(today))
    insight = await create_insight(org_id, project_id, "Checkouts", spec, user_id)
    alert = await alerts_service.create_alert(
        org_id,
        project_id,
        insight.id,
        "Checkout spike",
        AnomalyRule(method="zscore", window=10, sensitivity=3.0),
        AlertChannels(in_app=True),
        user_id,
    )

    outcome = await evaluate_alert(alert, insight)
    assert outcome.fired is True


async def test_anomaly_zscore_does_not_fire_on_a_normal_day() -> None:
    org_id, project_id, user_id = await _create_org_and_project()
    today = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    ch_client = await get_clickhouse_client()

    events = _daily_baseline_events(org_id, project_id, today, days=10)
    events += [
        generate_fake_event(org_id, project_id, event_name="checkout completed", timestamp=today)
        for _ in range(10)  # right at baseline -- not an anomaly
    ]
    await insert_events(ch_client, events)

    spec = TrendSpec(events=["checkout completed"], measure="count", range=_day_range(today))
    insight = await create_insight(org_id, project_id, "Checkouts", spec, user_id)
    alert = await alerts_service.create_alert(
        org_id,
        project_id,
        insight.id,
        "Checkout spike",
        AnomalyRule(method="zscore", window=10, sensitivity=3.0),
        AlertChannels(in_app=True),
        user_id,
    )

    outcome = await evaluate_alert(alert, insight)
    assert outcome.fired is False
    assert await alerts_service.list_events(org_id, project_id) == []


async def test_evaluate_all_enabled_never_leaks_one_orgs_data_into_anothers() -> None:
    org_a, project_a, user_a = await _create_org_and_project()
    org_b, project_b, user_b = await _create_org_and_project()
    today = datetime.now(UTC)
    ch_client = await get_clickhouse_client()

    await insert_events(
        ch_client,
        [
            generate_fake_event(org_a, project_a, event_name="checkout completed", timestamp=today)
            for _ in range(20)
        ]
        + [
            generate_fake_event(org_b, project_b, event_name="checkout completed", timestamp=today)
            for _ in range(1)
        ],
    )

    spec_a = TrendSpec(events=["checkout completed"], measure="count", range=_day_range(today))
    insight_a = await create_insight(org_a, project_a, "Checkouts A", spec_a, user_a)
    alert_a = await alerts_service.create_alert(
        org_a,
        project_a,
        insight_a.id,
        "Low checkouts A",
        ThresholdRule(comparator="lt", value=5),
        AlertChannels(in_app=True),
        user_a,
    )

    spec_b = TrendSpec(events=["checkout completed"], measure="count", range=_day_range(today))
    insight_b = await create_insight(org_b, project_b, "Checkouts B", spec_b, user_b)
    alert_b = await alerts_service.create_alert(
        org_b,
        project_b,
        insight_b.id,
        "Low checkouts B",
        ThresholdRule(comparator="lt", value=5),
        AlertChannels(in_app=True),
        user_b,
    )

    await evaluate_all_enabled()

    events_a = await alerts_service.list_events(org_a, project_a, alert_a.id)
    events_b = await alerts_service.list_events(org_b, project_b, alert_b.id)
    # Org A has 20 events (not breached, "lt 5"); org B has 1 (breached).
    assert events_a == []
    assert len(events_b) == 1
    assert events_b[0].value == 1
