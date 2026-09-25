# SPDX-License-Identifier: Apache-2.0
"""get_ai_cost_attribution: AI spend by project, workspace, key, team, user, tag.

"What does each team / feature / customer spend on AI" had no answer: every AI
tool split by model. These pin the cross-provider tool on top of the provider
attribution reads, and above all its honesty: a provider without the asked
field says so, a provider that could not be read is never a $0 row, and totals
that would double count are not added up.
"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

from finops.connectors import ai_attribution as attr
from finops.connectors.saas import anthropic_usage, langfuse, litellm, openai_usage

D0, D1 = date(2026, 9, 1), date(2026, 9, 30)

NOT_CONFIGURED = {"source": "none", "reason": "not_configured"}


def _read(source, *groups, total=None, **extra):
    gs = [{"group": g, "id": g, "cost_usd": c} for g, c in groups]
    return {"source": source, "groups": gs,
            "total_usd": sum(c for _, c in groups) if total is None else total, **extra}


def _stub(monkeypatch, **answers):
    """Replace each provider's get_cost_attribution; unnamed ones are not connected."""
    mods = {"openai": openai_usage, "anthropic": anthropic_usage,
            "litellm": litellm, "langfuse": langfuse}
    calls = []
    for name, mod in mods.items():
        ans = answers.get(name, NOT_CONFIGURED)

        def fn(dimension, start, end, _ans=ans, _name=name):
            calls.append((_name, dimension))
            if isinstance(_ans, Exception):
                raise _ans
            return _ans(dimension) if callable(_ans) else _ans
        monkeypatch.setattr(mod, "get_cost_attribution", fn)
    return calls


def test_groups_from_every_provider_carry_their_source(monkeypatch):
    _stub(monkeypatch,
          openai=_read("cost_api", ("Search", 30.0), ("Chat", 10.0)),
          anthropic=_read("cost_api", ("Growth", 20.0)))
    out = attr.get_ai_cost_attribution("project", start_date=D0, end_date=D1)

    assert [(g["group"], g["provider"], g["source"]) for g in out["groups"]] == [
        ("Search", "openai", "cost_api"), ("Growth", "anthropic", "cost_api"),
        ("Chat", "openai", "cost_api")]
    assert out["by_provider"]["openai"]["total_usd"] == 40.0
    assert out["total_usd"] == 60.0
    assert out["not_connected"] == ["litellm", "langfuse"]
    assert "partial" not in out


def test_a_provider_without_the_field_is_named_not_zeroed(monkeypatch):
    _stub(monkeypatch,
          openai={"source": "unsupported", "reason": "OpenAI has no team field."},
          litellm=_read("api", ("Search", 5.0)))
    out = attr.get_ai_cost_attribution("team", start_date=D0, end_date=D1)

    assert out["not_available"]["openai"].startswith("Not available from openai:")
    assert {g["provider"] for g in out["groups"]} == {"litellm"}
    assert "openai" not in out["by_provider"]


def test_a_failed_provider_is_partial_and_never_a_zero_row(monkeypatch):
    _stub(monkeypatch,
          openai=_read("cost_api", ("Search", 30.0)),
          anthropic={"source": "error", "reason": "credential_invalid",
                     "error": "Anthropic refused ANTHROPIC_ADMIN_KEY"},
          litellm=RuntimeError("proxy down"))
    out = attr.get_ai_cost_attribution("project", start_date=D0, end_date=D1)

    assert out["partial"] is True
    assert set(out["failed_providers"]) == {"anthropic", "litellm"}
    assert "refused" in out["failed_providers"]["anthropic"]
    assert all(g["provider"] == "openai" for g in out["groups"])
    assert "not zero" in out["note"]


def test_nothing_connected_is_an_error_not_an_empty_bill(monkeypatch):
    _stub(monkeypatch)
    out = attr.get_ai_cost_attribution("team", start_date=D0, end_date=D1)
    assert "No AI provider is connected" in out["error"]
    assert out["groups"] == []
    assert out["total_usd"] is None


def test_connected_providers_that_cannot_split_say_what_they_record(monkeypatch):
    _stub(monkeypatch, openai={"source": "unsupported", "reason": "use dimension='project'."})
    out = attr.get_ai_cost_attribution("team", start_date=D0, end_date=D1)
    assert "can split cost by 'team'" in out["error"]
    assert "project" in out["not_available"]["openai"]


@pytest.mark.parametrize("dim", ["project", "workspace", "team", "tag", "session"])
def test_with_nothing_connected_no_provider_claims_to_lack_the_field(monkeypatch, dim):
    """Found running `nable ai-costs --by team` on a clean machine: it said the
    connected providers cannot split by team, with none connected."""
    monkeypatch.setattr("finops.security.env.get_env", lambda k, d="": d)
    monkeypatch.setattr("httpx.get", lambda *a, **k: pytest.fail("no call expected"))
    out = attr.get_ai_cost_attribution(dim, start_date=D0, end_date=D1)
    assert "not_available" not in out
    assert out["not_connected"] == list(attr.PROVIDERS)
    assert "No AI provider is connected" in out["error"]


def test_every_configured_provider_failing_is_an_error(monkeypatch):
    _stub(monkeypatch, openai={"source": "none", "reason": "api_error", "error": "HTTP 503"})
    out = attr.get_ai_cost_attribution("project", start_date=D0, end_date=D1)
    assert "could be read" in out["error"]
    assert out["failed_providers"] == {"openai": "HTTP 503"}


def test_gateway_and_biller_together_do_not_add_up(monkeypatch):
    """A LiteLLM proxy in front of OpenAI logs the calls OpenAI bills."""
    _stub(monkeypatch,
          openai=_read("estimated", ("ada@example.com", 5.0)),
          litellm=_read("api", ("ada@example.com", 5.0)))
    out = attr.get_ai_cost_attribution("user", start_date=D0, end_date=D1)
    assert out["total_usd"] is None
    assert "count the same call twice" in out["note"]


def test_overlapping_tags_do_not_add_up(monkeypatch):
    _stub(monkeypatch, langfuse=_read("api", ("a", 4.0), ("b", 4.0), total=4.0,
                                      groups_overlap=True))
    out = attr.get_ai_cost_attribution("tag", start_date=D0, end_date=D1)
    assert out["total_usd"] is None
    assert out["by_provider"]["langfuse"]["groups_overlap"] is True


def test_customer_feature_and_agent_read_tags_and_say_so(monkeypatch):
    calls = _stub(monkeypatch, litellm=_read("api", ("customer:acme", 3.0)))
    out = attr.get_ai_cost_attribution("customer", start_date=D0, end_date=D1)
    assert out["dimension"] == "tag"
    assert "No AI provider records a customer field" in out["dimension_note"]
    assert ("litellm", "tag") in calls


def test_provider_filter_asks_only_that_provider(monkeypatch):
    calls = _stub(monkeypatch, openai=_read("cost_api", ("A", 1.0)))
    out = attr.get_ai_cost_attribution("project", provider="OpenAI", start_date=D0, end_date=D1)
    assert calls == [("openai", "project")]
    assert "not_connected" not in out


@pytest.mark.parametrize("kw,key", [({"dimension": "region"}, "valid_dimensions"),
                                    ({"dimension": "project", "provider": "bedrock"},
                                     "valid_providers")])
def test_unknown_inputs_list_the_valid_ones(monkeypatch, kw, key):
    _stub(monkeypatch)
    out = attr.get_ai_cost_attribution(**kw)
    assert "error" in out and key in out


def test_unpriced_or_truncated_provider_marks_partial(monkeypatch):
    _stub(monkeypatch, openai=_read("estimated", ("u", 1.0),
                                    unpriced_models={"x": {"input_tokens": 1, "output_tokens": 1}}))
    out = attr.get_ai_cost_attribution("user", start_date=D0, end_date=D1)
    assert out["partial"] is True
    assert out["by_provider"]["openai"]["unpriced_models"]


def test_unreadable_rows_mark_partial(monkeypatch):
    _stub(monkeypatch, langfuse=_read("api", ("u", 1.0), unreadable_rows=3))
    out = attr.get_ai_cost_attribution("user", start_date=D0, end_date=D1)
    assert out["partial"] is True
    assert out["by_provider"]["langfuse"]["unreadable_rows"] == 3


# ── the MCP tool ──────────────────────────────────────────────────────────

def _tool(**kw):
    import finops.server  # noqa: F401  (tools register against the server)
    from finops.tools import llm as llm_tools
    fn = getattr(llm_tools.get_ai_cost_attribution, "fn", llm_tools.get_ai_cost_attribution)
    return asyncio.run(fn(**kw))


@pytest.fixture
def _no_metrics(monkeypatch):
    async def none(allow_stripe=True):
        assert allow_stripe is False  # the attribution tool never calls Stripe
        return {}
    monkeypatch.setattr("finops.connectors.business_metrics.resolve_business_metrics", none)


def test_tool_caps_groups_but_keeps_the_count(monkeypatch, _no_metrics):
    monkeypatch.setattr("finops.demo_data.is_demo", lambda: False)
    many = [(f"key-{i}", float(100 - i)) for i in range(60)]
    _stub(monkeypatch, openai=_read("cost_api", *many))
    out = _tool(dimension="api_key")
    assert len(out["groups"]) == 50
    assert out["group_count"] == 60
    assert "top 50 of 60" in out["groups_truncated"]
    assert out["by_provider"]["openai"]["total_usd"] == sum(c for _, c in many)


def test_tool_adds_ai_unit_economics_when_the_total_is_whole(monkeypatch):
    monkeypatch.setattr("finops.demo_data.is_demo", lambda: False)

    async def metrics(allow_stripe=True):
        return {"paying_customers": 100, "mrr_usd": 10_000.0}
    monkeypatch.setattr("finops.connectors.business_metrics.resolve_business_metrics", metrics)
    _stub(monkeypatch, openai=_read("cost_api", ("A", 300.0)))

    out = _tool(dimension="project", days=30)

    assert out["unit_economics"]["cost_per_customer"] == 3.0
    assert out["unit_economics"]["ai_as_pct_of_mrr"] == 3.0
    assert "Bedrock" in out["unit_economics"]["basis"]


def test_tool_skips_unit_economics_on_an_estimate(monkeypatch):
    monkeypatch.setattr("finops.demo_data.is_demo", lambda: False)

    async def metrics(allow_stripe=True):
        return {"paying_customers": 100}
    monkeypatch.setattr("finops.connectors.business_metrics.resolve_business_metrics", metrics)
    _stub(monkeypatch, openai=_read("estimated", ("u", 300.0)))
    assert "unit_economics" not in _tool(dimension="user")


def test_tool_in_demo_mode_never_reads_a_provider(monkeypatch):
    monkeypatch.setattr("finops.demo_data.is_demo", lambda: True)
    _stub(monkeypatch, openai=RuntimeError("must not be called"))
    out = _tool(dimension="team")
    assert out["_demo_mode"] is True


# ── advertised where it will be found ─────────────────────────────────────

def test_tool_is_in_the_llm_family_and_front_door():
    from finops import tool_surface
    assert tool_surface._FAMILY_OF["get_ai_cost_attribution"] == "llm"
    assert "get_ai_cost_attribution" in tool_surface.LLM_FRONT_DOOR


def test_description_matches_the_questions_people_ask():
    from finops import server
    tools = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    desc = tools["get_ai_cost_attribution"].description.lower()
    for word in ("team", "feature", "customer", "project", "workspace", "tag"):
        assert word in desc, word


def test_llm_key_advertises_it_and_aws_alone_does_not(monkeypatch):
    from finops import server, tool_surface
    for k in ("AWS_ACCESS_KEY_ID", "AWS_PROFILE", "OPENAI_API_KEY", "FINOPS_ALL_TOOLS",
              "FINOPS_FLAT_TOOLS", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(tool_surface, "_kubeconfig_present", lambda: False)
    monkeypatch.setattr("finops.security.vault.Vault.default",
                        lambda: type("V", (), {"list_keys": staticmethod(lambda: [])})())
    monkeypatch.setattr("finops.accounts.list_accounts", lambda: [])
    monkeypatch.setattr("finops.demo_data.is_demo", lambda: False)

    def listed(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        tool_surface._reset_cache_for_tests()
        try:
            return {t.name for t in asyncio.run(server.mcp.list_tools())}
        finally:
            for k in env:
                monkeypatch.delenv(k, raising=False)
            tool_surface._reset_cache_for_tests()

    assert "get_ai_cost_attribution" in listed(OPENAI_API_KEY="sk-test")  # pragma: allowlist secret
    assert "get_ai_cost_attribution" not in listed(AWS_ACCESS_KEY_ID="AKIATEST")
