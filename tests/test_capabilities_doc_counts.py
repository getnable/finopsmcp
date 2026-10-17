"""The numbers CAPABILITIES.md states are the numbers the code has.

Found 2026-10-13: CAPABILITIES.md said "194 MCP tools", its per-area table
summed to 185, the server registered 204 (203 a customer can reach, plus one
staff tool), and docs/tool-inventory.txt listed 161 names, two of which no
longer existed. It also said "~48 CLI commands" when `nable --help` listed 59.
None of those numbers had a test, so each drifted the day a tool or a command
was added. These read the live registry and the live help instead.
"""
from __future__ import annotations

import contextlib
import io
import pathlib
import re
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[1]
CAPS = (ROOT / "CAPABILITIES.md").read_text(encoding="utf-8")


def _public_tools() -> set[str]:
    from finops import server, tool_surface
    registered = {t.name for t in server.mcp._tool_manager.list_tools()}
    return registered - set(tool_surface.INTERNAL_TOOLS)


def test_the_tool_count_is_the_registry():
    n = len(_public_tools())
    stated = [int(x) for x in re.findall(r"\*\*(\d+) MCP tools", CAPS)]
    stated += [int(x) for x in re.findall(r"## MCP tools \((\d+)\)", CAPS)]
    stated += [int(x) for x in re.findall(r"with (\d+) cost tools", CAPS)]
    stated += [int(x) for x in re.findall(r"does not list all (\d+)", CAPS)]
    assert len(stated) >= 4, "a tool-count sentence was reworded; update this test"
    assert set(stated) == {n}, f"CAPABILITIES.md says {sorted(set(stated))}, the registry has {n}"


def test_the_tool_inventory_is_the_registry():
    listed = (ROOT / "docs/tool-inventory.txt").read_text(encoding="utf-8").split()
    assert listed == sorted(listed), "keep docs/tool-inventory.txt sorted"
    public = _public_tools()
    assert set(listed) == public, (
        f"missing: {sorted(public - set(listed))}; no such tool: {sorted(set(listed) - public)}")


def test_the_family_table_is_the_tool_surface():
    from finops import tool_surface
    actual = Counter(tool_surface._FAMILY_OF[n] for n in _public_tools())
    table = CAPS.split("| Family | Tools |", 1)[1].split("\n\n", 1)[0]
    stated = {m.group(1): int(m.group(2))
              for m in re.finditer(r"^\| (\w+) \| (\d+) \|", table, re.MULTILINE)}
    assert stated == dict(actual)


def test_the_default_listing_is_described_as_it_is():
    from finops import tool_surface as ts
    assert len(ts.TIER1) == 17 and "the 17\nfront-door tools" in CAPS
    core_doors = {n for n in ts.TIER1 if ts._FAMILY_OF.get(n) == "core"}
    assert len(core_doors) == 10 and "(10 with nothing\nconnected" in CAPS
    assert len(ts.LLM_FRONT_DOOR) == 3 and "plus\nthree AI-spend tools" in CAPS
    # every front door is in a family that a connected provider turns on
    assert {ts._FAMILY_OF[n] for n in ts.TIER1} <= {"core", "cost"}


def _help_commands() -> list[str]:
    from finops import setup_wizard
    buf = io.StringIO()
    with (contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()),
          contextlib.suppress(SystemExit)):
        setup_wizard.main(["--help"])
    body = buf.getvalue().split("\noptions\n", 1)[0]
    return re.findall(r"^  ([a-z0-9][a-z0-9-]*)\s{2,}", body, re.MULTILINE)


def test_the_cli_count_and_list_are_nable_help():
    listed = _help_commands()
    assert len(listed) > 40, "could not read nable --help"
    m = re.search(r"## CLI \((\d+) commands\)", CAPS)
    assert m and int(m.group(1)) == len(listed), (m and m.group(1), len(listed))
    assert re.search(r"(\d+) CLI commands", CAPS).group(1) == str(len(listed))
    block = CAPS.split("## CLI (", 1)[1].split("```", 2)[1]
    in_doc = set()
    for line in block.splitlines():
        # the group label is the first column; the commands follow it
        words = line[14:].split() if len(line) > 14 else []
        in_doc.update(words)
    assert in_doc == set(listed), (
        f"not in CAPABILITIES.md: {sorted(set(listed) - in_doc)}; "
        f"not in nable --help: {sorted(in_doc - set(listed))}")


_TOOL_PREFIXES = (
    "get_", "list_", "audit_", "scan_", "check_", "set_", "send_", "create_", "open_",
    "estimate_", "recommend_", "forecast_", "explain_", "run_", "verify_", "mark_",
    "dismiss_", "compare_", "connect_", "push_", "subscribe_", "cancel_", "acknowledge_",
    "generate_", "export_", "take_", "what_", "optimize_", "propose_", "identify_",
    "suggest_", "sync_", "pin_", "unpin_", "remember_", "forget_", "benchmark_",
    "slice_", "cleanup_", "delete_", "revoke_", "activate_", "publish_", "fetch_",
)


def test_every_tool_the_docs_name_is_registered():
    """docs/RECOMMENDATIONS.md listed get_llm_unit_economics and
    get_llm_unit_economics_full, which moved to the hosted package, and
    docs/COST-ATTRIBUTION.md listed get_untagged_resource_cost, which was never
    an MCP tool. A reader who asks for them gets "Unknown tool"."""
    from finops import server
    registered = {t.name for t in server.mcp._tool_manager.list_tools()}
    docs = [ROOT / "README.md", ROOT / "CAPABILITIES.md", *sorted((ROOT / "docs").glob("*.md"))]
    unknown = []
    for doc in docs:
        text = doc.read_text(encoding="utf-8")
        for name in set(re.findall(r"`([a-z][a-z0-9_]+)(?:\([^`]*\))?`", text)):
            if name.startswith(_TOOL_PREFIXES) and name not in registered:
                unknown.append(f"{doc.relative_to(ROOT)}: {name}")
    assert not unknown, "docs name tools the server does not have:\n  " + "\n  ".join(unknown)
