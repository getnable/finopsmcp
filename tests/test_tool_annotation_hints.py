"""All four MCP tool hints, on every tool, as a client receives them.

tools/list carried readOnlyHint and destructiveHint only, so every tool took
the spec defaults for the other two: openWorldHint true for whoami, list_budgets
and every tool that only touches the local database, and idempotentHint false
for delete_budget and for every read. And destructiveHint was false for
unpin_view, forget_cost_context and cancel_report_subscription, which remove
what a person saved, and for set_alert_policy, set_ai_budget and
sync_budgets_from_yaml, which overwrite a setting in place.
"""
from __future__ import annotations

import asyncio

import pytest

from finops.tool_surface import (
    _FAMILY_OF,
    DESTRUCTIVE_TOOLS,
    IDEMPOTENT_WRITE_TOOLS,
    LOCAL_ONLY_TOOLS,
    WRITE_TOOLS,
)


@pytest.fixture(autouse=True)
def _all_tools(monkeypatch):
    monkeypatch.setenv("FINOPS_ALL_TOOLS", "1")


def _listed():
    from mcp.shared.memory import create_connected_server_and_client_session

    from finops import server

    async def run():
        async with create_connected_server_and_client_session(server.mcp._mcp_server) as c:
            return {t.name: t.annotations for t in (await c.list_tools()).tools}
    return asyncio.run(run())


def test_every_tool_carries_all_four_hints():
    for name, a in _listed().items():
        assert a is not None, name
        for hint in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
            assert getattr(a, hint) is not None, f"{name} has no {hint}"


def test_the_hint_sets_name_real_tools_and_agree_with_each_other():
    known = set(_FAMILY_OF)
    assert LOCAL_ONLY_TOOLS <= known, LOCAL_ONLY_TOOLS - known
    assert IDEMPOTENT_WRITE_TOOLS <= WRITE_TOOLS
    assert DESTRUCTIVE_TOOLS <= WRITE_TOOLS


@pytest.mark.parametrize("name", ["unpin_view", "forget_cost_context",
                                  "cancel_report_subscription", "set_alert_policy",
                                  "set_ai_budget", "sync_budgets_from_yaml"])
def test_tools_that_remove_or_overwrite_are_destructive(name):
    a = _listed()[name]
    assert a.readOnlyHint is False and a.destructiveHint is True, a


@pytest.mark.parametrize(("name", "open_world"), [
    ("whoami", False), ("list_budgets", False), ("delete_budget", False),
    ("get_savings_summary", False),
    ("get_cost_summary", True), ("create_ticket", True), ("push_to_n8n", True),
    ("open_rightsizing_pr", True), ("send_report_now", True), ("connect_aws", True),
    ("estimate_terraform_cost", True),
])
def test_open_world_is_true_only_where_something_outside_is_called(name, open_world):
    assert _listed()[name].openWorldHint is open_world


@pytest.mark.parametrize(("name", "idempotent"), [
    ("delete_budget", True), ("revoke_api_key", True), ("get_cost_summary", True),
    ("create_ticket", False), ("send_report_now", False), ("pin_view", False),
    ("open_rightsizing_pr", False),
])
def test_idempotent_is_true_only_where_a_repeat_changes_nothing(name, idempotent):
    assert _listed()[name].idempotentHint is idempotent
