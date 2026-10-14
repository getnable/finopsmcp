"""The sample is labelled demo, and it still has to be a fair sample.

`nable scan --demo` is the first output many people see. It showed 4 idle NAT
gateways at $12,960/mo ($3,240 each) where the engine's own price for an idle
gateway is its fixed charge, 730 h x $0.045 = $32.85, and 3 idle db.r5.xlarge
at $2,840/mo where the engine prices them at 3 x $0.48 x 730 = $1,051.20. A
demo that claims ~100x what a real scan of the same resources would report is
the number a careful reader checks first.

The MCP sample double counted too: the CloudFront commit ($62,000/mo) and the
encoder-fleet change ($8,400/mo) sat in the verified ledger as landed and again
in the open opportunities, so open potential included $70,400/mo already
banked, and the AI panel called the banked encoder change recoverable.
"""
from __future__ import annotations

from finops import cli_scan, demo_data
from finops.aws_prices import EC2_MONTHLY, NAT_GATEWAY_PER_MONTH, ebs_volume_monthly, rds_hourly


def _scan_lines():
    _, report = cli_scan._demo_payload()
    return {f["waste_type"]: f["estimated_monthly_savings"] for f in report["findings"]}, report


def test_each_scan_demo_line_is_at_most_what_the_engine_would_price_it_at():
    lines, report = _scan_lines()
    ceilings = {
        "idle_nat_gateway": 4 * NAT_GATEWAY_PER_MONTH,                 # 4 gateways
        "idle_rds": 3 * rds_hourly("db.r5.xlarge", "mysql") * 730,      # 3 x db.r5.xlarge
        "unattached_ebs": ebs_volume_monthly("gp2", 48 * 1000),         # 48 TB of gp2
        "oversized_ec2": 9 * EC2_MONTHLY["m5.2xlarge"],                 # 9 x m5.2xlarge
    }
    for wt, ceiling in ceilings.items():
        assert lines[wt] <= round(ceiling, 2) + 0.01, (wt, lines[wt], ceiling)
    assert lines["idle_nat_gateway"] == 131.40
    assert report["total_estimated_monthly_savings"] == round(sum(lines.values()), 2)


def test_landed_savings_are_not_also_open_potential():
    d = demo_data.dashboard_data()
    landed = {v["resource"] for v in d["verified_savings"]["ledger"]}
    assert landed, "the sample should still show verified savings"
    open_resources = {o["resource"] for o in d["recent_opportunities"]}
    assert not landed & open_resources, landed & open_resources
    assert d["opportunities_total_saving"] == round(
        sum(o["monthly_saving"] for o in d["recent_opportunities"]), 2)
    assert "encoder fleet" not in d["ai_efficiency"]["callout"]
