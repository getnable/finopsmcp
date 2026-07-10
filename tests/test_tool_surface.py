"""Connection-aware tool surface: advertise only what this machine can use.

Locks in the load-bearing properties:
  - every registered tool is mapped to exactly one family (a new tool that
    nobody classifies fails here, same self-healing pattern as the CLI help
    groups);
  - a clean machine advertises core only (provider families hidden), while
    connectors and cost tools stay discoverable;
  - detecting a provider reveals its family;
  - FINOPS_ALL_TOOLS=1 and demo mode advertise everything registered;
  - THE SAFETY PROPERTY: an unadvertised tool called by name still runs, so the
    in-chat connect flow can never be broken by filtering;
  - the filter actually pays: advertised-list token weight drops >20% on a
    clean machine vs the full surface.
"""
from __future__ import annotations

import asyncio

import pytest

from finops import server, tool_surface
from finops.tool_surface import FAMILY_TOOLS, _FAMILY_OF, advertise, connected_families


@pytest.fixture(autouse=True)
def _clean_surface(monkeypatch):
    """Scrub every detection signal so each test starts 'nothing connected'."""
    for keys in tool_surface._ENV_KEYS.values():
        for k in keys:
            monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("FINOPS_ALL_TOOLS", raising=False)
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.setattr(tool_surface, "_kubeconfig_present", lambda: False)
    monkeypatch.setattr("finops.security.vault.Vault.default",
                        classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("no vault"))))
    monkeypatch.setattr("finops.accounts.list_accounts", lambda: [])
    monkeypatch.setattr("finops.demo_data.is_demo", lambda: False)
    tool_surface._reset_cache_for_tests()
    yield
    tool_surface._reset_cache_for_tests()


# ── completeness: the enforcement for the fail-open runtime ───────────────────

def test_every_registered_tool_is_mapped():
    registered = {t.name for t in server.mcp._tool_manager.list_tools()}
    unmapped = registered - set(_FAMILY_OF)
    assert not unmapped, f"add these to a family in tool_surface.py: {sorted(unmapped)}"


def test_extras_are_mapped_too():
    # The 26 registration-gated extras only exist under FINOPS_ALL_TOOLS=1, so
    # the registry check above can miss them in a normal test run. Pin them here.
    unmapped = set(server._EXTRA_TOOLS) - set(_FAMILY_OF)
    assert not unmapped, f"extras missing a family: {sorted(unmapped)}"


def test_no_tool_in_two_families():
    total = sum(len(v) for v in FAMILY_TOOLS.values())
    assert total == len(_FAMILY_OF), "a tool appears in more than one family"


# ── filtering behavior ─────────────────────────────────────────────────────────

def _advertised_names():
    return {t.name for t in asyncio.run(server.mcp.list_tools())}


def test_clean_machine_advertises_core_only():
    names = _advertised_names()
    # cost tools + connectors stay discoverable for the first-run flow
    for must in ("get_cost_summary", "connect_aws", "connect_gcp", "connect_azure",
                 "what_can_nable_do", "get_agent_team"):
        assert must in names, must
    # provider families hidden
    for hidden in ("get_azure_budgets", "audit_gcp_waste", "get_kubernetes_costs",
                   "get_databricks_costs", "get_llm_costs", "create_ticket",
                   "send_weekly_digest_now"):
        assert hidden not in names, hidden
    # and nothing outside core is advertised at all
    assert names <= set(FAMILY_TOOLS["core"])


def test_aws_env_reveals_aws_and_llm(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    tool_surface._reset_cache_for_tests()
    fams = connected_families()
    assert "aws" in fams and "llm" in fams  # Bedrock rides the AWS account
    names = _advertised_names()
    assert "audit_aws_waste" in names
    assert "get_llm_costs" in names
    assert "get_azure_budgets" not in names


def test_databricks_env_reveals_family(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "https://x.cloud.databricks.com")
    tool_surface._reset_cache_for_tests()
    assert "get_databricks_costs" in _advertised_names()


def test_all_tools_flag_advertises_everything(monkeypatch):
    monkeypatch.setenv("FINOPS_ALL_TOOLS", "1")
    registered = {t.name for t in server.mcp._tool_manager.list_tools()}
    assert _advertised_names() == registered


def test_demo_mode_advertises_everything(monkeypatch):
    monkeypatch.setattr("finops.demo_data.is_demo", lambda: True)
    registered = {t.name for t in server.mcp._tool_manager.list_tools()}
    assert _advertised_names() == registered


def test_unmapped_tool_fails_open():
    assert advertise("some_future_tool_nobody_classified") is True


# ── THE SAFETY PROPERTY: hidden tools stay callable ────────────────────────────

def test_unadvertised_tool_is_still_callable():
    async def main():
        from mcp.shared.memory import create_connected_server_and_client_session
        async with create_connected_server_and_client_session(
            server.mcp._mcp_server
        ) as client:
            listed = await client.list_tools()
            names = {t.name for t in listed.tools}
            assert "get_databricks_costs" not in names  # hidden on a clean box
            result = await client.call_tool("get_databricks_costs", {})
            # It ran: it returns a not-configured answer, not "unknown tool".
            text = "".join(getattr(c, "text", "") for c in result.content).lower()
            assert "unknown tool" not in text
            assert text  # produced a real response
    asyncio.run(main())


# ── the payoff, measured ───────────────────────────────────────────────────────

def test_token_weight_drops_meaningfully(monkeypatch):
    from finops.token_budget import estimate_tokens

    def weight(tools):
        return estimate_tokens([
            {"name": t.name, "description": t.description, "schema": t.parameters}
            for t in tools
        ])

    every = server.mcp._tool_manager.list_tools()
    clean = [t for t in every if advertise(t.name)]
    full, filtered = weight(every), weight(clean)
    assert filtered < full * 0.8, f"expected >20% cut, got {full} -> {filtered}"
