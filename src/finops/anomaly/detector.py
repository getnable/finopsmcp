from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import and_, select

from ..storage.db import anomalies, get_engine
from ..storage.snapshots import get_history

_MIN_HISTORY_DAYS = 7      # need at least 7 data points
_MIN_SPEND_THRESHOLD = 5.0  # ignore noise below $5
_Z_SCORE_THRESHOLD = 2.0   # flag if |z| > 2.0
_PCT_THRESHOLD = 20.0       # AND |pct_change| > 20%


@dataclass
class AnomalyResult:
    provider: str
    service: str
    account_id: str
    snapshot_date: date
    severity: str           # "high" | "medium" | "low"
    direction: str          # "spike" | "drop"
    pct_change: float
    z_score: float
    baseline_mean: float
    current_amount: float
    is_new: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        arrow = "↑" if self.direction == "spike" else "↓"
        return (
            f"{self.provider.upper()} / {self.service}: "
            f"{arrow} {abs(self.pct_change):.0f}% vs 28-day baseline "
            f"(${self.current_amount:,.2f} vs avg ${self.baseline_mean:,.2f}) "
            f"[{self.severity.upper()}]"
        )


def _severity(z: float, pct: float) -> str:
    az, ap = abs(z), abs(pct)
    if az >= 3.5 or ap >= 100:
        return "high"
    if az >= 2.5 or ap >= 50:
        return "medium"
    return "low"


def detect_for_series(
    provider: str,
    service: str,
    account_id: str,
    snapshot_date: date,
    current_amount: float,
    history_amounts: list[float],
) -> AnomalyResult | None:
    if len(history_amounts) < _MIN_HISTORY_DAYS:
        return None
    if current_amount < _MIN_SPEND_THRESHOLD and max(history_amounts, default=0) < _MIN_SPEND_THRESHOLD:
        return None

    mean = statistics.mean(history_amounts)
    if mean < _MIN_SPEND_THRESHOLD:
        return None

    stdev = statistics.stdev(history_amounts) if len(history_amounts) > 1 else 0.0
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
    )


def detect_from_snapshot(
    provider: str,
    service: str,
    account_id: str,
    snapshot_date: date,
    current_amount: float,
    lookback_days: int = 28,
) -> AnomalyResult | None:
    history = get_history(provider, service, account_id, days=lookback_days)
    today_iso = snapshot_date.isoformat()
    amounts = [
        row["amount_usd"]
        for row in history
        if row["snapshot_date"] != today_iso and row["amount_usd"] > 0
    ]
    return detect_for_series(provider, service, account_id, snapshot_date, current_amount, amounts)


def persist_anomaly(result: AnomalyResult) -> int:
    engine = get_engine()
    with engine.begin() as conn:
        r = conn.execute(
            anomalies.insert().values(
                provider=result.provider,
                service=result.service,
                account_id=result.account_id,
                detected_at=datetime.now(timezone.utc),
                snapshot_date=result.snapshot_date.isoformat(),
                severity=result.severity,
                direction=result.direction,
                pct_change=result.pct_change,
                z_score=result.z_score,
                baseline_mean=result.baseline_mean,
                current_amount=result.current_amount,
                acknowledged=False,
                notified=False,
            )
        )
        return r.lastrowid  # type: ignore[return-value]


def get_active_anomalies(
    provider: str | None = None,
    severity: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    engine = get_engine()
    query = (
        select(anomalies)
        .where(anomalies.c.acknowledged == False)  # noqa: E712
        .order_by(anomalies.c.detected_at.desc())
        .limit(limit)
    )
    if provider:
        query = query.where(anomalies.c.provider == provider)
    if severity:
        query = query.where(anomalies.c.severity == severity)
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(query).fetchall()]


def acknowledge_anomaly(anomaly_id: int) -> bool:
    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(
            anomalies.update()
            .where(anomalies.c.id == anomaly_id)
            .values(acknowledged=True)
        )
        return result.rowcount > 0


def mark_notified(anomaly_id: int) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            anomalies.update()
            .where(anomalies.c.id == anomaly_id)
            .values(notified=True)
        )
