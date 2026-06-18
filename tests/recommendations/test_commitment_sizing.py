"""Tests for commitment recommendations sized to the consistent baseline (not a blanket %)."""
from __future__ import annotations

from finops.recommendations import commitments as c
from finops.recommendations.commitments import _COMPUTE_SP_DISCOUNT, _build_recommendations


def _sp(recs):
    return next((r for r in recs if r["type"] == "savings_plan"), None)


def test_sizes_to_baseline_not_average():
    # months: avg uncovered = 2400, but the floor (min) is 1000. We size to the floor.
    recs = _build_recommendations(30.0, 7200.0, 90.0, 90.0, monthly_uncovered_series=[1000, 5000, 1200])
    r = _sp(recs)
    assert r is not None
    assert r["baseline_monthly_uncovered_usd"] == 1000.0
    assert r["commitment_per_month"] == round(1000 * _COMPUTE_SP_DISCOUNT, 2)
    assert r["monthly_savings"] == round(1000 * (1 - _COMPUTE_SP_DISCOUNT), 2)
    assert "consistent monthly baseline" in r["sizing_basis"]


def test_spiky_usage_explains_it_sized_to_the_floor():
    recs = _build_recommendations(20.0, 11700.0, 90.0, 90.0, monthly_uncovered_series=[800, 10000, 900])
    r = _sp(recs)
    assert r["baseline_monthly_uncovered_usd"] == 800.0
    assert "peak" in r["description"].lower()   # warns it sized to floor, not the $10k peak


def test_flat_usage_has_no_peak_caveat():
    recs = _build_recommendations(20.0, 6000.0, 90.0, 90.0, monthly_uncovered_series=[2000, 2100, 1900])
    r = _sp(recs)
    assert r["baseline_monthly_uncovered_usd"] == 1900.0
    assert "peak" not in r["description"].lower()


def test_fallback_to_average_without_series():
    recs = _build_recommendations(30.0, 7200.0, 90.0, 90.0)
    r = _sp(recs)
    assert r["baseline_monthly_uncovered_usd"] == 2400.0   # 7200 / 3
    assert "3-month average" in r["sizing_basis"]


def test_no_rec_when_baseline_below_threshold():
    recs = _build_recommendations(20.0, 900.0, 90.0, 90.0, monthly_uncovered_series=[300, 400, 350])
    assert _sp(recs) is None   # baseline 300 < $500 floor


def test_no_rec_when_already_well_covered():
    recs = _build_recommendations(85.0, 30000.0, 90.0, 90.0, monthly_uncovered_series=[8000, 9000, 10000])
    assert _sp(recs) is None   # coverage >= 60%


def test_uncovered_on_demand_sums_monthly(monkeypatch):
    monkeypatch.setattr(c, "_uncovered_on_demand_monthly", lambda *a, **k: [100.0, 200.0, 150.0])
    assert c._uncovered_on_demand(None, "s", "e") == 450.0
