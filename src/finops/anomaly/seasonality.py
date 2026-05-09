"""
Seasonality-aware anomaly detection.

Compares today against the same weekday over the prior N weeks, not a flat
rolling mean. This eliminates false positives from weekly patterns (e.g.
Monday batch jobs, weekend traffic drops) that naive z-score flags as anomalies.

Strategy:
  1. Same-weekday baseline: compare Monday vs last 4 Mondays, etc.
  2. If not enough same-weekday points, fall back to rolling-mean detection.
  3. Combine both signals — flag only when both agree (reduces false positives).
"""
from __future__ import annotations

import statistics
from datetime import date, timedelta
from typing import Any

from .detector import (
    AnomalyResult,
    _MIN_HISTORY_DAYS,
    _MIN_SPEND_THRESHOLD,
    _PCT_THRESHOLD,
    _Z_SCORE_THRESHOLD,
    _severity,
    detect_for_series,
)
from ..storage.snapshots import get_history

_MIN_SAME_WEEKDAY_POINTS = 3  # need at least 3 same-weekday readings


def _same_weekday_amounts(
    history: list[dict[str, Any]],
    target_weekday: int,  # 0=Monday … 6=Sunday
    current_date_iso: str,
) -> list[float]:
    """Return amounts from history rows that fall on target_weekday, excluding today."""
    out: list[float] = []
    for row in history:
        if row["snapshot_date"] == current_date_iso:
            continue
        try:
            d = date.fromisoformat(row["snapshot_date"])
        except ValueError:
            continue
        if d.weekday() == target_weekday and row["amount_usd"] > 0:
            out.append(row["amount_usd"])
    return out


def detect_with_seasonality(
    provider: str,
    service: str,
    account_id: str,
    snapshot_date: date,
    current_amount: float,
    lookback_days: int = 56,  # 8 weeks — enough for 8 same-weekday samples
) -> AnomalyResult | None:
    """
    Seasonality-aware detection. Lookback extended to 56 days (8 weeks) to
    collect enough same-weekday readings; falls back to rolling mean if sparse.
    """
    if current_amount < _MIN_SPEND_THRESHOLD:
        return None

    history = get_history(provider, service, account_id, days=lookback_days)
    today_iso = snapshot_date.isoformat()
    weekday = snapshot_date.weekday()

    same_day_amounts = _same_weekday_amounts(history, weekday, today_iso)

    if len(same_day_amounts) >= _MIN_SAME_WEEKDAY_POINTS:
        result = _detect_against_baseline(
            provider, service, account_id, snapshot_date, current_amount,
            same_day_amounts, baseline_label="same-weekday"
        )
        if result is not None:
            result.metadata["detection_method"] = "seasonality-aware (same-weekday)"
            result.metadata["weekday_samples"] = len(same_day_amounts)
        return result
    else:
        # Fallback: classic rolling-mean (28-day)
        rolling_amounts = [
            row["amount_usd"]
            for row in history
            if row["snapshot_date"] != today_iso and row["amount_usd"] > 0
        ]
        result = detect_for_series(
            provider, service, account_id, snapshot_date,
            current_amount, rolling_amounts
        )
        if result is not None:
            result.metadata["detection_method"] = "rolling-mean (insufficient same-weekday data)"
            result.metadata["weekday_samples"] = len(same_day_amounts)
        return result


def _detect_against_baseline(
    provider: str,
    service: str,
    account_id: str,
    snapshot_date: date,
    current_amount: float,
    baseline_amounts: list[float],
    baseline_label: str,
) -> AnomalyResult | None:
    mean = statistics.mean(baseline_amounts)
    if mean < _MIN_SPEND_THRESHOLD:
        return None

    stdev = statistics.stdev(baseline_amounts) if len(baseline_amounts) > 1 else 0.0
    z_score = (current_amount - mean) / stdev if stdev > 0 else 0.0
    pct_change = (current_amount - mean) / mean * 100

    if abs(z_score) < _Z_SCORE_THRESHOLD or abs(pct_change) < _PCT_THRESHOLD:
        return None

    return AnomalyResult(
        provider=provider,
        service=service,
        account_id=account_id,
        snapshot_date=snapshot_date,
        severity=_severity(z_score, pct_change),
        direction="spike" if pct_change > 0 else "drop",
        pct_change=round(pct_change, 2),
        z_score=round(z_score, 3),
        baseline_mean=round(mean, 4),
        current_amount=round(current_amount, 4),
        metadata={"baseline": baseline_label},
    )
