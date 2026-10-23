"""An EC2 launch is judged in the region the command names.

Every EC2 rate nable holds is us-east-1's. `aws ec2 run-instances --region
sa-east-1 --instance-type c5.4xlarge` was judged at $496/mo, a warn under
the $500 threshold, for an instance the AWS Price List puts at $765/mo there
(SKU S8SHPWRKY2EMQG6C, $1.048/hr). The guard now judges at the least the
region charges over us-east-1 for the types it lists, which stays at or
under the region's real rate, and says the range.
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


@pytest.mark.parametrize("cmd,price_list_hourly,sku", [
    ("aws ec2 run-instances --region sa-east-1 --instance-type c5.4xlarge --image-id ami-1",
     1.048, "S8SHPWRKY2EMQG6C"),
    (("aws --region ap-northeast-1 ec2 run-instances --instance-type p4d.24xlarge "
     "--image-id ami-1"), 30.09784, "CASSTWDHBMWC5PPU"),
    ("AWS_REGION=eu-west-1 aws ec2 run-instances --instance-type m5.4xlarge --image-id ami-1",
     0.856, "HUR96BZEUM9ZQAKA"),
    ("aws ec2 run-instances --region sa-east-1 --instance-type g5.xlarge --image-id ami-1",
     1.71, "VC5HDNW5V38GYUF2"),
])
def test_a_regional_launch_is_judged_nearer_its_price_and_never_above_it(
        cmd, price_list_hourly, sku):
    est = g.estimate_command_monthly_cost(cmd)
    assert est["hourly_usd"] <= price_list_hourly, sku
    from finops.aws_prices import EC2_HOURLY
    assert est["hourly_usd"] > EC2_HOURLY[est["instance_type"]], sku
    assert est["region"] in cmd and "the least" in est["basis"]


def test_the_sao_paulo_launch_now_asks():
    v = g.gate_command("aws ec2 run-instances --region sa-east-1 --instance-type c5.4xlarge "
                       "--image-id ami-1", record=False)
    assert v["decision"] == "ask" and v["monthly_delta_usd"] > 500
    assert "in sa-east-1" in v["reason"]


@pytest.mark.parametrize("region,says", [
    ("ap-south-1", "so this is the us-east-1 figure"),
    ("xx-new-1", "whose rates nable does not hold"),
])
def test_a_region_cheaper_or_unknown_keeps_the_us_east_1_figure(region, says):
    est = g.estimate_command_monthly_cost(
        f"aws ec2 run-instances --region {region} --instance-type m5.4xlarge --image-id ami-1")
    assert est["monthly_usd"] == pytest.approx(0.768 * H, abs=0.01)
    assert says in est["basis"]


def test_no_region_is_us_east_1_as_before():
    for cmd in ("aws ec2 run-instances --instance-type m5.4xlarge --image-id ami-1",
                "aws ec2 run-instances --region us-west-2 --instance-type m5.4xlarge"):
        est = g.estimate_command_monthly_cost(cmd)
        assert "region" not in est and "the least" not in est["basis"]


def test_each_command_of_a_line_is_judged_in_the_region_it_names():
    """A --region on one command is not where the next one runs: each is
    judged at its own region's floor, never above that region's rate."""
    est = g.estimate_command_monthly_cost(
        "aws ec2 run-instances --region sa-east-1 --instance-type m5.4xlarge --image-id ami-1 "
        "&& aws ec2 run-instances --region eu-west-1 --instance-type m5.4xlarge "
        "--image-id ami-1")
    assert [p["sku"]["region"] for p in est["parts"]] == ["sa-east-1", "eu-west-1"]
    for p, most in zip(est["parts"], (1.529, 1.069), strict=True):
        assert p["monthly_usd"] == pytest.approx(0.768 * most * H, abs=0.02)


def test_a_region_another_command_names_is_not_this_ones():
    """The review's case: the S3 copy names ap-south-1, the RDS create names
    no region (so runs in the profile's), and was priced and recorded at
    ap-south-1's listed rate, marked exact."""
    alone = g.estimate_command_monthly_cost(
        "aws rds create-db-instance --db-instance-class db.r6g.large --engine postgres "
        "--db-instance-identifier x")
    est = g.estimate_command_monthly_cost(
        "aws s3 cp dump.sql s3://acme-dumps/ --region ap-south-1 && aws rds "
        "create-db-instance --db-instance-class db.r6g.large --engine postgres "
        "--db-instance-identifier x")
    assert est["sku"] == alone["sku"]
    assert est["sku"]["region"] is None and est["sku"]["list_exact"] is False


def test_an_exported_region_is_where_the_next_command_runs():
    est = g.estimate_command_monthly_cost(
        "export AWS_REGION=ap-south-1 && aws rds create-db-instance --db-instance-class "
        "db.r6g.large --engine postgres --db-instance-identifier x")
    assert est["sku"]["region"] == "ap-south-1"
