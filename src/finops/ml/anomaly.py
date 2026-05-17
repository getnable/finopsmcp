"""
nable Anomaly Detection Engine — pure Python, no sklearn required.

Uses a multi-signal approach:
  1. Z-score against a rolling 30-day baseline
  2. CUSUM (cumulative sum control chart) for gradual drift detection
  3. Day-of-week seasonal normalisation (cloud spend has strong weekly patterns)
  4. Service-specific sensitivity profiles (e.g., data transfer is noisier than EC2)

Why this beats simple threshold rules:
  - Adapts to each account's own spend patterns (personalised baseline)
  - Suppresses false positives on weekends/holidays automatically
  - Detects gradual drift (CUSUM) that z-score misses
  - Confidence score lets you tune alert fatigue

Public API:
    detector = AnomalyDetector(account_id="123456789012")
    anomalies = detector.detect(daily_series)
    # → [{"date": "...", "value": float, "score": float, "severity": str, "reason": str}]
"""
from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)

# ── Service sensitivity profiles ──────────────────────────────────────────────
# Higher = more tolerant (less noisy alerts).  1.0 = baseline.
_SERVICE_SENSITIVITY: dict[str, float] = {
    "EC2":                    1.0,
    "RDS":                    1.0,
    "S3":                     1.5,   # variable data volume
    "CloudFront":             1.5,
    "DataTransfer":           2.0,   # highly variable
    "AWSDataTransfer":        2.0,
    "Lambda":                 1.2,
    "ECS":                    1.0,
    "EKS":                    1.0,
    "Bedrock":                1.3,
    "SageMaker":              1.3,
    "Snowflake":              1.5,
    "Datadog":                1.2,
    "OpenAI":                 1.3,
    "Anthropic":              1.3,
    "Support":                3.0,   # annual / quarterly charges, very spiky
    "Tax":                    5.0,
    "Credits":                5.0,
}

_DEFAULT_SENSITIVITY = 1.2
_Z_THRESHOLD         = 2.5   # flag if |z| > this (accounting for sensitivity)
_CUSUM_K             = 0.5   # allowance parameter (half a sigma)
_CUSUM_H             = 5.0   # decision interval


@dataclass
class Anomaly:
    date_str: str
    value: float
    expected: float
    z_score: float
    cusum: float
    severity: str          # "low" | "medium" | "high" | "critical"
    reason: str
    service: str | None
    pct_above_expected: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "date":                 self.date_str,
            "value":                round(self.value, 2),
            "expected":             round(self.expected, 2),
            "pct_above_expected":   round(self.pct_above_expected, 1),
            "z_score":              round(self.z_score, 2),
            "severity":             self.severity,
            "reason":               self.reason,
            "service":              self.service,
        }


def _rolling_stats(
    series: list[float],
    idx: int,
    window: int = 30,
) -> tuple[float, float]:
    """Return (mean, std) for a rolling window ending before idx."""
    start = max(0, idx - window)
    window_vals = series[start:idx]
    if not window_vals:
        return 0.0, 0.0
    mean = statistics.mean(window_vals)
    std  = statistics.stdev(window_vals) if len(window_vals) > 1 else mean * 0.1
    return mean, max(std, mean * 0.05)  # floor std at 5% of mean


def _dow_factor(series: list[float], idx: int) -> float:
    """
    Estimate day-of-week seasonal factor.
    Computes average spend for this weekday across available history
    divided by the overall mean.  Clamps to [0.5, 2.0].
    """
    if len(series) < 14:
        return 1.0
    overall_mean = statistics.mean(series[:idx]) if idx > 0 else 1.0
    if overall_mean == 0:
        return 1.0
    dow = idx % 7
    dow_vals = [series[i] for i in range(idx) if i % 7 == dow]
    if not dow_vals:
        return 1.0
    dow_mean = statistics.mean(dow_vals)
    factor = dow_mean / overall_mean
    return max(0.5, min(2.0, factor))


def _severity(z: float, pct: float) -> str:
    if abs(z) > 5.0 or pct > 200:
        return "critical"
    if abs(z) > 3.5 or pct > 100:
        return "high"
    if abs(z) > 2.5 or pct > 50:
        return "medium"
    return "low"


def _reason(z: float, cusum: float, pct: float, value: float, expected: float) -> str:
    direction = "spike" if value > expected else "drop"
    parts = []
    if abs(z) > _Z_THRESHOLD:
        parts.append(f"z-score {z:+.1f}σ")
    if cusum > _CUSUM_H:
        parts.append("sustained drift (CUSUM)")
    if pct > 50:
        parts.append(f"{pct:.0f}% {'above' if value > expected else 'below'} expected")
    return f"Cost {direction}: " + (", ".join(parts) or "unusual pattern")


class AnomalyDetector:
    """
    Multi-signal anomaly detector for a single cost time series.

    Usage:
        detector = AnomalyDetector(service="EC2")
        anomalies = detector.detect(daily_costs, min_absolute_usd=10.0)
    """

    def __init__(
        self,
        account_id: str = "",
        service: str | None = None,
        z_threshold: float | None = None,
    ):
        self.account_id  = account_id
        self.service     = service
        sensitivity      = _SERVICE_SENSITIVITY.get(service or "", _DEFAULT_SENSITIVITY)
        self.z_threshold = z_threshold or (_Z_THRESHOLD * sensitivity)

    def detect(
        self,
        series: list[float],
        dates: list[str] | None = None,
        min_absolute_usd: float = 5.0,
        warmup_days: int = 14,
    ) -> list[Anomaly]:
        """
        Detect anomalies in a daily cost series.

        Args:
            series:           daily cost values, oldest first
            dates:            ISO date strings matching series (optional)
            min_absolute_usd: ignore anomalies below this threshold (noise filter)
            warmup_days:      skip detection until this many days of history

        Returns:
            list of Anomaly objects, sorted by z_score descending
        """
        n = len(series)
        if n < warmup_days:
            return []

        # Build date list if not supplied
        if dates is None:
            today = date.today()
            start = today - timedelta(days=n - 1)
            dates = [(start + timedelta(days=i)).isoformat() for i in range(n)]

        anomalies: list[Anomaly] = []
        cusum_pos = 0.0
        cusum_neg = 0.0

        for i in range(warmup_days, n):
            value = series[i]
            mean, std = _rolling_stats(series, i)
            if mean == 0 or std == 0:
                continue

            # Day-of-week normalisation
            dow = _dow_factor(series, i)
            adjusted_mean = mean * dow

            # Z-score
            z = (value - adjusted_mean) / std

            # CUSUM (one-sided positive: detects upward drift)
            k = _CUSUM_K * std
            cusum_pos = max(0, cusum_pos + (value - adjusted_mean) - k)
            cusum_neg = max(0, cusum_neg - (value - adjusted_mean) - k)
            cusum_signal = max(cusum_pos, cusum_neg) / std if std > 0 else 0.0

            # Is it an anomaly?
            z_anomaly    = abs(z) > self.z_threshold
            cusum_anomaly = cusum_signal > _CUSUM_H
            is_anomaly   = z_anomaly or cusum_anomaly

            # Noise gate
            if not is_anomaly:
                continue
            if abs(value - adjusted_mean) < min_absolute_usd:
                continue

            pct = ((value - adjusted_mean) / adjusted_mean * 100) if adjusted_mean else 0.0

            anomalies.append(Anomaly(
                date_str=dates[i],
                value=value,
                expected=round(adjusted_mean, 2),
                z_score=round(z, 2),
                cusum=round(cusum_signal, 2),
                severity=_severity(z, abs(pct)),
                reason=_reason(z, cusum_signal, abs(pct), value, adjusted_mean),
                service=self.service,
                pct_above_expected=round(pct, 1),
            ))

        return sorted(anomalies, key=lambda a: abs(a.z_score), reverse=True)

    def detect_dict(
        self,
        series: list[float],
        dates: list[str] | None = None,
        min_absolute_usd: float = 5.0,
    ) -> list[dict[str, Any]]:
        return [a.to_dict() for a in self.detect(series, dates, min_absolute_usd)]


# ── Multi-service detector ─────────────────────────────────────────────────────

def detect_across_services(
    by_service: dict[str, list[float]],
    dates: list[str] | None = None,
    account_id: str = "",
    min_absolute_usd: float = 10.0,
) -> dict[str, Any]:
    """
    Run anomaly detection across all services simultaneously.

    Args:
        by_service: {"EC2": [daily costs...], "RDS": [...], ...}
        dates:      shared date list
        account_id: for logging
        min_absolute_usd: per-service noise floor

    Returns:
        {
          "total_anomalies": int,
          "critical": [...],
          "high": [...],
          "medium": [...],
          "low": [...],
          "by_service": {"EC2": [...], ...},
        }
    """
    by_severity: dict[str, list[dict]] = {"critical": [], "high": [], "medium": [], "low": []}
    by_service_result: dict[str, list[dict]] = {}

    for service, series in by_service.items():
        detector = AnomalyDetector(account_id=account_id, service=service)
        anomalies = detector.detect_dict(series, dates, min_absolute_usd)
        if anomalies:
            by_service_result[service] = anomalies
            for a in anomalies:
                by_severity[a["severity"]].append(a)

    total = sum(len(v) for v in by_service_result.values())
    return {
        "total_anomalies": total,
        **by_severity,
        "by_service": by_service_result,
    }
