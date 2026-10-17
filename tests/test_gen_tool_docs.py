"""scripts/gen_tool_docs.py documents the tools the server registers, no more.

The generator parsed source files for @mcp.tool() decorators. It found the
204 registered tools, internal one included, so the public reference it
writes (web/tools.json, web/tools.html) listed send_onboarding_email, a
staff tool customers cannot call, and counted 204 where CAPABILITIES.md and
docs/tool-inventory.txt (pinned by tests/test_capabilities_doc_counts.py)
say 203. Its descriptions were the raw docstrings, not what the registry
serves. And its output directory, web/, is not in this repo, so a run
failed with FileNotFoundError before writing anything.

It now reads the live registry, as the capability-count tests do.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location(
        "gen_tool_docs", ROOT / "scripts" / "gen_tool_docs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _registry() -> dict:
    from finops import server
    from finops.tool_surface import INTERNAL_TOOLS
    return {t.name: t for t in server.mcp._tool_manager.list_tools()
            if t.name not in INTERNAL_TOOLS}


def test_the_reference_lists_exactly_the_public_registry():
    names = [t["name"] for t in _script().extract_tools()]
    assert len(names) == len(set(names))
    public = _registry()
    assert set(names) == set(public), (
        f"missing: {sorted(set(public) - set(names))}; "
        f"not public: {sorted(set(names) - set(public))}")
    listed = (ROOT / "docs/tool-inventory.txt").read_text(encoding="utf-8").split()
    assert sorted(names) == listed


def test_each_entry_is_what_the_registry_serves():
    gen = _script()
    public = _registry()
    for t in gen.extract_tools():
        reg = public[t["name"]]
        assert t["summary"] == gen.first_paragraph(reg.description), t["name"]
        schema = reg.parameters or {}
        assert [p["name"] for p in t["params"]] == list(schema.get("properties", {})), t["name"]
        assert {p["name"] for p in t["params"] if p["required"]} == set(schema.get("required", []))


def test_it_writes_its_pages_where_it_is_told(tmp_path, monkeypatch):
    gen = _script()
    out = tmp_path / "web"           # does not exist yet, like web/ in this repo
    monkeypatch.setattr(gen, "OUT_JSON", out / "tools.json")
    monkeypatch.setattr(gen, "OUT_HTML", out / "tools.html")
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    gen.main()
    data = json.loads((out / "tools.json").read_text())
    assert data["count"] == len(_registry())
    html = (out / "tools.html").read_text()
    assert f"Tool reference ({data['count']})" in html
    assert "send_onboarding_email" not in html
