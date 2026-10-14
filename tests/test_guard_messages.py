"""What the person reads when the guard asks: right, specific, and readable.

- A sentence after a period started in lower case ("...). a $2.50/hr Savings
  Plan", "amounts to `...`. this mutates").
- A production ask did not say what looked like production, nor how to
  change what counts.
- `kubectl drain` said "It cannot be undone": `kubectl uncordon` reopens the
  node; what does not come back is emptyDir data and pods with nowhere to go.
- terragrunt's destroy, an account closure and an AMI deregistration all
  read "would delete cloud resources".
"""
from __future__ import annotations

import pytest

import finops.guard as g


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FINOPS_POLICY_FILE", str(tmp_path / "none.yaml"))


def _reason(cmd: str) -> str:
    return g.gate_command(cmd, record=False)["reason"]


def test_a_commitment_figure_starts_its_own_sentence():
    r = _reason("aws savingsplans create-savings-plan --savings-plan-offering-id x "
                "--commitment 2.5")
    assert "--commitment 2.5`). A $2.50/hr Savings Plan commitment is $1,825/mo" in r


def test_an_mcp_ask_reads_as_sentences():
    v = g.gate_mcp_call("mcp__kubernetes__kubectl_scale",
                        {"name": "api", "resourceType": "deployment", "replicas": 0,
                         "namespace": "prod"}, record=False)
    assert "--replicas=0 -n prod`. This mutates infrastructure" in v["reason"]


def test_a_production_ask_names_what_looked_like_production():
    r = _reason("kubectl apply -f k8s/ -n production")
    assert "PRODUCTION context (`production`)" in r
    assert "FINOPS_GUARD_PROD_PATTERNS" in r


def test_a_drain_says_what_it_costs_not_that_it_is_final():
    r = _reason("kubectl drain ip-10-0-1-23.ec2.internal --ignore-daemonsets")
    assert "cannot be undone" not in r
    assert "kubectl uncordon" in r and "emptyDir" in r


@pytest.mark.parametrize("cmd,says", [
    ("terragrunt run-all destroy", "This would destroy infrastructure"),
    ("aws organizations close-account --account-id 123456789012",
     "This would close an AWS account"),
    ("aws ec2 deregister-image --image-id ami-1", "This would deregister an image"),
])
def test_a_one_way_door_says_what_it_does(cmd, says):
    assert says in _reason(cmd)
