"""A priced flag set twice to different values gets no figure.

The guard used to read the first `--instance-type` and the first
`--commitment`; argparse, which the AWS CLI is built on, keeps the last. So
`create-capacity-reservation --instance-type t3.micro --instance-type
p5.48xlarge --instance-count 8` was priced as eight t3.micros (about $729)
for what is about $3.86M over its term, `create-savings-plan --commitment 0.01
--commitment 250` read as $7 a month for $250 an hour, and the same
reservation without a term, or a launch written the same way, passed with no
ask. The guard does not rely on which value the CLI keeps: a command that sets
one flag to two values shows no figure, says why, and asks.
"""
from __future__ import annotations

import pytest

import finops.guard as g


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD", "FINOPS_POLICY_FILE",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS"):
        monkeypatch.delenv(var, raising=False)


CR = ("aws ec2 create-capacity-reservation --instance-type t3.micro --instance-type p5.48xlarge "
      "--instance-platform Linux/UNIX --availability-zone us-east-1a --instance-count 8")


def test_a_term_reservation_with_two_instance_types_has_no_figure_and_says_why():
    v = g.gate_command(f"{CR} --commitment-duration 31536000", record=False)
    assert v["decision"] == "ask"
    assert "t3.micro" not in (v.get("estimate") or {}).get("line", "")
    assert "$729" not in v["reason"], v["reason"]
    assert "sets --instance-type more than once (t3.micro, then p5.48xlarge)" in v["reason"]
    assert g.estimate_command_monthly_cost(f"{CR} --commitment-duration 31536000") is None


def test_a_savings_plan_with_two_commitments_has_no_figure():
    cmd = ("aws savingsplans create-savings-plan --savings-plan-offering-id o-1 "
           "--commitment 0.01 --commitment 250")
    v = g.gate_command(cmd, record=False)
    assert v["decision"] == "ask"
    assert "$7/mo" not in v["reason"], v["reason"]
    assert "sets --commitment more than once (0.01, then 250)" in v["reason"], v["reason"]


def test_a_reservation_without_a_term_written_that_way_asks_instead_of_passing():
    v = g.gate_command(CR, record=False)
    assert v["decision"] == "ask", v
    assert "sets --instance-type more than once" in v["reason"]


def test_a_launch_written_that_way_asks_instead_of_passing_at_the_first_value():
    v = g.gate_command("aws ec2 run-instances --instance-type t3.micro "
                       "--instance-type p4d.24xlarge --count 8", record=False)
    assert v["decision"] == "ask", v
    assert "sets --instance-type more than once (t3.micro, then p4d.24xlarge)" in v["reason"]
    assert "$8/mo" not in v["reason"]


def test_the_same_value_twice_is_still_priced():
    one = g.estimate_command_monthly_cost("aws ec2 run-instances --instance-type m5.large")
    twice = g.estimate_command_monthly_cost(
        "aws ec2 run-instances --instance-type m5.large --instance-type m5.large")
    assert one is not None and twice is not None
    assert twice["monthly_usd"] == one["monthly_usd"]


def test_a_repeat_in_one_command_does_not_unprice_another():
    est = g.estimate_command_monthly_cost(
        "aws ec2 run-instances --instance-type m5.large && "
        "aws ec2 run-instances --instance-type t3.micro --instance-type p4d.24xlarge")
    assert est is not None and est.get("unpriced_changes") == 1
    assert est["instance_type"] == "m5.large"


def test_an_unpriced_command_with_a_repeated_flag_is_unaffected():
    v = g.gate_command("aws s3 ls --region us-east-1 --region eu-west-1", record=False)
    assert v is None or v["decision"] == "allow"
