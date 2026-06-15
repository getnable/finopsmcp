"""Tests for token-spend forecasting and runway-to-exhaustion."""
from __future__ import annotations

from finops.analytics.token_forecast import forecast_token_spend, _to_series


def test_to_series_accepts_dict_and_float_shapes():
    assert _to_series([{"date": "x", "total_usd": 10}, {"date": "y", "total_usd": 20}]) == [10.0, 20.0]
    assert _to_series([10, 20.5, -3]) == [10.0, 20.5, 0.0]  # negatives clamped to 0


def test_insufficient_history():
    out = forecast_token_spend([{"date": "a", "total_usd": 1}, {"date": "b", "total_usd": 2}])
    assert out["status"] == "insufficient_history"


def test_basic_forecast_shape():
    series = [{"date": f"d{i}", "total_usd": 100.0 + i} for i in range(20)]
    out = forecast_token_spend(series, horizon_days=30)
    assert out["status"] == "ok"
    assert out["projected_next_30d_usd"] > 0
    assert out["method"] in {"naive", "linear", "holt_winters"}
    assert len(out["daily_forecast"]) == 30
    assert out["days_of_history"] == 20


def test_growth_is_positive_for_rising_series():
    # Clearly rising daily spend -> projected month should exceed trailing month.
    series = [100.0 + 5 * i for i in range(40)]
    out = forecast_token_spend(series, horizon_days=30)
    assert out["implied_mom_growth_pct"] is not None
    assert out["implied_mom_growth_pct"] > 0


def test_exhaustion_within_horizon():
    series = [100.0] * 30
    out = forecast_token_spend(series, horizon_days=90, balance_usd=1000)
    assert out["runway"]["status"] == "exhausts_within_horizon"
    # ~100/day burns $1000 in roughly 10 days
    assert 5 <= out["runway"]["days_remaining"] <= 20
    assert out["runway"]["exhausts_on"]


def test_balance_lasts_beyond_horizon():
    series = [100.0] * 30
    out = forecast_token_spend(series, horizon_days=30, balance_usd=10_000_000)
    assert out["runway"]["status"] == "beyond_horizon"


def test_float_series_supported():
    out = forecast_token_spend([100.0] * 20, horizon_days=14)
    assert out["status"] == "ok"
    assert len(out["daily_forecast"]) == 14
