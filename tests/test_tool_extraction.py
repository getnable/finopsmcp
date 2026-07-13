"""Guards the per-family tool extraction seam (finops/tools/*).

server.py was split from ~14k lines into per-family tool modules. Every extracted
module must register its tools against the same telemetry-wrapped mcp instance,
and server.py must keep re-exporting the moved names so `finops.server.<tool>`
stays a stable address for internal callers and tests. These tests fail loudly if
a future edit breaks either property.
"""
from __future__ import annotations

import ast
import pathlib

from finops import server

TOOLS_DIR = pathlib.Path(server.__file__).parent / "tools"


def _module_tool_names(path: pathlib.Path) -> list[str]:
    out = []
    for n in ast.parse(path.read_text()).body:
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and any(
            isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "tool"
            for d in n.decorator_list
        ):
            out.append(n.name)
    return out


def _all_extracted() -> dict[str, list[str]]:
    return {
        f.stem: _module_tool_names(f)
        for f in sorted(TOOLS_DIR.glob("*.py"))
        if f.name != "__init__.py"
    }


def test_there_are_several_extracted_modules():
    # The split actually happened (not silently collapsed back into server.py).
    mods = _all_extracted()
    assert len(mods) >= 10, f"expected many family modules, got {list(mods)}"
    assert sum(len(v) for v in mods.values()) >= 150


def test_every_extracted_tool_is_registered_and_addressable():
    registered = {t.name for t in server.mcp._tool_manager.list_tools()}
    # The long-tail _EXTRA_TOOLS are deliberately unadvertised unless FINOPS_ALL_TOOLS=1
    # (they stay importable/callable); every other extracted tool must be registered.
    extras = server._EXTRA_TOOLS
    for mod, names in _all_extracted().items():
        for tool in names:
            assert hasattr(server, tool), f"finops.server.{tool} no longer resolves"
            assert tool in registered or tool in extras, \
                f"{mod}.{tool} neither registered nor a known extra"


def test_no_tool_definitions_remain_in_server_py():
    # The whole point of the split: server.py holds helpers/main, not tool bodies.
    server_src = pathlib.Path(server.__file__).read_text()
    tree = ast.parse(server_src)
    leftover = [
        n.name
        for n in tree.body
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
        and any(
            isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "tool"
            for d in n.decorator_list
        )
    ]
    assert leftover == [], f"tool defs still inline in server.py: {leftover}"
