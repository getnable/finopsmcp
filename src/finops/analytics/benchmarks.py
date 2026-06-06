"""
nable Cross-Account Benchmarking

Opt-in anonymised spend pool. Users who enable benchmarking contribute
aggregated, differential-private metrics to a shared pool. In return they
get "your RDS spend per GB is 2.4× the median for your peer group."

Privacy model:
  - Only aggregate ratios are submitted, never raw dollar amounts
  - Account IDs are hashed with a per-install salt (never reversible)
  - All submissions are differential-private (Laplace noise added before upload)
  - Users can opt out at any time: `finops benchmark --disable`
  - Data is retained for 90 days on the pool server, then deleted

Peer groups are matched by:
  - Industry vertical (self-reported or inferred from service mix)
  - Company size band (total monthly spend quartile)
  - Primary cloud (AWS / GCP / Azure / multi)
  - Primary compute type (EC2 / EKS / Lambda / mixed)

Metrics submitted (all as ratios, not raw $):
  - EC2 cost as % of total
  - RDS cost per compute $ (memory/compute ratio proxy)
  - Savings Plans coverage %
  - Idle resource % (from pattern scanner)
  - LLM spend as % of total
  - Number of anomalies per $10K spend (ops efficiency proxy)

Env vars:
  NABLE_BENCHMARKING_ENABLED  — "true" to opt in (default: false)
  NABLE_BENCHMARKING_ENDPOINT — pool server URL (default: https://bench.nable.dev)
  NABLE_BENCHMARKING_SALT     — random salt for account ID hashing (auto-generated)

Local-only mode: if NABLE_BENCHMARKING_ENABLED is not set, benchmarks()
returns static industry medians from a built-in table.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)

BENCH_ENDPOINT = os.environ.get("NABLE_BENCHMARKING_ENDPOINT", "https://bench.nable.dev")
ENABLED        = os.environ.get("NABLE_BENCHMARKING_ENABLED", "").lower() == "true"

# ── Built-in industry medians (static fallback) ───────────────────────────────
# Source: synthesised from public FinOps Foundation reports + AWS re:Invent data
# Refreshed quarterly in nable releases.

_STATIC_MEDIANS: dict[str, dict[str, float]] = {
    "saas": {
        "ec2_pct":              38.0,
        "rds_pct":              14.0,
        "s3_pct":               5.0,
        "data_transfer_pct":    8.0,
        "savings_plan_coverage": 62.0,
        "idle_resource_pct":    12.0,
        "llm_pct":              3.0,
        "gp3_migration_pct":    45.0,   # % of EBS that's already gp3
        "rightsizing_opp_pct":  18.0,   # % of compute that's oversized
    },
    "ecommerce": {
        "ec2_pct":              45.0,
        "rds_pct":              18.0,
        "s3_pct":               8.0,
        "data_transfer_pct":    12.0,
        "savings_plan_coverage": 55.0,
        "idle_resource_pct":    8.0,
        "llm_pct":              1.0,
        "gp3_migration_pct":    38.0,
        "rightsizing_opp_pct":  15.0,
    },
    "fintech": {
        "ec2_pct":              32.0,
        "rds_pct":              22.0,
        "s3_pct":               4.0,
        "data_transfer_pct":    6.0,
        "savings_plan_coverage": 70.0,
        "idle_resource_pct":    9.0,
        "llm_pct":              5.0,
        "gp3_migration_pct":    52.0,
        "rightsizing_opp_pct":  12.0,
    },
    "media": {
        "ec2_pct":              28.0,
        "rds_pct":              8.0,
        "s3_pct":               22.0,
        "data_transfer_pct":    18.0,
        "savings_plan_coverage": 48.0,
        "idle_resource_pct":    15.0,
        "llm_pct":              2.0,
        "gp3_migration_pct":    35.0,
        "rightsizing_opp_pct":  22.0,
    },
    "ai_ml": {
        "ec2_pct":              30.0,
        "rds_pct":              6.0,
        "s3_pct":               10.0,
        "data_transfer_pct":    5.0,
        "savings_plan_coverage": 40.0,
        "idle_resource_pct":    20.0,
        "llm_pct":              28.0,
        "gp3_migration_pct":    30.0,
        "rightsizing_opp_pct":  25.0,
    },
    "default": {
        "ec2_pct":              40.0,
        "rds_pct":              15.0,
        "s3_pct":               7.0,
        "data_transfer_pct":    9.0,
        "savings_plan_coverage": 58.0,
        "idle_resource_pct":    14.0,
        "llm_pct":              4.0,
        "gp3_migration_pct":    42.0,
        "rightsizing_opp_pct":  18.0,
    },
}

# Size bands by monthly spend
_SIZE_BANDS = [
    (0,    1_000,  "startup"),
    (1_000, 10_000, "smb"),
    (10_000, 100_000, "mid-market"),
    (100_000, float("inf"), "enterprise"),
]


def _size_band(monthly_spend: float) -> str:
    for lo, hi, label in _SIZE_BANDS:
        if lo <= monthly_spend < hi:
            return label
    return "enterprise"


# ── Differential privacy (Laplace mechanism) ──────────────────────────────────

def _add_noise(value: float, sensitivity: float = 1.0, epsilon: float = 2.0) -> float:
    """Add Laplace noise for differential privacy."""
    scale = sensitivity / epsilon
    # Box-Muller-style Laplace: u ~ Uniform(-0.5, 0.5), noise = -scale*sign(u)*ln(1-2|u|)
    import math
    u = random.uniform(-0.5 + 1e-10, 0.5 - 1e-10)
    noise = -scale * (1 if u >= 0 else -1) * math.log(1 - 2 * abs(u))
    return round(value + noise, 1)


def _hash_account(account_id: str) -> str:
    """One-way hash of account ID using per-install salt."""
    salt = _get_or_create_salt()
    return hashlib.sha256(f"{salt}:{account_id}".encode()).hexdigest()[:16]


def _get_or_create_salt() -> str:
    salt = os.environ.get("NABLE_BENCHMARKING_SALT", "")
    if salt:
        return salt
    # Persist to ~/.finops/bench_salt
    salt_path = os.path.expanduser("~/.finops/bench_salt")
    if os.path.exists(salt_path):
        with open(salt_path) as f:
            return f.read().strip()
    import secrets
    salt = secrets.token_hex(32)
    os.makedirs(os.path.dirname(salt_path), exist_ok=True)
    with open(salt_path, "w") as f:
        f.write(salt)
    return salt


# ── Metric extraction ──────────────────────────────────────────────────────────

def _extract_metrics(
    account_id: str,
    days: int = 30,
) -> dict[str, float]:
    """
    Extract anonymised ratio metrics from the local DB.
    Returns {} if no data available.
    """
    try:
        from ..storage.db import get_engine
        from sqlalchemy import text as sql_text

        engine = get_engine()
        start  = (date.today() - timedelta(days=days)).isoformat()

        with engine.connect() as conn:
            rows = conn.execute(sql_text("""
                SELECT service, SUM(amount_usd) as total
                FROM cost_snapshots
                WHERE account_id = :aid AND snapshot_date >= :start
                GROUP BY service
            """), {"aid": account_id, "start": start}).fetchall()

        if not rows:
            return {}

        by_service = {r[0]: float(r[1]) for r in rows}
        total = sum(by_service.values())
        if total == 0:
            return {}

        def _pct(keys: list[str]) -> float:
            return round(sum(by_service.get(k, 0) for k in keys) / total * 100, 1)

        ec2_pct = _pct(["Amazon EC2", "Amazon Elastic Compute Cloud - Compute"])
        rds_pct = _pct(["Amazon RDS", "Amazon Relational Database Service"])
        s3_pct  = _pct(["Amazon S3", "Amazon Simple Storage Service"])
        dt_pct  = _pct(["AWSDataTransfer", "Amazon CloudFront"])
        llm_pct = _pct(["Amazon Bedrock", "OpenAI", "Anthropic"])

        return {
            "ec2_pct":    ec2_pct,
            "rds_pct":    rds_pct,
            "s3_pct":     s3_pct,
            "data_transfer_pct": dt_pct,
            "llm_pct":    llm_pct,
            "monthly_spend_band": _size_band(total / days * 30),
        }
    except Exception as e:
        log.debug("metric extraction failed: %s", e)
        return {}


# ── Submission ────────────────────────────────────────────────────────────────

def submit_metrics(account_id: str, vertical: str = "default") -> bool:
    """
    Opt-in: submit anonymised, differential-private metrics to the pool.
    Returns True if submitted successfully.
    """
    if not ENABLED:
        log.debug("Benchmarking not enabled (NABLE_BENCHMARKING_ENABLED != true)")
        return False

    from ..config import is_airgap
    if is_airgap():
        # Air-gap mode promises provider-only traffic. Benchmarking POSTs cost
        # ratios to bench.nable.dev, so it must be suppressed here regardless of
        # the opt-in flag, or the air-gap guarantee is false.
        log.debug("Air-gap mode (FINOPS_AIRGAP): benchmarking egress disabled")
        return False

    metrics = _extract_metrics(account_id)
    if not metrics:
        return False

    # Apply differential privacy
    noisy = {k: _add_noise(v) if isinstance(v, float) else v
             for k, v in metrics.items()}

    payload = {
        "account_hash": _hash_account(account_id),
        "vertical":     vertical,
        "submitted_at": date.today().isoformat(),
        "metrics":      noisy,
        "version":      "1",
    }

    try:
        import httpx
        resp = httpx.post(
            f"{BENCH_ENDPOINT}/v1/submit",
            json=payload,
            timeout=10,
            headers={"User-Agent": "nable-finops/1.0"},
        )
        resp.raise_for_status()
        log.info("Benchmarking metrics submitted")
        return True
    except Exception as e:
        log.debug("Benchmark submission failed: %s", e)
        return False


# ── Comparison ────────────────────────────────────────────────────────────────

@dataclass
class BenchmarkComparison:
    metric:       str
    your_value:   float
    peer_median:  float
    peer_group:   str
    percentile:   float         # estimated percentile (0–100)
    delta_pct:    float         # (your - median) / median * 100
    assessment:   str           # "better", "similar", "worse"
    insight:      str           # human-readable insight


def compare(
    account_id: str,
    vertical: str = "default",
    days: int = 30,
) -> dict[str, Any]:
    """
    Compare this account's spend metrics against peer group medians.

    Returns:
        {
          "peer_group":   str,
          "size_band":    str,
          "comparisons":  [BenchmarkComparison.to_dict(), ...],
          "summary":      str,
          "pool_size":    int,   # number of accounts in peer group (0 if static)
          "data_source":  "pool" | "static",
        }
    """
    my_metrics = _extract_metrics(account_id, days)
    medians    = _STATIC_MEDIANS.get(vertical, _STATIC_MEDIANS["default"])
    pool_size  = 0
    data_source = "static"

    # Try to fetch live pool medians if enabled. Air-gap mode falls back to the
    # static medians rather than reaching bench.nable.dev (non-provider egress).
    from ..config import is_airgap
    if ENABLED and not is_airgap():
        try:
            import httpx
            resp = httpx.get(
                f"{BENCH_ENDPOINT}/v1/medians",
                params={"vertical": vertical,
                        "size_band": my_metrics.get("monthly_spend_band", "smb")},
                timeout=10,
            )
            if resp.status_code == 200:
                pool_data = resp.json()
                medians   = pool_data.get("medians", medians)
                pool_size = pool_data.get("pool_size", 0)
                data_source = "pool"
        except Exception:
            pass

    comparisons: list[BenchmarkComparison] = []

    metric_labels = {
        "ec2_pct":               "EC2 as % of total spend",
        "rds_pct":               "RDS as % of total spend",
        "s3_pct":                "S3 as % of total spend",
        "data_transfer_pct":     "Data transfer as % of total spend",
        "savings_plan_coverage": "Savings Plans coverage %",
        "idle_resource_pct":     "Idle resource %",
        "llm_pct":               "AI/LLM as % of total spend",
        "rightsizing_opp_pct":   "Oversized compute %",
    }

    # Lower-is-better metrics
    lower_better = {"idle_resource_pct", "rightsizing_opp_pct", "data_transfer_pct"}

    for metric, label in metric_labels.items():
        your_val = my_metrics.get(metric)
        peer_med = medians.get(metric)

        if your_val is None or peer_med is None:
            continue

        delta_pct = (your_val - peer_med) / max(peer_med, 0.1) * 100

        if metric in lower_better:
            # Lower is better: positive delta = worse than peers
            if delta_pct > 20:
                assessment = "worse"
                insight = f"Your {label} ({your_val:.1f}%) is {abs(delta_pct):.0f}% above the {vertical} median ({peer_med:.1f}%). This represents optimisation headroom."
            elif delta_pct < -20:
                assessment = "better"
                insight = f"Your {label} ({your_val:.1f}%) is below the peer median ({peer_med:.1f}%). Good efficiency."
            else:
                assessment = "similar"
                insight = f"Your {label} ({your_val:.1f}%) is in line with the {vertical} peer group median ({peer_med:.1f}%)."
        else:
            # Higher-is-better (e.g. savings plan coverage)
            if delta_pct < -20:
                assessment = "worse"
                insight = f"Your {label} ({your_val:.1f}%) is below the {vertical} median ({peer_med:.1f}%). Consider increasing coverage."
            elif delta_pct > 20:
                assessment = "better"
                insight = f"Your {label} ({your_val:.1f}%) exceeds the peer median ({peer_med:.1f}%). Well optimised."
            else:
                assessment = "similar"
                insight = f"Your {label} ({your_val:.1f}%) is in line with the {vertical} peer group ({peer_med:.1f}%)."

        # Rough percentile estimate (assumes normal distribution around median)
        import math
        z = delta_pct / 30   # rough normalisation
        percentile = round(50 + 50 * math.erf(z / math.sqrt(2)), 0)
        if metric in lower_better:
            percentile = 100 - percentile   # invert for lower-is-better

        comparisons.append(BenchmarkComparison(
            metric=metric,
            your_value=your_val,
            peer_median=peer_med,
            peer_group=vertical,
            percentile=percentile,
            delta_pct=round(delta_pct, 1),
            assessment=assessment,
            insight=insight,
        ))

    worse   = [c for c in comparisons if c.assessment == "worse"]
    better  = [c for c in comparisons if c.assessment == "better"]
    summary = (
        f"{len(better)} metric(s) better than {vertical} peers, "
        f"{len(worse)} metric(s) with optimisation headroom."
    )
    if worse:
        top_gap = sorted(worse, key=lambda c: abs(c.delta_pct), reverse=True)[0]
        summary += f" Biggest gap: {metric_labels.get(top_gap.metric, top_gap.metric)}."

    return {
        "account_id":  account_id,
        "peer_group":  vertical,
        "size_band":   my_metrics.get("monthly_spend_band", "unknown"),
        "data_source": data_source,
        "pool_size":   pool_size,
        "summary":     summary,
        "comparisons": [
            {
                "metric":      c.metric,
                "label":       metric_labels.get(c.metric, c.metric),
                "your_value":  c.your_value,
                "peer_median": c.peer_median,
                "percentile":  c.percentile,
                "delta_pct":   c.delta_pct,
                "assessment":  c.assessment,
                "insight":     c.insight,
            }
            for c in sorted(comparisons, key=lambda x: abs(x.delta_pct), reverse=True)
        ],
    }
