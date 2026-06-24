"""
Persona detection and formatting for nable MCP server.

Personas control how the AI model formats its responses. The current persona is
stored in ~/.finops-mcp/config.yaml and can be changed via:
    finops config --persona <role>
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

PERSONAS: dict[str, dict[str, str]] = {
    "engineer": {
        "label": "Engineer / Developer",
        "description": "technical details, CLI commands, instance types",
        "mcp_context": (
            "Format all responses with full technical detail. Include instance types, CLI commands, "
            "API names, and specific configuration changes. Use technical terminology. "
            "Show exact resource identifiers."
        ),
    },
    "finops": {
        "label": "FinOps / Cloud Ops",
        "description": "trends, savings plans, coverage, chargeback",
        "mcp_context": (
            "Format responses with FinOps metrics: coverage percentages, on-demand vs committed spend, "
            "savings plan ROI, MoM variance, unit economics. Use FinOps terminology. "
            "Lead with financial impact."
        ),
    },
    "finance": {
        "label": "Finance / Management",
        "description": "spend summaries, budget tracking, plain English",
        "mcp_context": (
            "Format all responses for a non-technical finance audience. Use dollar amounts prominently. "
            "Avoid instance type names, API terminology, and technical jargon entirely. "
            "Explain what services are in plain English. Lead with the bottom line."
        ),
    },
    "platform": {
        "label": "Platform / SRE",
        "description": "Kubernetes, per-service costs, tag attribution",
        "mcp_context": (
            "Format responses focused on workload attribution, namespace breakdowns, tag-level analysis, "
            "and per-service cost ownership. Include infrastructure topology context."
        ),
    },
    "agent": {
        "label": "Agent / Automation",
        "description": "concise structured output for an AI agent or automated caller",
        "mcp_context": (
            "You are being called by an automated agent, not a human reading prose. Be terse: "
            "no preamble, no restating the question, no multi-paragraph explanations, no markdown "
            "tables. Lead with the answer and the numbers. Pass through the tool's structured "
            "fields and surface any machine-readable verdict/status field verbatim. Keep exact "
            "identifiers (instance types, resource IDs, regions); the caller is a machine."
        ),
    },
}

_DEFAULT_PERSONA = "engineer"
_CONFIG_PATH = Path.home() / ".finops-mcp" / "config.yaml"


def _read_config() -> dict[str, Any]:
    """Read ~/.finops-mcp/config.yaml. Returns empty dict if file does not exist."""
    if not _CONFIG_PATH.exists():
        return {}
    try:
        import yaml
        text = _CONFIG_PATH.read_text()
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_config(data: dict[str, Any]) -> None:
    """Write config to ~/.finops-mcp/config.yaml, preserving any existing keys."""
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_config()
    existing.update(data)
    try:
        import yaml
        _CONFIG_PATH.write_text(yaml.dump(existing, default_flow_style=False))
    except Exception as exc:
        raise RuntimeError(f"Could not write config to {_CONFIG_PATH}: {exc}") from exc


def get_persona() -> str:
    """Return the current persona key. Defaults to 'engineer' if not set.

    FINOPS_PERSONA in the environment wins over the config file, so an agent-driven
    MCP deployment can select the concise 'agent' persona per-process without writing
    config (e.g. FINOPS_PERSONA=agent in the server env)."""
    import os
    env = os.getenv("FINOPS_PERSONA", "").strip().lower()
    if env in PERSONAS:
        return env
    cfg = _read_config()
    persona = cfg.get("persona", _DEFAULT_PERSONA)
    if persona not in PERSONAS:
        return _DEFAULT_PERSONA
    return persona


def set_persona(persona: str) -> None:
    """Write the persona key to config. Raises ValueError for unknown personas."""
    if persona not in PERSONAS:
        valid = ", ".join(PERSONAS.keys())
        raise ValueError(f"Unknown persona '{persona}'. Valid options: {valid}")
    _write_config({"persona": persona})


def get_persona_mcp_context() -> str:
    """Return the mcp_context string for the current persona."""
    persona = get_persona()
    return PERSONAS[persona]["mcp_context"]


def format_currency(amount: float, persona: str | None = None) -> str:
    """
    Format a dollar amount for the given persona.

    finance: "$4,200/mo"
    finops:  "$4,200/mo"  (base value; callers can append variance hints)
    others:  "$4,200/mo"
    """
    if persona is None:
        persona = get_persona()
    formatted = f"${amount:,.0f}/mo"
    return formatted


def format_instance_type(instance_type: str, persona: str | None = None) -> str:
    """
    Format an instance type for the given persona.

    finance: "large compute instance"
    others:  the raw instance type string (e.g. "m5.4xlarge")
    """
    if persona is None:
        persona = get_persona()
    if persona == "finance":
        return "large compute instance"
    return instance_type


def is_technical_persona(persona: str | None = None) -> bool:
    """Return True for engineer, finops, platform, and agent personas."""
    if persona is None:
        persona = get_persona()
    return persona in {"engineer", "finops", "platform", "agent"}
