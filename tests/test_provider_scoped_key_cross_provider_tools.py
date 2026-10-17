"""A key scoped to one provider sees that provider's spend, in every tool.

The dispatch applies an API key's scope_provider to a tool's `provider`
argument (server._SurfacedFastMCP.call_tool). The tools that read every
connected provider and take no `provider` argument had nothing for it to
apply to. Reproduced with a key scoped to aws, AWS ($1,280) and Datadog
($900) connected, every one of these read Datadog:

  compare_providers               Datadog's $900 beside AWS's, 41.3% of $2,180
  get_total_spend_all_sources     the same $900 as saas_total_usd
  get_saas_spend_summary          Datadog's bill, by service
  get_unit_economics              "$2,180.00" of infrastructure against MRR
  explain_cost_change             "$2,180.00" now and before
  explain_recent_cost_drivers     total_current_usd 2180.0
  audit_duplicate_spend           names datadog among the providers checked
  export_cost_report              read Datadog for the report it wrote

The safer of the two fixes is the refusal: a "compare providers" or "all
sources" answer cut down to one provider would still be labelled as the
whole picture, and a refusal cannot leak. The scoped key is pointed at
get_cost_summary, which honours the scope.
"""
from __future__ import annotations

import ast
import json
import pathlib

import pytest

from tests.mcp_support import Reads, connectors_fixture
from tests.mcp_support import call as _call

_AWS = {"Amazon Elastic Compute Cloud - Compute": 1280.0}
_DATADOG = {"Datadog Infrastructure": 900.0}


@pytest.fixture
def connectors(monkeypatch, tmp_path):
    yield from connectors_fixture(monkeypatch, tmp_path)


def _aws_only_key():
    from finops.auth.rbac import Identity
    return Identity(key_id=7, name="aws-team", email="", role="admin",
                    scope_team=None, scope_provider="aws")


def _call_any(name: str, args: dict, identity=None) -> tuple[bool, str]:
    """(is_error, text) for one tool call through the in-memory MCP client."""
    import asyncio

    from mcp.shared.memory import create_connected_server_and_client_session

    from finops import server
    from finops.auth.rbac import set_current_identity

    async def run():
        set_current_identity(identity)
        async with create_connected_server_and_client_session(server.mcp._mcp_server) as c:
            res = await c.call_tool(name, args)
            return res.isError, "".join(getattr(b, "text", "") for b in res.content)
    return asyncio.run(run())


@pytest.mark.parametrize("tool", [
    "compare_providers", "get_total_spend_all_sources", "get_saas_spend_summary",
    "get_unit_economics", "explain_cost_change", "explain_recent_cost_drivers",
    "audit_duplicate_spend", "export_cost_report",
])
def test_a_provider_scoped_key_never_reads_another_providers_spend(connectors, tool):
    datadog = Reads("datadog", _DATADOG)
    connectors(aws=Reads("aws", _AWS), datadog=datadog)

    is_error, text = _call_any(tool, {}, identity=_aws_only_key())

    assert "900" not in text and "2,180" not in text and "atadog" not in text, text
    assert datadog.asked == [], "the other provider was read for a key scoped to aws"
    assert is_error and "scoped to aws" in text, text
    assert "get_cost_summary" in text


@pytest.mark.parametrize("tool", ["compare_providers", "get_total_spend_all_sources"])
def test_an_unscoped_key_still_sees_every_provider(connectors, tool):
    connectors(aws=Reads("aws", _AWS), datadog=Reads("datadog", _DATADOG))
    out = _call(tool, {})
    assert out["grand_total_usd"] == pytest.approx(2180.0), out


# ── the rule, for tools not written yet ──────────────────────────────────────

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "finops"
_READS_EVERY_PROVIDER = ("_active(", "_gather_costs(", "get_cost_summary(")


def _cross_provider_tools_without_a_provider_argument() -> set[str]:
    found = set()
    for path in [_SRC / "server.py", *sorted((_SRC / "tools").glob("*.py"))]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "tool"
                       for d in node.decorator_list):
                continue
            params = {a.arg for a in node.args.args + node.args.kwonlyargs}
            body = ast.unparse(node)
            if "provider" not in params and any(k in body for k in _READS_EVERY_PROVIDER):
                found.add(node.name)
    return found


def test_every_tool_that_reads_every_provider_without_a_provider_argument_is_scoped():
    """A tool that enumerates the connected providers and has no `provider`
    parameter is out of the dispatch's reach, so it must be in the refusal
    list (or take a provider argument the scope can fill in)."""
    from finops.server import PROVIDER_SCOPED_REFUSED

    missing = _cross_provider_tools_without_a_provider_argument() - set(PROVIDER_SCOPED_REFUSED)
    assert not missing, (
        "these tools read every provider's spend and a provider-scoped key "
        f"would see all of it: {sorted(missing)}")


def test_the_refusal_names_the_scope_and_where_to_go(connectors):
    connectors(aws=Reads("aws", _AWS), datadog=Reads("datadog", _DATADOG))
    is_error, text = _call_any("compare_providers", {"category": "cloud"},
                               identity=_aws_only_key())
    assert is_error
    assert "compare_providers" in text and "aws" in text
    with pytest.raises(ValueError):
        json.loads(text)   # a plain refusal, not a partial answer
