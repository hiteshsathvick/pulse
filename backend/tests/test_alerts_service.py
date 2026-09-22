import uuid
from pathlib import Path
from typing import Any

import pytest

from alembic import command
from alembic.config import Config
from pulse.alerts import service as alerts_service
from pulse.alerts.rules import AlertChannels, AnomalyRule, ThresholdRule
from pulse.insights.service import create_insight
from pulse.models import User
from pulse.query.spec import DateRange, FunnelSpec, FunnelStep, FunnelWindow, TrendSpec
from pulse.repositories.postgres import session_scope
from pulse.services import orgs as orgs_service
from pulse.services import projects as projects_service

_BACKEND_ROOT = Path(__file__).resolve().parent.parent


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


async def _create_org_project_and_user() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with session_scope() as session:
        user = User(
            email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="not-a-real-hash",
            name="Owner",
        )
        session.add(user)
        await session.commit()

    org = await orgs_service.create_organization(
        "Alerts Test Org", f"alerts-org-{uuid.uuid4().hex[:8]}", user.id
    )
    project = await projects_service.create_project(org.id, "Web", "web", "UTC", user.id)
    return org.id, project.id, user.id


_RANGE = DateRange.model_validate({"from": "2026-01-01", "to": "2026-01-31"})


def _trend_spec() -> TrendSpec:
    return TrendSpec(events=["checkout completed"], measure="count", range=_RANGE)


def _funnel_spec() -> FunnelSpec:
    return FunnelSpec(
        steps=[FunnelStep(event="a"), FunnelStep(event="b")],
        window=FunnelWindow(value=1, unit="day"),
        range=_RANGE,
    )


_THRESHOLD = ThresholdRule(comparator="gt", value=10)
_ANOMALY = AnomalyRule()
_CHANNELS = AlertChannels(in_app=True)


async def test_create_and_get_alert() -> None:
    org_id, project_id, user_id = await _create_org_project_and_user()
    insight = await create_insight(org_id, project_id, "Checkouts", _trend_spec(), user_id)

    alert = await alerts_service.create_alert(
        org_id, project_id, insight.id, "Too many checkouts", _THRESHOLD, _CHANNELS, user_id
    )
    assert alert.name == "Too many checkouts"
    assert alert.enabled is True
    assert alert.is_breaching is False
    assert alert.rule["kind"] == "threshold"

    fetched = await alerts_service.get_alert(org_id, project_id, alert.id)
    assert fetched is not None
    assert fetched.id == alert.id


async def test_create_alert_rejects_an_unknown_insight() -> None:
    org_id, project_id, user_id = await _create_org_project_and_user()
    with pytest.raises(alerts_service.InsightNotFound):
        await alerts_service.create_alert(
            org_id, project_id, uuid.uuid4(), "x", _THRESHOLD, _CHANNELS, user_id
        )


async def test_anomaly_rule_is_rejected_on_a_funnel_insight() -> None:
    org_id, project_id, user_id = await _create_org_project_and_user()
    insight = await create_insight(org_id, project_id, "Signup funnel", _funnel_spec(), user_id)
    with pytest.raises(alerts_service.AnomalyRequiresTrend):
        await alerts_service.create_alert(
            org_id, project_id, insight.id, "x", _ANOMALY, _CHANNELS, user_id
        )


async def test_anomaly_rule_is_accepted_on_a_trend_insight() -> None:
    org_id, project_id, user_id = await _create_org_project_and_user()
    insight = await create_insight(org_id, project_id, "Checkouts", _trend_spec(), user_id)
    alert = await alerts_service.create_alert(
        org_id, project_id, insight.id, "x", _ANOMALY, _CHANNELS, user_id
    )
    assert alert.rule["kind"] == "anomaly"


async def test_a_channel_set_needs_at_least_one_channel_enabled() -> None:
    with pytest.raises(ValueError, match="at least one delivery channel"):
        AlertChannels(in_app=False, email=[], webhook_url=None)


async def test_update_alert_can_change_rule_channels_and_enabled() -> None:
    org_id, project_id, user_id = await _create_org_project_and_user()
    insight = await create_insight(org_id, project_id, "Checkouts", _trend_spec(), user_id)
    alert = await alerts_service.create_alert(
        org_id, project_id, insight.id, "x", _THRESHOLD, _CHANNELS, user_id
    )

    updated = await alerts_service.update_alert(
        org_id,
        project_id,
        alert.id,
        user_id,
        rule=ThresholdRule(comparator="lt", value=1),
        enabled=False,
    )
    assert updated is not None
    assert updated.rule["comparator"] == "lt"
    assert updated.enabled is False


async def test_update_to_an_anomaly_rule_is_still_checked_against_the_insight_kind() -> None:
    org_id, project_id, user_id = await _create_org_project_and_user()
    insight = await create_insight(org_id, project_id, "Signup funnel", _funnel_spec(), user_id)
    alert = await alerts_service.create_alert(
        org_id, project_id, insight.id, "x", _THRESHOLD, _CHANNELS, user_id
    )
    with pytest.raises(alerts_service.AnomalyRequiresTrend):
        await alerts_service.update_alert(org_id, project_id, alert.id, user_id, rule=_ANOMALY)


async def test_changing_the_rule_resets_the_breach_episode() -> None:
    org_id, project_id, user_id = await _create_org_project_and_user()
    insight = await create_insight(org_id, project_id, "Checkouts", _trend_spec(), user_id)
    alert = await alerts_service.create_alert(
        org_id, project_id, insight.id, "x", _THRESHOLD, _CHANNELS, user_id
    )
    async with session_scope(org_id=org_id) as session:
        from pulse.models import Alert as AlertModel

        row = await session.get(AlertModel, alert.id)
        assert row is not None
        row.is_breaching = True
        await session.commit()

    updated = await alerts_service.update_alert(
        org_id, project_id, alert.id, user_id, rule=ThresholdRule(comparator="gt", value=999)
    )
    assert updated is not None
    assert updated.is_breaching is False


async def test_delete_alert() -> None:
    org_id, project_id, user_id = await _create_org_project_and_user()
    insight = await create_insight(org_id, project_id, "Checkouts", _trend_spec(), user_id)
    alert = await alerts_service.create_alert(
        org_id, project_id, insight.id, "x", _THRESHOLD, _CHANNELS, user_id
    )
    assert await alerts_service.delete_alert(org_id, project_id, alert.id, user_id) is True
    assert await alerts_service.get_alert(org_id, project_id, alert.id) is None
    assert await alerts_service.delete_alert(org_id, project_id, alert.id, user_id) is False


async def test_alerts_never_leak_across_tenants() -> None:
    """DoD-adjacent tenant-leakage test, matching the pattern every other
    module's own suite (query engine, dashboards, insights) already uses."""
    org_a, project_a, user_a = await _create_org_project_and_user()
    org_b, _project_b, _user_b = await _create_org_project_and_user()

    insight = await create_insight(org_a, project_a, "Checkouts", _trend_spec(), user_a)
    alert = await alerts_service.create_alert(
        org_a, project_a, insight.id, "x", _THRESHOLD, _CHANNELS, user_a
    )

    assert await alerts_service.get_alert(org_b, project_a, alert.id) is None
    assert await alerts_service.list_alerts(org_b, project_a) == []


async def test_list_and_acknowledge_events() -> None:
    org_id, project_id, user_id = await _create_org_project_and_user()
    insight = await create_insight(org_id, project_id, "Checkouts", _trend_spec(), user_id)
    alert = await alerts_service.create_alert(
        org_id, project_id, insight.id, "x", _THRESHOLD, _CHANNELS, user_id
    )

    from datetime import UTC, datetime

    from pulse.models import AlertEvent

    async with session_scope(org_id=org_id) as session:
        session.add(
            AlertEvent(
                org_id=org_id,
                alert_id=alert.id,
                triggered_at=datetime.now(UTC),
                value=42.0,
                message="breached",
                delivered={"in_app": "recorded"},
            )
        )
        await session.commit()

    events = await alerts_service.list_events(org_id, project_id)
    assert len(events) == 1
    assert events[0].acknowledged_at is None

    acked = await alerts_service.acknowledge_event(org_id, project_id, events[0].id)
    assert acked is not None
    assert acked.acknowledged_at is not None

    unacked = await alerts_service.list_events(org_id, project_id, unacknowledged_only=True)
    assert unacked == []
