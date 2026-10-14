"""The same kind of change gets the same verdict whichever tool makes it.

- `terraform apply` in production asked; `pulumi up --stack prod`, `cdk
  deploy`, `sam deploy`, `helm rollback` and `kubectl set image|patch|rollout
  undo` in production did not (the MCP side already counted kubectl patch
  and rollout).
- `eksctl create cluster --node-type p4d.24xlarge --nodes 4` (about $64k a
  month) was not even classified, while `aws eks create-nodegroup` with the
  same nodes asked with a figure.
- With infra_apply left out of FINOPS_POLICY_ALLOWED_ACTIONS a $8/mo
  t3.micro launch was denied and `terraform apply` let through.
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


@pytest.mark.parametrize("prod,staging", [
    ("pulumi up --yes --stack prod", "pulumi up --yes --stack staging"),
    ("npx cdk deploy app-prod --require-approval never",
     "npx cdk deploy app-staging --require-approval never"),
    ("sam deploy --config-env prod --no-confirm-changeset",
     "sam deploy --config-env staging --no-confirm-changeset"),
    ("helm rollback api 3 -n prod", "helm rollback api 3 -n staging"),
    ("kubectl set image deploy/api api=repo/api:v2 -n prod",
     "kubectl set image deploy/api api=repo/api:v2 -n staging"),
    ("kubectl rollout undo deploy/api -n prod", "kubectl rollout undo deploy/api -n staging"),
    ("kubectl patch deploy api -n prod -p '{\"spec\":{\"replicas\":0}}'",
     "kubectl patch deploy api -n staging -p '{\"spec\":{\"replicas\":0}}'"),
])
def test_a_production_change_asks_whichever_tool_makes_it(prod, staging):
    v = g.gate_command(prod, record=False)
    assert v is not None and v["decision"] == "ask" and "PRODUCTION" in v["reason"], prod
    assert g.gate_command(staging, record=False) is None, staging


@pytest.mark.parametrize("cmd", [
    "kubectl rollout status deploy/api -n prod",
    "kubectl rollout history deploy/api -n prod",
    "kubectl config set-context prod --namespace api",
    "pulumi stack select prod",
])
def test_reading_production_stays_silent(cmd):
    assert g.gate_command(cmd, record=False) is None, cmd


def test_an_eksctl_cluster_is_priced_with_its_control_plane():
    cmd = "eksctl create cluster --name train --node-type p4d.24xlarge --nodes 4"
    est = g.estimate_command_monthly_cost(cmd)
    # p4d.24xlarge: AWS Price List SKU H7NGEAC6UEHNTKSJ, $21.957642/hr;
    # EKS control plane: SKU ZYWMR684YSMFHWEU, $0.10/hr.
    assert est["monthly_usd"] == pytest.approx((4 * 21.957642 + 0.10) * H, abs=0.01)
    assert "control plane" in est["line"]
    v = g.gate_command(cmd, record=False)
    assert v["decision"] == "ask" and v.get("rule") == "threshold"


def test_an_eksctl_node_group_without_nodes_is_eksctls_default_two():
    est = g.estimate_command_monthly_cost(
        "eksctl create nodegroup --cluster dev --name gpu -t g5.12xlarge -M 10")
    assert est["count"] == 2 and est["monthly_usd"] == pytest.approx(2 * 5.672 * H, abs=0.01)
    assert "starts 2 nodes" in est["basis"] and "scale to 10" in est["basis"]


@pytest.mark.parametrize("cmd", ["eksctl create cluster -f cluster.yaml",
                                 "eksctl create cluster --name x --without-nodegroup",
                                 "eksctl create cluster --name x"])
def test_an_eksctl_create_the_command_does_not_size_has_no_figure(cmd):
    assert g.estimate_command_monthly_cost(cmd) is None
    assert g.classify_command(cmd) == ("two_way", "infra_apply")


@pytest.mark.parametrize("cmd", ["terraform apply -auto-approve", "kubectl apply -f k8s/",
                                 "helm upgrade --install api ./chart",
                                 "aws ec2 run-instances --instance-type t3.micro"])
def test_the_allowed_actions_apply_priced_or_not(monkeypatch, cmd):
    monkeypatch.setenv("FINOPS_POLICY_ALLOWED_ACTIONS", "stop_idle,tag_fix")
    v = g.gate_command(cmd, record=False)
    assert v is not None and v["decision"] == "deny", cmd
    assert "FINOPS_POLICY_ALLOWED_ACTIONS" in v["reason"]
    assert "not in your allowlist of permitted actions" in v["reason"]


def test_a_one_way_door_still_asks_under_an_allowlist(monkeypatch):
    monkeypatch.setenv("FINOPS_POLICY_ALLOWED_ACTIONS", "stop_idle")
    v = g.gate_command("terraform destroy", record=False)
    assert v["decision"] == "ask" and v["door"] == "one_way"
