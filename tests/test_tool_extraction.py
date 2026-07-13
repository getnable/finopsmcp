"""Guards the per-family tool extraction seam (finops/tools/*).

server.py is being split by tool family. Each extracted module must register its
tools against the SAME telemetry-wrapped mcp instance, and server.py must keep
re-exporting the moved names so `finops.server.<tool>` stays a stable address.
These tests fail loudly if a future extraction breaks either property.
"""
from __future__ import annotations

from finops import server
from finops.tools import recommendations as rec

# The recommendations & learning family that moved out of server.py first.
_MOVED = [
    "mark_recommendation_acted_on",
    "dismiss_recommendation",
    "remember_cost_context",
    "get_learned_cost_context",
    "forget_cost_context",
    "verify_savings",
    "get_savings_ledger",
    "get_recommendation_quality",
    "get_recommendation_learning",
]


def test_extracted_module_shares_the_server_mcp_instance():
    # Registration only lands on the real server if it's the same object.
    assert rec.mcp is server.mcp


def test_moved_tools_are_registered():
    names = {t.name for t in server.mcp._tool_manager.list_tools()}
    for tool in _MOVED:
        assert tool in names, f"{tool} not registered after extraction"


def test_moved_tools_still_addressable_on_server():
    # Internal callers and older tests reach these as finops.server.<tool>.
    for tool in _MOVED:
        assert hasattr(server, tool), f"finops.server.{tool} no longer resolves"
        assert getattr(server, tool) is getattr(rec, tool)
