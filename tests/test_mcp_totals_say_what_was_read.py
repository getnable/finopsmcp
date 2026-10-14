"""Every MCP tool that sums spend says what it did not read.

get_cost_summary already refuses a total nothing stands behind and labels a
partial one. The tools beside it, asked the same question about the same
bill, did not:

  compare_providers()            every provider failed  -> "$0.00"
  get_total_spend_all_sources()  every provider failed  -> "$0.00"
  get_total_spend_all_sources()  one of two failed      -> a total, no "partial"
  get_top_cost_drivers()         one of two failed      -> a total, no "partial"
  get_costs_by_service()         read, no cost rows     -> total_usd 0, services []

A model reading any of those tells the user what they spent. Every call here
goes through the real MCP dispatch, the way a client makes it; only the
connectors are fakes, raising or answering the way the real ones do.
"""
from __future__ import annotations

import pytest

from tests.mcp_support import Denied as _Denied
from tests.mcp_support import Reads as _Reads
from tests.mcp_support import call as _call
from tests.mcp_support import connectors_fixture


@pytest.fixture
def connectors(monkeypatch, tmp_path):
    """Install fake connectors as the connected set; return a setter."""
    yield from connectors_fixture(monkeypatch, tmp_path)


_EC2 ={"Amazon Elastic Compute Cloud - Compute": 1200.0, "Amazon Simple Storage Service": 80.0}


# ── every provider failed: no total at all ────────────────────────────────────

@pytest.mark.parametrize("tool", ["compare_providers", "get_total_spend_all_sources"])
def test_every_provider_failed_is_no_cost_data_not_zero(connectors, tool):
    connectors(aws=_Denied("aws"))
    out = _call(tool)

    assert out.get("error") == "no_cost_data", out
    assert "grand_total_usd" not in out, "a failed read was reported as a $0 total"
    assert "Cost Explorer permissions" in out["message"]
    assert "not a finding of zero spend" in out["note"]


# ── read, and no cost rows came back: not $0 either ───────────────────────────

@pytest.mark.parametrize("tool", ["compare_providers", "get_total_spend_all_sources",
                                  "get_costs_by_service", "get_top_cost_drivers"])
def test_no_cost_rows_is_not_a_zero_total(connectors, tool):
    connectors(aws=_Reads("aws", {}))
    out = _call(tool)

    assert out.get("error") == "no_cost_data", out
    assert "no cost rows" in out["message"]
    assert "not a finding of zero spend" in out["note"]


# ── one of two providers failed: a real total, labelled partial ───────────────

@pytest.mark.parametrize("tool", ["compare_providers", "get_total_spend_all_sources",
                                  "get_top_cost_drivers"])
def test_one_failed_provider_makes_the_total_partial(connectors, tool):
    connectors(aws=_Reads("aws", _EC2), datadog=_Denied("datadog"))
    out = _call(tool)

    assert out.get("partial") is True, out
    assert "datadog" in out["failed_providers"]
    assert "could not be read" in out["partial_warning"]
    assert out["grand_total_usd"] == pytest.approx(1280.0)


# ── the same bill gives the same number from every tool ───────────────────────

def test_the_four_totals_agree_on_one_bill(connectors):
    connectors(aws=_Reads("aws", _EC2))
    summary = _call("get_cost_summary")["grand_total_usd"]
    by_service = _call("get_costs_by_service")["total_usd"]
    drivers = _call("get_top_cost_drivers")["grand_total_usd"]
    everything = _call("get_total_spend_all_sources")["grand_total_usd"]
    compared = _call("compare_providers")["grand_total_usd"]

    assert summary == by_service == drivers == everything == compared == pytest.approx(1280.0)


def test_top_cost_drivers_rejects_a_limit_below_one(connectors):
    connectors(aws=_Reads("aws", _EC2))
    out = _call("get_top_cost_drivers", {"limit": 0})

    assert "error" in out and "limit" in out["error"], out


# ── a key scoped to one provider cannot widen by leaving the filter out ───────

def _aws_only_key():
    from finops.auth.rbac import Identity
    return Identity(key_id=7, name="aws-team", email="", role="viewer",
                    scope_team=None, scope_provider="aws")


@pytest.mark.parametrize("args", [{}, {"provider": "datadog"}, {"provider": None}])
def test_a_provider_scoped_key_sees_only_its_provider(connectors, args):
    """A key created with scope_provider="aws" called get_cost_summary({}) and
    got the Datadog bill too: the dispatch applied the scope only when the
    caller passed a provider argument, so leaving it out read everything."""
    connectors(aws=_Reads("aws", _EC2),
               datadog=_Reads("datadog", {"Datadog Infrastructure": 900.0}))
    out = _call("get_cost_summary", args, identity=_aws_only_key())

    assert set(out["by_provider"]) == {"aws"}, out["by_provider"].keys()
    assert out["grand_total_usd"] == pytest.approx(1280.0)
