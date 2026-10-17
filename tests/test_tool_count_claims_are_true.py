"""Every place that states how many MCP tools nable has states the registry.

Found stale against a registry of 204 tools (203 callable, one internal):

  CAPABILITIES.md           "194 MCP tools", a per-area table summing to 185
  what_can_nable_do         "lights up 128 of nable's 160+ tools"
  server instructions       "nable registers roughly 190 tools"
  docs/tool-inventory.txt   "the full list of MCP tools": 161 names, 47 live
                            tools missing and 4 that no longer exist
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _callable_tools() -> set[str]:
    from finops import server
    from finops.tool_surface import INTERNAL_TOOLS

    return {t.name for t in server.mcp._tool_manager.list_tools()} - INTERNAL_TOOLS


def test_the_capability_count_is_the_registry():
    from finops.capabilities import TOOLS_CLAIM, TOTAL_TOOLS

    assert TOTAL_TOOLS == len(_callable_tools())
    assert TOOLS_CLAIM.startswith(f"{TOTAL_TOOLS} tools")


def test_the_server_instructions_count_is_the_registry():
    from finops import server

    assert f"registers {len(_callable_tools())} tools" in server.mcp.instructions


def test_capabilities_md_states_the_registry_and_its_table_adds_up():
    text = (ROOT / "CAPABILITIES.md").read_text()
    n = len(_callable_tools())
    assert f"**{n} MCP tools" in text
    assert f"## MCP tools ({n})" in text

    section = text.split("## MCP tools", 1)[1].split("\n## ", 1)[0]
    counts = []
    for row in section.splitlines():
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if len(cells) == 3 and re.fullmatch(r"\d+(-\d+)*", cells[1] or ""):
            counts += [int(x) for x in cells[1].split("-")]
    assert sum(counts) == n, f"the per-area table sums to {sum(counts)}, not {n}"


# docs/tool-inventory.txt against the registry: the identical assertion (and
# that the list is sorted) is tests/test_capabilities_doc_counts.py's
# test_the_tool_inventory_is_the_registry, so it is made once, there.
