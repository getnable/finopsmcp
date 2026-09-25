"""Every IAM artifact nable hands out grants what the scan calls.

There were three, and they disagreed: the dry-run policy listed 23 actions,
`nable iam-template` 58 (calling itself "Exact permissions nable needs, nothing
more"), and the committed cloudformation/readonly-key.json 57. The template
missed two the dry run granted, and the dry run missed five the scan called.
A security reviewer comparing them could not tell which to believe.

Now the scan statement in every artifact is generated from the manifest, the
optional statements are separate and named, and these tests hold them to it.
"""
from __future__ import annotations

import ast
import inspect
import io
import json
import pathlib
import re
from contextlib import redirect_stderr, redirect_stdout

import pytest

from finops.scan_manifest import SPEND_ACTIONS, iam_actions, iam_policy
from finops.security import iam_setup as I

_REPO = pathlib.Path(__file__).resolve().parent.parent
_SCAN = set(iam_actions(include_spend=False, include_get_metric_data=False))


def _statements(template: dict, policy: str) -> list[dict]:
    """(Resolved) statements of a template's managed policy, optional ones
    taken as enabled."""
    raw = template["Resources"][policy]["Properties"]["PolicyDocument"]["Statement"]
    return [st["Fn::If"][1] if "Fn::If" in st else st for st in raw]


def _artifacts() -> dict[str, list[dict]]:
    committed = json.loads((_REPO / "cloudformation" / "readonly-key.json").read_text())
    return {
        "iam-template (role)": _statements(json.loads(I.generate_cloudformation()),
                                           "NableReadOnlyPolicy"),
        "one-click key": _statements(json.loads(I.generate_cloudformation_key()),
                                     "NableReadOnlyPolicy"),
        "cloudformation/readonly-key.json": _statements(committed, "NableReadOnlyPolicy"),
        "org stackset": _statements(I.org_stackset_template(), "NableOrgReadOnlyPolicy"),
    }


@pytest.mark.parametrize("name", sorted(_artifacts()))
def test_the_scan_statement_is_exactly_the_manifest(name):
    [scan] = [st for st in _artifacts()[name] if st["Sid"] == "NableReadOnlyScan"]
    assert set(scan["Action"]) == _SCAN, name


@pytest.mark.parametrize("name", sorted(_artifacts()))
def test_every_manifest_action_is_granted_by_every_artifact(name):
    granted = {a for st in _artifacts()[name] for a in st["Action"]}
    spend = {a for _, a in SPEND_ACTIONS}
    assert _SCAN | spend <= granted, sorted((_SCAN | spend) - granted)


def test_the_dry_run_policy_is_the_manifest():
    assert set(iam_policy(include_get_metric_data=False)["Statement"][0]["Action"]) == _SCAN


def test_terraform_grants_the_manifest_too():
    tf = I.generate_terraform()
    granted = set(re.findall(r'"([a-z0-9-]+:[A-Za-z0-9]+)"', tf))
    assert _SCAN <= granted
    assert '"NableReadOnlyScan"' in tf


def test_no_artifact_claims_to_be_exactly_what_nable_needs():
    blob = (I.generate_cloudformation() + I.generate_cloudformation_key()
            + json.dumps(I.org_stackset_template()) + I.generate_terraform())
    assert "nothing more" not in blob


def test_each_optional_statement_says_what_it_unlocks_and_can_be_switched_off():
    tpl = json.loads(I.generate_cloudformation_key())
    optional = [st for st in tpl["Resources"]["NableReadOnlyPolicy"]["Properties"]
                ["PolicyDocument"]["Statement"] if "Fn::If" in st]
    assert len(optional) == len(I._OPTIONAL_GROUPS)
    for st in optional:
        param = st["Fn::If"][0]
        assert tpl["Conditions"][param]
        desc = tpl["Parameters"][param]["Description"]
        assert "Unlocks" in desc or "Finds" in desc, desc
    ce = tpl["Parameters"]["IncludeCostExplorer"]["Description"]
    assert "BILLED" in ce and "$0.01" in ce


def test_iam_template_on_stdout_is_valid_json():
    """`nable iam-template > nable-iam.json` wrote comments and a footer
    around the JSON, so the file did not parse."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        I.print_iam_template("cloudformation")
    assert json.loads(out.getvalue())["AWSTemplateFormatVersion"]
    assert "aws cloudformation deploy" in err.getvalue()


# ── the --spend manifest is what the --spend path calls ──────────────────────

def _ce_calls(fn) -> set[str]:
    tree = ast.parse(inspect.getsource(inspect.getmodule(fn)))
    tree = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == fn.__name__)
    return {n.func.attr for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name) and n.func.value.id == "ce"}


def test_spend_actions_are_the_cost_explorer_calls_spend_makes():
    """ce:GetCostForecast and ce:GetDimensionValues were listed for --spend and
    nothing on that path calls them."""
    from finops import cli_scan
    from finops.connectors import llm_costs
    called = _ce_calls(cli_scan._spend_snapshot) | _ce_calls(llm_costs.get_bedrock_costs)
    assert called == {"get_cost_and_usage"}
    assert {a for _, a in SPEND_ACTIONS} == {"ce:GetCostAndUsage"}


# ── connect --scopes ─────────────────────────────────────────────────────────

def test_aws_is_not_graded_billing_only():
    from finops import connector_scopes as cs
    aws = cs.CONNECTOR_SCOPES["aws"]
    assert aws.grade == cs.INVENTORY
    out = cs.render()
    assert "scan_manifest" not in out and ".py" not in out
    assert out.index("INVENTORY") < out.index("AWS") < out.index("SCOPED")
