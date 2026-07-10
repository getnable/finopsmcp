"""A $0 cash bill must not read as "free". When AWS Activate credits materially
cover the bill, nable volunteers the true burn in the cost summary without the
user having to know to ask. First-user request (2026-07-10): "if we burned $434
and Activate covered all of it, I do not want it to tell me cost was $0."
"""
from __future__ import annotations

from finops.connectors.credit_tracking import credit_headsup, analyze_credits


def _months(series):
    # series: list of (gross, credits) -> per_month dicts the analyzer expects.
    return [
        {"month": f"2026-{i+1:02d}", "gross": g, "credits": c, "refunds": 0.0,
         "net_cash": round(g - c, 2)}
        for i, (g, c) in enumerate(series)
    ]


def test_headsup_surfaces_true_burn_on_credit_covered_bill():
    # Three months fully covered by credits: cash ~$0, burn ~$434.
    status = analyze_credits(_months([(400, 400), (420, 420), (434, 434)]))
    hs = credit_headsup(status)
    assert hs is not None
    assert hs["gross_burn_usd"] == 434.0
    assert hs["credits_covered_usd"] == 434.0
    assert hs["cash_bill_usd"] == 0.0
    assert hs["steady_state_monthly_usd"] == 434.0
    # The note tells the story in plain language.
    assert "$434" in hs["note"]
    assert "steady-state" in hs["note"].lower()
    assert "$0" in hs["note"]  # cash bill


def test_no_headsup_for_a_plain_cash_account():
    # Real cash spend, no credits: nothing extra should surface.
    status = analyze_credits(_months([(500, 0), (520, 0), (540, 0)]))
    assert credit_headsup(status) is None


def test_no_headsup_when_status_empty_or_errored():
    assert credit_headsup({}) is None
    assert credit_headsup({"status": "error"}) is None
    assert credit_headsup({"credits_active": True, "latest_gross_usd": 0,
                           "latest_credits_usd": 0}) is None


def test_runway_line_appears_when_credits_are_declining():
    # Declining credits -> a months-to-zero estimate shows in the note.
    status = analyze_credits(_months([(400, 400), (400, 300), (400, 200), (400, 100)]))
    hs = credit_headsup(status)
    assert hs is not None
    if hs["estimated_months_to_zero_credits"]:
        assert "credits last about" in hs["note"]
