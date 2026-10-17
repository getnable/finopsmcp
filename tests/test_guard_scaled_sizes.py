"""A size the price tables do not list, in a family they do, is priced.

`aws ec2 run-instances --instance-type c6i.32xlarge --count 4` is about
$16,000 a month, and it passed in silence while `c6i.8xlarge` asked: the
table stops at 8xlarge and a type it does not hold had no figure. AWS prices
the sizes of a family by vCPU, so the guard scales the family's rate. The
expected rates are the AWS Price List's (AmazonEC2 and AmazonRDS us-east-1
offer files, 2026-10-12 and 2026-10-06), SKU beside each.
"""
from __future__ import annotations

import pytest

import finops.guard as g

H = 730.0


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FINOPS_POLICY_FILE", str(tmp_path / "none.yaml"))


@pytest.mark.parametrize("cmd,hourly,sku", [
    ("aws ec2 run-instances --instance-type c6i.32xlarge --count 4 --image-id ami-1",
     4 * 5.44, "WES4FF4FTRGSBS7W"),
    ("aws ec2 run-instances --instance-type m7i.48xlarge --image-id ami-1", 9.6768,
     "BQE6XQ9SJ9ZWTHDX"),
    ("aws ec2 run-instances --instance-type r6i.16xlarge --count 2 --image-id ami-1",
     2 * 4.032, "J4EBNS2ZR7N8TB8T"),
    (("aws rds create-db-instance --db-instance-identifier big --db-instance-class "
     "db.r6g.8xlarge --engine mysql"), 3.437, "JEUU8MQD5B9QGEDC"),
    (("aws rds create-db-instance --db-instance-identifier big --db-instance-class "
     "db.r6i.32xlarge --engine postgres"), 16.0, "HP9TE3NZ64XQ65FH"),
])
def test_an_unlisted_size_is_priced_at_its_list_rate(cmd, hourly, sku):
    est = g.estimate_command_monthly_cost(cmd)
    assert est is not None, sku
    # Scaled from the family's per-size rate: within 1% of the Price List.
    assert est["monthly_usd"] == pytest.approx(hourly * H, rel=0.01), (sku, est["line"])
    assert "scaled from the" in est["basis"]
    v = g.gate_command(cmd, record=False)
    assert v is not None and v["decision"] == "ask" and v.get("rule") == "threshold"


@pytest.mark.parametrize("itype", ["p5.4xlarge", "g5.16xlarge-not", "m5.metal", "x9z.large"])
def test_what_does_not_scale_by_vcpu_is_not_scaled(itype):
    from finops.aws_prices import EC2_HOURLY
    assert g._scaled(EC2_HOURLY, itype) == (None, "")


def test_a_listed_size_keeps_its_table_rate_and_basis():
    est = g.estimate_command_monthly_cost(
        "aws ec2 run-instances --instance-type m5.4xlarge --image-id ami-1")
    assert est["hourly_usd"] == 0.768 and "scaled" not in est["basis"]
