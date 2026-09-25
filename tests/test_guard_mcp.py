"""The guard sees infrastructure changes made through MCP tools, not only Bash.

Before this, the hook matcher was "Bash" and run_hook returned early for every
other tool. An agent with the HashiCorp Terraform, AWS API or a Kubernetes MCP
server connected could destroy a workspace, terminate instances or delete a
deployment and the guard was never consulted.

Invariants under test:
  - known infra tools are judged exactly like the shell command they amount
    to: same doors, same prices, same production-context rule
  - an unknown MCP tool gets no verdict of its own, but the AI budget stop
    applies to it: an agent over budget cannot keep spending through tools
    the guard does not otherwise judge
  - a known tool NAME with a foreign argument shape passes through, so an
    unrelated server's `create_run` or `delete_resource` is never asked about
  - the table is data, and every name in it is exercised here
  - the installed matcher covers Bash and MCP and nothing else, and an old
    Bash-only entry is widened in place without touching anyone else's hook
"""
from __future__ import annotations

import io
import json

import pytest

import finops.guard as g
import finops.guard_mcp as gm
from finops import ai_budget

# Figures come from the price table, never typed in: the p4d rate is revised
# when AWS cuts GPU prices, and a test that pins yesterday's rate fails for a
# reason that has nothing to do with the guard.
from finops.connectors.terraform_estimate import _EC2_HOURLY
from finops.policy import door_of

P4D_HOURLY = _EC2_HOURLY["p4d.24xlarge"]
P4D_X8_MONTHLY = 8 * P4D_HOURLY * 730
P4D_X8_TEXT = f"${P4D_X8_MONTHLY:,.0f}"


@pytest.fixture(autouse=True)
def _clean_policy_env(monkeypatch):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS",
                "FINOPS_GUARD_STOP_ON_BUDGET"):
        monkeypatch.delenv(var, raising=False)
    # The budget stop reads the developer's own Claude Code logs; pin it under.
    monkeypatch.setattr(ai_budget, "status", lambda: {"verdict": ai_budget.BUDGET_OK})


# ── the table, case by case ───────────────────────────────────────────────────
# (tool name as Claude Code sends it, arguments, decision or None, action_type)

P4D_X8 = "aws ec2 run-instances --instance-type p4d.24xlarge --count 8"

CASES = [
    # AWS API MCP server: the argument IS a CLI command
    ("mcp__aws-api__call_aws", {"cli_command": "aws ec2 terminate-instances --instance-ids i-1"},
     "ask", "terminate_instance"),
    ("mcp__aws-api__call_aws", {"cli_command": ["aws ec2 describe-instances",
                                                "aws s3 rb s3://prod-data --force"]},
     "ask", "delete_resource"),
    ("mcp__aws-api__call_aws", {"cli_command": P4D_X8}, "ask", "infra_apply"),
    ("mcp__aws-api__call_aws", {"cli_command": "aws ec2 describe-instances"}, None, None),
    # the managed AWS MCP Server, whose argument name we could not verify
    ("mcp__aws-mcp__aws___call_aws", {"cli_command": "aws ec2 release-address --allocation-id e"},
     "ask", "release_ip"),
    ("mcp__aws-mcp__aws___call_aws", {"command": "aws savingsplans create-savings-plan "
                                                 "--savings-plan-offering-id o --commitment 5"},
     "ask", "purchase_commitment"),
    # Amazon Q's use_aws shape
    ("mcp__q__use_aws", {"service_name": "ec2", "operation_name": "terminate-instances",
                         "parameters": {"instance-ids": ["i-1"]}, "region": "us-east-1"},
     "ask", "terminate_instance"),
    ("mcp__q__use_aws", {"service_name": "ec2", "operation_name": "TerminateInstances",
                         "parameters": {"InstanceIds": ["i-1"]}},
     "ask", "terminate_instance"),
    ("mcp__q__use_aws", {"service_name": "ec2", "operation_name": "describe-instances",
                         "parameters": {}}, None, None),
    # Cloud Control API server
    ("mcp__ccapi__delete_resource", {"resource_type": "AWS::RDS::DBInstance",
                                     "identifier": "orders-db"}, "ask", "delete_resource"),
    ("mcp__ccapi__create_resource", {"resource_type": "AWS::S3::Bucket"}, None, None),
    ("mcp__ccapi__update_resource", {"resource_type": "AWS::EC2::Instance",
                                     "identifier": "i-1"}, None, None),
    # HashiCorp Terraform MCP server (HCP Terraform / TFE)
    ("mcp__terraform__create_run", {"terraform_org_name": "acme", "workspace_name": "net",
                                    "run_type": "is_destroy"}, "ask", "delete_resource"),
    ("mcp__terraform__create_run", {"terraform_org_name": "acme", "workspace_name": "net"},
     None, None),
    ("mcp__terraform__create_run", {"terraform_org_name": "acme", "workspace_name": "prod-net"},
     "ask", "infra_apply"),
    ("mcp__terraform__create_run", {"terraform_org_name": "acme", "workspace_name": "net",
                                    "run_type": "plan_only"}, None, None),
    ("mcp__terraform__action_run", {"run_id": "run-abc", "run_action": "apply"}, None, None),
    ("mcp__terraform__action_run", {"run_id": "run-abc", "run_action": "discard"}, None, None),
    ("mcp__terraform__delete_workspace_safely", {"terraform_org_name": "acme",
                                                 "workspace_name": "net"},
     "ask", "delete_resource"),
    # awslabs Terraform MCP server (deprecated, still installed)
    ("mcp__awslabs_terraform-mcp-server__ExecuteTerraformCommand",
     {"command": "destroy", "working_directory": "/infra"}, "ask", "delete_resource"),
    ("mcp__tf__ExecuteTerraformCommand", {"command": "plan", "working_directory": "/infra"},
     None, None),
    ("mcp__tf__ExecuteTerragruntCommand", {"command": "destroy", "working_directory": "/infra"},
     "ask", "delete_resource"),
    ("mcp__tf__ExecuteTerragruntCommand", {"command": "run-all", "working_directory": "/infra"},
     None, None),
    # Flux159 mcp-server-kubernetes
    ("mcp__kubernetes__kubectl_delete", {"resourceType": "deployment", "name": "api",
                                         "namespace": "default"}, "ask", "delete_resource"),
    ("mcp__kubernetes__kubectl_apply", {"manifest": "kind: Deployment"}, None, None),
    ("mcp__kubernetes__kubectl_scale", {"resourceType": "deployment", "name": "api",
                                        "replicas": 10, "namespace": "prod"},
     "ask", "infra_apply"),
    ("mcp__kubernetes__kubectl_create", {"resourceType": "namespace", "name": "x"}, None, None),
    ("mcp__kubernetes__kubectl_patch", {"resourceType": "deployment", "name": "api",
                                        "namespace": "production"}, "ask", "infra_apply"),
    ("mcp__kubernetes__kubectl_rollout", {"subCommand": "status", "resourceType": "deployment",
                                          "name": "api", "namespace": "prod"}, None, None),
    ("mcp__kubernetes__kubectl_generic", {"command": "delete", "resourceType": "pvc",
                                          "name": "data"}, "ask", "delete_resource"),
    ("mcp__kubernetes__kubectl_generic", {"command": "get", "resourceType": "pods"}, None, None),
    ("mcp__kubernetes__install_helm_chart", {"name": "api", "chart": "bitnami/nginx"}, None, None),
    ("mcp__kubernetes__upgrade_helm_chart", {"name": "api", "chart": "./chart",
                                             "namespace": "prod"}, "ask", "infra_apply"),
    ("mcp__kubernetes__uninstall_helm_chart", {"name": "api"}, "ask", "delete_resource"),
    # containers/kubernetes-mcp-server
    ("mcp__k8s__resources_delete", {"apiVersion": "apps/v1", "kind": "Deployment",
                                    "name": "api"}, "ask", "delete_resource"),
    ("mcp__k8s__pods_delete", {"name": "worker-0"}, "ask", "delete_resource"),
    ("mcp__k8s__resources_create_or_update", {"resource": "apiVersion: v1\nkind: Pod"},
     None, None),
    ("mcp__k8s__resources_scale", {"apiVersion": "apps/v1", "kind": "Deployment",
                                   "name": "api"}, None, None),
    ("mcp__k8s__helm_install", {"chart": "oci://x/y", "namespace": "prod"}, "ask", "infra_apply"),
    ("mcp__k8s__helm_uninstall", {"name": "api"}, "ask", "delete_resource"),
    # awslabs EKS MCP server
    ("mcp__eks__manage_k8s_resource", {"operation": "delete", "cluster_name": "c",
                                       "kind": "Service", "api_version": "v1", "name": "lb"},
     "ask", "delete_resource"),
    ("mcp__eks__manage_k8s_resource", {"operation": "read", "cluster_name": "c",
                                       "kind": "Service", "api_version": "v1", "name": "lb"},
     None, None),
    ("mcp__eks__apply_yaml", {"yaml_path": "/m/app.yaml", "cluster_name": "c",
                              "namespace": "default"}, None, None),
    ("mcp__eks__manage_eks_stacks", {"operation": "delete", "cluster_name": "c"},
     "ask", "delete_resource"),
    ("mcp__eks__manage_eks_stacks", {"operation": "describe", "cluster_name": "c"}, None, None),
]


@pytest.mark.parametrize("tool,args,decision,action", CASES,
                         ids=[f"{c[0].rsplit('__', 1)[-1]}-{i}" for i, c in enumerate(CASES)])
def test_known_mcp_tools_are_judged_like_their_shell_form(tool, args, decision, action):
    v = g.gate_mcp_call(tool, args)
    if decision is None:
        assert v is None, f"{tool} {args} should pass silently, got {v}"
        return
    assert v is not None, f"{tool} {args} slipped past the guard"
    assert v["decision"] == decision
    assert v["action_type"] == action
    assert v["mcp_tool"] == tool
    assert tool in v["reason"], "the human must be told which tool call this is"


def test_every_rule_in_the_table_is_exercised():
    """The table is data; a rule nobody tests is a rule nobody knows works."""
    tested = {gm.split_tool_name(t)[1] for t, *_ in CASES}
    untested = sorted(set(gm._BY_NAME) - tested)
    assert not untested, f"rules with no test case: {untested}"


def test_every_rule_names_its_source_and_a_known_door():
    for rule in gm.MCP_RULES:
        assert rule.source, f"{rule.names} has no source"
    assert len(gm._BY_NAME) == sum(len(r.names) for r in gm.MCP_RULES), "duplicate tool name"
    for hit in (gm.ONE_WAY_DELETE, gm.TWO_WAY_APPLY):
        assert door_of(hit[1]) == hit[0]


def test_an_mcp_launch_is_priced_like_the_typed_command():
    typed = g.gate_command(P4D_X8)
    via_mcp = g.gate_mcp_call("mcp__aws-api__call_aws", {"cli_command": P4D_X8})
    assert via_mcp["monthly_delta_usd"] == typed["monthly_delta_usd"]
    assert P4D_X8_TEXT in via_mcp["reason"]
    assert via_mcp["estimate"]["basis"] == typed["estimate"]["basis"]


def test_use_aws_parameters_become_priceable_flags():
    v = g.gate_mcp_call("mcp__q__use_aws", {
        "service_name": "ec2", "operation_name": "run_instances",
        "parameters": {"instance-type": "p4d.24xlarge", "count": 8}})
    assert v and P4D_X8_TEXT in v["reason"]


def test_the_reason_says_what_the_call_amounts_to():
    v = g.gate_mcp_call("mcp__terraform__create_run",
                        {"workspace_name": "net", "run_type": "is_destroy"})
    assert "would start a destroy run on HCP Terraform workspace net" in v["reason"]
    assert "It cannot be undone; confirm to proceed." in v["reason"]


@pytest.mark.parametrize("tool,args,says", [
    ("mcp__aws-api__call_aws", {"cli_command": "aws ec2 terminate-instances --instance-ids i-1"},
     "mcp__aws-api__call_aws amounts to `aws ec2 terminate-instances --instance-ids i-1`"),
    ("mcp__ccapi__delete_resource", {"resource_type": "AWS::RDS::DBInstance", "identifier": "db"},
     "would delete AWS::RDS::DBInstance db through the Cloud Control API"),
    ("mcp__tf__ExecuteTerraformCommand", {"command": "destroy", "working_directory": "/infra"},
     "would run `terraform destroy` in /infra"),
])
def test_mcp_reasons_read_as_a_sentence(tool, args, says):
    assert says in g.gate_mcp_call(tool, args)["reason"]


# ── what must pass through untouched ──────────────────────────────────────────

@pytest.mark.parametrize("tool,args", [
    ("mcp__github__delete_file", {"path": "x", "message": "m"}),
    ("mcp__github__create_run", {"workflow_id": "ci.yml"}),       # a name clash, not HCP
    ("mcp__notion__delete_resource", {"id": "page-1"}),
    ("mcp__notion__delete_resource", {"resource_type": "page", "id": "p"}),
    ("mcp__memory__create_entities", {"entities": []}),
    ("mcp__terraform__search_providers", {"provider_name": "aws"}),
    ("mcp__terraform__create_run", "not a dict"),
    ("mcp__aws-api__call_aws", {"cli_command": 42}),
])
def test_unknown_or_foreign_shaped_tools_get_no_verdict(tool, args):
    assert g.gate_mcp_call(tool, args) is None


def _over_budget(monkeypatch):
    monkeypatch.setattr(ai_budget, "status", lambda **_: {
        "verdict": ai_budget.BUDGET_OVER, "verdict_basis": "tokens", "pct_of_budget": 1.5,
        "billable_tokens_mtd": 15, "budget": {"monthly_tokens": 10}})


def test_the_budget_stop_covers_unknown_mcp_tools_too(monkeypatch):
    """It used to return before the budget check for any tool guard_mcp did
    not translate, so an agent over budget kept spending through GitHub,
    Slack or any other MCP server."""
    _over_budget(monkeypatch)
    v = g.gate_mcp_call("mcp__github__create_issue", {"title": "x"})
    assert v and v["decision"] == "ask" and v["action_type"] == "ai_budget"
    assert v["mcp_tool"] == "mcp__github__create_issue"
    monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "1")
    assert g.gate_mcp_call("mcp__slack__post_message", {"text": "x"})["decision"] == "deny"
    known = g.gate_mcp_call("mcp__kubernetes__kubectl_delete", {"resourceType": "pod", "name": "x"})
    assert known and known["action_type"] == "ai_budget"


def test_a_budget_stop_on_an_unknown_tool_is_recorded(monkeypatch):
    _over_budget(monkeypatch)
    g.gate_mcp_call("mcp__github__create_issue", {"title": "x"})
    import finops.guard_ledger as gl
    [r] = [json.loads(line) for line in gl.ledger_path().read_text().splitlines()]
    assert (r["decision"], r["action_type"], r["tool"]) == \
        ("ask", "ai_budget", "mcp__github__create_issue")


def test_the_hook_stops_an_unknown_mcp_tool_when_over_budget(monkeypatch):
    _over_budget(monkeypatch)
    out = io.StringIO()
    g.run_hook(io.StringIO(json.dumps({"tool_name": "mcp__github__create_issue",
                                       "tool_input": {"title": "x"}})), out)
    assert json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_under_budget_an_unknown_tool_is_still_silent_and_unrecorded():
    assert g.gate_mcp_call("mcp__github__create_issue", {"title": "x"}) is None
    import finops.guard_ledger as gl
    assert not gl.ledger_path().exists()


def test_doctor_says_which_tools_the_budget_stop_covers():
    gaps = " ".join(g.doctor()["not_covered"])
    assert "AI budget stop on Claude Code's built-in tools (Edit, Write, Read" in gaps


@pytest.mark.parametrize("name,expected", [
    ("mcp__aws-mcp__aws___call_aws", ("aws-mcp", "aws___call_aws")),
    ("mcp__plugin_my-plugin_k8s__kubectl_delete", ("plugin_my-plugin_k8s", "kubectl_delete")),
    ("mcp__odd__name__call_aws", ("odd__name", "call_aws")),
    ("mcp__github__search_code", None),
    ("Bash", None),
    ("mcp__call_aws", None),
])
def test_tool_names_split_on_the_right_double_underscore(name, expected):
    assert gm.split_tool_name(name) == expected


# ── the hook routes MCP calls ─────────────────────────────────────────────────

def _hook(payload):
    out = io.StringIO()
    assert g.run_hook(stdin=io.StringIO(json.dumps(payload)), stdout=out) == 0
    return json.loads(out.getvalue()) if out.getvalue() else None


def test_the_hook_asks_on_an_mcp_destroy():
    body = _hook({"tool_name": "mcp__terraform__create_run",
                  "tool_input": {"workspace_name": "net", "run_type": "is_destroy"}})
    d = body["hookSpecificOutput"]
    assert d["permissionDecision"] == "ask"
    assert "mcp__terraform__create_run" in d["permissionDecisionReason"]


def test_the_hook_is_silent_on_unknown_mcp_tools():
    assert _hook({"tool_name": "mcp__github__delete_file", "tool_input": {"path": "x"}}) is None


def test_the_hook_still_ignores_non_mcp_tools():
    assert _hook({"tool_name": "Write", "tool_input": {"file_path": "x", "content": "terraform destroy"}}) is None


# ── the matcher ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tool,covered", [
    ("Bash", True),
    ("mcp__terraform__create_run", True),
    ("mcp__aws-mcp__aws___call_aws", True),
    ("BashOutput", False),       # unanchored "Bash|mcp__.*" would spawn the hook here
    ("KillBash", False),
    ("Edit", False),
    ("Read", False),
])
def test_the_matcher_is_exactly_bash_and_mcp(tool, covered):
    assert g.matcher_covers(g._HOOK_MATCHER, tool) is covered


@pytest.mark.parametrize("matcher,tool,covered", [
    ("Bash", "Bash", True),
    ("Bash", "mcp__x__call_aws", False),
    ("Edit|Write", "Write", True),
    ("Edit, Write", "Edit", True),
    ("*", "mcp__x__y", True),
    ("", "Bash", True),
    ("mcp__memory__.*", "mcp__memory__create", True),
    ("mcp__memory__.*", "mcp__other__create", False),
    ("([", "Bash", False),
])
def test_matcher_covers_follows_the_documented_rules(matcher, tool, covered):
    assert g.matcher_covers(matcher, tool) is covered


@pytest.fixture
def settings(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    monkeypatch.setattr(g, "_settings_path", lambda global_scope: p)
    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/uvx" if n == "uvx" else None)
    return p


def test_a_fresh_install_covers_bash_and_mcp(settings):
    g.install()
    assert g.hook_surfaces(settings) == {"bash": True, "mcp": True}


def test_an_old_bash_only_entry_is_widened_in_place(settings):
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": g._UVX_HOOK_CMD,
                                       "timeout": 30}]}]}}))
    assert g.hook_surfaces(settings) == {"bash": True, "mcp": False}
    g.install()
    pre = json.loads(settings.read_text())["hooks"]["PreToolUse"]
    assert len(pre) == 1 and pre[0]["matcher"] == g._HOOK_MATCHER
    assert g.hook_surfaces(settings) == {"bash": True, "mcp": True}


def test_a_shared_entry_keeps_its_matcher_and_ours_moves_out(settings):
    """Widening a shared entry would start running someone else's hook on every
    MCP call. Their hook keeps its Bash-only entry; ours gets its own."""
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [
            {"type": "command", "command": "other-tool check"},
            {"type": "command", "command": g._UVX_HOOK_CMD, "timeout": 30}]}]}}))
    g.install()
    pre = json.loads(settings.read_text())["hooks"]["PreToolUse"]
    assert pre[0] == {"matcher": "Bash",
                      "hooks": [{"type": "command", "command": "other-tool check"}]}
    assert pre[1]["matcher"] == g._HOOK_MATCHER
    assert pre[1]["hooks"][0]["command"] == g._UVX_HOOK_CMD
    g.install()
    assert len(json.loads(settings.read_text())["hooks"]["PreToolUse"]) == 2, "not idempotent"
    g.uninstall()
    left = json.loads(settings.read_text())["hooks"]["PreToolUse"]
    assert left == [{"matcher": "Bash",
                     "hooks": [{"type": "command", "command": "other-tool check"}]}]


def test_status_says_when_mcp_calls_are_not_checked(settings, monkeypatch):
    import argparse
    import contextlib

    from finops import setup_wizard
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": g._UVX_HOOK_CMD,
                                       "timeout": 30}]}]}}))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        setup_wizard._run_guard(argparse.Namespace(guard_action="status", guard_global=False))
    assert "installed, Bash only" in out.getvalue()
    assert "MCP tool calls" in out.getvalue()


def test_a_hand_written_matcher_is_left_alone(settings):
    body = json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash|Write", "hooks": [{"type": "command", "command": g._UVX_HOOK_CMD,
                                             "timeout": 30}]}]}}, indent=2)
    settings.write_text(body)
    g.install()
    assert settings.read_text() == body
    assert g.hook_surfaces(settings) == {"bash": True, "mcp": False}


# ── an agent changing its own budget ──────────────────────────────────────────

@pytest.mark.parametrize("tool", ["mcp__nable__set_ai_budget", "mcp__finops-mcp__set_ai_budget",
                                  "mcp__plugin_x_nable__set_ai_budget"])
@pytest.mark.parametrize("args", [{"spend_cap": 5000}, {"session_cap": 0},
                                  {"monthly_tokens": 10**9}, {"mode": "plan", "plan_cost": 20},
                                  {"session_cap": 40, "every_session": True}])
def test_an_agent_raising_its_own_budget_is_asked_about(tool, args):
    v = g.gate_mcp_call(tool, args)
    assert v and v["decision"] == "ask" and v["action_type"] == "ai_budget_change"
    assert "the agent is changing its own AI budget" in v["reason"]
    assert "a human should confirm" in v["reason"]


def test_the_budget_change_is_recorded():
    g.gate_mcp_call("mcp__nable__set_ai_budget", {"spend_cap": 5000})
    import finops.guard_ledger as gl
    [r] = [json.loads(line) for line in gl.ledger_path().read_text().splitlines()]
    assert (r["decision"], r["action_type"]) == ("ask", "ai_budget_change")
    assert r["command"] == "set_ai_budget spend_cap=5000"


@pytest.mark.parametrize("args", [{}, {"plan_label": "Max"}, None, {"spend_cap": None}])
def test_a_budget_call_that_changes_no_cap_is_left_alone(args):
    assert g.gate_mcp_call("mcp__nable__set_ai_budget", args) is None


def test_an_agent_over_budget_cannot_lift_the_hard_stop(monkeypatch):
    _over_budget(monkeypatch)
    monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "1")
    v = g.gate_mcp_call("mcp__nable__set_ai_budget", {"monthly_tokens": 10**12})
    assert v["decision"] == "deny" and v["action_type"] == "ai_budget_change"
    assert "changing its own AI budget" in v["reason"] and "over its AI budget" in v["reason"]


def test_the_hook_asks_before_the_budget_changes():
    out = io.StringIO()
    g.run_hook(io.StringIO(json.dumps({"tool_name": "mcp__nable__set_ai_budget",
                                       "tool_input": {"spend_cap": 99999}})), out)
    assert json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] == "ask"


# ── command lines hidden inside a shell server's arguments ────────────────────

_DESTROY = "terraform destroy -auto-approve"


def _nested(value, levels):
    for _ in range(levels):
        value = {"x": value}
    return value


@pytest.mark.parametrize("args", [
    {"command": f"echo hi && {_DESTROY}"},
    {"command": f"/usr/bin/env {_DESTROY}"},
    {"command": f"timeout 600 {_DESTROY}"},
    {"command": f"({_DESTROY})"},
    {"command": f"set -e; {_DESTROY}"},
    {"command": f"true\n{_DESTROY}"},
    {"script": f"#!/bin/bash\n{_DESTROY}"},
    {"body": f"#!/bin/bash\nset -e\n{_DESTROY}"},
    {"pad": ["x"] * 64, "command": _DESTROY},
    {"notes": [f"aws s3 cp a{i} b" for i in range(70)], "command": _DESTROY},
    _nested({"command": _DESTROY}, 4),
])
def test_a_command_anywhere_on_a_shell_line_is_judged(args):
    """The pre-filter used to be anchored at the start of the string, so any
    prefix the shell guard would see through (`echo hi &&`, `timeout 600`, a
    script's first line) walked a destroy past it."""
    v = g.gate_mcp_call("mcp__shell__run_command", args, record=False)
    assert v is not None and v["decision"] == "ask", args
    assert v["action_type"] == "delete_resource"


@pytest.mark.parametrize("args,why", [
    (_nested({"command": _DESTROY}, 12), "nested more than"),
    ({f"c{i}": f"aws s3 cp x{i} y" for i in range(70)}, "more than 64 command lines"),
    ({"rows": [{"a": str(i)} for i in range(5000)] + [{"cmd": _DESTROY}]},
     "argument values"),
])
def test_command_lines_past_the_budget_are_asked_about_not_passed(args, why):
    v = g.gate_mcp_call("mcp__shell__run_command", args, record=False)
    assert v is not None and v["decision"] == "ask"
    assert why in v["reason"] and "mcp__shell__run_command" in v["reason"]


@pytest.mark.parametrize("args", [
    _nested({"text": "hello"}, 12),
    {"rows": [{"a": str(i), "b": "note"} for i in range(5000)]},
    {f"c{i}": "plain text" for i in range(200)},
    {"body": "we moved the aws account; never run terraform destroy by hand"},
])
def test_big_or_deep_arguments_without_a_cli_stay_silent(args):
    assert g.gate_mcp_call("mcp__notes__save", args, record=False) is None
