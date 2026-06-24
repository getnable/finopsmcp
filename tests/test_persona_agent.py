"""The agent persona: concise structured output for automated callers, env-activatable."""
from __future__ import annotations

from finops import persona


def test_agent_persona_exists_and_is_concise():
    assert "agent" in persona.PERSONAS
    ctx = persona.PERSONAS["agent"]["mcp_context"].lower()
    assert "terse" in ctx and "agent" in ctx


def test_agent_persona_is_technical():
    # agents want raw identifiers, not "large compute instance"
    assert persona.is_technical_persona("agent") is True
    assert persona.format_instance_type("m5.4xlarge", persona="agent") == "m5.4xlarge"


def test_env_var_selects_the_agent_persona(monkeypatch):
    monkeypatch.setenv("FINOPS_PERSONA", "agent")
    assert persona.get_persona() == "agent"
    assert "terse" in persona.get_persona_mcp_context().lower()


def test_env_var_wins_over_config(monkeypatch):
    monkeypatch.setattr(persona, "_read_config", lambda: {"persona": "finance"})
    monkeypatch.setenv("FINOPS_PERSONA", "agent")
    assert persona.get_persona() == "agent"


def test_unknown_env_persona_is_ignored(monkeypatch):
    monkeypatch.setattr(persona, "_read_config", lambda: {"persona": "platform"})
    monkeypatch.setenv("FINOPS_PERSONA", "nonsense")
    assert persona.get_persona() == "platform"  # falls through to config
