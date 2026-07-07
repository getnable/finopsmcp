"""Never fabricate a dollar for a resource we can't price.

Two silent fallbacks used to emit a made-up $0.10/hr for an unknown instance
class: idle-RDS waste (analyzers/waste.py) and k8s node cost
(connectors/kubernetes_costs.py). On a large or GPU instance that is off by
100x. These tests lock in the honest behavior: an unknown type is left unpriced
(None / excluded and flagged), never costed at a magic default.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from finops.analyzers.waste import check_rds_idle
from finops.connectors.kubernetes_costs import _node_daily_cost


# ── k8s node pricing ──────────────────────────────────────────────────────────

def test_node_daily_cost_known_type():
    # m5.large is in the table; returns a real per-day number, not None.
    c = _node_daily_cost("m5.large")
    assert c is not None and c > 0


def test_node_daily_cost_unknown_is_none_not_ten_cents():
    # The whole point: an unknown type is unpriced, never $0.10/hr ($2.40/day).
    assert _node_daily_cost("totally-made-up.type") is None


def test_node_daily_cost_modern_gpu_now_priced():
    # p5.48xlarge (H100) used to hit the $0.10 fallback; now it is in the table.
    c = _node_daily_cost("p5.48xlarge")
    assert c is not None and c > 1000  # ~$98/hr * 24 = ~$2360/day


# ── idle-RDS pricing ──────────────────────────────────────────────────────────

def _rds_cw_for(db_class: str):
    """Mock rds + cloudwatch clients for one available, idle instance of db_class."""
    rds = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [{
        "DBInstances": [{
            "DBInstanceIdentifier": "db-1",
            "DBInstanceClass": db_class,
            "Engine": "postgres",
            "DBInstanceStatus": "available",
            "MultiAZ": False,
        }]
    }]
    rds.get_paginator.return_value = paginator

    cw = MagicMock()
    now = datetime.now(timezone.utc)
    # >= 7 datapoints, all near-zero connections -> idle
    cw.get_metric_statistics.return_value = {
        "Datapoints": [{"Maximum": 0.0, "Timestamp": now} for _ in range(10)]
    }
    return rds, cw


def test_idle_rds_known_class_gets_real_dollar():
    rds, cw = _rds_cw_for("db.t3.medium")  # in _RDS_HOURLY
    findings = check_rds_idle(rds, cw, region="us-east-1")
    assert len(findings) == 1
    f = findings[0]
    assert f["estimated_monthly_savings"] is not None
    assert f["estimated_monthly_savings"] > 0
    assert f.get("unpriced") is False


def test_idle_rds_unknown_class_is_unpriced_not_fabricated():
    rds, cw = _rds_cw_for("db.quantum.42xlarge")  # not in the table
    findings = check_rds_idle(rds, cw, region="us-east-1")
    assert len(findings) == 1
    f = findings[0]
    # The idle signal survives (still a finding), but no fabricated dollar.
    assert f["estimated_monthly_savings"] is None
    assert f["unpriced"] is True
    assert f["severity"] == "unknown"
    assert "cost unknown" in f["detail"]
