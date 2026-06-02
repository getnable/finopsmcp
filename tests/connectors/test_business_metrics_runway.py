"""
Tests for the Phase 1 business-context layer: runway math + merge-upsert.

compute_runway is pure (no DB). The save_metrics tests use a temp SQLite DB via
FINOPS_DB_PATH and reset the cached engine, matching the project test pattern.
"""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from src.finops.connectors.business_metrics import (
    compute_runway,
    save_metrics,
    get_latest_metrics,
)


# ── compute_runway (pure) ─────────────────────────────────────────────────────

def test_infra_runway_basic():
    r = compute_runway(cash_on_hand_usd=2_000_000, infra_monthly_burn_usd=190_000)
    assert r["available"] is True
    assert r["mode"] == "infra"
    # 2,000,000 / 190,000 ≈ 10.5
    assert 10.4 <= r["months"] <= 10.6
    assert "Infra runway" in r["label"]
    assert "payroll" in r["label"].lower()


def test_company_runway_uses_net_burn():
    # opex 210k, revenue 180k -> net burn 30k -> 2M / 30k ≈ 66.7 months
    r = compute_runway(
        cash_on_hand_usd=2_000_000,
        infra_monthly_burn_usd=190_000,
        monthly_opex_usd=210_000,
        mrr_usd=180_000,
    )
    assert r["available"] is True
    assert r["mode"] == "company"
    assert 66.0 <= r["months"] <= 67.5
    assert "Company runway" in r["label"]


def test_burn_zero_does_not_divide_by_zero():
    # CRITICAL: infra burn 0 must not raise, must report unavailable.
    r = compute_runway(cash_on_hand_usd=2_000_000, infra_monthly_burn_usd=0)
    assert r["available"] is False
    assert "reason" in r


def test_missing_cash_is_unavailable():
    assert compute_runway(None, 190_000)["available"] is False
    assert compute_runway(0, 190_000)["available"] is False
    assert compute_runway(-5, 190_000)["available"] is False


def test_cash_flow_positive_has_no_runway_limit():
    # revenue 150k exceeds opex 100k -> net burn negative -> profitable
    r = compute_runway(
        cash_on_hand_usd=2_000_000,
        infra_monthly_burn_usd=190_000,
        monthly_opex_usd=100_000,
        mrr_usd=150_000,
    )
    assert r["available"] is True
    assert r["months"] is None
    assert "positive" in r["label"].lower()


def test_runway_end_date_present_for_finite_runway():
    r = compute_runway(cash_on_hand_usd=1_200_000, infra_monthly_burn_usd=100_000)
    assert "runway_end_date" in r
    # YYYY-MM-DD
    assert len(r["runway_end_date"]) == 10


# ── save_metrics merge-upsert (DB) ────────────────────────────────────────────

def _with_temp_db(fn):
    with tempfile.TemporaryDirectory() as td:
        with patch.dict(os.environ, {"FINOPS_DB_PATH": str(Path(td) / "test.db")}):
            from src.finops.storage import db as db_mod
            db_mod._ENGINE = None
            try:
                return fn()
            finally:
                db_mod._ENGINE = None


def test_runway_fields_persist():
    def body():
        save_metrics(
            metric_date="2026-06-01",
            cash_on_hand_usd=2_400_000,
            monthly_opex_usd=210_000,
            last_raise_amount_usd=8_000_000,
            last_raise_date="2025-09-15",
        )
        latest = get_latest_metrics(n=1)[0]
        assert latest["cash_on_hand_usd"] == 2_400_000
        assert latest["monthly_opex_usd"] == 210_000
        assert latest["last_raise_amount_usd"] == 8_000_000
        assert latest["last_raise_date"] == "2025-09-15"
    _with_temp_db(body)


def test_same_date_merge_does_not_clobber():
    # Set revenue in one call, cash in a second call on the SAME date.
    # The merge-upsert must preserve both, not wipe the first.
    def body():
        save_metrics(metric_date="2026-06-01", mrr_usd=45_000, paying_customers=340)
        save_metrics(metric_date="2026-06-01", cash_on_hand_usd=2_000_000)
        latest = get_latest_metrics(n=1)[0]
        assert latest["mrr_usd"] == 45_000          # not clobbered
        assert latest["paying_customers"] == 340     # not clobbered
        assert latest["cash_on_hand_usd"] == 2_000_000
    _with_temp_db(body)


def test_custom_metrics_shallow_merge():
    def body():
        save_metrics(metric_date="2026-06-01", custom_metrics={"nps": 42})
        save_metrics(metric_date="2026-06-01", custom_metrics={"free_signups": 4200})
        latest = get_latest_metrics(n=1)[0]
        assert latest["custom_metrics"]["nps"] == 42
        assert latest["custom_metrics"]["free_signups"] == 4200
    _with_temp_db(body)
