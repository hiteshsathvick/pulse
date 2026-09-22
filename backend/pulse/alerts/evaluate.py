"""Phase 19: alert evaluation (SPEC.md #6.16). Loads an Alert + its Insight,
computes a fresh current value through the unchanged query engine (never a
second query path -- tenant scoping, caps, and the rate limiter are all
inherited, not reimplemented), applies the rule, and fires at most once per
breach episode."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter
from sqlalchemy import select

from pulse.alerts import delivery
from pulse.alerts.anomaly import seasonal_zscore_anomaly, zscore_anomaly
from pulse.alerts.rules import (
    AlertChannels,
    AnomalyMethod,
    AnomalyRule,
    DiscriminatedAlertRule,
    ThresholdRule,
)
from pulse.models import Alert, AlertEvent, Insight, Organization
from pulse.query import service as query_service
from pulse.query.spec import (
    DateRange,
    DiscriminatedInsightSpec,
    FunnelSpec,
    RetentionSpec,
    TrendSpec,
)
from pulse.repositories.postgres import session_scope
from pulse.services import projects as projects_service

_spec_adapter: TypeAdapter[Any] = TypeAdapter(DiscriminatedInsightSpec)
_rule_adapter: TypeAdapter[Any] = TypeAdapter(DiscriminatedAlertRule)


class InsufficientData(Exception):
    """Not enough history yet to judge -- never treated as a breach, and
    never noise either: the caller simply skips this evaluation."""


@dataclass
class EvaluationOutcome:
    fired: bool
    recovered: bool
    value: float | None


def _shifted_range(range_: DateRange, today: date) -> DateRange:
    """Preserves the saved spec's lookback duration but slides it to end
    today. An insight's own saved range is a snapshot from whenever it was
    built ("2026-01-01 to 2026-01-31"); reusing it verbatim for ongoing
    evaluation would go stale the moment "today" moves past `to`."""
    duration = range_.to - range_.from_
    # DateRange's alias for its `from_` field is the reserved word "from",
    # so it can't be passed as a literal keyword argument -- model_validate
    # against a plain dict (the same validated-JSON-in pattern used
    # everywhere else a spec is built from parts) sidesteps that cleanly.
    return DateRange.model_validate({"from": today - duration, "to": today, "tz": range_.tz})


async def _current_trend_value(
    spec: TrendSpec, org_id: uuid.UUID, project_id: uuid.UUID, today: date
) -> float:
    evaluated = spec.model_copy(update={"range": _shifted_range(spec.range, today)})
    result = await query_service.run_trend(evaluated, org_id, project_id, refresh=True)
    if not result.results:
        raise InsufficientData("no data in the evaluation window")
    return float(result.results[-1]["value"])  # type: ignore[arg-type]


async def _trend_series(
    spec: TrendSpec, org_id: uuid.UUID, project_id: uuid.UUID, today: date, window: int
) -> list[tuple[date, float]]:
    # window + a few extra days of buffer, so a short gap in the data
    # (a quiet weekend, a missed day) doesn't starve the baseline of exactly
    # the number of points the rule asked for.
    evaluated = spec.model_copy(
        update={
            "range": DateRange.model_validate(
                {"from": today - timedelta(days=window + 3), "to": today, "tz": spec.range.tz}
            )
        }
    )
    result = await query_service.run_trend(evaluated, org_id, project_id, refresh=True)
    series: list[tuple[date, float]] = []
    for row in result.results:
        bucket = datetime.fromisoformat(str(row["bucket"])).date()
        series.append((bucket, float(row["value"])))  # type: ignore[arg-type]
    return series


async def _current_funnel_value(
    spec: FunnelSpec, org_id: uuid.UUID, project_id: uuid.UUID, today: date
) -> float:
    evaluated = spec.model_copy(update={"range": _shifted_range(spec.range, today)})
    result = await query_service.run_funnel(evaluated, org_id, project_id, refresh=True)
    if not result.results:
        raise InsufficientData("no funnel data in the evaluation window")
    return float(result.results[-1]["conversion_pct"])  # type: ignore[arg-type]


async def _current_retention_value(
    spec: RetentionSpec, org_id: uuid.UUID, project_id: uuid.UUID, today: date
) -> float:
    """The classic single-number retention KPI: the most recent cohort's
    period-1 retention (a cohort grid otherwise has no one "the" value --
    picking period 1 specifically, rather than the furthest offset
    available, keeps the metric comparable cohort to cohort instead of
    conflating different measurement horizons as data accumulates)."""
    evaluated = spec.model_copy(update={"range": _shifted_range(spec.range, today)})
    result = await query_service.run_retention(evaluated, org_id, project_id, refresh=True)
    candidates = [row for row in result.results if int(row["period_offset"]) == 1]  # type: ignore[call-overload]
    if not candidates:
        raise InsufficientData("no period-1 retention cohort in the evaluation window")
    latest = max(candidates, key=lambda row: str(row["cohort_period"]))
    return float(latest["retention_pct"])  # type: ignore[arg-type]


async def evaluate_alert(alert: Alert, insight: Insight) -> EvaluationOutcome:
    project = await projects_service.get_project(alert.org_id, alert.project_id)
    if project is None:
        raise query_service.ProjectNotFound()

    today = datetime.now(ZoneInfo(project.timezone)).date()
    spec = _spec_adapter.validate_python(insight.spec)
    rule = _rule_adapter.validate_python(alert.rule)

    if isinstance(rule, ThresholdRule):
        if isinstance(spec, TrendSpec):
            value = await _current_trend_value(spec, alert.org_id, alert.project_id, today)
        elif isinstance(spec, FunnelSpec):
            value = await _current_funnel_value(spec, alert.org_id, alert.project_id, today)
        else:
            value = await _current_retention_value(spec, alert.org_id, alert.project_id, today)
        breached = rule.breached(value)
        message = f'"{insight.name}" is {value:.2f} ({rule.comparator} threshold {rule.value:.2f})'
    else:
        assert isinstance(rule, AnomalyRule)
        # Enforced at alert-creation time (pulse.alerts.service): an anomaly
        # rule can only ever be attached to a trend insight.
        assert isinstance(spec, TrendSpec)
        series = await _trend_series(spec, alert.org_id, alert.project_id, today, rule.window)
        if not series:
            raise InsufficientData("no data in the evaluation window")
        current_date, value = series[-1]
        history = series[:-1]
        if rule.method == AnomalyMethod.SEASONAL_ZSCORE:
            anomaly = seasonal_zscore_anomaly(
                history, current_date, value, sensitivity=rule.sensitivity
            )
        else:
            trailing = [v for _, v in history[-rule.window :]]
            anomaly = zscore_anomaly(trailing, value, sensitivity=rule.sensitivity)
        breached = anomaly.is_anomaly
        message = f'"{insight.name}" is {value:.2f}'
        if anomaly.z_score is not None:
            message += f" ({anomaly.z_score:+.2f}σ from baseline {anomaly.baseline_mean:.2f})"

    return await _apply_outcome(alert, breached, value, message)


async def _apply_outcome(
    alert: Alert, breached: bool, value: float, message: str
) -> EvaluationOutcome:
    fired = False
    recovered = False
    async with session_scope(org_id=alert.org_id) as session:
        row = await session.get(Alert, alert.id)
        assert row is not None
        row.last_evaluated_at = datetime.now(UTC)
        if breached and not row.is_breaching:
            row.is_breaching = True
            fired = True
        elif not breached and row.is_breaching:
            row.is_breaching = False
            recovered = True
        await session.commit()

    if fired:
        channels = AlertChannels.model_validate(alert.channels)
        delivered = await delivery.deliver(alert, message, channels)
        async with session_scope(org_id=alert.org_id) as session:
            session.add(
                AlertEvent(
                    org_id=alert.org_id,
                    alert_id=alert.id,
                    triggered_at=datetime.now(UTC),
                    value=value,
                    message=message,
                    delivered=delivered,
                )
            )
            await session.commit()

    return EvaluationOutcome(fired=fired, recovered=recovered, value=value)


async def evaluate_all_enabled() -> list[EvaluationOutcome]:
    """The alert-worker's (pulse/alerts/main.py) per-cycle entry point.
    There is no RLS-safe way to select across every org's alerts in one
    query -- an unscoped session_scope() default-denies every RLS-protected
    table (SPEC.md #4.1). Organization itself is *not* RLS-protected (it
    carries no org_id; it IS the tenant), so it's the one table this can
    list unscoped, then loop scoped per org for everything else."""
    outcomes: list[EvaluationOutcome] = []
    async with session_scope() as session:
        org_ids = list(await session.scalars(select(Organization.id)))

    for org_id in org_ids:
        async with session_scope(org_id=org_id) as session:
            alerts = list(await session.scalars(select(Alert).where(Alert.enabled.is_(True))))
            insights_by_id: dict[uuid.UUID, Insight] = {}
            if alerts:
                insight_ids = {a.insight_id for a in alerts}
                insights = list(
                    await session.scalars(select(Insight).where(Insight.id.in_(insight_ids)))
                )
                insights_by_id = {i.id: i for i in insights}

        for alert in alerts:
            insight = insights_by_id.get(alert.insight_id)
            if insight is None:
                continue
            try:
                outcomes.append(await evaluate_alert(alert, insight))
            except (InsufficientData, query_service.ProjectNotFound):
                continue

    return outcomes
