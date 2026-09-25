"""Demo mode is a try-before-connect path, so every tool must answer in it.

Found by dogfooding 0.8.216: 22 of the no-argument tools crashed in demo with a
pydantic output validation error. The demo layer answered in dicts, and those
tools are declared `-> str` or `-> list`, so a user trying run_full_cost_audit or
export_cost_report_csv on the sample saw a stack of validation text instead.

The test below calls EVERY registered tool the way an MCP client does (through
the tool manager, so output validation runs) with the minimum arguments its
schema requires, and fails on any exception.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from finops import server


@pytest.fixture
def demo(monkeypatch, tmp_path):
    from finops import demo_data
    import finops.storage.db as db_mod

    monkeypatch.setattr(demo_data, "DEMO_MODE", True)
    # Demo even on a machine that has real credentials, and nothing of the
    # developer's (database, pinned views, accounts) is read or written.
    monkeypatch.setenv("FINOPS_DEMO_FORCE", "1")
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "demo.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    db_mod._ENGINE = None
    # The in-chat connect tools run for real in demo (they are how a user
    # leaves it). Keep them off the network: nothing is found on this machine.
    import finops.setup_wizard as sw
    monkeypatch.setattr(sw, "_detect_aws_candidates", lambda: [], raising=False)
    monkeypatch.setattr(sw, "_detect_sso_profiles_needing_login", lambda: [], raising=False)
    yield
    db_mod._ENGINE = None


_FILLER = {"string": "x", "integer": 1, "number": 1, "boolean": False,
           "array": [], "object": {}}


def _minimal_args(schema: dict) -> dict:
    args = {}
    for key in schema.get("required", []):
        prop = schema.get("properties", {}).get(key, {})
        kind = prop.get("type") or (prop.get("anyOf") or [{}])[0].get("type")
        args[key] = _FILLER.get(kind, "x")
    return args


def _all_tools():
    return sorted(server.mcp._tool_manager.list_tools(), key=lambda t: t.name)


def test_the_registry_is_the_whole_product():
    # A guard on the guard: if tool discovery broke, the sweep below would pass
    # vacuously on an empty list.
    assert len(_all_tools()) > 150


def _text(result) -> str:
    parts: list[str] = []

    def walk(node):
        if isinstance(node, (list, tuple)):
            for item in node:
                walk(item)
        elif hasattr(node, "text"):
            parts.append(str(node.text))
        else:
            parts.append(json.dumps(node, default=str))

    walk(result)
    return "\n".join(parts)


def test_every_tool_answers_in_demo_mode(demo):
    failures: dict[str, str] = {}
    unlabelled: list[str] = []
    for tool in _all_tools():
        try:
            out = asyncio.run(server.mcp.call_tool(tool.name, _minimal_args(tool.parameters)))
        except Exception as exc:  # validation errors surface as ToolError
            failures[tool.name] = f"{type(exc).__name__}: {str(exc)[:160]}"
            continue
        # Every demo answer says it is the sample, in a flag a program can read
        # and in words a person reads.
        # A text tool has nowhere to put a flag, so its text must open with the
        # label instead.
        text = _text(out)
        if server._declared_return(tool.fn) is str:
            labelled = "sample data (demo mode)" in text.lower()
        else:
            labelled = '"_demo_mode": true' in text and "sample data" in text.lower()
        if not labelled:
            unlabelled.append(tool.name)
    assert not failures, "tools that crash in demo mode:\n" + "\n".join(
        f"  {k}: {v}" for k, v in failures.items())
    assert not unlabelled, f"demo answers with no sample-data label: {unlabelled}"


@pytest.mark.parametrize("tool", [
    "run_full_cost_audit", "export_cost_report_csv", "get_savings_ledger",
    "scan_graviton_migration_opportunities", "recommend_spot_adoption",
])
def test_str_tools_answer_with_labelled_text(demo, tool):
    from finops.demo_data import render_text

    fn = server.mcp._tool_manager.get_tool(tool).fn
    out = asyncio.run(fn())
    assert isinstance(out, str)
    assert out.lower().startswith("sample data"), out[:80]
    assert render_text(out) == out  # never labelled twice


def test_list_tools_answer_with_a_list(demo):
    out = asyncio.run(server.mcp._tool_manager.get_tool("list_api_keys").fn())
    assert isinstance(out, list) and out and out[0].get("_demo_mode") is True


# ── the way out of demo runs from chat ─────────────────────────────────────────

def test_connect_runs_in_demo_and_says_it_is_still_sample(demo):
    """connect_aws used to get the "not in the sample dataset" placeholder, so a
    demo user could not leave demo from the chat that told them to connect."""
    out = asyncio.run(server.mcp._tool_manager.get_tool("connect_aws").fn())
    assert "how_to_connect" in out            # the real tool ran
    assert out["_demo_mode"] is True
    assert "sample data" in out["_demo_note"]


def test_connect_that_lands_leaves_demo(demo, monkeypatch):
    from finops import demo_data

    monkeypatch.delenv("FINOPS_DEMO_FORCE", raising=False)
    monkeypatch.setattr(demo_data, "_real_provider_connected", lambda: True)
    out = demo_data.after_connect_in_demo({"connected": True})
    assert out["_demo_mode"] is False
    assert "not the StreamCo sample" in out["_demo_exit"]


def test_first_answer_directive_in_demo_names_a_sample_backed_tool():
    from finops.demo_data import demo_bridge_result, demo_tool_names

    d = server._first_run_onboarding_directive(demo=True)
    assert "list_idle_resources" not in d["directive"]
    assert "SAMPLE DATA" in d["directive"]
    assert "get_savings_summary" in d["directive"]
    assert "get_savings_summary" in demo_tool_names()
    assert "isn't in the sample" not in str(demo_bridge_result("get_savings_summary", {}))


def test_first_demo_cost_answer_carries_the_demo_directive(demo, monkeypatch):
    monkeypatch.setattr(server, "_first_cost_query_fired", False)
    out = asyncio.run(server.mcp._tool_manager.get_tool("get_cost_summary").fn())
    assert out["_demo_mode"] is True
    assert "SAMPLE DATA" in out["_onboarding"]["directive"]


# ── the "what am I connected to" views agree with each other ───────────────────

def _run_tool(name, **kwargs):
    return asyncio.run(server.mcp._tool_manager.get_tool(name).fn(**kwargs))


def test_connected_views_agree_in_demo(demo):
    """Dogfood: list_connected_providers reported nine providers "connected"
    with no demo flag, while what_can_nable_do said nothing was connected. All
    four views now say the same thing: sample providers, none of the user's."""
    from finops.demo_data import SAMPLE_PROVIDERS_NOTE, connected_providers

    sample = {e["name"] for e in connected_providers()}

    listed = _run_tool("list_connected_providers")
    assert listed["_demo_mode"] is True
    assert listed["_demo_note"] == SAMPLE_PROVIDERS_NOTE
    rows = {k: v for k, v in listed.items() if not k.startswith("_")}
    assert set(rows) == sample
    assert all(v["configured"] is False and v["sample_data"] for v in rows.values())
    assert not any(v["status"] == "connected" for v in rows.values())

    health = _run_tool("check_connector_health")
    assert health["_demo_mode"] is True and health["healthy_count"] == 0
    assert {c["name"] for c in health["connectors"]} == sample

    setup = _run_tool("nable_setup_status")
    assert setup["_demo_mode"] is True
    assert set(setup["sample_providers"]) == sample

    caps = _run_tool("what_can_nable_do")
    assert caps.lower().startswith("sample data")
    assert "None of your own accounts are connected" in caps
    assert "connect_aws" in caps
    for label in ("AWS", "GCP", "Azure", "OpenAI", "Snowflake"):
        assert label in caps


def test_capability_map_in_demo_lists_only_sample_backed_tools(demo):
    from finops.demo_data import demo_tool_names

    caps = _run_tool("what_can_nable_do", detailed=True)
    listed = caps.rsplit("### Tools that answer from the sample", 1)[1]
    assert {t.strip() for t in listed.split(",")} == set(demo_tool_names())
