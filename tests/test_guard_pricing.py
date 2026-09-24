"""An "ask" should carry a dollar figure, and every figure states its basis.

The guard's question to a human is only as useful as the number in it. These
tests pin which commands agents actually run get priced, from which table, and
the refusal side just as hard: a command whose price the repo does not know
gets NO figure, never an invented one.
"""
from __future__ import annotations

import pytest

import finops.ai_budget as ai_budget
import finops.guard as g


@pytest.fixture(autouse=True)
def _clean_policy_env(monkeypatch):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS",
                "FINOPS_GUARD_STOP_ON_BUDGET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ai_budget, "status", lambda: {"verdict": ai_budget.BUDGET_OK})


# ── AWS global options must not hide a price ──────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "aws --region us-east-1 ec2 run-instances --instance-type p4d.24xlarge --count 8",
    "aws --output json --no-cli-pager ec2 run-instances --instance-type p4d.24xlarge --count 8",
    'aws ec2 run-instances --instance-type "p4d.24xlarge" --count 8',
])
def test_a_launch_with_global_options_is_still_priced(cmd):
    """The classifier stripped global options and the pricer did not, so these
    classified as a launch, found no price, and passed silently at ~$191k/mo."""
    v = g.gate_command(cmd)
    assert v is not None and v["decision"] == "ask", f"{cmd!r} passed silently"
    assert "$191,377" in v["reason"]
