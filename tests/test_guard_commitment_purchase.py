# SPDX-License-Identifier: Apache-2.0
"""The guard asks before any commitment purchase an agent tries.

A reservation or a Savings Plan is money committed for one or three years,
and the guard knew four ways to buy one: Savings Plans, EC2 Reserved
Instances, EC2 Dedicated Host reservations and RDS reserved instances. The
same commitment bought for ElastiCache, MemoryDB, Redshift or OpenSearch, an
EC2 Capacity Block, a Scheduled Instance or a Reserved Instance exchange ran
without a word, and so did the same purchase made from a boto3 one-liner.

What has to stay true:
  - every commitment purchase below, each pinned to an operation in the
    installed botocore's service model, asks: through `nable guard check`,
    through the hook, and through the MCP call_aws and use_aws paths
  - a boto3 one-liner that buys one asks too (a heuristic, like the one
    for deletes), and one that only reads does not
  - nothing makes a commitment purchase weaker than an ask: not a policy
    (escalate_one_way_doors false, a high max_auto_monthly_usd, the
    allowed-actions list), not a pack rule, not a learned threshold, not a
    one-time approval; a pack's deny still denies
  - a figure is shown only where the command carries one (the Savings Plan
    --commitment, the EC2 --limit-price); otherwise the ask says the
    offering id fixes the price and shows none
"""
from __future__ import annotations

import io
import json
import re

import botocore.session
import pytest
from botocore import xform_name

import finops.guard as g
from finops import ai_budget, guard_adapters, guard_approvals, org, policy
from finops.org.cli import _who as human

# (command, service, operation): the purchases the guard learned here.
NEW = [
    (("aws elasticache purchase-reserved-cache-nodes-offering --reserved-cache-nodes-offering-id "
      "o-1 --cache-node-count 2"), "elasticache", "PurchaseReservedCacheNodesOffering"),
    (("aws memorydb purchase-reserved-nodes-offering --reserved-nodes-offering-id o-1 "
      "--node-count 2"), "memorydb", "PurchaseReservedNodesOffering"),
    (("aws redshift purchase-reserved-node-offering --reserved-node-offering-id o-1 "
      "--node-count 2"), "redshift", "PurchaseReservedNodeOffering"),
    (("aws opensearch purchase-reserved-instance-offering --reserved-instance-offering-id o-1 "
      "--reservation-name r --instance-count 3"), "opensearch", "PurchaseReservedInstanceOffering"),
    (("aws es purchase-reserved-elasticsearch-instance-offering "
      "--reserved-elasticsearch-instance-offering-id o-1 --reservation-name r --instance-count 3"),
     "es", "PurchaseReservedElasticsearchInstanceOffering"),
    ("aws ec2 purchase-scheduled-instances --purchase-requests PurchaseToken=t,InstanceCount=1",
     "ec2", "PurchaseScheduledInstances"),
    (("aws ec2 purchase-capacity-block --capacity-block-offering-id cbr-1 "
      "--instance-platform Linux/UNIX"), "ec2", "PurchaseCapacityBlock"),
    (("aws ec2 purchase-capacity-block-extension --capacity-block-extension-offering-id cbe-1 "
      "--capacity-reservation-id cr-1"), "ec2", "PurchaseCapacityBlockExtension"),
    (("aws ec2 accept-reserved-instances-exchange-quote --reserved-instance-ids ri-1 "
      "--target-configurations OfferingId=o-1"), "ec2", "AcceptReservedInstancesExchangeQuote"),
]
# The four the guard already asked about.
EXISTING = [
    ("aws savingsplans create-savings-plan --savings-plan-offering-id o-1 --commitment 2.5",
     "savingsplans", "CreateSavingsPlan"),
    (("aws ec2 purchase-reserved-instances-offering --reserved-instances-offering-id o-1 "
      "--instance-count 2"), "ec2", "PurchaseReservedInstancesOffering"),
    ("aws ec2 purchase-host-reservation --offering-id hro-1 --host-id-set h-1", "ec2",
     "PurchaseHostReservation"),
    (("aws rds purchase-reserved-db-instances-offering --reserved-db-instances-offering-id o-1 "
      "--db-instance-count 2"), "rds", "PurchaseReservedDBInstancesOffering"),
]
ALL = NEW + EXISTING
IDS = [op for _, _, op in ALL]
SP_BUY = EXISTING[0][0]
RDS_BUY = EXISTING[3][0]

BOTO3_BUYS = [
    ("python3 -c \"import boto3; boto3.client('rds').purchase_reserved_db_instances_offering("
     "ReservedDBInstancesOfferingId='o-1', DBInstanceCount=1)\""),
    ("python -c \"import boto3; c = boto3.client('ec2'); c.purchase_capacity_block("
     "CapacityBlockOfferingId='cbr-1', InstancePlatform='Linux/UNIX')\""),
    ("python3 -c \"import boto3; boto3.client('ec2').accept_reserved_instances_exchange_quote("
     "ReservedInstanceIds=['ri-1'])\""),
    ("python3 -c \"import boto3; boto3.client('savingsplans').create_savings_plan("
     "savingsPlanOfferingId='o-1', commitment='5')\""),
    ("python3.12 -c 'import boto3; boto3.client(\"elasticache\")"
     ".purchase_reserved_cache_nodes_offering(ReservedCacheNodesOfferingId=\"o-1\")'"),
    ("python3 -c \"import boto3; boto3.client('ec2').purchase_capacity_block_extension("
     "CapacityBlockExtensionOfferingId='x', CapacityReservationId='cr-1')\""),
]


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS",
                "FINOPS_GUARD_STOP_ON_BUDGET", "FINOPS_POLICY_VELOCITY_CAP_USD",
                "FINOPS_POLICY_LOOP_COUNT", "FINOPS_GUARD_TEAM", "FINOPS_GUARD_ACCOUNT",
                "FINOPS_POLICY_ON_BUDGET_BREACH", "FINOPS_POLICY_FILE",
                *guard_adapters._COPILOT_CLOUD_ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FINOPS_ACCOUNTS_FILE", str(tmp_path / "no-accounts.yaml"))
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})


@pytest.fixture
def work(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return d


# ── every purchase is a real operation ────────────────────────────────────────

@pytest.mark.parametrize("cmd,service,operation", ALL, ids=IDS)
def test_each_purchase_is_an_operation_in_botocore(cmd, service, operation):
    model = botocore.session.get_session().get_service_model(service)
    assert operation in model.operation_names
    verb = cmd.split()[2]
    assert xform_name(operation, "-") == verb
    assert cmd.split()[1] == service


# ── it asks, by every door ────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd,service,operation", ALL, ids=IDS)
def test_classified_as_a_commitment_purchase(cmd, service, operation):
    assert g.classify_command(cmd) == ("one_way", "purchase_commitment")
    assert g.classify_command(cmd.replace("aws ", "aws --profile prod --region eu-west-1 ", 1)) \
        == ("one_way", "purchase_commitment")


def _guard_check(capsys, cmd) -> dict:
    from finops import setup_wizard
    try:
        setup_wizard.main(["guard", "check", "--json", "--command", cmd])
    except SystemExit as e:
        assert e.code in (0, None)
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("cmd,service,operation", ALL, ids=IDS)
def test_guard_check_asks(capsys, cmd, service, operation):
    doc = _guard_check(capsys, cmd)
    assert doc["decision"] == "ask" and doc["action_type"] == "purchase_commitment"
    assert "This would buy a commitment" in doc["reason"]


@pytest.mark.parametrize("cmd,service,operation", ALL, ids=IDS)
def test_the_hook_asks(cmd, service, operation):
    out = io.StringIO()
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    assert g.run_hook(stdin=io.StringIO(json.dumps(payload)), stdout=out) == 0
    d = json.loads(out.getvalue())["hookSpecificOutput"]
    assert d["permissionDecision"] == "ask"
    assert "buy a commitment" in d["permissionDecisionReason"]


@pytest.mark.parametrize("cmd,service,operation", ALL, ids=IDS)
def test_mcp_call_aws_asks(cmd, service, operation):
    v = g.gate_mcp_call("mcp__aws-api__call_aws", {"cli_command": cmd}, record=False)
    assert v["decision"] == "ask" and v["action_type"] == "purchase_commitment"


_USE_AWS_PARAMS = {
    "PurchaseReservedCacheNodesOffering": {"ReservedCacheNodesOfferingId": "o-1",
                                           "CacheNodeCount": 2},
    "PurchaseReservedDBInstancesOffering": {"ReservedDBInstancesOfferingId": "o-1",
                                            "DBInstanceCount": 3},
    "CreateSavingsPlan": {"savingsPlanOfferingId": "o-1", "commitment": "2.5"},
}


@pytest.mark.parametrize("cmd,service,operation", ALL, ids=IDS)
def test_mcp_use_aws_asks(cmd, service, operation):
    args = {"service_name": service, "operation_name": operation,
            "parameters": _USE_AWS_PARAMS.get(operation, {"OfferingId": "o-1"}),
            "region": "us-east-1"}
    v = g.gate_mcp_call("mcp__q__use_aws", args, record=False)
    assert v["decision"] == "ask" and v["action_type"] == "purchase_commitment"
    # The kebab-case operation name works too.
    args["operation_name"] = xform_name(operation, "-")
    assert g.gate_mcp_call("mcp__q__use_aws", args, record=False)["decision"] == "ask"


@pytest.mark.parametrize("cmd", BOTO3_BUYS)
def test_a_boto3_one_liner_that_buys_asks(cmd):
    assert g.classify_command(cmd) == ("one_way", "purchase_commitment")
    v = g.gate_command(cmd, record=False)
    assert v["decision"] == "ask" and v["action_type"] == "purchase_commitment"


@pytest.mark.parametrize("cmd", [
    "python3 -c \"import boto3; print(boto3.client('rds').describe_reserved_db_instances())\"",
    "python3 -c \"print('purchase_reserved_db_instances_offering')\"",
    "grep -rn purchase_reserved_instances_offering src/",
    "git commit -m 'guard: ask before aws rds purchase-reserved-db-instances-offering'",
])
def test_reading_or_naming_a_purchase_does_not_ask(cmd):
    assert g.gate_command(cmd, record=False) is None


def test_an_ec2_dry_run_buys_nothing():
    assert g.gate_command("aws ec2 purchase-capacity-block --capacity-block-offering-id c "
                          "--instance-platform Linux/UNIX --dry-run", record=False) is None


# ── never weaker than ask ─────────────────────────────────────────────────────

_LOOSE = {"escalate_one_way_doors": False, "max_auto_monthly_usd": 1e12,
          "allowed_action_types": [*policy.TWO_WAY_DOORS, "purchase_commitment"]}


def test_the_policy_gate_never_allows_a_commitment_purchase():
    for delta in (0.0, 5.0, -1000.0):
        gate = policy.evaluate_action_gate("purchase_commitment", delta, policy=dict(_LOOSE))
        assert gate["gate"] == policy.GATE_ESCALATE, gate
    boost = {"verdict": "boost", "act_rate": 1.0, "accuracy": 1.0, "coverage": 1.0, "why": "x"}
    gate = policy.evaluate_action_gate("purchase_commitment", 0.0, policy=dict(_LOOSE),
                                       signal=boost)
    assert gate["gate"] == policy.GATE_ESCALATE
    # A deny the policy gives stays a deny.
    gate = policy.evaluate_action_gate("purchase_commitment", 0.0,
                                       policy={**_LOOSE, "allowed_action_types": []})
    assert gate["gate"] == policy.GATE_BLOCK
    # Other one-way doors are the policy's to open, as before.
    opened = {**_LOOSE, "allowed_action_types": [*_LOOSE["allowed_action_types"], "release_ip"]}
    assert policy.evaluate_action_gate("release_ip", 0.0, policy=opened)["gate"] \
        == policy.GATE_ALLOW


@pytest.fixture
def loose_policy(monkeypatch):
    """The loosest policy a person could write: one-way doors not escalated,
    commitment purchases on the allowed-actions list, a huge threshold."""
    monkeypatch.setitem(policy.DEFAULT_POLICY, "escalate_one_way_doors", False)
    monkeypatch.setenv("FINOPS_POLICY_MAX_AUTO_USD", "1000000000")
    monkeypatch.setenv("FINOPS_POLICY_VELOCITY_CAP_USD", "0")
    monkeypatch.setenv("FINOPS_POLICY_ALLOWED_ACTIONS",
                       ",".join([*policy.TWO_WAY_DOORS, "purchase_commitment"]))


@pytest.mark.parametrize("cmd,service,operation", ALL, ids=IDS)
def test_no_policy_turns_a_purchase_into_an_allow(loose_policy, cmd, service, operation):
    v = g.gate_command(cmd, record=False)
    assert v is not None and v["decision"] == "ask", v
    v = g.gate_mcp_call("mcp__aws-api__call_aws", {"cli_command": cmd}, record=False)
    assert v is not None and v["decision"] == "ask", v


def test_the_allowed_actions_list_alone_does_not_allow_one(monkeypatch):
    monkeypatch.setenv("FINOPS_POLICY_ALLOWED_ACTIONS", "purchase_commitment")
    monkeypatch.setenv("FINOPS_POLICY_MAX_AUTO_USD", "1000000000")
    assert g.gate_command(SP_BUY, record=False)["decision"] == "ask"


def test_a_learned_threshold_does_not_allow_one(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "infra").mkdir()
    org.set_fact(org.make_fact("owner", "repo_path:repo//infra", {"team": "payments"},
                               source="human"), human("maria"))
    org.set_fact(org.make_fact("threshold", "team:payments",
                               {"max_auto_monthly_usd": 1e9, "velocity_cap_usd": 1e9},
                               source="human"), human("maria"))
    where = str(repo / "infra")
    # The threshold is in force: a large launch here no longer asks.
    assert g.gate_command("aws ec2 run-instances --instance-type m5.2xlarge --count 40",
                          cwd=where, record=False) is None
    for cmd in (SP_BUY, RDS_BUY):
        assert g.gate_command(cmd, cwd=where, record=False)["decision"] == "ask"


def _pack_rules(monkeypatch, verdict: str):
    from finops import guard_packs
    from finops.packs.content import parse_guard_rules
    problems: list = []
    rule = {"id": "buying", "pattern": r"purchase|create-savings|accept-reserved",
            "verdict": verdict, "reason": "not this quarter"}
    rules = parse_guard_rules({"rules": [rule]}, "g.yaml", problems)
    assert not problems
    monkeypatch.setattr(guard_packs, "state", lambda: {"rules": rules, "guard_problems": []})


@pytest.mark.parametrize("cmd,service,operation", ALL, ids=IDS)
def test_a_pack_deny_still_denies(monkeypatch, cmd, service, operation):
    _pack_rules(monkeypatch, "deny")
    v = g.gate_command(cmd, record=False)
    assert v["decision"] == "deny" and "not this quarter" in v["reason"]


def test_a_pack_ask_leaves_it_an_ask(monkeypatch, loose_policy):
    _pack_rules(monkeypatch, "ask")
    assert g.gate_command(SP_BUY, record=False)["decision"] == "ask"


def test_a_verdict_layer_that_allows_is_overruled(monkeypatch):
    """Belt and braces: if any layer ever let a purchase through (here the
    policy verdict itself is made to allow), the guard still asks, on the
    shell and the MCP paths."""
    monkeypatch.setattr(g, "_policy_verdict", lambda command, hit, **kw: {
        "decision": "allow", "action_type": hit[1], "door": hit[0]})
    for cmd in (SP_BUY, RDS_BUY, BOTO3_BUYS[0]):
        v = g.gate_command(cmd, record=False)
        assert v["decision"] == "ask" and v["action_type"] == "purchase_commitment", cmd
        assert "whatever the policy" in v["reason"]
    v = g.gate_mcp_call("mcp__aws-api__call_aws", {"cli_command": RDS_BUY}, record=False)
    assert v["decision"] == "ask"


def _codex(command: str, cwd) -> dict:
    out, err = io.StringIO(), io.StringIO()
    payload = {"hook_event_name": "PreToolUse", "turn_id": "t1", "session_id": "s1",
               "tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    assert guard_adapters.run_hook(stdin=io.StringIO(json.dumps(payload)), stdout=out,
                                   stderr=err) == 0
    d = json.loads(out.getvalue())["hookSpecificOutput"]
    assert d["permissionDecision"] == "deny"
    return d


def test_a_one_time_approval_is_never_offered_for_a_purchase(work):
    reason = _codex(SP_BUY, work)["permissionDecisionReason"]
    assert "nable guard approve " not in reason
    assert "never approved out of band" in reason
    assert guard_approvals.waiting() == []


def test_a_one_time_approval_never_lets_a_purchase_through(work):
    """Even an approval a person gave for the identical call (made here
    directly in the store) does not turn the purchase into an allow."""
    aid = guard_approvals.pending("shell", SP_BUY, cwd=str(work), harness="codex",
                                  session="s1", tool="Bash", verdict={"reason": "x"})
    guard_approvals.approve(aid, human("maria"))
    _codex(SP_BUY, work)
    from finops import guard_ledger
    records = [json.loads(line) for line in
               guard_ledger.ledger_path().read_text().splitlines()]
    assert records and all(not r.get("approved_out_of_band") for r in records)
    assert records[-1]["decision"] == "ask"


def test_a_purchase_behind_another_one_way_door_is_not_approvable_either(work):
    """Classification stops at the first one-way rule, so this is a terminate;
    the purchase after it still keeps the approval away."""
    cmd = f"aws ec2 terminate-instances --instance-ids i-1 && {SP_BUY}"
    assert g.classify_command(cmd) == ("one_way", "terminate_instance")
    reason = _codex(cmd, work)["permissionDecisionReason"]
    assert "nable guard approve " not in reason and "never approved out of band" in reason
    assert guard_approvals.waiting() == []


def test_other_asks_are_still_approvable(work):
    reason = _codex("terraform destroy", work)["permissionDecisionReason"]
    assert "nable guard approve " in reason


# ── figures only where the command carries them ───────────────────────────────

def test_a_savings_plan_shows_its_commitment():
    v = g.gate_command(SP_BUY, record=False)
    assert "$2.50/hr Savings Plan commitment is $1,825/mo" in v["reason"]
    v = g.gate_mcp_call("mcp__q__use_aws", {"service_name": "savingsplans",
                                            "operation_name": "CreateSavingsPlan",
                                            "parameters": _USE_AWS_PARAMS["CreateSavingsPlan"]},
                        record=False)
    assert "$2.50/hr Savings Plan commitment" in v["reason"]


def test_an_ec2_reservation_shows_its_limit_price():
    v = g.gate_command("aws ec2 purchase-reserved-instances-offering --reserved-instances-"
                       "offering-id o-1 --instance-count 2 --limit-price "
                       "Amount=1200,CurrencyCode=USD", record=False)
    assert "2 Reserved Instances, the order capped at $1,200" in v["reason"]


_NO_FIGURE = "the offering id fixes the price, which the guard cannot look up offline"


@pytest.mark.parametrize("cmd,count", [
    (RDS_BUY, "2 DB instances"),
    (NEW[0][0], "2 cache nodes"),
    (NEW[1][0], "2 nodes"),
    (NEW[2][0], "2 nodes"),
    (NEW[3][0], "3 instances"),
    (NEW[4][0], "3 instances"),
    (EXISTING[1][0], "2 instances"),
    (NEW[6][0], None),
    (NEW[8][0], None),
    (EXISTING[2][0], None),
    (BOTO3_BUYS[0], None),
])
def test_without_a_price_in_the_command_there_is_no_figure(cmd, count):
    v = g.gate_command(cmd, record=False)
    assert v["decision"] == "ask"
    assert "$" not in v["reason"], v["reason"]
    assert "monthly_delta_usd" not in v and "estimate" not in v
    assert _NO_FIGURE in v["reason"]
    if count:
        assert re.search(rf"\b{count}\b", v["reason"]), v["reason"]


def test_the_rds_count_reaches_the_ask_through_use_aws():
    v = g.gate_mcp_call("mcp__q__use_aws", {
        "service_name": "rds", "operation_name": "PurchaseReservedDBInstancesOffering",
        "parameters": _USE_AWS_PARAMS["PurchaseReservedDBInstancesOffering"]}, record=False)
    assert "3 DB instances" in v["reason"] and "$" not in v["reason"]
