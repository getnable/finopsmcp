"""Anomaly detector blind spots: shapes of spend change the detector never flagged.

Each test is a series a customer would call an obvious anomaly. The detector
used to return None for all of them, so get_anomalies reported an all-clear on
the exact day the bill went sideways.
"""
from __future__ import annotations

from datetime import date, timedelta

import finops.anomaly.seasonality as seasonality
from finops.anomaly.detector import detect_for_series
from finops.anomaly.seasonality import detect_with_seasonality

TODAY = date(2026, 9, 23)


def _rows(amounts_by_days_ago: dict[int, float]) -> list[dict]:
    return [
        {"snapshot_date": (TODAY - timedelta(days=n)).isoformat(), "amount_usd": amt}
        for n, amt in sorted(amounts_by_days_ago.items(), reverse=True)
    ]


# ── a perfectly flat baseline has stdev 0 ────────────────────────────────────


def test_flat_baseline_then_huge_spike_is_flagged_by_rolling_mean():
    """28 days at exactly $100, then $10,000. stdev is 0, which used to force
    z=0 and fail the z gate no matter how large the jump."""
    out = detect_for_series("aws", "EC2", "123", TODAY, 10_000.0, [100.0] * 28)
    assert out is not None
    assert out.direction == "spike"
    assert out.severity == "high"


def test_flat_baseline_then_huge_spike_is_flagged_by_same_weekday(monkeypatch):
    history = _rows({n: 100.0 for n in range(1, 57)})
    monkeypatch.setattr(seasonality, "get_history", lambda *a, **kw: history)
    out = detect_with_seasonality("aws", "EC2", "123", TODAY, 10_000.0)
    assert out is not None
    assert out.direction == "spike"
    assert out.metadata["detection_method"].startswith("seasonality-aware")


def test_flat_baseline_small_wobble_is_still_quiet():
    """The floor must not turn a flat series into a hair trigger."""
    assert detect_for_series("aws", "EC2", "123", TODAY, 110.0, [100.0] * 28) is None


# ── spend falling to (nearly) nothing ────────────────────────────────────────


def test_drop_from_4000_a_day_to_zero_is_flagged(monkeypatch):
    """The small-spend noise floor belongs on the baseline side. Applied to
    today's amount it hid the drop that matters most: a pipeline or a backup
    job that stopped running."""
    history = _rows({n: 4_000.0 for n in range(1, 57)})
    monkeypatch.setattr(seasonality, "get_history", lambda *a, **kw: history)
    out = detect_with_seasonality("aws", "EC2", "123", TODAY, 0.0)
    assert out is not None
    assert out.direction == "drop"
    assert out.severity == "high"


def test_tiny_baseline_is_still_ignored(monkeypatch):
    """A service that never cost more than a few dollars stays below the noise floor."""
    history = _rows({n: 2.0 for n in range(1, 57)})
    monkeypatch.setattr(seasonality, "get_history", lambda *a, **kw: history)
    assert detect_with_seasonality("aws", "S3", "123", TODAY, 0.0) is None
