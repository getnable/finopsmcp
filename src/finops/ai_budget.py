"""
Local budget for your AI coding agent.

nable's cost tools point at your cloud. This one points at the agent itself: the
tokens Claude Code / Cursor burn against your Claude or Cursor plan, and the dollars
a metered API key spends. It answers one question before you (or the agent) kick off
a big task: "am I about to blow my budget?"

Two honest data sources, no guessing:

  1. Local usage meter (subscription plans). Claude Code writes every message's real
     token usage to ~/.claude/projects/**/*.jsonl. We tally it over a rolling window
     and month-to-date. This is exact token counts, read locally, nothing leaves the
     machine. What we deliberately do NOT do: claim a percentage of Anthropic's Max
     rate-limit. That number is not exposed by any API, so we report real burn rate
     against YOUR budget instead of a fabricated "% of plan left".

  2. Metered API spend (pay-per-token keys). get_all_llm_costs gives real provider
     dollars month-to-date. Precise budgeting for OpenAI/Anthropic/Bedrock API keys.

The gate, `check(...)`, mirrors policy.py: ok / warn / over, advice only. It never
stops the agent; it tells you where you stand so you decide.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import llm_prices, token_budget

# ── Verdicts (mirror policy.py's vocabulary) ─────────────────────────────────
BUDGET_OK = "ok"        # comfortably under budget
BUDGET_WARN = "warn"    # crossed the warn threshold (default 80%)
BUDGET_OVER = "over"    # at or past the budget

_WARN_AT = float(os.getenv("FINOPS_AI_BUDGET_WARN_PCT", "0.80"))

# A rolling window for "right now" usage. Claude's heaviest plan gate is a ~5h
# window, so 5h is a sensible default to show burn against. Configurable.
_WINDOW_HOURS = float(os.getenv("FINOPS_AI_WINDOW_HOURS", "5"))

# API-equivalent dollars so a subscription user sees a figure they can reason about
# ("this session would be ~$18 on the API"). Not what the plan charges (that is
# flat); it is the metered-equivalent, priced per response at the list rate of the
# model that produced it (llm_prices). A model that table does not know falls back
# to this blended rate, Sonnet 4.6's by default, and is reported as unpriced rather
# than silently folded in. Configurable for the model you run.
_BLEND = llm_prices.MODEL_PRICES["claude-sonnet-4-6"]
_FALLBACK = llm_prices.ModelPrice(
    model="fallback", provider="fallback",
    input=float(os.getenv("FINOPS_AI_USD_PER_MTOK_IN", str(_BLEND.input))),
    output=float(os.getenv("FINOPS_AI_USD_PER_MTOK_OUT", str(_BLEND.output))),
    cache_write_5m=float(os.getenv("FINOPS_AI_USD_PER_MTOK_CACHE_WRITE",
                                   str(_BLEND.cache_write_5m))),
    cache_write_1h=float(os.getenv("FINOPS_AI_USD_PER_MTOK_CACHE_WRITE_1H",
                                   str(_BLEND.cache_write_1h))),
    cache_read=float(os.getenv("FINOPS_AI_USD_PER_MTOK_CACHE_READ", str(_BLEND.cache_read))),
)

def _data_dir() -> Path:
    d = Path(os.getenv("FINOPS_DATA_DIR") or (Path.home() / ".nable"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _budget_path() -> Path:
    return _data_dir() / "ai-budget.json"


_SESSION_CAPS_KEPT = 100


# ── Budget config ────────────────────────────────────────────────────────────

def get_budget() -> dict[str, Any]:
    """The user's AI budget. Empty/zero fields mean 'not set'.

    mode: 'flat' (a subscription, budget = plan_cost + optional usage cap) or
    'metered' (pay-per-token, budget = spend_cap). monthly_tokens is a usage cap
    that works in either mode ("warn before I burn N tokens").

    session_cap is a per-task cap in list-price dollars, measured against one
    Claude Code session (its subagents included) rather than the month:
    "this task may spend at most $40". It applies to every session;
    session_caps overrides it for named sessions."""
    # on_breach: what the guard does when usage crosses the budget.
    #   "notify" (default) -> the agent asks the human before continuing
    #   "stop"             -> the agent is denied outright
    # Asked once at `nable guard install`. Default is notify because a tool that
    # silently halts your agent gets uninstalled before anyone finds the setting
    # that caused it; stopping has to be a thing you chose.
    default = {"mode": "", "plan_cost": 0.0, "spend_cap": 0.0,
               "monthly_tokens": 0, "plan_label": "", "set_at": 0.0,
               "on_breach": "notify", "session_cap": 0.0, "session_caps": {}}
    try:
        data = json.loads(_budget_path().read_text())
        if isinstance(data, dict):
            default.update({k: data[k] for k in default if k in data})
    except (OSError, ValueError):
        pass
    caps = default["session_caps"]
    default["session_caps"] = ({str(k): float(v) for k, v in caps.items()
                                if isinstance(v, (int, float)) and v > 0}
                               if isinstance(caps, dict) else {})
    return default


def set_budget(mode: str | None = None, plan_cost: float | None = None,
               spend_cap: float | None = None, monthly_tokens: int | None = None,
               plan_label: str | None = None,
               on_breach: str | None = None,
               session_cap: float | None = None,
               session_id: str | None = None) -> dict[str, Any]:
    """Set the AI budget. `mode` is 'flat' (subscription: pass plan_cost) or
    'metered' (pay-per-token: pass spend_cap). Passing plan_cost/spend_cap infers
    the mode. monthly_tokens is an optional usage cap for either. Any subset.

    session_cap with a session_id caps that one session; without one it is the
    cap for every session. 0 clears it."""
    b = get_budget()
    if mode in ("flat", "metered"):
        b["mode"] = mode
    if plan_cost is not None:
        b["plan_cost"] = max(0.0, float(plan_cost))
        if not b["mode"]:
            b["mode"] = "flat"
    if spend_cap is not None:
        b["spend_cap"] = max(0.0, float(spend_cap))
        if not b["mode"]:
            b["mode"] = "metered"
    if monthly_tokens is not None:
        b["monthly_tokens"] = max(0, int(monthly_tokens))
    if plan_label is not None:
        b["plan_label"] = plan_label
    if on_breach in ("stop", "notify"):
        b["on_breach"] = on_breach
    if session_cap is not None:
        cap = max(0.0, float(session_cap))
        if session_id:
            caps = b["session_caps"]
            caps.pop(session_id, None)          # re-insert so the newest is last
            if cap > 0:
                caps[session_id] = cap
            # One entry per task someone capped; keep the recent ones.
            b["session_caps"] = dict(list(caps.items())[-_SESSION_CAPS_KEPT:])
        else:
            b["session_cap"] = cap
    b["set_at"] = time.time()
    fd = os.open(_budget_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(b, fh)
    return b


def reset_budget() -> None:
    """Forget the saved budget (so the next run re-asks)."""
    try:
        _budget_path().unlink()
    except OSError:
        pass


# ── Local usage meter (Claude Code session logs) ─────────────────────────────

def _claude_projects_dir() -> Path:
    base = os.getenv("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def read_agent_usage(since_epoch: float) -> dict[str, Any]:
    """Tally Claude Code token usage across all local sessions since `since_epoch`.

    Exact counts, read locally. Skips log files whose mtime predates the window so a
    long history stays cheap. Returns totals, a per-model split of tokens and of
    list-price dollars, the models priced at the fallback rate, the costliest
    sessions, and first/last activity.
    """
    proj = _claude_projects_dir()
    if not proj.is_dir():
        return _tally([], source_present=False)
    return _tally(_responses(proj, since_epoch), source_present=True)


def read_session_usage(session_id: str) -> dict[str, Any]:
    """Everything one Claude Code session has used, from its first response.

    A session is the sessionId Claude Code stamps on every transcript line, so
    the subagents it spawned count toward it: they are part of the same task.
    """
    proj = _claude_projects_dir()
    if not proj.is_dir() or not session_id:
        return _tally([], source_present=False)
    return _tally(_responses(proj, 0, session_id=session_id), source_present=True)


# Claude Code names a session's transcript <project>/<sessionId>.jsonl and puts its
# subagents under <project>/<sessionId>/subagents/. A session is found by those
# names and nothing else: the guard reads it on every tool call, and falling back
# to a scan of the whole history would put seconds on each one. An id that is not
# safe to put in a glob matches nothing.
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _transcripts(proj: Path, session_id: str | None) -> list[Path]:
    if not session_id:
        return list(proj.rglob("*.jsonl"))
    if not _SAFE_SESSION_ID.match(session_id):
        return []
    return [*proj.glob(f"*/{session_id}.jsonl"), *proj.glob(f"*/{session_id}/**/*.jsonl")]


def _path_session(path: Path) -> str:
    return path.parent.parent.name if path.parent.name == "subagents" else path.stem


def _responses(proj: Path, since_epoch: float,
               session_id: str | None = None) -> list[dict[str, Any]]:
    """One entry per API response in the window, however many lines logged it.

    Claude Code writes a transcript line per content block of a response
    (thinking, text, each tool_use), and every one of those lines carries the
    whole response's usage. Summing lines counted each response's input and
    cache tokens once per block: about 2x on real transcripts, with output 1.3x
    because output_tokens grows as the blocks stream. Keyed on the response, the
    last line wins, and it holds the final output count. A line with no message
    id (older logs) is its own response.
    """
    seen: dict[Any, dict[str, Any]] = {}
    for path in _transcripts(proj, session_id):
        try:
            if path.stat().st_mtime < since_epoch - 1:
                continue  # whole file is older than the window
        except OSError:
            continue
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                for lineno, line in enumerate(fh):
                    if '"usage"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    ts = _rec_epoch(rec.get("timestamp"))
                    if ts is None or ts < since_epoch:
                        continue
                    msg = rec.get("message") or {}
                    usage = msg.get("usage") or {}
                    if not usage:
                        continue
                    ti = int(usage.get("input_tokens", 0) or 0)
                    to = int(usage.get("output_tokens", 0) or 0)
                    cw = int(usage.get("cache_creation_input_tokens", 0) or 0)
                    cr = int(usage.get("cache_read_input_tokens", 0) or 0)
                    if ti == to == cw == cr == 0:
                        continue
                    session = str(rec.get("sessionId") or _path_session(path))
                    if session_id and session != session_id:
                        continue
                    # Claude Code writes its main-thread cache with the 1-hour TTL,
                    # billed at 2x input against 1.25x for the 5-minute one. The
                    # split is in usage.cache_creation; anything it does not
                    # account for is priced as a 5-minute write.
                    split = usage.get("cache_creation")
                    cw_1h = 0
                    if isinstance(split, dict):
                        cw_1h = min(cw, int(split.get("ephemeral_1h_input_tokens", 0) or 0))
                    key = ((msg.get("id"), rec.get("requestId")) if msg.get("id")
                           else (str(path), lineno))
                    seen[key] = {
                        "ts": ts, "model": str(msg.get("model", "") or "unknown"),
                        "input": ti, "output": to, "cache_write": cw, "cache_read": cr,
                        "cache_write_1h": cw_1h,
                        "fast": usage.get("speed") == "fast",
                        "us_only": usage.get("inference_geo") == "us",
                        "session": session, "cwd": rec.get("cwd"),
                    }
        except OSError:
            continue
    return list(seen.values())


_SESSIONS_LISTED = 20


def _response_usd(r: dict[str, Any]) -> tuple[float, bool]:
    """(list-price USD, priced) for one response. Unpriced means the fallback rate."""
    price = llm_prices.price_for(r["model"])
    usd = (price or _FALLBACK).cost(
        input_tokens=r["input"], output_tokens=r["output"],
        cache_write_5m_tokens=r["cache_write"] - r["cache_write_1h"],
        cache_write_1h_tokens=r["cache_write_1h"], cache_read_tokens=r["cache_read"],
        fast=r["fast"], us_only=r["us_only"])
    return usd, price is not None


def _tally(responses: list[dict[str, Any]], source_present: bool) -> dict[str, Any]:
    tin = tout = cwrite = cread = 0
    usd_total = 0.0
    by_model: dict[str, int] = {}
    usd_by_model: dict[str, float] = {}
    unpriced: dict[str, int] = {}
    sessions: dict[str, dict[str, Any]] = {}
    first_ts: float | None = None
    last_ts: float | None = None
    for r in responses:
        ti, to, cw, cr = r["input"], r["output"], r["cache_write"], r["cache_read"]
        tin += ti; tout += to; cwrite += cw; cread += cr
        model = r["model"]
        by_model[model] = by_model.get(model, 0) + ti + to + cw + cr
        usd, priced = _response_usd(r)
        usd_total += usd
        usd_by_model[model] = usd_by_model.get(model, 0.0) + usd
        if not priced:
            unpriced[model] = unpriced.get(model, 0) + ti + to + cw
        ts = r["ts"]
        first_ts = ts if first_ts is None else min(first_ts, ts)
        last_ts = ts if last_ts is None else max(last_ts, ts)
        sess = sessions.setdefault(r["session"], {
            "usd_equivalent": 0.0, "billable_tokens": 0, "messages": 0,
            "first_activity": ts, "last_activity": ts, "project": None})
        sess["usd_equivalent"] += usd
        sess["billable_tokens"] += ti + to + cw
        sess["messages"] += 1
        sess["last_activity"] = max(sess["last_activity"], ts)
        # Named for where the session started: a subagent's worktree later on
        # is still the same task.
        if r.get("cwd") and (sess["project"] is None or ts < sess["first_activity"]):
            sess["project"] = Path(r["cwd"]).name
        sess["first_activity"] = min(sess["first_activity"], ts)

    # "Billable" = the tokens that represent real new work and cost: input, output,
    # and cache creation. cache_read is Claude Code re-reading its own cached context
    # every turn; it is cheap and would otherwise dwarf every other number, so it is
    # reported separately and NOT the headline the budget measures against.
    billable = tin + tout + cwrite
    out = {
        "input_tokens": tin, "output_tokens": tout,
        "cache_creation_tokens": cwrite, "cache_read_tokens": cread,
        "billable_tokens": billable, "total_tokens": billable + cread,
        "messages": len(responses),
        "usd_equivalent": round(usd_total, 2),
        "by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1])),
        "cost_by_model": {m: round(v, 2) for m, v in
                          sorted(usd_by_model.items(), key=lambda kv: -kv[1])},
        # Billable tokens from models llm_prices has no confirmed rate for. Their
        # dollars are in usd_equivalent at the fallback rate, and named here so a
        # figure built on a guessed rate is never presented as a list price.
        "unpriced_models": dict(sorted(unpriced.items(), key=lambda kv: -kv[1])),
        # The costliest sessions, each one task's worth of agent work. Capped so a
        # month of sessions cannot swamp an MCP response; session_count is all.
        "by_session": {sid: {**v, "usd_equivalent": round(v["usd_equivalent"], 2)}
                       for sid, v in sorted(sessions.items(),
                                            key=lambda kv: -kv[1]["usd_equivalent"])
                       [:_SESSIONS_LISTED]},
        "session_count": len(sessions),
        "prices_as_of": llm_prices.AS_OF,
        "first_activity": first_ts, "last_activity": last_ts,
        "source_present": source_present,
    }
    if unpriced:
        out["unpriced_note"] = (
            f"{', '.join(unpriced)} priced at the fallback ${_FALLBACK.input:g}/"
            f"${_FALLBACK.output:g} per 1M in/out; set FINOPS_AI_USD_PER_MTOK_IN/OUT "
            f"to the rate you pay.")
    return out


def _rec_epoch(ts: Any) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _month_start_epoch() -> float:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


# ── The current session ──────────────────────────────────────────────────────

def resolve_session(session_id: str | None = None) -> tuple[str | None, str | None]:
    """(session id, where it came from) for "this session".

    In order: an id the caller passed (the guard hook has it in its payload),
    CLAUDE_CODE_SESSION_ID (Claude Code sets it for the processes it starts),
    then the session whose transcript was written last, which is the one calling
    when only one agent is running. The source is returned so a guess is never
    reported as a fact.
    """
    if session_id:
        return str(session_id), "argument"
    env = os.getenv("CLAUDE_CODE_SESSION_ID", "").strip()
    if env:
        return env, "env"
    proj = _claude_projects_dir()
    latest: tuple[float, Path] | None = None
    try:
        for path in proj.rglob("*.jsonl"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if latest is None or mtime > latest[0]:
                latest = (mtime, path)
    except OSError:
        return None, None
    if latest is None:
        return None, None
    return _path_session(latest[1]), "latest_activity"


def _session_lens(budget: dict[str, Any], session_id: str | None) -> dict[str, Any] | None:
    sid, source = resolve_session(session_id)
    if not sid:
        return None
    u = read_session_usage(sid)
    cap = budget["session_caps"].get(sid) or budget["session_cap"] or 0.0
    usd = u["usd_equivalent"]
    pct = usd / cap if cap > 0 else None
    return {
        "id": sid, "id_source": source,
        "usd_equivalent": usd, "billable_tokens": u["billable_tokens"],
        "messages": u["messages"],
        "cost_by_model": u["cost_by_model"], "unpriced_models": u["unpriced_models"],
        "first_activity": u["first_activity"], "last_activity": u["last_activity"],
        "cap_usd": cap or None,
        "cap_scope": ("this_session" if sid in budget["session_caps"]
                      else "every_session" if cap else None),
        "pct_of_cap": round(pct, 3) if pct is not None else None,
        "remaining_usd": round(max(0.0, cap - usd), 2) if cap > 0 else None,
        "verdict": _verdict(pct) if pct is not None else None,
    }


def _verdict(pct: float) -> str:
    return BUDGET_OVER if pct >= 1.0 else (BUDGET_WARN if pct >= _WARN_AT else BUDGET_OK)


_RANK = {BUDGET_OK: 0, BUDGET_WARN: 1, BUDGET_OVER: 2}


# ── Status + gate ────────────────────────────────────────────────────────────

def status(session_id: str | None = None) -> dict[str, Any]:
    """Where you stand. Honest by construction: tokens and burn rate are exact
    (read from local logs); dollars are ONLY ever an estimate at list price, never
    your real bill. Two lenses:
      - metered (pay-per-token / enterprise): a dollar spend budget, gate on it.
      - flat (subscription): budget your USAGE so you do not run out, and see how
        much subsidized compute you are pulling for your fixed fee.
    Either can add a per-session cap, which gates this session's own spend; the
    verdict is the worse of the two. `session_id` names the session (see
    resolve_session for what "this session" means without one)."""
    now = time.time()
    window = read_agent_usage(now - _WINDOW_HOURS * 3600)
    mtd = read_agent_usage(_month_start_epoch())
    budget = get_budget()
    mode = budget["mode"]

    tokens_mtd = mtd["billable_tokens"]        # exact
    est_usd_mtd = mtd["usd_equivalent"]        # ESTIMATE at list price, not a bill

    mtok = tokens_mtd / 1e6 if tokens_mtd else 0.0
    cost_per_1m_list = round(est_usd_mtd / mtok, 2) if mtok else None
    cost_per_1m_effective = (round(budget["plan_cost"] / mtok, 2)
                             if mode == "flat" and budget["plan_cost"] > 0 and mtok else None)

    # Verdict off the honest dial for the lens: metered → the dollar spend cap
    # (labeled estimate until a real Cost API is wired); either mode → a usage cap.
    verdict, pct, basis = BUDGET_OK, None, "none"
    if mode == "metered" and budget["spend_cap"] > 0:
        pct, basis = est_usd_mtd / budget["spend_cap"], "spend"
    elif budget["monthly_tokens"] > 0:
        pct, basis = tokens_mtd / budget["monthly_tokens"], "tokens"
    if pct is not None:
        verdict = _verdict(pct)

    # The per-session cap can only make the verdict worse. On a tie the one
    # further past its line is the one to name.
    session = _session_lens(budget, session_id)
    if session and session["verdict"] is not None:
        s_rank, m_rank = _RANK[session["verdict"]], _RANK[verdict]
        if basis == "none" or s_rank > m_rank or (
                s_rank == m_rank and s_rank > 0 and session["pct_of_cap"] > (pct or 0)):
            verdict, pct, basis = session["verdict"], session["pct_of_cap"], "session"

    headroom = {
        "session_usd": session["remaining_usd"] if session else None,
        "month_usd": (round(max(0.0, budget["spend_cap"] - est_usd_mtd), 2)
                      if mode == "metered" and budget["spend_cap"] > 0 else None),
        "month_tokens": (max(0, budget["monthly_tokens"] - tokens_mtd)
                         if budget["monthly_tokens"] > 0 else None),
    }

    burn = round(window["billable_tokens"] / max(_WINDOW_HOURS, 0.1))

    subsidy = None
    if mode == "flat" and budget["plan_cost"] > 0:
        subsidy = {
            "plan_cost_usd": budget["plan_cost"],
            "compute_value_est_usd": est_usd_mtd,
            "multiple": round(est_usd_mtd / budget["plan_cost"], 1) if budget["plan_cost"] else None,
        }

    return {
        "verdict": verdict,
        "verdict_basis": basis,
        "mode": mode,
        "window_hours": _WINDOW_HOURS,
        "window": window,
        "month_to_date": mtd,
        "billable_tokens_mtd": tokens_mtd,
        "est_usd_mtd_list_price": est_usd_mtd,
        "budget": budget,
        "plan_label": budget["plan_label"],
        "pct_of_budget": round(pct, 3) if pct is not None else None,
        "session": session,
        "headroom": headroom,
        "burn_tokens_per_hour": burn,
        "subsidy": subsidy,
        "cost_per_1m_list": cost_per_1m_list,
        "cost_per_1m_effective": cost_per_1m_effective,
        "summary": _summary_line(verdict, basis, mode, tokens_mtd, est_usd_mtd, budget,
                                 subsidy, window, cost_per_1m_effective, session),
    }


def _summary_line(verdict, basis, mode, tokens_mtd, est_usd, budget, subsidy, window,
                  eff_per_1m, session=None) -> str:
    tag = {BUDGET_OK: "on track", BUDGET_WARN: "approaching your budget",
           BUDGET_OVER: "over budget"}[verdict]
    if basis == "session":
        return (f"~${session['usd_equivalent']:,.2f} of this session's "
                f"${session['cap_usd']:,.2f} cap used (estimated at list price), {tag}.")
    if basis == "spend":
        return (f"~${est_usd:,.0f} estimated at list price of your "
                f"${budget['spend_cap']:,.0f} spend cap, {tag}. "
                f"Connect an Admin key for exact spend.")
    if basis == "tokens":
        return f"{tokens_mtd:,} of {budget['monthly_tokens']:,} tokens this month, {tag}."
    if subsidy and subsidy["multiple"]:
        extra = f" ~${eff_per_1m:g}/1M effective." if eff_per_1m else ""
        return (f"You pay ${subsidy['plan_cost_usd']:,.0f}/mo and have pulled "
                f"~${est_usd:,.0f} of compute (estimated at list price), "
                f"~{subsidy['multiple']:g}x your plan.{extra} The provider covers the rest.")
    # A budget IS configured but there is no usage to measure against yet. Confirm
    # it, never tell someone to set a budget they just set (found dogfooding).
    if mode == "flat" and budget["plan_cost"] > 0:
        return (f"Your ${budget['plan_cost']:,.0f}/mo plan is set. No agent usage recorded yet "
                f"this month; the numbers fill in as your agent runs.")
    if mode == "metered" and budget["spend_cap"] > 0:
        return (f"Your ${budget['spend_cap']:,.0f}/mo spend cap is set. No agent usage recorded "
                f"yet this month.")
    if budget["monthly_tokens"] > 0:
        return (f"Usage cap of {budget['monthly_tokens']:,} tokens/mo is set. No agent usage "
                f"recorded yet this month.")
    return (f"{window['billable_tokens']:,} tokens in the last {_WINDOW_HOURS:g}h "
            f"(~${window['usd_equivalent']:,.0f} at list price). "
            f"Run `nable ai-budget` to set a budget.")


def check(estimated_next_tokens: int = 0, session_id: str | None = None) -> dict[str, Any]:
    """The gate the agent calls before a big task. Advice only, never blocks.

    Metered plan: about dollars against your spend cap (estimated). Flat plan: about
    usage and not getting rate-limited, spoken in tokens and burn rate, never a fake
    dollar overage. Mirrors policy.py: a verdict and a reason, the human decides."""
    st = status(session_id=session_id)
    verdict, reason = st["verdict"], st["summary"]

    # A token/usage budget is exact, so a next-task estimate can honestly tip it.
    if estimated_next_tokens and st["budget"]["monthly_tokens"] > 0:
        after = st["billable_tokens_mtd"] + estimated_next_tokens
        pct_after = after / st["budget"]["monthly_tokens"]
        if pct_after >= 1.0 and verdict != BUDGET_OVER:
            verdict = BUDGET_WARN
            reason = (f"this task (~{estimated_next_tokens:,} tokens) would push you to "
                      f"{pct_after * 100:.0f}% of your monthly token budget.")

    if st["verdict_basis"] == "session":
        s = st["session"]
        left, cap = s["remaining_usd"], s["cap_usd"]
        rec = {
            BUDGET_OK: f"Proceed. ~${left:,.2f} of this session's ${cap:,.2f} cap left.",
            BUDGET_WARN: (f"Proceed with a tight scope: ~${left:,.2f} of this session's "
                          f"${cap:,.2f} cap left."),
            BUDGET_OVER: (f"This session is past its ${cap:,.2f} cap. Confirm with the "
                          f"human before continuing."),
        }[verdict]
    elif st["mode"] == "metered":
        rec = {
            BUDGET_OK: "Proceed.",
            BUDGET_WARN: "Proceed, but you are close to your spend cap. Consider a tighter scope.",
            BUDGET_OVER: "You are over your AI spend cap. Confirm with the human before continuing.",
        }[verdict]
    else:
        rec = {
            BUDGET_OK: f"Proceed. Flat plan, so this is about pace, not a bill: ~{st['burn_tokens_per_hour']:,} tok/hr.",
            BUDGET_WARN: "Proceed, but you are near the usage budget you set for the month.",
            BUDGET_OVER: "You are past the usage budget you set. Confirm with the human first.",
        }[verdict]

    return {
        "verdict": verdict,
        "reason": reason,
        "recommendation": rec,
        "advice_only": True,
        "mode": st["mode"],
        "billable_tokens_mtd": st["billable_tokens_mtd"],
        "est_usd_mtd_list_price": st["est_usd_mtd_list_price"],
        "burn_tokens_per_hour": st["burn_tokens_per_hour"],
        "verdict_basis": st["verdict_basis"],
        "session": st["session"],
        "headroom": st["headroom"],
    }
