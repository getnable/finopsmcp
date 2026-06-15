"""Tests for the LLM commitment & contract engine and the AI-spend monitor."""
from __future__ import annotations

import asyncio
import json

import pytest

from finops.analytics.llm_commitments import (
    total_tokens,
    analyze_commitment,
    analyze_portfolio,
    recommend_commitment,
    load_contracts,
)

# Clean capacity numbers used across tests (see hand-computed expectations below):
#   units=1, tpm=100000, rate_hr=10, on_demand=5.0, days=30
#   committed_cost   = 1 * 10 * 720h            = 7,200
#   capacity_tokens  = 1 * 100000 * 43,200min   = 4.32e9  (4,320 Mtok)
#   break_even_util  = 7200 / (4320 * 5)        = 33.3%
CAP = {
    "type": "bedrock_provisioned", "label": "Bedrock PT", "provider": "aws",
    "units": 1, "unit_throughput_tpm": 100_000, "unit_rate_usd_hr": 10.0,
    "term_months": 1, "on_demand_rate_per_mtok": 5.0,
}
CAPACITY_TOKENS = 4_320_000_000


# ── token helper ──────────────────────────────────────────────────────────────

def test_total_tokens_sums_and_excludes_request_count():
    by_model = {
        "m1": {"input": 100, "output": 200, "cache_read": 50, "request_count": 5},
        "m2": {"input": 10},
    }
    assert total_tokens(by_model) == 360  # 100+200+50+10, request_count ignored


def test_total_tokens_handles_empty_and_garbage():
    assert total_tokens(None) == 0
    assert total_tokens({"m": {"input": "oops"}}) == 0


# ── credits adapter ───────────────────────────────────────────────────────────

def test_credits_runway_from_balance_and_burn():
    r = analyze_commitment(
        {"type": "credits", "label": "Activate", "balance_usd": 100_000, "monthly_burn_usd": 8000},
        {},
    )
    assert r["status"] == "ok"
    assert r["runway"]["estimated_months_to_zero"] == 12.5


def test_credits_cash_flip_is_expiring():
    r = analyze_commitment(
        {"type": "credits", "label": "Activate"},
        {"credit_analysis": {"cash_flip_detected": True, "latest_credit_coverage_pct": 5.0}},
    )
    assert r["status"] == "expiring"


def test_credits_expiring_soon():
    r = analyze_commitment(
        {"type": "credits", "label": "Activate"},
        {"credit_analysis": {"cash_flip_detected": False,
                             "estimated_months_to_zero_credits": 2}},
    )
    assert r["status"] == "expiring"


# ── capacity adapter ──────────────────────────────────────────────────────────

def test_capacity_well_utilized_saves_vs_on_demand():
    used = int(0.85 * CAPACITY_TOKENS)
    r = analyze_commitment(CAP, {"tokens": used, "days": 30})
    assert r["status"] == "ok"
    assert r["utilization_pct"] == pytest.approx(85.0, abs=0.5)
    assert r["savings_vs_on_demand_usd"] > 0
    assert r["effective_rate_per_mtok"] < r["on_demand_rate_per_mtok"]
    assert r["break_even_utilization_pct"] == pytest.approx(33.3, abs=0.5)


def test_capacity_underutilized_is_flagged_and_costs_more():
    used = int(0.20 * CAPACITY_TOKENS)  # below the 33% break-even
    r = analyze_commitment(CAP, {"tokens": used, "days": 30})
    assert r["status"] == "underutilized"
    assert r["savings_vs_on_demand_usd"] < 0          # paying more than on-demand
    assert r["effective_rate_per_mtok"] > r["on_demand_rate_per_mtok"]
    assert r["recommended_units"] < 1.0               # right-size down


def test_capacity_oversubscribed_when_near_full():
    used = int(0.98 * CAPACITY_TOKENS)
    r = analyze_commitment(CAP, {"tokens": used, "days": 30})
    assert r["status"] == "oversubscribed"


def test_capacity_missing_fields_is_no_data():
    r = analyze_commitment({"type": "azure_ptu", "label": "x", "units": 1}, {"tokens": 100})
    assert r["status"] == "no_data"


# ── rate card adapter ─────────────────────────────────────────────────────────

def test_rate_card_discount():
    r = analyze_commitment(
        {"type": "rate_card", "label": "Anthropic Ent",
         "negotiated_rate_per_mtok": 6.0, "list_rate_per_mtok": 9.0},
        {"tokens": 1_000_000_000, "spend_usd": 6000},
    )
    assert r["status"] == "ok"
    assert r["savings_vs_on_demand_usd"] == pytest.approx(3000, abs=1)   # (9-6)*1000 Mtok
    assert r["savings_vs_on_demand_pct"] == pytest.approx(33.3, abs=0.5)


def test_rate_card_minimum_shortfall():
    r = analyze_commitment(
        {"type": "rate_card", "label": "Anthropic Ent",
         "negotiated_rate_per_mtok": 6.0, "list_rate_per_mtok": 9.0,
         "minimum_spend_usd": 20000},
        {"tokens": 1_000_000_000, "spend_usd": 12000},
    )
    assert r["status"] == "minimum_shortfall"
    assert r["detail"]["minimum_shortfall_usd"] == pytest.approx(8000, abs=1)


def test_unknown_type_is_no_data():
    assert analyze_commitment({"type": "mystery"}, {})["status"] == "no_data"


# ── portfolio ─────────────────────────────────────────────────────────────────

def test_portfolio_rolls_up_savings_waste_and_attention():
    contracts = [
        {**CAP, "label": "good"},                         # well utilized -> ok
        {**CAP, "label": "bad"},                          # underutilized
        {"type": "rate_card", "label": "ent",
         "negotiated_rate_per_mtok": 6.0, "list_rate_per_mtok": 9.0,
         "minimum_spend_usd": 20000},                     # shortfall
    ]
    # Different usage per contract isn't supported in one call, so assert the
    # rollup over a single shared usage that makes the capacity contracts "ok".
    used = int(0.85 * CAPACITY_TOKENS)
    port = analyze_portfolio(contracts, {"tokens": used, "spend_usd": 12000, "days": 30})
    assert port["contract_count"] == 3
    # both capacity contracts save; the rate card has a minimum shortfall
    assert port["total_realized_savings_usd"] > 0
    assert port["total_committed_waste_usd"] == pytest.approx(8000, abs=1)
    labels = {a["label"] for a in port["needs_attention"]}
    assert "ent" in labels


# ── commitment recommendation (no contract yet) ───────────────────────────────

def test_recommend_skips_small_spend():
    r = recommend_commitment([3.0] * 30, on_demand_monthly_usd=100)
    assert r["recommend"] is False


def test_recommend_yes_for_high_stable_spend():
    r = recommend_commitment([100.0] * 30, on_demand_monthly_usd=3000)
    assert r["recommend"] is True
    assert r["estimated_savings_range_usd"][0] > 0


def test_recommend_partial_for_spiky_spend():
    spiky = [10.0] * 27 + [400.0] * 3
    r = recommend_commitment(spiky, on_demand_monthly_usd=5000)
    assert r["recommend"] == "partial"


# ── contract loading ──────────────────────────────────────────────────────────

def test_load_contracts_from_env(monkeypatch):
    monkeypatch.setenv("FINOPS_AI_CONTRACTS", json.dumps([CAP]))
    contracts = load_contracts()
    assert len(contracts) == 1
    assert contracts[0]["label"] == "Bedrock PT"


def test_load_contracts_none_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("FINOPS_AI_CONTRACTS", raising=False)
    monkeypatch.setenv("FINOPS_HOME", str(tmp_path))
    assert load_contracts() == []


# ── monitor job ───────────────────────────────────────────────────────────────

def test_ai_monitor_detects_spend_spike(monkeypatch, tmp_path):
    monkeypatch.setenv("FINOPS_HOME", str(tmp_path))
    monkeypatch.delenv("FINOPS_AI_CONTRACTS", raising=False)

    # 28 days of varying ~$100/day, then a 10x spike on the latest day.
    daily = [{"date": f"2026-05-{i+1:02d}", "total_usd": (90.0 if i % 2 else 110.0)}
             for i in range(28)]
    daily.append({"date": "2026-06-01", "total_usd": 1000.0})

    def fake_costs(*_a, **_k):
        return {"daily": daily, "total_usd": 3800.0, "by_model_tokens": {}}

    import finops.connectors.llm_costs as lc
    import finops.notifications.slack as slack
    monkeypatch.setattr(lc, "get_all_llm_costs", fake_costs)
    monkeypatch.setattr(slack, "is_configured", lambda: False)

    from finops.scheduler.jobs import _check_ai_spend_and_alert
    result = asyncio.run(_check_ai_spend_and_alert())
    assert result is not None
    assert any("spike" in f.lower() for f in result["findings"])
