"""Hosting-credit purchase -> managed-AI budget wiring, box side.

A Stripe purchase lands as a grant: additive within the month (two purchases
stack), use-it-or-lose-it at the period roll, and it arms the meter even when
no monthly budget is configured (the customer paid for a specific allowance,
so the clamp must be real). The 80% low_balance flag is the bill-shock guard:
the block at zero must never be the first the user hears about the meter.
"""
from __future__ import annotations

import pytest

from finops.billing import credits


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("FINOPS_MANAGED_AI_BUDGET_USD", raising=False)
    credits.reset()
    yield
    credits.reset()


def test_grant_arms_the_meter_without_a_budget():
    assert credits.budget_status()["metered"] is False
    st = credits.add_grant(100.0, source="stripe", note="evt_123")
    assert st["metered"] is True
    assert st["total"] == 100.0
    assert st["remaining"] == 100.0
    assert st["grants"] == 100.0


def test_grants_stack_within_the_month():
    credits.add_grant(100.0)
    st = credits.add_grant(50.0)
    assert st["grants"] == 150.0
    assert st["total"] == 150.0
    log = credits.grant_log()
    assert [g["usd"] for g in log] == [100.0, 50.0]


def test_grants_add_on_top_of_a_budget():
    credits.set_monthly_budget(200.0)
    st = credits.add_grant(100.0)
    assert st["total"] == 300.0


def test_grants_are_use_it_or_lose_it_at_period_roll():
    credits.add_grant(100.0)
    credits.record_spend(model="claude-sonnet-4-6", input_tokens=1_000_000,
                         output_tokens=0, period="2026-07")
    st = credits.budget_status(period="2026-08")  # next month
    assert st["grants"] == 0.0
    assert st["metered"] is False  # nothing left to meter against
    assert credits.grant_log() == []  # log reset with the period


def test_low_balance_flags_at_80_pct_and_block_at_zero():
    credits.add_grant(10.0)
    # Spend $8.50 of $10 (85%): warned, not blocked.
    credits.record_spend(model="claude-sonnet-4-6", input_tokens=0,
                         output_tokens=int(8.50 / 15.0 * 1_000_000))
    st = credits.budget_status()
    assert st["low_balance"] is True
    assert st["pct_used"] >= 80.0
    assert st["remaining"] > 0
    # Burn the rest: block state (remaining 0), no longer "low", it's out.
    credits.record_spend(model="claude-sonnet-4-6", input_tokens=0,
                         output_tokens=int(2.0 / 15.0 * 1_000_000))
    st = credits.budget_status()
    assert st["remaining"] == 0.0
    assert st["low_balance"] is False


def test_invalid_grants_are_ignored():
    for bad in (0, -5, None, "junk"):
        st = credits.add_grant(bad)  # type: ignore[arg-type]
        assert st["metered"] is False


def test_nudge_url_carries_context_before_fragment():
    from finops.server import _nudge_url
    url = _nudge_url("anomalies")
    assert url.startswith("https://getnable.com/?utm_source=nable")
    assert "utm_campaign=anomalies" in url
    assert url.endswith("#pricing")  # fragment preserved, after the query
    assert _nudge_url("") == "https://getnable.com/#pricing"  # untagged unchanged
