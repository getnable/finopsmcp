"""MCP tool annotations for Connectors Directory submission.

Every advertised tool must carry a title + readOnlyHint (or destructiveHint).
nable is read-only + propose-only, so read-only is the default; only WRITE_TOOLS
mutate state and only DESTRUCTIVE_TOOLS remove/revoke. These tests are the
enforcement: a renamed or new write tool that isn't classified fails CI instead
of silently shipping a wrong annotation to the directory.
"""
from __future__ import annotations

import asyncio

import pytest

from finops import server
from finops.tool_surface import (
    DESTRUCTIVE_TOOLS,
    WRITE_TOOLS,
    _FAMILY_OF,
    tool_annotation,
    tool_title,
)


@pytest.fixture(autouse=True)
def _all_tools(monkeypatch):
    # Annotate/inspect the full surface, not a family-filtered subset.
    monkeypatch.setenv("FINOPS_ALL_TOOLS", "1")


def _tools():
    return asyncio.run(server.mcp.list_tools())


def test_write_and_destructive_names_are_real_tools():
    # _FAMILY_OF spans every known tool including the registration-gated extras
    # (the tool-surface completeness test keeps it in sync with the live
    # registry), so it is the authoritative name set for the typo guard.
    known = set(_FAMILY_OF)
    assert WRITE_TOOLS <= known, WRITE_TOOLS - known
    assert DESTRUCTIVE_TOOLS <= known, DESTRUCTIVE_TOOLS - known
    # Every destructive tool is also a write.
    assert DESTRUCTIVE_TOOLS <= WRITE_TOOLS


def test_every_advertised_tool_has_annotations():
    tools = _tools()
    assert tools, "expected the full tool surface"
    for t in tools:
        assert t.annotations is not None, f"{t.name} missing annotations"
        assert t.annotations.title, f"{t.name} missing annotation title"
        assert t.annotations.readOnlyHint is not None, f"{t.name} missing readOnlyHint"


def test_readonly_and_destructive_hints_match_classification():
    for t in _tools():
        want_ro = t.name not in WRITE_TOOLS
        assert t.annotations.readOnlyHint is want_ro, (
            f"{t.name}: readOnlyHint={t.annotations.readOnlyHint}, expected {want_ro}"
        )
        want_destr = t.name in DESTRUCTIVE_TOOLS
        assert bool(t.annotations.destructiveHint) is want_destr, t.name
        # A read-only tool can never be destructive.
        if t.annotations.readOnlyHint:
            assert not t.annotations.destructiveHint, t.name


def test_read_only_is_the_majority_surface():
    tools = _tools()
    ro = sum(1 for t in tools if t.annotations.readOnlyHint)
    # nable is propose-only: the vast majority of tools are read-only. If this
    # ratio inverts, something mislabeled the surface.
    assert ro / len(tools) > 0.7


def test_tool_title_humanizes_with_acronyms():
    assert tool_title("get_cost_summary") == "Get cost summary"
    assert tool_title("get_ai_kpis") == "Get AI KPIs"
    assert tool_title("open_rightsizing_pr") == "Open rightsizing PR"
    assert tool_title("audit_public_ipv4_addresses") == "Audit public IPv4 addresses"
    assert tool_title("list_aws_accounts") == "List AWS accounts"


def test_tool_annotation_shape():
    a = tool_annotation("delete_budget")
    assert a == {"title": "Delete budget", "readOnlyHint": False, "destructiveHint": True}
    b = tool_annotation("get_cost_summary")
    assert b == {"title": "Get cost summary", "readOnlyHint": True, "destructiveHint": False}
