"""`nable scan` calls recoverable only what the audit measured.

The deep audit classifies every finding (analyzers/waste_evidence): an
unattached volume or an idle NAT gateway is observed and priced, a
recommendation; a CPU-only "idle" instance or a CPU-only RDS downsize is an
inference, an investigation that says what to check first. run_deep_audit
reports the two totals apart (measured_monthly_savings and
unconfirmed_monthly_opportunity), and `nable brief`, built from the same
findings, totals only the measured ones.

`nable scan` added them into one "$X/mo recoverable". On a two-region
fixture account (unattached io1/gp3/gp2 volumes, an idle NAT gateway and
load balancer, an idle RDS instance, a CPU-only idle m5.large, a CPU-only
db.m5.xlarge downsize, S3 buckets with an "up to" tiering saving) the same
audit printed "$888/mo recoverable" in `nable scan` and "$574/mo across 13
item(s)" in `nable brief`: $313.65 of the scan's figure was findings the
audit itself files as unconfirmed, the "idle EC2 instance" among them
("Low CPU is not idle", its own why_unsure says).
"""
from __future__ import annotations

import io
import re

import pytest

from finops import cli_scan
from finops.analyzers.waste_evidence import annotate


def _findings() -> list[dict]:
    return annotate([
        # Observed state, priced at list: recommendations.
        {"waste_type": "unattached_ebs_volume", "resource_id": "vol-0io1",
         "resource_type": "EBS Volume", "region": "us-east-1",
         "estimated_monthly_savings": 207.50,      # 100 GB io1 + 3,000 IOPS
         "detail": "100 GB io1 volume was unattached at scan time."},
        {"waste_type": "idle_nat_gateway", "resource_id": "nat-0idle",
         "resource_type": "NAT Gateway", "region": "us-east-1",
         "estimated_monthly_savings": 32.85,       # 730 h x $0.045
         "detail": "NAT Gateway nat-0idle averaged 0.002 GB/day."},
        # Inferences: investigations.
        {"waste_type": "idle_ec2_low_cpu", "resource_id": "i-0idle01",
         "resource_type": "EC2 Instance", "region": "us-east-1",
         "estimated_monthly_savings": 70.08,       # m5.large, 730 h x $0.096
         "detail": "EC2 instance i-0idle01 averaged 1.5% CPU."},
        {"waste_type": "rds_overprovisioned", "resource_id": "db-legacy",
         "resource_type": "RDS Instance", "region": "us-east-1",
         "estimated_monthly_savings": 124.83,      # (0.342 - 0.171) x 730
         "detail": "RDS instance db-legacy averaged 8% CPU."},
    ])


def _report() -> dict:
    findings = _findings()
    return {"total_estimated_monthly_savings": round(sum(
        f["estimated_monthly_savings"] for f in findings), 2),
        "findings": findings, "regions_scanned": ["us-east-1"],
        "checks_run": ["ebs", "nat", "ec2", "rds_rightsizing"]}


def _text(report: dict) -> str:
    out = io.StringIO()
    cli_scan._render(out, None, report, demo=False, ce_denied=False)
    return re.sub(r"\x1b\[[0-9;]*m", "", out.getvalue())


def test_the_scan_headline_is_the_measured_total():
    report = _report()
    assert report["total_estimated_monthly_savings"] == 435.26
    text = _text(report)
    # 207.50 + 32.85 measured; 70.08 + 124.83 to confirm.
    assert "$240/mo recoverable" in text, text
    assert "$435/mo recoverable" not in text, text
    assert "$195/mo to confirm" in text, text


def test_a_row_that_is_an_inference_says_so():
    text = _text(_report())
    rds = next(ln for ln in text.splitlines() if "RDS" in ln)
    ec2 = next(ln for ln in text.splitlines() if "EC2" in ln)
    assert "to confirm" in rds and "to confirm" in ec2, (rds, ec2)
    ebs = next(ln for ln in text.splitlines() if "EBS" in ln)
    assert "to confirm" not in ebs, ebs


def test_the_json_recoverable_is_the_measured_total():
    doc = cli_scan._json_payload(None, _report(), demo=False, profile=None,
                                 account_id="111122223333", duration_s=1.0)
    rec = doc["recoverable"]
    assert rec["monthly_usd"] == pytest.approx(240.35)
    assert rec["annual_usd"] == pytest.approx(2884.20)
    assert rec["to_confirm_monthly_usd"] == pytest.approx(194.91)
    assert rec["to_confirm_findings"] == 2


def test_scan_and_brief_agree_on_the_same_findings():
    from finops.briefing.brief import build_brief

    findings = _findings()
    brief = build_brief([dict(f) for f in findings], use_llm=False,
                        prober=lambda *a, **k: None)
    doc = cli_scan._json_payload(None, _report(), demo=False, profile=None,
                                 account_id="111122223333", duration_s=1.0)
    assert brief.total_monthly_usd == pytest.approx(doc["recoverable"]["monthly_usd"])
