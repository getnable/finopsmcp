"""`nable scan` compares like with like: a monthly figure against a monthly figure.

Recoverable is a per-month estimate. The spend headline under --spend is month
to date, which on the 13th of a 31-day month is twelve days of spend. The scan
divided one by the other and printed it as "% of spend", so the percentage was
inflated by the ratio of the month to the days elapsed: 2.6x on the 13th, about
30x on the 2nd. The cross-provider summary did the same thing in a sum, adding
twelve days of AWS to thirty days of AI and Azure spend and calling it "/mo".

Worked example used below. Window 2026-10-01 to 2026-10-13 (end exclusive, as
Cost Explorer reads it): 12 days at $1,000/day, so $12,000 month to date and
$31,000 for October at that rate. $3,100/mo recoverable is 10.0% of a month of
spend, not 25.8%.
"""
from __future__ import annotations

import io

from finops import cli_scan
from finops import scan_assembler as sa

_MTD = {
    "period": "2026-10-01 to 2026-10-13",
    "covers": "month-to-date",
    "total": 12_000.0,
    "services": [("Amazon EC2", 8000.0), ("Amazon S3", 4000.0)],
    "regions": {},
    "has_data": True,
}
_REPORT = {
    "total_estimated_monthly_savings": 3_100.0,
    "findings": [{"waste_type": "unattached_ebs_volume", "estimated_monthly_savings": 3100.0,
                  "region": "us-east-1"}],
    "regions_scanned": ["us-east-1"],
    "regions_timed_out": [],
}


def _render(spend, report, extra=None) -> str:
    buf = io.StringIO()
    cli_scan._render(buf, spend, report, demo=False, ce_denied=False, extra_blocks=extra)
    return buf.getvalue()


def test_pct_of_spend_is_against_a_month_not_month_to_date():
    out = _render(_MTD, _REPORT)
    assert "25.8%" not in out, out           # 3,100 / 12,000: monthly over 12 days
    assert "10.0%" in out, out               # 3,100 / 31,000
    # The base is an extrapolation, so the line says what it was measured against.
    assert "this month's rate" in out, out

    doc = cli_scan._json_payload(_MTD, _REPORT, demo=False, profile="default",
                                 account_id="1", duration_s=1.0)
    assert doc["recoverable"]["pct_of_spend"] == 10.0
    assert doc["recoverable"]["pct_of_spend_basis"] == "month_to_date_run_rate"
    # The spend block itself still reports what Cost Explorer returned.
    assert doc["spend"]["month_to_date_usd"] == 12_000.0


def test_a_closed_month_is_already_a_whole_month():
    spend = dict(_MTD, period="2026-09-01 to 2026-10-01", covers="last month", total=30_000.0)
    report = dict(_REPORT, total_estimated_monthly_savings=3_000.0)
    out = _render(spend, report)
    assert "10.0%" in out and "last month" in out, out
    doc = cli_scan._json_payload(spend, report, demo=False, profile="default",
                                 account_id="1", duration_s=1.0)
    assert doc["recoverable"]["pct_of_spend"] == 10.0
    assert doc["recoverable"]["pct_of_spend_basis"] == "last_month"


def test_an_unreadable_window_gives_no_percentage_rather_than_a_wrong_one():
    spend = dict(_MTD, period="MTD")
    out = _render(spend, _REPORT)
    assert "% of" not in out, out
    doc = cli_scan._json_payload(spend, _REPORT, demo=False, profile="default",
                                 account_id="1", duration_s=1.0)
    assert doc["recoverable"]["pct_of_spend"] is None


def test_visible_total_puts_aws_on_the_same_monthly_basis_as_the_other_providers():
    # AI and Azure blocks are 30-day figures. AWS month to date has to be scaled
    # to a month before it is added to them: 31,000 + 9,200 = 40,200. The old sum,
    # 12,000 + 9,200 = 21,200, mixed 12 days with 30 and printed it as "/mo".
    ai = sa.ProviderBlock(family="ai", label="AI & GPU", status="ok", spend_usd=9_200.0,
                          estimated=True, by_provider={"openai": 9_200.0})
    out = _render(_MTD, _REPORT, extra=[ai])
    assert "$21,200/mo visible" not in out, out
    assert "$40,200/mo visible" in out, out


def test_cloud_native_ai_is_deducted_on_the_same_basis():
    # Under --spend Bedrock sits in both the AWS total and the AI block. The AI
    # block's Bedrock is 30 days, so it comes off the AWS monthly figure, not off
    # twelve days of it: 31,000 + 14,300 - 5,100 = 40,200.
    ai = sa.ProviderBlock(family="ai", label="AI & GPU", status="ok", spend_usd=14_300.0,
                          estimated=True, by_provider={"openai": 9_200.0, "bedrock": 5_100.0})
    out = _render(_MTD, _REPORT, extra=[ai])
    assert "$40,200/mo visible" in out, out
