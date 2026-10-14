"""The README's `nable scan` example is what the scan prints from its own prices.

It said "3 idle NAT gateways $1,200/mo" and "14 unattached EBS volumes (2.1 TB)
$610/mo" under a "$2,140/mo recoverable" headline. The scan prices an idle NAT
gateway at its hourly charge, aws_prices.NAT_GATEWAY_PER_MONTH ($32.85), so
three are $98.55; an unattached gp2 volume at $0.10/GB-month, so 2,100 GB is
$210; and an idle db.r5.xlarge MySQL instance at $0.48/hr x 730 = $350.40.
The headline is their sum, $658.95, printed as $659.

This builds the findings the detectors would make for that account and renders
them with the scan's own printer, so a price or a format change that leaves the
README behind fails here.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

from finops import cli_scan
from finops.aws_prices import HOURS_PER_MONTH, NAT_GATEWAY_PER_MONTH, ebs_volume_monthly, rds_hourly

README = Path(__file__).resolve().parents[1] / "README.md"


def _readme_example() -> list[str]:
    text = README.read_text(encoding="utf-8")
    block = text.split("uvx nable scan\n```\n\n```text\n", 1)[1].split("```", 1)[0]
    lines = block.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("─"))
    end = next(i for i, ln in enumerate(lines) if ln.startswith("run `nable scan --spend`"))
    # The README adds what each figure was priced on in parentheses; the scan
    # prints the group label and region.
    return [re.sub(r" \([^)]*\)", "", ln) for ln in lines[start + 1:end]]


def _findings() -> list[dict]:
    rds = rds_hourly("db.r5.xlarge", "mysql") * HOURS_PER_MONTH
    return (
        [{"waste_type": "unattached_ebs_volume", "region": "us-east-1",
          "estimated_monthly_savings": round(ebs_volume_monthly("gp2", 150), 2)}] * 14
        + [{"waste_type": "idle_nat_gateway", "region": "us-east-1",
            "estimated_monthly_savings": NAT_GATEWAY_PER_MONTH}] * 3
        + [{"waste_type": "rds_idle_no_connections", "region": "eu-west-1",
            "estimated_monthly_savings": round(rds, 2)}]
    )


def test_the_readme_scan_example_is_the_scans_own_output():
    findings = _findings()
    total = sum(f["estimated_monthly_savings"] for f in findings)
    assert round(total, 2) == 658.95      # 350.40 + 14 x 15.00 + 3 x 32.85
    out = io.StringIO()
    cli_scan._render(out, None, {"total_estimated_monthly_savings": total,
                                 "findings": findings}, demo=False, ce_denied=False)
    printed = [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in out.getvalue().splitlines()]
    example = _readme_example()
    assert example, "the README example block was not found"
    for line in example:
        assert line in printed, (line, printed)


def test_the_readme_region_counts_are_the_findings():
    text = README.read_text(encoding="utf-8")
    counts = {r: int(n) for r, n in re.findall(r"^  ([a-z]{2}-[a-z]+-\d) \.+ (\d+) finding",
                                                 text, re.MULTILINE)}
    by_region: dict[str, int] = {}
    for f in _findings():
        by_region[f["region"]] = by_region.get(f["region"], 0) + 1
    assert counts == by_region
