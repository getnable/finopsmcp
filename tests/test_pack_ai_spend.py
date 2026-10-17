# SPDX-License-Identifier: Apache-2.0
"""packs/ai-spend: the first-party AI spend data pack, end to end.

It validates as `nable pack validate` does, installs into a throwaway data
dir, tightens the guard for GPU launches (and never loosens it), and its
report shows AI spend by vendor, model, feature and customer from what the
existing connectors return (mocked here: nothing reaches the network).
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pytest

import finops.guard as g
import finops.guard_packs as gpk
import finops.guard_plugin as gp
from finops import ai_budget, packs
from finops.packs import install as inst
from finops.packs import reports
from finops.packs.content import tighten
from finops.packs.errors import PackError
from finops.setup_wizard import main
from tests import packs_support
from tests.packs_support import copy_pack, sign_pack

packs_env = packs_support.packs_env
first_party_key = packs_support.first_party_key

REPO = Path(__file__).resolve().parent.parent
PACK = REPO / "packs" / "ai-spend"
PID = "io.github.getnable/ai-spend"

GPU_LAUNCHES = [
    "aws ec2 run-instances --image-id ami-1 --instance-type g4dn.xlarge",
    ("aws sagemaker create-training-job --resource-config InstanceType=ml.p4d.24xlarge,"
     "InstanceCount=1"),
    "gcloud compute instances create x --machine-type g2-standard-4 --zone us-central1-a",
    "gcloud container node-pools create gpu --cluster c --accelerator type=nvidia-l4,count=1",
    "az vm create -g rg -n vm1 --image Ubuntu2204 --size Standard_NC6s_v3",
    "az aks nodepool add -g rg --cluster-name c -n gpu --node-vm-size Standard_ND96asr_v4",
    # the AWS CLI's global options go before the service as often as after
    "aws --region us-east-1 ec2 run-instances --image-id ami-1 --instance-type g4dn.xlarge",
    "aws --profile ml --output json ec2 run-instances --instance-type g4dn.xlarge",
    "aws ec2 run-instances --image-id ami-1 --instance-type trn2u.48xlarge",
]
NOT_GPU = [
    "aws ec2 run-instances --image-id ami-1 --instance-type m5.large",
    "aws ec2 run-instances --image-id ami-1 --instance-type c7g.large",
    "aws ec2 describe-instances --filters Name=instance-type,Values=p5.48xlarge",
    "gcloud compute instances create x --machine-type e2-standard-4",
    "az vm create -g rg -n vm1 --size Standard_D2s_v3",
]


def _cli(capsys, *argv: str) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as ei:
        main(list(argv))
    out = capsys.readouterr()
    return ei.value.code, out.out, out.err


@pytest.fixture
def install(packs_env, first_party_key, tmp_path):
    """Installs a copy of the pack signed with the (test) first-party key, as
    a release is signed with nable's first-party key."""
    def go() -> dict:
        src = copy_pack(PACK, tmp_path / "ai-spend")
        sign_pack(src, first_party_key)
        return inst.install(str(src), yes=True)
    return go


# ── the manifest ──────────────────────────────────────────────────────────────

def test_the_pack_validates_as_nable_pack_validate_does(capsys):
    code, out, _ = _cli(capsys, "pack", "validate", str(PACK))
    assert code == 0, out
    assert out.startswith(f"OK {PID} 1.0.0 (first-party)")
    r = inst.validate_dir(PACK)
    assert r["ok"] and not r["problems"], r
    # The only warning: this unsigned copy cannot be installed as first-party.
    [warning] = r["warnings"]
    assert warning.startswith(f"install will refuse it: {PID} says it is first-party")
    assert r["provides"] == {"policies": 3, "guard_rules": 4, "reports": 1, "skills": 1}
    assert r["code"] == []
    # The least it can ask for: the data its report reads, and a guard that tightens.
    assert r["capabilities"] == {"read_data": ["focus.cost"], "guard": "tighten-only",
                                 "max_autonomy": "L1"}


def test_unsigned_its_first_party_claim_is_refused(packs_env, first_party_key, tmp_path):
    # Shipped unsigned: a release is signed with nable's first-party key.
    assert inst.validate_dir(PACK)["signature"]["status"] == "unsigned"
    with pytest.raises(PackError, match="first-party"):
        inst.install(str(copy_pack(PACK, tmp_path / "unsigned")), yes=True)
    assert not packs.guard_rules()


def test_nothing_in_the_pack_uses_an_em_dash_or_an_exclamation_point():
    for p in sorted(PACK.rglob("*")):
        if p.is_file():
            text = p.read_text(encoding="utf-8")
            assert chr(0x2014) not in text, p
            assert chr(33) not in text, p


# ── install ───────────────────────────────────────────────────────────────────

def test_it_installs_into_a_throwaway_data_dir_and_loads(packs_env, tmp_path, install):
    r = install()
    assert r["status"] == "installed" and r["pack"]["id"] == PID
    assert (packs_env.root / "io.github.getnable" / "ai-spend" / "1.0.0").is_dir()
    assert str(packs_env.root).startswith(str(tmp_path))
    rules = packs.guard_rules()
    assert {x.id for x in rules} == {"ask-aws-gpu-launch", "ask-gcp-gpu-launch",
                                     "ask-azure-gpu-launch", "ask-mcp-gpu-launch"}
    assert all(x.pack == PID and x.verdict == "ask" for x in rules)
    (skill,) = packs.active("skills")
    assert skill.name == "check-ai-budget" and "check_ai_budget" in skill.body
    assert "estimated_next_tokens" in skill.body and "set_ai_budget" in skill.body
    assert {p.id for p in packs.active("policies")} == {
        "ai-feature-tags-missing", "ai-customer-tags-missing", "ai-untagged-spend-large"}


# ── the guard ─────────────────────────────────────────────────────────────────

@pytest.fixture
def guard_machine(packs_env, tmp_path, monkeypatch):
    """A home and a repo for the guard to judge in, as its hook would."""
    home = tmp_path / "home"
    (home / ".finops").mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repo / "src")
    for var in ("FINOPS_DATA_DIR", "FINOPS_PROFILE", "FINOPS_GUARD_STRICT",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_POLICY_MAX_AUTO_USD",
                "CLAUDE_CONFIG_DIR", "CLAUDE_PROJECT_DIR", "FINOPS_GUARD_STOP_ON_BUDGET",
                "FINOPS_POLICY_VELOCITY_CAP_USD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(gp, "_data_root_override", None)
    monkeypatch.setattr(gp, "_user_dir_override", None)
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})
    gpk.invalidate()
    yield packs_env
    gpk.invalidate()


def _verdict(command: str) -> dict | None:
    v = g.gate_command(command, record=False, cwd=os.getcwd())
    return v if v and v["decision"] in ("ask", "deny") else None


def test_a_gpu_launch_under_the_guards_threshold_asks_once_installed(guard_machine, install):
    # Without the pack: a g4dn.xlarge is under the 500 USD auto threshold,
    # and GCP and Azure GPU launches are not priced, so the guard is silent.
    for cmd in GPU_LAUNCHES:
        assert _verdict(cmd) is None, cmd
    install()
    gpk.invalidate()
    for cmd in GPU_LAUNCHES:
        v = _verdict(cmd)
        assert v is not None and v["decision"] == "ask", cmd
        assert any(r.startswith(f"{PID}:ask-") for r in v["pack_rules"]), v
        assert "AI spend pack asks" in v["reason"]
    for cmd in NOT_GPU:
        assert _verdict(cmd) is None, cmd


def test_the_gpu_rules_only_tighten(guard_machine, monkeypatch, install):
    install()
    gpk.invalidate()
    rules = packs.guard_rules()
    launch = "aws ec2 run-instances --instance-type p5.48xlarge"
    assert tighten("allow", rules, command=launch)["verdict"] == "ask"
    assert tighten("ask", rules, command=launch)["verdict"] == "ask"
    assert tighten("deny", rules, command=launch)["verdict"] == "deny"
    assert tighten("allow", rules, command="aws ec2 run-instances --instance-type m5.large"
                   )["verdict"] == "allow"
    # The guard's own ask for a priced launch stays an ask, with its reason.
    v = _verdict(launch)
    assert v["decision"] == "ask" and "p5.48xlarge" in v["reason"]
    # A deny from the org's policy stays a deny: an ask rule never softens it.
    monkeypatch.setenv("FINOPS_POLICY_ALLOWED_ACTIONS", "ticket")
    for cmd in (launch, "aws ec2 run-instances --instance-type g4dn.xlarge"):
        v = _verdict(cmd)
        assert v["decision"] == "deny", cmd
        assert v["pack_rules"] == [f"{PID}:ask-aws-gpu-launch"]
        assert "not in your allowlist of permitted actions" in v["reason"]


def test_an_mcp_launch_with_a_gpu_type_asks(guard_machine, install):
    install()
    gpk.invalidate()
    v = g.gate_mcp_call("mcp__cloud__run_instances", {"InstanceType": "p4d.24xlarge"},
                        record=False)
    assert v["decision"] == "ask" and f"{PID}:ask-mcp-gpu-launch" in v["pack_rules"]
    assert g.gate_mcp_call("mcp__cloud__get_pricing", {"InstanceType": "p4d.24xlarge"},
                           record=False) is None


# ── the policy ────────────────────────────────────────────────────────────────

def _finding(**kw) -> dict:
    base = {"type": "ai_attribution", "period_days": 30, "total_usd": 2000.0,
            "feature_tagged_pct": 90.0, "customer_tagged_pct": 90.0,
            "untagged_feature_usd": 200.0, "untagged_customer_usd": 200.0,
            "tag_sources": ["litellm"]}
    return {**base, **kw}


def test_the_attribution_policy_flags_untagged_spend_and_never_guesses(packs_env, install):
    install()
    rules = {r.id: r for r in packs.active("policies")}
    assert all(r.evaluate(_finding()) is None for r in rules.values())
    hit = rules["ai-feature-tags-missing"].evaluate(
        _finding(feature_tagged_pct=40.0, untagged_feature_usd=1200.0))
    assert hit["action"] == "flag" and "Only 40.0% of 2000.0 USD" in hit["message"]
    hit = rules["ai-customer-tags-missing"].evaluate(_finding(customer_tagged_pct=0.0))
    assert hit and "customer:<id>" in hit["message"]
    big = rules["ai-untagged-spend-large"].evaluate(_finding(untagged_feature_usd=9000.0))
    assert big["action"] == "escalate" and big["severity"] == "high"
    # Small spend is not worth a flag; a share nobody could compute is not a pass.
    assert rules["ai-feature-tags-missing"].evaluate(
        _finding(total_usd=50.0, feature_tagged_pct=0.0)) is None
    assert rules["ai-feature-tags-missing"].evaluate(
        _finding(feature_tagged_pct=None)) is None


# ── the report ────────────────────────────────────────────────────────────────

LLM = {"total_usd": 2000.0,
       "by_provider": {"openai": 1200.0, "anthropic": 700.0, "bedrock": 100.0},
       "by_model": {"gpt-4o": 900.0, "claude-sonnet-4-5": 700.0, "gpt-4o-mini": 300.0,
                    "amazon.nova-pro": 100.0}}
TAGS = {"dimension": "tag", "groups": [
    {"group": "feature:search", "provider": "litellm", "source": "logged", "cost_usd": 800.0},
    {"group": "feature:search", "provider": "langfuse", "source": "calc", "cost_usd": 780.0},
    {"group": "feature:chat", "provider": "litellm", "source": "logged", "cost_usd": 400.0},
    {"group": "customer:acme", "provider": "litellm", "source": "logged", "cost_usd": 300.0},
    {"group": "env:prod", "provider": "litellm", "source": "logged", "cost_usd": 1500.0}]}


@pytest.fixture
def ai_data(monkeypatch):
    from finops.connectors import ai_attribution, llm_costs
    calls = []

    def llm(**kw):
        calls.append(("llm", kw))
        return dict(LLM)

    def tags(dimension, **kw):
        calls.append(("tags", dimension, kw))
        return dict(TAGS)

    monkeypatch.setattr(llm_costs, "get_all_llm_costs", llm)
    monkeypatch.setattr(ai_attribution, "get_ai_cost_attribution", tags)
    return calls


def test_the_report_shows_spend_by_vendor_model_feature_and_customer(packs_env, ai_data,
                                                                      capsys, install):
    install()
    code, out, err = _cli(capsys, "pack", "report", PID, "--days", "30")
    assert code == 0, err
    assert out.startswith("# AI spend, ")
    assert "Total: $2,000.00 across 3 vendors and 4 models" in out
    assert "| OpenAI | 1,200.00 | 60.0% |" in out
    assert "| AWS Bedrock | 100.00 | 5.0% |" in out
    assert "| gpt-4o | 900.00 | 45.0% |" in out
    # LiteLLM and Langfuse both saw search: the larger, never the sum.
    assert "| search | 800.00 | 40.0% |" in out and "| chat | 400.00 | 20.0% |" in out
    assert "| (no feature tag) | 800.00 | 40.0% |" in out
    assert "| acme | 300.00 | 15.0% |" in out and "| (no customer tag) | 1,700.00 |" in out
    assert "env:prod" not in out and "prod |" not in out
    assert "60% of this spend carries a feature tag and 15% a customer tag" in out
    # The pack's own policy, evaluated against the same numbers.
    assert "rule ai-feature-tags-missing" in out and "rule ai-customer-tags-missing" in out
    assert "Only 15.0% of 2000.0 USD of AI spend carries a customer tag" in out
    assert "${" not in out
    assert [c[0] for c in ai_data] == ["llm", "tags"] and ai_data[1][1] == "tag"
    code, out, _ = _cli(capsys, "pack", "report", PID, "ai-spend", "--json")
    body = json.loads(out)
    assert code == 0 and body["sources"] == ["ai"] and body["report"] == "reports/ai-spend.md"
    assert "| Anthropic | 700.00 | 35.0% |" in body["text"]


def test_the_report_says_what_it_could_not_read(packs_env, monkeypatch, capsys, install):
    from finops.connectors import ai_attribution, llm_costs
    monkeypatch.setattr(llm_costs, "get_all_llm_costs", lambda **kw: {
        "total_usd": 0.0, "by_provider": {}, "by_model": {},
        "error": "No AI provider is connected, so there is no AI spend to report."})
    monkeypatch.setattr(ai_attribution, "get_ai_cost_attribution",
                        lambda d, **kw: {"groups": [], "error": "nothing connected"})
    install()
    r = reports.render(PID, days=7, today=date(2026, 9, 27))
    text = r["text"]
    assert "# AI spend, 2026-09-20 to 2026-09-27" in text
    assert "No AI spend was read for this period." in text
    assert "unknown of this spend carries a feature tag" in text
    assert "No AI provider is connected" in text and "No feature or customer tags" in text
    assert "No attribution policy flags this period." in text
