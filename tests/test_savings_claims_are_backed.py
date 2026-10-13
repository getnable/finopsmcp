"""What nable says it saved, and what it says is still on the table.

Every number here is a claim about money, so each is pinned against a worked
example rather than against whatever the code happened to print.

Saved:
  - A change verified against the bill at $0 (the bill did not move) was shown
    by the savings ledger at its predicted estimate, because `0.0 or estimate`
    reads a measured zero as missing.
  - "Verified (banked, actually left the bill)" covered every verified row,
    including ones priced at list price or the effective rate after the
    verifier saw the config change. Those are confirmed changes with
    estimated value; only `bill_measured` rows were measured off the bill.
  - ROI: "Verified savings cover tool cost" printed for any verified amount
    above zero ($10/mo against a $25/mo plan), a payback period was quoted
    when savings never reach the cost, the free plan was told Pro pays back
    in "less than 1 week" whatever the amount, and competitor costs quoted as
    "2.5% of managed spend" were 2.5% of nable's verified savings.

On the table:
  - Two sources on one instance (rightsizing m5.xlarge to m5.large, $70.08,
    and Graviton m5.xlarge to m6g.xlarge, $28.03) were summed into $98.11 of
    potential. Applied together they make an m6g.large, $84.10 below today,
    and applied as listed you can bank one of them. The sweep already counts
    the best fix per resource; the ledger now does the same: $70.08.
  - The ROI and ledger tools summed raw rows, so a recommendation written
    twice by an older release (the Graviton key once carried its own
    estimate) counted twice there while get_summary collapsed it.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from finops import server
from finops.recommendations import savings_tracker as st


def _run(value):
    """The server wraps its tools; a wrapped tool hands back a coroutine."""
    return asyncio.run(value) if inspect.isawaitable(value) else value


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    from finops.storage import db as db_mod

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(db_mod, "_ENGINE", None)
    yield st
    db_mod._ENGINE = None


def _rec(source, rid, est, desc=None, cfg=None):
    return st.record_recommendation(
        source=source, provider="aws", resource_id=rid, resource_type="ec2",
        resource_name=rid, current_config={}, recommended_config=cfg or {"src": source},
        description=desc or f"{source} {rid}", estimated_monthly_savings_usd=est)


def _verified(source, rid, est, actual, basis):
    rid_ = _rec(source, rid, est)
    st.mark_acted_on(rid_)
    st.mark_verified(rid_, actual, basis)
    return rid_


class _Plan:
    def __init__(self, mode):
        self.mode = mode


# ── saved ────────────────────────────────────────────────────────────────────

def test_ledger_counts_a_measured_zero_as_zero(ledger):
    _verified("rightsizing", "i-0zero", 500.0, 0.0, "bill_measured")
    out = _run(server.get_savings_ledger(days=30))
    assert "VERIFIED: $0/mo" in out, out
    assert "VERIFIED: $500/mo" not in out, out


def test_roi_does_not_call_list_price_savings_money_off_the_bill(ledger, monkeypatch):
    monkeypatch.setattr(server, "get_status", lambda: _Plan("pro"))
    _verified("rightsizing", "i-0bill", 200.0, 180.0, "bill_measured")
    _verified("graviton", "i-0list", 120.0, 120.0, "list_price")
    roi = _run(server.get_nable_roi(period_days=90))
    assert roi["verified_monthly_usd"] == 300.0
    assert roi["verified_bill_measured_monthly_usd"] == 180.0
    assert roi["verified_estimated_monthly_usd"] == 120.0
    text = roi["summary"]
    # The bill-measured part may say it left the bill; the total may not.
    assert "$300/mo" in text and "$180/mo" in text, text
    assert "actually left the bill): $300" not in text, text


def test_roi_never_says_savings_cover_the_cost_when_they_do_not(ledger, monkeypatch):
    monkeypatch.setattr(server, "get_status", lambda: _Plan("pro"))   # $25/mo
    _verified("rightsizing", "i-0small", 12.0, 10.0, "bill_measured")
    roi = _run(server.get_nable_roi(period_days=90))
    assert roi["verified_monthly_usd"] == 10.0
    assert "Verified savings cover tool cost" not in roi["summary"], roi["summary"]
    # $10/mo saved against $25/mo paid never pays back; 25 / 10 = 2.5 "months"
    # was printed as a payback period.
    assert roi["payback_months"] is None
    assert "2.5 months" not in roi["summary"], roi["summary"]


def test_roi_still_credits_savings_that_do_cover_the_cost(ledger, monkeypatch):
    monkeypatch.setattr(server, "get_status", lambda: _Plan("pro"))   # $25/mo
    _verified("rightsizing", "i-0big", 60.0, 50.0, "bill_measured")
    roi = _run(server.get_nable_roi(period_days=90))
    assert "Verified savings cover tool cost" in roi["summary"], roi["summary"]
    assert roi["payback_months"] == 0.5


def test_free_plan_payback_is_not_promised_below_the_pro_price(ledger, monkeypatch):
    monkeypatch.setattr(server, "get_status", lambda: _Plan("free"))
    _verified("rightsizing", "i-0small", 12.0, 10.0, "bill_measured")
    text = _run(server.get_nable_roi(period_days=90))["summary"]
    assert "less than 1 week" not in text, text


def test_competitor_cost_is_not_a_percentage_of_nables_savings(ledger, monkeypatch):
    monkeypatch.setattr(server, "get_status", lambda: _Plan("pro"))
    _verified("rightsizing", "i-0k", 2400.0, 2000.0, "bill_measured")
    text = _run(server.get_nable_roi(period_days=90))["summary"]
    # $2,000/mo verified is $24,000/yr; 2.5% of that is $600 and 1.5% is $360.
    # Those tools price on managed cloud spend, which this report does not read.
    assert "$600/yr" not in text and "$360/yr" not in text, text


def test_verify_savings_does_not_say_off_your_bill_for_estimates(ledger, monkeypatch):
    from finops.tools import recommendations as rec_tools

    monkeypatch.setattr(rec_tools, "require_pro", lambda feature: None)
    _verified("graviton", "i-0list", 120.0, 120.0, "list_price")
    monkeypatch.setattr(st, "auto_verify_acted_on", lambda: [{
        "id": 1, "resource_id": "i-0list", "description": "graviton i-0list",
        "verified_monthly_savings_usd": 120.0, "verified_basis": "list_price"}])
    out = _run(rec_tools.verify_savings())
    assert "confirmed off your bill" not in out["message"], out["message"]
    assert out["verified_bill_measured_monthly_usd"] == 0.0


# ── on the table ─────────────────────────────────────────────────────────────

def test_two_fixes_on_one_instance_count_once_in_potential(ledger):
    _rec("rightsizing", "i-0abc", 70.08, desc="Downsize m5.xlarge to m5.large")
    _rec("graviton", "i-0abc", 28.03, desc="Migrate m5.xlarge to Graviton m6g.xlarge")
    _rec("idle", "vol-0other", 8.0, desc="Unattached volume")
    s = st.get_summary()
    assert s["potential_monthly_usd"] == 78.08          # 70.08 + 8.00, not 106.11
    assert s["overlap_excluded_monthly_usd"] == 28.03


def test_roi_and_ledger_agree_with_the_summary_on_found(ledger, monkeypatch):
    monkeypatch.setattr(server, "get_status", lambda: _Plan("pro"))
    _rec("rightsizing", "i-0abc", 70.08, desc="Downsize m5.xlarge to m5.large")
    _rec("graviton", "i-0abc", 28.03, desc="Migrate m5.xlarge to Graviton m6g.xlarge")
    # The same Graviton recommendation written twice by an older release: the
    # key once hashed the estimate, so a re-priced estimate made a second row.
    _rec("graviton", "i-0abc", 29.10, desc="Migrate m5.xlarge to Graviton m6g.xlarge",
         cfg={"graviton_equivalent": "m6g.xlarge", "estimated_monthly_savings_usd": 29.10})
    assert st.get_summary()["potential_monthly_usd"] == 70.08
    assert _run(server.get_nable_roi(period_days=90))["found_monthly_usd"] == 70.08
    assert "FOUND:    $70/mo" in _run(server.get_savings_ledger(days=30))


def test_one_resource_measured_twice_on_the_bill_counts_once(ledger):
    # Two recommendations on one instance, both verified off the same CUR
    # before/after window, each measure the instance's whole cost drop.
    _verified("rightsizing", "i-0two", 70.0, 84.0, "bill_measured")
    _verified("graviton", "i-0two", 28.0, 84.0, "bill_measured")
    s = st.get_summary()
    assert s["verified_monthly_usd"] == 84.0
    assert s["verified_bill_measured_monthly_usd"] == 84.0
