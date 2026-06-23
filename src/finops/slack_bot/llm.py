"""
Tiered Claude agentic loop for the Slack bot.

Model tiers (a cost tool should not quietly burn Opus tokens on "what did we
spend yesterday"):
  simple  Haiku   button follow-ups and quick lookups
  chat    Sonnet  free-text questions (mentions, DMs)
  rca     Opus    root-cause investigations

Overrides:
  FINOPS_SLACK_MODEL          force one model for every tier
  FINOPS_SLACK_MODEL_SIMPLE   per-tier override
  FINOPS_SLACK_MODEL_CHAT     per-tier override
  FINOPS_SLACK_MODEL_RCA      per-tier override

This module also fixes a real RBAC bug: identity was a ContextVar set in the
Slack handler thread, but the loop ran in a ThreadPoolExecutor thread where
ContextVars do not propagate. Identity is now passed explicitly and set inside
the worker thread before any tool runs.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

TIER_DEFAULTS = {
    "simple": "claude-haiku-4-5",
    "chat": "claude-sonnet-4-6",
    "rca": "claude-opus-4-8",
}

_QUERY_TIMEOUT = int(os.getenv("FINOPS_QUERY_TIMEOUT", "60"))
_RCA_TIMEOUT = int(os.getenv("FINOPS_RCA_TIMEOUT", "150"))
_MAX_TOOL_CALLS = int(os.getenv("FINOPS_MAX_TOOL_CALLS", "12"))

_RCA_TRIGGERS = (
    "why ",
    "why?",
    "why did",
    "investigat",
    "root cause",
    "rca",
    "what caused",
    "what changed",
    "what's driving",
    "whats driving",
    "what is driving",
    "spike",
    "explain the",
)

SYSTEM_PROMPT = """You are nable, a senior cloud FinOps analyst with read access to real
billing data across AWS, Azure, GCP, Kubernetes, SaaS, and AI/LLM providers, plus anomaly
detection, rightsizing, commitment analysis, and waste audits.

Voice: sharp, precise, plain. Write like a senior analyst dropping a tight internal note,
not like a chatbot. Lead with the number and the finding. Short sentences. No filler, no
preamble, no hedging.

Formatting rules, follow exactly:
- No emojis. None. Not for severity, not for decoration, not anywhere.
- No em dashes. Use a period, a comma, or a colon instead.
- No "TL;DR", no "Summary:", no cute headers. Just say the thing.
- Mark severity in plain words (High, Medium, Low), never with colored dots or icons.
- Money with $ and commas. Put resource ids, instance types, and regions in `backticks`.
- A short bold label and a tight bullet list are fine where they earn it. Prose is fine too.
  Do not over-structure a two-line answer into a template.

For "why did costs change" questions, use explain_recent_cost_drivers first, then drill
into the top driver with get_costs_by_service or the relevant audit tool. Lead with the
dollar impact, then the cause, then the next step.

When the user wants something fixed, you can draft a ticket (draft_ticket) or a Terraform
rightsizing PR (draft_rightsizing_pr) if those tools are available to you. Both only
create a preview card. A human must click Approve in Slack before anything is filed or
opened. Never claim a ticket or PR exists until it has been approved.

When a tool returns a `finding` object, it is already classified for you. Honor it exactly:
- kind "recommendation": we measured it. Give the precise dollar figure and the remediation,
  and stand behind it.
- kind "investigation": a real signal we have NOT confirmed. Present it as "let's look at this
  together": give the magnitude (for example "~thousands/mo"), never a precise dollar figure,
  lead with confirm_steps, and use why_unsure to be honest about what we could not verify. If
  pro_can_confirm is true, offer the pro_unlock: nable can confirm it automatically with deeper
  data access (CUR, CloudTrail) on Pro.
Never turn an investigation into a precise number or a confident recommendation.

Never make up data. Only report what the tools return. If no cloud provider is
connected (tools return nothing, or errors about no providers/credentials), don't
show a raw error. Say plainly that no cloud accounts are connected yet and that
whoever installed nable can connect one with `finops setup aws` (or azure/gcp).
Keep responses under 400 words unless the user asks for detail."""


def model_for_tier(tier: str) -> str:
    """Resolve the model for a tier, honoring env overrides."""
    master = os.getenv("FINOPS_SLACK_MODEL", "").strip()
    if master:
        return master
    per_tier = os.getenv(f"FINOPS_SLACK_MODEL_{tier.upper()}", "").strip()
    if per_tier:
        return per_tier
    return TIER_DEFAULTS.get(tier, TIER_DEFAULTS["chat"])


def pick_tier(text: str) -> str:
    """Route free text to a tier. Investigation language escalates to rca."""
    lowered = text.lower()
    if any(t in lowered for t in _RCA_TRIGGERS):
        return "rca"
    return "chat"


# ── Efficiency router ─────────────────────────────────────────────────────────
# A cost tool must not default to an expensive model. The router picks the
# CHEAPEST tier that fits the task and escalates only on a real signal, then
# clamps by the account's remaining managed-AI credit so spend can never run past
# what was prepaid. It chooses a difficulty TIER; model_for_tier resolves the
# actual model, and the per-tier env overrides can repoint a tier at any vetted
# provider (Anthropic direct, Bedrock, Vertex) without touching this logic.

# Signals that a question is analytical enough to warrant the mid tier (Sonnet)
# rather than the cheap tier (Haiku). RCA triggers (above) escalate further to Opus.
_CHAT_SIGNALS = (
    "compare", "optimi", "forecast", "break down", "breakdown", "recommend",
    "audit", "across", "commitment", "savings plan", "reserved", "rightsiz",
    "unit economic", "scenario", "trend", "by team", "by service", "by tag",
    "should i", "how much would", "what if",
)

# Cheaper turns get tighter tool budgets: fewer tool calls means fewer tokens.
_TIER_TOOL_CAP = {"simple": 6, "chat": 10, "rca": _MAX_TOOL_CALLS}


@dataclass
class RouteDecision:
    tier: str
    model: str
    max_tool_calls: int
    reason: str
    blocked: bool = False


def route_request(
    message: str,
    *,
    agent: str | None = None,
    budget_remaining: float | None = None,
    budget_total: float | None = None,
) -> RouteDecision:
    """Pick the cheapest model that fits the task, then clamp by budget.

    budget_remaining / budget_total are dollars of managed-AI credit for the
    account. None means no managed budget applies (for example BYO-key), so the
    router never blocks or degrades."""
    text = (message or "").lower()

    # 1. Out of credit: block. The surface offers a top-up or a BYO key.
    if budget_remaining is not None and budget_remaining <= 0:
        return RouteDecision("simple", model_for_tier("simple"), 4,
                             "managed-AI budget exhausted", blocked=True)

    # 2. Difficulty. Default to the cheap tier; escalate only on a real signal.
    if any(t in text for t in _RCA_TRIGGERS) or agent == "rca":
        tier = "rca"
    elif (agent in ("reco", "arch") or len(text) > 220
          or any(s in text for s in _CHAT_SIGNALS)):
        tier = "chat"
    else:
        tier = "simple"
    reason = f"matched {tier}"

    # 3. Budget-aware degrade: as credit runs low, drop the expensive tiers so the
    #    remaining budget stretches and the account stays margin-positive.
    if budget_total and budget_remaining is not None and budget_total > 0:
        frac = budget_remaining / budget_total
        if frac < 0.15 and tier == "rca":
            tier, reason = "chat", "downgraded rca to chat (low budget)"
        if frac < 0.05 and tier in ("rca", "chat"):
            tier, reason = "simple", "downgraded to simple (very low budget)"

    return RouteDecision(tier, model_for_tier(tier),
                         _TIER_TOOL_CAP.get(tier, _MAX_TOOL_CALLS), reason)


@dataclass
class LoopResult:
    answer: str
    side_effects: list[dict] = field(default_factory=list)  # e.g. approval cards to post
    input_tokens: int = 0   # summed across every model call in the loop
    output_tokens: int = 0  # summed across every model call in the loop


def record_managed_ai_usage(
    *,
    surface: str,
    tier: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    requested_by: str = "",
) -> None:
    """Emit one structured usage event per agent turn so credit billing can meter
    managed AI later. This is the metering seam: today it only writes a single
    parseable log line. A hosted control plane can tail these, or swap the body for
    a real ledger write, without touching the agent loop. Never raises into the
    caller: a metering failure must not break a user's answer."""
    try:
        event = {
            "event": "managed_ai_usage",
            "surface": surface,
            "tier": tier,
            "model": model,
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "total_tokens": int(input_tokens or 0) + int(output_tokens or 0),
            "requested_by": requested_by or "",
        }
        log.info("managed_ai_usage %s", json.dumps(event, default=str))
    except Exception as exc:
        log.debug("record_managed_ai_usage failed: %s", exc)
    # Persist this turn's cost to the credit ledger so the router can clamp spend
    # to the prepaid balance. Isolated from the log above and best-effort: a ledger
    # failure must not break the answer or swallow the usage event.
    try:
        from ..billing import credits

        credits.record_spend(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            surface=surface,
            requested_by=requested_by,
        )
    except Exception as exc:
        log.debug("ledger write failed: %s", exc)


def _run_agent_loop_sync(
    user_message: str,
    *,
    tier: str,
    identity: Any = None,
    history: list[dict] | None = None,
    requested_by: str = "",
    max_tool_calls: int | None = None,
) -> LoopResult:
    """The agentic loop. Runs inside the worker thread; sets identity there."""
    try:
        import anthropic
    except ImportError:
        log.error('anthropic package not installed; install "finops-mcp[slack]"')
        return LoopResult("nable isn't fully set up yet. Ask whoever installed me to finish configuring it.")

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set; the Slack bot cannot call the model")
        return LoopResult("nable isn't fully set up yet (no AI key configured). Ask whoever installed me to finish setup.")

    # Identity must be set in THIS thread: ContextVars do not cross executor threads.
    role = "admin"
    if identity is not None:
        from ..auth.rbac import set_current_identity

        set_current_identity(identity)
        role = identity.role

    from . import remediation
    from .bridge import execute_bridge_tool, get_bridge_tools

    tools = list(get_bridge_tools(role))
    remediation_names = set()
    if remediation.role_can_draft(role) and remediation.drafting_enabled():
        drafts = remediation.draft_tool_schemas()
        remediation_names = {t["name"] for t in drafts}
        tools = tools + drafts

    # Prompt caching: the tool block is identical across calls, so cache it.
    if tools:
        tools = tools[:-1] + [{**tools[-1], "cache_control": {"type": "ephemeral"}}]

    if max_tool_calls is None:
        max_tool_calls = _MAX_TOOL_CALLS

    side_effects: list[dict] = []
    # Token totals for the managed-AI metering hook. Summed across every model
    # call this turn makes (tool-use loops can be several round-trips).
    usage = {"input": 0, "output": 0}

    def _append_cost_card(result_str: str, sinks: list[dict]) -> None:
        """When slice_costs runs, surface its renderable card on side_effects so the
        web Ask tab can draw the chart and offer 'Pin to dashboard'. Read-only."""
        try:
            data = json.loads(result_str)
        except (ValueError, TypeError):
            return
        if isinstance(data, dict) and data.get("card"):
            sinks.append({"type": "cost_card", "card": data["card"], "data": data.get("result")})

    def _append_pinned_view(result_str: str, sinks: list[dict]) -> None:
        """When pin_view runs, flag that the pinned-views set changed so the web
        dashboard can re-fetch /api/views and slide the new card in live, without a
        full reload. Read-only on the cloud: pin_view only writes the local
        dashboard_views table. Carries the new view id so the UI can highlight it."""
        try:
            data = json.loads(result_str)
        except (ValueError, TypeError):
            return
        if isinstance(data, dict) and data.get("pinned") and data.get("id") is not None:
            sinks.append({
                "type": "view_pinned",
                "id": data.get("id"),
                "title": data.get("title", ""),
            })

    def _accumulate_usage(response: Any) -> None:
        u = getattr(response, "usage", None)
        if u is None:
            return
        usage["input"] += int(getattr(u, "input_tokens", 0) or 0)
        usage["output"] += int(getattr(u, "output_tokens", 0) or 0)

    client = anthropic.Anthropic(api_key=api_key)
    messages: list[dict] = list(history or []) + [{"role": "user", "content": user_message}]
    model = model_for_tier(tier)

    for _ in range(max_tool_calls):
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=tools,
            messages=messages,
        )
        _accumulate_usage(response)
        text_parts = [b.text for b in response.content if hasattr(b, "text")]
        if response.stop_reason == "end_turn":
            return LoopResult("\n".join(text_parts).strip(), side_effects,
                              usage["input"], usage["output"])
        if response.stop_reason != "tool_use":
            return LoopResult("\n".join(text_parts).strip() or "No response.", side_effects,
                              usage["input"], usage["output"])

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            if block.name in remediation_names:
                result_str = remediation.execute_draft_tool(
                    block.name, block.input or {}, requested_by=requested_by, side_effects=side_effects
                )
            else:
                result_str = execute_bridge_tool(block.name, block.input or {}, role=role)
                if block.name == "slice_costs":
                    _append_cost_card(result_str, side_effects)
                elif block.name == "pin_view":
                    _append_pinned_view(result_str, side_effects)
            tool_results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": result_str}
            )
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return LoopResult(
        "Reached maximum tool call depth. Please try a more specific question.", side_effects,
        usage["input"], usage["output"]
    )


def ask(
    user_message: str,
    *,
    tier: str = "chat",
    identity: Any = None,
    history: list[dict] | None = None,
    requested_by: str = "",
    max_tool_calls: int | None = None,
) -> LoopResult:
    """Run the agentic loop with a wall-clock timeout."""
    import concurrent.futures

    timeout = _RCA_TIMEOUT if tier == "rca" else _QUERY_TIMEOUT
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            _run_agent_loop_sync,
            user_message,
            tier=tier,
            identity=identity,
            history=history,
            requested_by=requested_by,
            max_tool_calls=max_tool_calls,
        )
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            # Strip CR/LF so a crafted message can't forge log lines (log-injection).
            _msg = user_message[:100].replace("\n", " ").replace("\r", " ")
            log.warning("Claude query timed out after %ds: %s", timeout, _msg)
            return LoopResult(
                f"Sorry, that took longer than {timeout} seconds and was stopped. "
                "Try a more specific question or break it into smaller parts."
            )
