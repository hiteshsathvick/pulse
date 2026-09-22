"""CRUD for Alert + AlertEvent (pulse/api/alerts.py). Mirrors
pulse/insights/service.py's shape exactly -- session_scope, an audit record
per mutation, a cross-project 404 via an explicit org_id/project_id check
rather than trusting RLS alone (belt-and-braces, not the actual boundary)."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from pulse.alerts.rules import AlertChannels, AlertRule, AnomalyRule
from pulse.models import Alert, AlertEvent, Insight, InsightKind
from pulse.repositories.postgres import session_scope
from pulse.services import audit


class InsightNotFound(Exception):
    pass


class AnomalyRequiresTrend(Exception):
    """An anomaly rule needs a bucketed time series -- only a trend insight
    already produces one (SPEC.md #6.16). Funnel/retention insights are
    snapshot-shaped; they can only carry a threshold rule."""


def _serialize_rule(rule: AlertRule) -> dict[str, object]:
    return rule.model_dump(mode="json")


def _serialize_channels(channels: AlertChannels) -> dict[str, object]:
    return channels.model_dump(mode="json")


def _check_rule_against_insight(rule: AlertRule, insight: Insight) -> None:
    if isinstance(rule, AnomalyRule) and insight.kind != InsightKind.TREND:
        raise AnomalyRequiresTrend()


async def create_alert(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    insight_id: uuid.UUID,
    name: str,
    rule: AlertRule,
    channels: AlertChannels,
    actor_id: uuid.UUID,
) -> Alert:
    async with session_scope(org_id=org_id) as session:
        insight = await session.get(Insight, insight_id)
        if insight is None or insight.org_id != org_id or insight.project_id != project_id:
            raise InsightNotFound()
        _check_rule_against_insight(rule, insight)

        alert = Alert(
            org_id=org_id,
            project_id=project_id,
            insight_id=insight_id,
            name=name,
            rule=_serialize_rule(rule),
            channels=_serialize_channels(channels),
            enabled=True,
            created_by=actor_id,
        )
        session.add(alert)
        await session.flush()
        audit.record(
            session,
            org_id=org_id,
            actor_id=actor_id,
            action="alert.created",
            target=str(alert.id),
            metadata={"rule_kind": rule.kind},
        )
        await session.refresh(alert)
        await session.commit()
        return alert


async def list_alerts(org_id: uuid.UUID, project_id: uuid.UUID) -> list[Alert]:
    async with session_scope(org_id=org_id) as session:
        result = await session.scalars(
            select(Alert)
            .where(Alert.project_id == project_id)
            .order_by(Alert.created_at.desc(), Alert.id)
        )
        return list(result)


async def get_alert(org_id: uuid.UUID, project_id: uuid.UUID, alert_id: uuid.UUID) -> Alert | None:
    async with session_scope(org_id=org_id) as session:
        alert = await session.get(Alert, alert_id)
        if alert is None or alert.org_id != org_id or alert.project_id != project_id:
            return None
        return alert


async def update_alert(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    alert_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    name: str | None = None,
    rule: AlertRule | None = None,
    channels: AlertChannels | None = None,
    enabled: bool | None = None,
) -> Alert | None:
    async with session_scope(org_id=org_id) as session:
        alert = await session.get(Alert, alert_id)
        if alert is None or alert.org_id != org_id or alert.project_id != project_id:
            return None

        if rule is not None:
            insight = await session.get(Insight, alert.insight_id)
            if insight is not None:
                _check_rule_against_insight(rule, insight)

        if name is not None:
            alert.name = name
        if rule is not None:
            alert.rule = _serialize_rule(rule)
            # A changed rule starts a fresh breach episode rather than
            # inheriting whatever the old rule's is_breaching state was.
            alert.is_breaching = False
        if channels is not None:
            alert.channels = _serialize_channels(channels)
        if enabled is not None:
            alert.enabled = enabled

        audit.record(
            session,
            org_id=org_id,
            actor_id=actor_id,
            action="alert.updated",
            target=str(alert_id),
        )
        await session.flush()
        await session.refresh(alert)
        await session.commit()
        return alert


async def delete_alert(
    org_id: uuid.UUID, project_id: uuid.UUID, alert_id: uuid.UUID, actor_id: uuid.UUID
) -> bool:
    async with session_scope(org_id=org_id) as session:
        alert = await session.get(Alert, alert_id)
        if alert is None or alert.org_id != org_id or alert.project_id != project_id:
            return False
        await session.delete(alert)
        audit.record(
            session,
            org_id=org_id,
            actor_id=actor_id,
            action="alert.deleted",
            target=str(alert_id),
        )
        await session.commit()
        return True


async def list_events(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    alert_id: uuid.UUID | None = None,
    *,
    unacknowledged_only: bool = False,
) -> list[AlertEvent]:
    async with session_scope(org_id=org_id) as session:
        query = (
            select(AlertEvent)
            .join(Alert, AlertEvent.alert_id == Alert.id)
            .where(Alert.project_id == project_id)
        )
        if alert_id is not None:
            query = query.where(AlertEvent.alert_id == alert_id)
        if unacknowledged_only:
            query = query.where(AlertEvent.acknowledged_at.is_(None))
        # A bounded recent-activity feed, not a paginated resource -- the
        # same "cap it" discipline as everything else in this app rather
        # than a full pagination system this phase's DoD doesn't ask for.
        query = query.order_by(AlertEvent.triggered_at.desc()).limit(100)
        result = await session.scalars(query)
        return list(result)


async def acknowledge_event(
    org_id: uuid.UUID, project_id: uuid.UUID, event_id: uuid.UUID
) -> AlertEvent | None:
    async with session_scope(org_id=org_id) as session:
        event = await session.get(AlertEvent, event_id)
        if event is None or event.org_id != org_id:
            return None
        alert = await session.get(Alert, event.alert_id)
        if alert is None or alert.project_id != project_id:
            return None
        event.acknowledged_at = datetime.now(UTC)
        await session.flush()
        await session.refresh(event)
        await session.commit()
        return event
