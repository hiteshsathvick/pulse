"""Pure-math tests for pulse.alerts.anomaly, hand-computed like this
codebase's funnel/retention fixtures (SPEC.md #8: "known hand-computed
expected answers")."""

from datetime import date

from pulse.alerts.anomaly import seasonal_zscore_anomaly, zscore_anomaly


def test_a_clear_spike_is_flagged() -> None:
    # mean=10, pstdev=0 (a perfectly flat baseline) -- any deviation counts.
    result = zscore_anomaly([10, 10, 10, 10], 50, sensitivity=3.0)
    assert result.is_anomaly is True
    assert result.baseline_mean == 10
    assert result.baseline_stddev == 0
    assert result.z_score is None  # can't divide by a zero stddev


def test_normal_fluctuation_within_a_few_stddev_is_not_flagged() -> None:
    # history: 8, 9, 10, 11, 12 -> mean=10, pstdev=sqrt(2)=1.4142...
    # current=12 -> z = 2/1.4142... = 1.4142..., well under sensitivity=3.
    result = zscore_anomaly([8, 9, 10, 11, 12], 12, sensitivity=3.0)
    assert result.is_anomaly is False
    assert result.z_score is not None
    assert round(result.z_score, 4) == round(2 / (2**0.5), 4)


def test_a_hand_computed_zscore_breach() -> None:
    # history: 10, 10, 10, 10, 10 -> mean=10, pstdev=0... use varied data
    # instead so pstdev is non-zero and the z-score is exactly checkable.
    # history: 8, 10, 12 -> mean=10, pstdev=sqrt(((8-10)^2+(10-10)^2+(12-10)^2)/3)
    #        = sqrt(8/3) = 1.63299...
    # current=20 -> z = 10 / 1.63299... = 6.1237...
    result = zscore_anomaly([8, 10, 12], 20, sensitivity=3.0)
    assert result.is_anomaly is True
    assert result.z_score is not None
    assert round(result.z_score, 4) == round(10 / ((8 / 3) ** 0.5), 4)


def test_fewer_than_two_history_points_is_never_an_anomaly() -> None:
    assert zscore_anomaly([], 999, sensitivity=1.0).is_anomaly is False
    assert zscore_anomaly([5], 999, sensitivity=1.0).is_anomaly is False


def test_the_current_point_is_never_part_of_its_own_baseline() -> None:
    """A caller that accidentally included `current` in `history` would see
    a smaller z-score than the true one -- this test exists to make sure
    pulse.alerts.evaluate never does that (see its own test suite), not to
    test this function's math again."""
    without_current = zscore_anomaly([8, 10, 12], 20, sensitivity=3.0)
    with_current_included = zscore_anomaly([8, 10, 12, 20], 20, sensitivity=3.0)
    assert without_current.z_score is not None
    assert with_current_included.z_score is not None
    assert abs(with_current_included.z_score) < abs(without_current.z_score)


# --- Seasonal baseline: the "seasonality handled" DoD test -----------------
#
# seasonal_zscore_anomaly only ever looks at history entries that share
# `current_date`'s weekday -- everything else in a passed-in history is
# filtered out internally, so each fixture below only needs to contain the
# dates that actually matter for that test.

_SATURDAY = date(2026, 1, 10)

# Three prior Saturdays: 38, 42, 40 -> mean=40, pstdev=sqrt(8/3)=1.63299...,
# plus a couple of weekday entries mixed in to prove they get ignored.
_SATURDAY_HISTORY: list[tuple[date, float]] = [
    (date(2025, 12, 20), 38.0),
    (date(2025, 12, 27), 42.0),
    (date(2026, 1, 3), 40.0),
    (date(2026, 1, 5), 100.0),  # Monday -- a different weekday, must be ignored
    (date(2026, 1, 6), 100.0),  # Tuesday -- likewise
]


def test_a_normal_weekend_dip_is_not_flagged_by_the_seasonal_baseline() -> None:
    # current=38 -> z = (38-40) / sqrt(8/3) = -1.2247..., well under sensitivity=3.
    result = seasonal_zscore_anomaly(_SATURDAY_HISTORY, _SATURDAY, 38.0, sensitivity=3.0)
    assert result.is_anomaly is False
    assert result.z_score is not None
    assert round(result.z_score, 4) == round(-2 / ((8 / 3) ** 0.5), 4)


def test_the_same_normal_weekend_value_IS_flagged_by_a_naive_trailing_window() -> None:
    """The realistic failure mode a naive trailing-window z-score has: going
    into a weekend, the last few raw days are weekday-heavy (only one
    weekend day -- last Sunday -- in a 6-day window), so the baseline is
    pulled toward weekday numbers and an ordinary weekend value looks like
    an outlier against it. history: 40 (Sun), 100x5 (Mon-Fri) -> mean=90,
    pstdev=sqrt(3000/6)=22.3607...; current=38 -> z=-2.3253..."""
    naive_trailing_window = [40.0, 100.0, 100.0, 100.0, 100.0, 100.0]
    naive_result = zscore_anomaly(naive_trailing_window, 38.0, sensitivity=2.0)
    assert naive_result.is_anomaly is True


def test_a_genuine_weekday_crash_is_still_flagged_by_the_seasonal_baseline() -> None:
    # A flat 100-per-Monday baseline (pstdev=0): a real Monday collapsing to
    # 5 is way outside it -- the seasonal method must still catch a real
    # anomaly, not just avoid false positives.
    monday_history = [
        (date(2025, 12, 15), 100.0),
        (date(2025, 12, 22), 100.0),
        (date(2025, 12, 29), 100.0),
    ]
    result = seasonal_zscore_anomaly(monday_history, date(2026, 1, 5), 5.0, sensitivity=3.0)
    assert result.is_anomaly is True
