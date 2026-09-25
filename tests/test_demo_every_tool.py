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


def test_every_tool_answers_in_demo_mode(demo):
    failures: dict[str, str] = {}
    for tool in _all_tools():
        try:
            asyncio.run(server.mcp.call_tool(tool.name, _minimal_args(tool.parameters)))
        except Exception as exc:  # validation errors surface as ToolError
            failures[tool.name] = f"{type(exc).__name__}: {str(exc)[:160]}"
    assert not failures, "tools that crash in demo mode:\n" + "\n".join(
        f"  {k}: {v}" for k, v in failures.items())


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
