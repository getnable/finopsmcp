"""
Token-cost accounting for nable's MCP tools.

The margin problem: nable runs as an MCP server that an LLM calls. Every tool
response is injected into the model's context and billed as input tokens, often
re-read across several turns of an agentic loop. A careless tool that dumps a
full cost ledger can cost the customer more in tokens than the waste it surfaces.

This module makes every response measurable, bounded, and honest about its cost:

  1. estimate_tokens(obj)   how many tokens this response adds to context
  2. fit_to_budget(rows)    cap a row list to a token budget, report what was cut
  3. cost_note(...)         a short user-facing line: what it cost, what it found

The reduction strategy these enable:
  - aggregate server-side so the model never receives raw rows to sum itself
  - default to summaries, make raw detail opt-in
  - cap every response to a soft token ceiling, with a "narrow it down" hint
  - surface the cost so the customer sees the ROI, not a silent token meter
"""
from __future__ import annotations

import json
import os
from typing import Any

# Rough USD per 1K tokens of model input context. Tool responses are read by the
# model as input, frequently across multiple turns, so this is a floor estimate.
# Configurable so it tracks whatever model the customer actually runs.
_USD_PER_1K_TOKENS = float(os.getenv("FINOPS_USD_PER_1K_TOKENS", "0.003"))

# Soft ceiling for a single tool response. Above this, summarize instead of dump.
DEFAULT_MAX_TOKENS = int(os.getenv("FINOPS_MAX_RESPONSE_TOKENS", "6000"))


def estimate_tokens(obj: Any) -> int:
    """Approximate token count of a response.

    Uses the ~4-characters-per-token rule, accurate to within ~10-15% for
    JSON-ish English content and free of any tokenizer dependency. Good enough
    to budget against and to show the customer a credible cost.
    """
    if isinstance(obj, str):
        text = obj
    else:
        try:
            text = json.dumps(obj, default=str)
        except (TypeError, ValueError):
            text = str(obj)
    return max(1, len(text) // 4)


def estimate_cost_usd(tokens: int) -> float:
    return round(tokens / 1000 * _USD_PER_1K_TOKENS, 4)


def hoist_finding_boilerplate(report: dict, category_key: str = "category") -> dict:
    """Deduplicate per-category boilerplate across a report's findings.

    Envelope-style findings repeat the same fixed ``why`` paragraph and
    ``remediation`` steps on every finding of a category: 20 idle disks carry the
    same two sentences 20 times (~140 tokens each). This moves those shared strings
    into one top-level ``playbooks[category]`` entry and strips them from the
    individual findings, so the model reads the guidance once and each finding
    carries only its unique data. No information is lost; it changes where the
    text lives, not what is said. Only hoists when the text is identical across
    the category, a finding with bespoke text keeps it inline.
    """
    findings = report.get("findings") or []
    by_cat: dict[str, list[dict]] = {}
    for f in findings:
        env = f.get("finding")
        if isinstance(env, dict):
            by_cat.setdefault(str(f.get(category_key, "")), []).append(env)

    playbooks: dict[str, dict] = {}
    for cat, envs in by_cat.items():
        if not cat:
            continue
        shared: dict[str, Any] = {}
        for field in ("why", "remediation"):
            values = [json.dumps(e.get(field), default=str) for e in envs]
            if values[0] != "null" and all(v == values[0] for v in values):
                shared[field] = envs[0].get(field)
        if shared:
            playbooks[cat] = shared
            for e in envs:
                for field in shared:
                    e.pop(field, None)
    if playbooks:
        report["playbooks"] = playbooks
        report["playbooks_note"] = (
            "why/remediation for each finding lives once in playbooks, keyed by the "
            "finding's category."
        )
    return report


def fit_to_budget(
    rows: list,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overhead_tokens: int = 200,
) -> tuple[list, int]:
    """Cap a row list so the kept rows fit roughly within max_tokens.

    Assumes rows are pre-sorted by importance (most important first), so
    truncation drops the least important. Always keeps at least one row.
    Returns (kept_rows, omitted_count).
    """
    budget = max(0, max_tokens - overhead_tokens)
    kept: list = []
    used = 0
    for r in rows:
        cost = estimate_tokens(r)
        if used + cost > budget and kept:
            break
        kept.append(r)
        used += cost
    return kept, len(rows) - len(kept)


def cost_note(response: Any, savings_found_usd: float | None = None) -> str:
    """A short, honest, user-facing line about what a response cost to produce in
    model tokens, and the savings it surfaced if known. Turns the margin concern
    into visible ROI: the customer sees pennies spent against dollars found.
    """
    tokens = estimate_tokens(response)
    usd = estimate_cost_usd(tokens)
    note = f"This analysis added ~{tokens:,} tokens to context (~${usd:.2f})"
    if savings_found_usd:
        note += f" and surfaced ${savings_found_usd:,.0f}/mo in savings"
    return note + "."
