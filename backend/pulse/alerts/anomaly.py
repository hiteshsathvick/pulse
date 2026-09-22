"""Pure statistical functions behind AnomalyRule (pulse/alerts/rules.py). No
I/O, no ClickHouse, no Postgres -- hand-testable against fixtures with known
expected answers, matching this codebase's funnel/retention testing culture
(SPEC.md #8). Deliberately just trailing-window z-score and a same-weekday
seasonal variant of it -- "robust statistical methods... before anything
fancier" (CLAUDE.md/PULSE_PROJECT_GUIDE.md), not a full seasonal
decomposition model."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class AnomalyResult:
    is_anomaly: bool
    # None when there wasn't enough history to compute one at all -- "not
    # enough signal to judge" is reported as no anomaly, never as one.
    z_score: float | None
    baseline_mean: float | None
    baseline_stddev: float | None


def zscore_anomaly(history: list[float], current: float, *, sensitivity: float) -> AnomalyResult:
    """`history` is the trailing window strictly BEFORE `current` -- the
    current point is never part of its own baseline, or every anomaly would
    partially explain itself away. Needs at least 2 points to compute a
    stddev; a perfectly flat baseline (stddev == 0) is judged by simple
    inequality instead of dividing by zero."""
    if len(history) < 2:
        return AnomalyResult(False, None, None, None)
    mean = statistics.mean(history)
    stddev = statistics.pstdev(history)
    if stddev == 0:
        return AnomalyResult(current != mean, None, mean, stddev)
    z = (current - mean) / stddev
    return AnomalyResult(abs(z) >= sensitivity, z, mean, stddev)


def seasonal_zscore_anomaly(
    history: list[tuple[date, float]],
    current_date: date,
    current: float,
    *,
    sensitivity: float,
) -> AnomalyResult:
    """Same z-score math, but the baseline is only prior values that fall on
    the *same weekday* as `current_date` -- product/e-commerce traffic is
    heavily day-of-week seasonal, so a naive trailing average flags every
    normal weekend dip as an anomaly. Restricting `history` to matching
    weekdays before averaging is the entire "seasonal baseline"
    (SPEC.md #2/#7's "moving avg + z-score / seasonal baseline")."""
    same_weekday = [value for day, value in history if day.weekday() == current_date.weekday()]
    return zscore_anomaly(same_weekday, current, sensitivity=sensitivity)
