"""Cost-preflight verdict logic: the agent-native on-ramp's decision function."""
from __future__ import annotations

from finops.preflight import evaluate_preflight, OK, WARN, OVER_BUDGET, NO_BUDGET


def _budget(limit=10000.0, run_rate=5000.0, name="prod", alert=80.0):
    return {"name": name, "limit_usd": limit, "run_rate_usd": run_rate}


# ── no budget ─────────────────────────────────────────────────────────────────
def test_no_budget_reports_delta_and_says_no_budget():
    r = evaluate_preflight(2140.0, budget=None)
    assert r["verdict"] == NO_BUDGET
    assert r["budget"] is None
    assert r["monthly_delta_usd"] == 2140.0
    assert r["annual_delta_usd"] == 2140.0 * 12
    assert "No budget" in r["reason"]


def test_empty_limit_is_treated_as_no_budget():
    r = evaluate_preflight(100.0, budget={"name": "x", "limit_usd": 0})
    assert r["verdict"] == NO_BUDGET


# ── within budget ─────────────────────────────────────────────────────────────
def test_small_change_well_under_limit_is_ok():
    # 5000 + 500 = 5500 of 10000 = 55%
    r = evaluate_preflight(500.0, budget=_budget())
    assert r["verdict"] == OK
    b = r["budget"]
    assert b["projected_run_rate_usd"] == 5500.0
    assert b["projected_pct_of_limit"] == 55.0
    assert b["headroom_usd"] == 4500.0


# ── warn band ─────────────────────────────────────────────────────────────────
def test_change_into_alert_band_warns():
    # 5000 + 3500 = 8500 of 10000 = 85% >= alert 80
    r = evaluate_preflight(3500.0, budget=_budget(), alert_pct=80.0)
    assert r["verdict"] == WARN
    assert r["budget"]["projected_pct_of_limit"] == 85.0
    assert "headroom" in r["reason"]


def test_alert_threshold_is_configurable():
    # 55% projected, but a strict 50% alert -> warn
    r = evaluate_preflight(500.0, budget=_budget(), alert_pct=50.0)
    assert r["verdict"] == WARN


# ── over budget ───────────────────────────────────────────────────────────────
def test_change_over_limit_is_over_budget_with_negative_headroom():
    # 5000 + 6000 = 11000 of 10000 = 110%
    r = evaluate_preflight(6000.0, budget=_budget())
    assert r["verdict"] == OVER_BUDGET
    assert r["budget"]["headroom_usd"] == -1000.0
    assert "over" in r["reason"].lower()


# ── savings ───────────────────────────────────────────────────────────────────
def test_a_saving_is_always_ok_even_when_run_rate_is_already_high():
    # already at 9500/10000 = 95%, but the change SAVES money
    r = evaluate_preflight(-800.0, budget=_budget(run_rate=9500.0))
    assert r["verdict"] == OK
    assert r["monthly_delta_usd"] == -800.0
    assert r["annual_delta_usd"] == -9600.0
    assert "saves" in r["reason"]


def test_zero_delta_is_ok():
    r = evaluate_preflight(0.0, budget=_budget())
    assert r["verdict"] == OK


# ── boundaries ────────────────────────────────────────────────────────────────
def test_exactly_at_limit_is_over_budget():
    # 5000 + 5000 = 10000 of 10000 = 100% -> over_budget (>= 100)
    r = evaluate_preflight(5000.0, budget=_budget())
    assert r["verdict"] == OVER_BUDGET


def test_just_below_alert_is_ok():
    # 5000 + 2999 = 7999 of 10000 = 79.99% -> ok (< 80)
    r = evaluate_preflight(2999.0, budget=_budget(), alert_pct=80.0)
    assert r["verdict"] == OK
