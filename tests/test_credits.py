"""The managed-AI credit ledger: meter spend, expose the prepaid balance, and
feed the router's budget clamp. Default is unmetered (track but never block)."""
from __future__ import annotations

import pytest

from finops.billing import credits


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """Isolate the ledger to a tmp file and start unmetered + empty."""
    f = tmp_path / "managed_ai_ledger.json"
    monkeypatch.setattr(credits, "_ledger_file", lambda: f)
    monkeypatch.delenv("FINOPS_MANAGED_AI_BUDGET_USD", raising=False)
    credits.reset()
    return f


def test_cost_priced_per_model(ledger):
    # Haiku 1/5 per 1M: 1M in + 1M out = 1 + 5
    assert credits.cost_usd("claude-haiku-4-5", 1_000_000, 1_000_000) == pytest.approx(6.0)
    # Opus 5/25
    assert credits.cost_usd("claude-opus-4-8", 1_000_000, 1_000_000) == pytest.approx(30.0)


def test_unknown_model_prices_as_mid_tier(ledger):
    assert credits.cost_usd("some-future-model", 1_000_000, 0) == pytest.approx(3.0)


def test_date_suffixed_id_matches_by_prefix(ledger):
    assert credits.cost_usd("claude-opus-4-8-20260101", 0, 1_000_000) == pytest.approx(25.0)


def test_garbage_tokens_never_negative(ledger):
    assert credits.cost_usd("claude-haiku-4-5", -5, -5) == 0.0


def test_record_spend_accumulates(ledger):
    credits.record_spend(model="claude-haiku-4-5", input_tokens=1_000_000, output_tokens=0)  # $1
    credits.record_spend(model="claude-haiku-4-5", input_tokens=1_000_000, output_tokens=0)  # $1
    s = credits.budget_status()
    assert s["spent"] == pytest.approx(2.0)
    assert s["turns"] == 2


def test_unmetered_by_default_tracks_but_never_blocks(ledger):
    credits.record_spend(model="claude-opus-4-8", input_tokens=1_000_000, output_tokens=1_000_000)
    s = credits.budget_status()
    assert s["metered"] is False
    assert s["remaining"] is None and s["total"] is None
    assert s["spent"] == pytest.approx(30.0)  # still tracked for the profile meter


def test_env_budget_makes_it_metered(ledger, monkeypatch):
    monkeypatch.setenv("FINOPS_MANAGED_AI_BUDGET_USD", "50")
    credits.record_spend(model="claude-haiku-4-5", input_tokens=10_000_000, output_tokens=0)  # $10
    s = credits.budget_status()
    assert s["metered"] is True
    assert s["budget"] == pytest.approx(50.0)
    assert s["remaining"] == pytest.approx(40.0)


def test_set_monthly_budget_overrides_env(ledger, monkeypatch):
    monkeypatch.setenv("FINOPS_MANAGED_AI_BUDGET_USD", "50")
    credits.set_monthly_budget(100.0)
    assert credits.budget_status()["total"] == pytest.approx(100.0)
    credits.set_monthly_budget(None)  # clear -> falls back to env
    assert credits.budget_status()["total"] == pytest.approx(50.0)


def test_remaining_floors_at_zero(ledger, monkeypatch):
    monkeypatch.setenv("FINOPS_MANAGED_AI_BUDGET_USD", "5")
    credits.record_spend(model="claude-opus-4-8", input_tokens=10_000_000, output_tokens=0)  # $50 >> $5
    assert credits.budget_status()["remaining"] == 0.0


def test_rollover_carries_leftover_into_next_period(ledger, monkeypatch):
    monkeypatch.setenv("FINOPS_MANAGED_AI_BUDGET_USD", "50")
    credits.record_spend(
        model="claude-haiku-4-5", input_tokens=10_000_000, output_tokens=0, period="2026-05"
    )  # $10 of $50
    s = credits.budget_status(period="2026-06")
    assert s["rollover"] == pytest.approx(40.0)  # unused $40 carries
    assert s["total"] == pytest.approx(90.0)     # 50 fresh + 40 rolled
    assert s["spent"] == 0.0


def test_router_blocks_when_ledger_is_exhausted(ledger, monkeypatch):
    from finops.slack_bot.llm import route_request

    monkeypatch.setenv("FINOPS_MANAGED_AI_BUDGET_USD", "5")
    credits.record_spend(model="claude-opus-4-8", input_tokens=10_000_000, output_tokens=0)  # blow $5
    s = credits.budget_status()
    d = route_request("why did costs change?", budget_remaining=s["remaining"], budget_total=s["total"])
    assert d.blocked is True


def test_metering_helper_writes_through_to_the_ledger(ledger):
    from finops.slack_bot.llm import record_managed_ai_usage

    record_managed_ai_usage(
        surface="test", tier="simple", model="claude-haiku-4-5",
        input_tokens=1_000_000, output_tokens=0,
    )
    assert credits.budget_status()["spent"] == pytest.approx(1.0)
