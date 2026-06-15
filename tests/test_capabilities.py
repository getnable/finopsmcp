"""Tests for the stack-tailored capability map."""
from __future__ import annotations

from finops.capabilities import render_capabilities, has_cloud, has_llm, CATALOG


def test_no_providers_prompts_to_connect():
    out = render_capabilities(set())
    assert "Nothing's connected" in out
    assert "setup aws" in out


def test_aws_only_shows_aws_groups_and_nudges_llm():
    out = render_capabilities({"aws"}, plan="free")
    assert "AWS" in out
    assert "Find savings" in out
    assert "Deep AWS audits" in out
    assert "Credits & the cash cliff" in out
    # Bedrock-backed AI group shows for AWS even without a direct LLM key
    assert "AI / LLM cost" in out
    # but the deeper AI features that need token data do not
    assert "AI commitments & contracts" not in out
    # Azure / Kubernetes groups should not appear
    assert "Azure deep dives" not in out
    assert "Kubernetes" not in out
    # nudge to connect an LLM key
    assert "OpenAI/Anthropic key" in out


def test_aws_plus_llm_unlocks_ai_commitments_and_forecast():
    out = render_capabilities({"aws", "openai", "llm"}, plan="team")
    assert "AI commitments & contracts" in out
    assert "AI forecast & monitor" in out
    # llm is connected, so it should not be in the connect-more nudge
    assert "OpenAI/Anthropic key" not in out


def test_kubernetes_group_gated_on_kubeconfig():
    assert "Kubernetes" not in render_capabilities({"aws"})
    assert "Kubernetes" in render_capabilities({"aws", "kubernetes"})


def test_relevant_count_grows_with_stack():
    def count(out: str) -> int:
        # the header states "lights up N of"
        import re
        m = re.search(r"lights up \*\*(\d+)\*\*", out)
        return int(m.group(1)) if m else 0
    aws = count(render_capabilities({"aws"}))
    aws_llm = count(render_capabilities({"aws", "openai", "llm"}))
    assert aws > 0
    assert aws_llm > aws


def test_detailed_lists_tool_names():
    plain = render_capabilities({"aws"}, detailed=False)
    detailed = render_capabilities({"aws"}, detailed=True)
    assert "get_cost_summary" not in plain
    assert "get_cost_summary" in detailed


def test_no_plain_english_phrase_anywhere():
    for connected in (set(), {"aws"}, {"aws", "openai", "llm", "azure", "kubernetes"}):
        assert "plain english" not in render_capabilities(connected).lower()


def test_helpers():
    assert has_cloud({"aws"}) and has_cloud({"azure"}) and not has_cloud({"openai"})
    assert has_llm({"openai"}) and has_llm({"llm"}) and not has_llm({"aws"})


def test_catalog_wellformed():
    for g in CATALOG:
        assert {"id", "title", "gate", "count", "blurb", "asks", "tools"} <= set(g)
        assert callable(g["gate"])
        assert g["count"] > 0
        assert g["asks"] and all(len(a) == 2 for a in g["asks"])
