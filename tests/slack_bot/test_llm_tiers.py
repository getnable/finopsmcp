"""Tests for model tier routing and env overrides."""
from __future__ import annotations

from finops.slack_bot import llm


def test_defaults_per_tier(monkeypatch):
    monkeypatch.delenv("FINOPS_SLACK_MODEL", raising=False)
    for tier in ("simple", "chat", "rca"):
        monkeypatch.delenv(f"FINOPS_SLACK_MODEL_{tier.upper()}", raising=False)
    assert llm.model_for_tier("simple") == llm.TIER_DEFAULTS["simple"]
    assert llm.model_for_tier("chat") == llm.TIER_DEFAULTS["chat"]
    assert llm.model_for_tier("rca") == llm.TIER_DEFAULTS["rca"]
    # Unknown tier falls back to chat
    assert llm.model_for_tier("nope") == llm.TIER_DEFAULTS["chat"]


def test_master_override_wins(monkeypatch):
    monkeypatch.setenv("FINOPS_SLACK_MODEL", "claude-fable-5")
    monkeypatch.setenv("FINOPS_SLACK_MODEL_RCA", "claude-opus-4-5")
    assert llm.model_for_tier("rca") == "claude-fable-5"
    assert llm.model_for_tier("chat") == "claude-fable-5"


def test_per_tier_override(monkeypatch):
    monkeypatch.delenv("FINOPS_SLACK_MODEL", raising=False)
    monkeypatch.setenv("FINOPS_SLACK_MODEL_SIMPLE", "claude-haiku-3-5")
    assert llm.model_for_tier("simple") == "claude-haiku-3-5"
    assert llm.model_for_tier("chat") == llm.TIER_DEFAULTS["chat"]


def test_pick_tier_routes_investigations_to_rca():
    rca_questions = [
        "why did our AWS bill spike?",
        "investigate the RDS cost increase",
        "what caused the jump in Bedrock spend",
        "root cause for the anomaly please",
        "what's driving compute costs up?",
    ]
    for q in rca_questions:
        assert llm.pick_tier(q) == "rca", q


def test_pick_tier_routes_lookups_to_chat():
    chat_questions = [
        "show me last month's spend",
        "top 5 services by cost",
        "how much did we spend on Snowflake?",
        "list idle resources",
    ]
    for q in chat_questions:
        assert llm.pick_tier(q) == "chat", q


def test_thinking_config_gates_by_model_family():
    # Deliberative tiers get adaptive summarized thinking; Haiku/older get none.
    from finops.slack_bot import llm as _llm
    assert _llm._thinking_config("claude-opus-4-8") == {"type": "adaptive", "display": "summarized"}
    assert _llm._thinking_config("claude-sonnet-4-6") == {"type": "adaptive", "display": "summarized"}
    assert _llm._thinking_config("claude-fable-5") == {"type": "adaptive", "display": "summarized"}
    # Haiku 4.5 and unknown/older models: no thinking (stay fast / avoid 400).
    assert _llm._thinking_config("claude-haiku-4-5") is None
    assert _llm._thinking_config("claude-sonnet-3-5") is None
    assert _llm._thinking_config("") is None
