"""`finops ai-budget` — set and check a local budget for your AI coding agent.

Run bare `finops ai-budget` the first time and it asks you two questions (flat
subscription or metered API, and what you pay), then remembers. After that, bare
`finops ai-budget` just prints where you stand: this window, month to date, your
budget, this session against its cap, burn rate, and what each model and each
session cost. Flags (--plan-cost / --spend-cap / --tokens / --session-cap) skip
the questions for scripts. Numbers come from finops.ai_budget: real local token usage from Claude
Code's session logs and Codex CLI's rollouts, split by agent. Nothing leaves your machine, except
that Cursor's usage is read from its Admin API when CURSOR_ADMIN_API_KEY is set.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

_ACCENT = "\033[38;5;38m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_WARN = "\033[38;5;208m"
_OVER = "\033[38;5;203m"
_OK = "\033[38;5;71m"
_RST = "\033[0m"


def _c(s: str, color: str) -> str:
    return s if not sys.stdout.isatty() else f"{color}{s}{_RST}"


def _tok(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n/1e9:.1f}B"
    if n >= 1_000_000:
        return f"{n/1e6:.1f}M"
    if n >= 1_000:
        return f"{n/1e3:.0f}K"
    return str(n)


def _num(raw: str) -> float:
    """Parse '100', '$2,500', '2.5k', '1m' into a float. Returns 0.0 on garbage."""
    raw = raw.strip().lower().replace(",", "").replace("$", "").replace(" ", "")
    if not raw:
        return 0.0
    mult = 1.0
    if raw[-1] in "kmb":
        mult = {"k": 1e3, "m": 1e6, "b": 1e9}[raw[-1]]
        raw = raw[:-1]
    try:
        return float(raw) * mult
    except ValueError:
        return 0.0


def add_parser(sub) -> None:
    p = sub.add_parser(
        "ai-budget",
        help="Set and check a local budget for your AI coding agent",
        description="A local budget for your coding agent's own spend. First run asks "
                    "two questions; after that it just reports. Reads Claude Code and Codex "
                    "CLI usage locally, nothing leaves your machine (Cursor usage too, "
                    "from its Admin API, when CURSOR_ADMIN_API_KEY is set).",
    )
    p.add_argument("--plan-cost", type=float, metavar="USD",
                   help="Flat plan: what you pay per month, any number, e.g. --plan-cost 100")
    p.add_argument("--spend-cap", type=float, metavar="USD",
                   help="Metered API: monthly dollar cap, e.g. --spend-cap 2500")
    p.add_argument("--tokens", type=int, metavar="N",
                   help="Usage cap: warn before N billable tokens/month (either mode)")
    p.add_argument("--session-cap", type=float, metavar="USD",
                   help="Per-task cap: what one agent session may spend at list price, "
                        "e.g. --session-cap 40 (0 clears)")
    p.add_argument("--month", action="store_true",
                   help="Split cost by model and session over the month, not the 5h window")
    p.add_argument("--reset", action="store_true", help="Forget the saved budget")
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p.set_defaults(cmd="ai-budget")


def _interactive_setup(ab, out) -> None:
    """Two questions, asked only when nothing is configured and we have a TTY."""
    print(_c("Set a budget for your AI coding agent.", _BOLD), file=out)
    print(_c("Two quick questions. Everything stays on this machine.", _DIM), file=out)
    print(file=out)
    print("  How do you pay for your coding agent?", file=out)
    print(f"    {_c('1', _ACCENT)}  Flat subscription   (Claude Pro/Max, Cursor, ...)", file=out)
    print(f"    {_c('2', _ACCENT)}  Metered API / work  (pay per token)", file=out)
    try:
        kind = input("  Choose 1 or 2: ").strip()
        if kind == "2":
            cap = _num(input("  Monthly spend cap in USD (e.g. 2500): $"))
            tok = input("  Optional: warn before N million tokens/mo (blank to skip): ").strip()
            ab.set_budget(mode="metered", spend_cap=cap, plan_label="metered API",
                          monthly_tokens=int(_num(tok) * 1e6) if tok else None)
        else:
            cost = _num(input("  What do you pay per month in USD (e.g. 100): $"))
            tok = input("  Optional: warn before N million tokens/mo (blank to skip): ").strip()
            ab.set_budget(mode="flat", plan_cost=cost, plan_label="subscription",
                          monthly_tokens=int(_num(tok) * 1e6) if tok else None)
    except (EOFError, KeyboardInterrupt):
        print(file=out)
        return
    print(file=out)


def run(args) -> int:
    from . import ai_budget as ab

    out = sys.stdout

    if getattr(args, "reset", False):
        ab.reset_budget()

    session_cap = getattr(args, "session_cap", None)
    gave_flags = (args.plan_cost is not None or args.spend_cap is not None
                  or args.tokens is not None or session_cap is not None)
    if gave_flags:
        ab.set_budget(plan_cost=args.plan_cost, spend_cap=args.spend_cap,
                      monthly_tokens=args.tokens, session_cap=session_cap)

    # First run, nothing set, a real terminal: ask instead of making them read flags.
    if (not gave_flags and not getattr(args, "json", False)
            and not ab.get_budget()["mode"] and sys.stdin.isatty() and out.isatty()):
        _interactive_setup(ab, out)

    st = ab.status()
    if getattr(args, "json", False):
        print(json.dumps(st, indent=2, default=str))
        return 0

    b = st["budget"]
    w = st["window"]
    mode = st["mode"]
    verdict = st["verdict"]
    vcolor = {"ok": _OK, "warn": _WARN, "over": _OVER}[verdict]

    label = st["plan_label"] or {"flat": "subscription", "metered": "metered API"}.get(
        mode, "per-session cap" if (st.get("session") or {}).get("cap_usd") else "no budget set")
    print(_c("nable ai-budget", _BOLD) + _c(f"  ·  {label}", _DIM), file=out)
    if not w["source_present"]:
        # A dead end otherwise. Claude Code and Codex are the only agents readable
        # with no key, so someone on Cursor, Windsurf, Zed or a plain API sees nothing but
        # zeros here and has no reason to look further. Name the next move.
        print(_c("  no Claude Code usage found yet (looked in ~/.claude/projects),", _DIM), file=out)
        print(_c("  and no Codex CLI usage (looked in ~/.codex/sessions).", _DIM), file=out)
        print(_c("  On a Cursor team? Set CURSOR_ADMIN_API_KEY to read its usage.", _DIM), file=out)
        print(_c("  Another agent? Meter the provider you pay:", _DIM), file=out)
        print("  " + _c("nable connect openai", _ACCENT) + _c("   (or anthropic, openrouter,", _DIM), file=out)
        print(_c("                          litellm, modal, together, replicate, cohere, mistral)", _DIM), file=out)

    def row(lbl: str, value: str) -> None:
        print(f"  {_c(lbl.ljust(16), _DIM)}{value}", file=out)

    print(file=out)
    row("this 5h window", f"{_tok(w['billable_tokens'])} tokens · {w['messages']} msgs · "
                          f"~{_c('$'+format(w['usd_equivalent'], ',.0f'), _ACCENT)} at list price (est.)")
    row("month to date", f"{_tok(st['billable_tokens_mtd'])} tokens · "
                         f"~${st['est_usd_mtd_list_price']:,.0f} at list price (est.)")

    # cost per 1M: for a flat plan the story is effective (your fee) vs list; for
    # metered it is simply the blended list rate.
    eff, lst = st["cost_per_1m_effective"], st["cost_per_1m_list"]
    if eff is not None and lst is not None:
        row("cost / 1M", f"~{_c('$'+format(eff, ',.2f'), _OK)} on your plan  ·  "
                         f"vs ~${lst:,.2f} at list")
    elif lst is not None:
        row("cost / 1M", f"~${lst:,.2f} at list price (est.)")

    sub = st["subsidy"]
    # The month's rows show the month's standing. st["verdict"] can be the
    # session's (a session past its cap), which belongs on the session row.
    m_verdict = st.get("month_verdict", verdict)
    m_color = {"ok": _OK, "warn": _WARN, "over": _OVER}[m_verdict]
    if mode == "metered" and b["spend_cap"] > 0:
        pct = st["est_usd_mtd_list_price"] / b["spend_cap"] * 100
        row("spend cap", f"~${st['est_usd_mtd_list_price']:,.0f} est of ${b['spend_cap']:,.0f}  ·  "
                         f"{_c(m_verdict.upper(), m_color)} ({pct:.0f}%)")
    elif mode == "flat" and b["plan_cost"] > 0:
        # Always confirm the configured plan, even with no usage yet. The subsidy
        # multiple only appears once there is usage to value against it.
        extra = (f"  ·  ~{sub['multiple']:g}x value pulled {_c('(subsidized)', _OK)}"
                 if sub and sub.get("multiple") else "")
        row("your plan", f"${b['plan_cost']:,.0f}/mo flat{extra}")
    if b["monthly_tokens"] > 0:
        on_tokens = st.get("month_verdict_basis", st["verdict_basis"]) == "tokens"
        row("usage cap", f"{_tok(st['billable_tokens_mtd'])} of {_tok(b['monthly_tokens'])} tokens  ·  "
                         f"{_c(m_verdict.upper() if on_tokens else 'tracking', m_color if on_tokens else _DIM)}"
                         f" ({st['billable_tokens_mtd']/b['monthly_tokens']*100:.0f}%)")
    if not mode:
        row("budget", _c("not set · run `nable ai-budget` to set one", _DIM))
    sess = st.get("session")
    if sess and (sess["messages"] or sess["cap_usd"]):
        # "latest session" when the id is a guess from transcript times rather
        # than the session this command runs in.
        lbl = "this session" if sess["id_source"] != "latest_activity" else "latest session"
        spent = f"~${sess['usd_equivalent']:,.2f}"
        if sess["cap_usd"]:
            scolor = {"ok": _OK, "warn": _WARN, "over": _OVER}[sess["verdict"]]
            row(lbl, f"{spent} of ${sess['cap_usd']:,.2f} cap  ·  "
                     f"{_c(sess['verdict'].upper(), scolor)} ({sess['pct_of_cap'] * 100:.0f}%)"
                     f"  ·  ~${sess['remaining_usd']:,.2f} left")
        else:
            row(lbl, f"{spent} · {sess['messages']} msgs  "
                     + _c("· no session cap (--session-cap USD)", _DIM))
    row("burn rate", f"~{_tok(st['burn_tokens_per_hour'])} tokens/hour")

    _breakdown(st, out, month=getattr(args, "month", False))

    print(file=out)
    print("  " + _c(st["summary"], vcolor), file=out)
    print(_c("  local · exact token counts · dollars are list-price estimates at each "
             "model's rate, not your bill", _DIM), file=out)
    return 0


_TOP_SESSIONS = 5
_HARNESS_LABELS = {"claude-code": "Claude Code", "codex": "Codex CLI", "cursor": "Cursor"}


def _breakdown(st: dict, out, month: bool) -> None:
    """Where the dollars went: each model, then the costliest sessions (tasks)."""
    u = st["month_to_date"] if month else st["window"]
    if not u.get("cost_by_model"):
        return
    period = "month to date" if month else f"last {st['window_hours']:g}h"
    total = u["usd_equivalent"] or 0.0
    unpriced = u.get("unpriced_models") or {}
    print(file=out)
    harnesses = u.get("cost_by_harness") or {}
    if harnesses:
        print(_c(f"  by agent, {period}", _DIM), file=out)
        hwidth = max(len(_HARNESS_LABELS.get(h, h)) for h in harnesses)
        for h, usd in harnesses.items():
            share = f"{usd / total * 100:3.0f}%" if total else "  -"
            print(f"    {_HARNESS_LABELS.get(h, h).ljust(hwidth)}  {_usd(usd)}  {share}", file=out)
    print(_c(f"  by model, {period}", _DIM), file=out)
    width = max(len(m) for m in u["cost_by_model"])
    for model, usd in u["cost_by_model"].items():
        share = f"{usd / total * 100:3.0f}%" if total else "  -"
        note = _c("  unpriced, at the fallback rate", _WARN) if model in unpriced else ""
        print(f"    {model.ljust(width)}  {_usd(usd)}  {share}{note}", file=out)

    sessions = u.get("by_session") or {}
    if not sessions:
        return
    count = u.get("session_count", len(sessions))
    shown = list(sessions.items())[:_TOP_SESSIONS]
    more = f", top {len(shown)} of {count}" if count > len(shown) else ""
    print(_c(f"  by session, {period}{more}", _DIM), file=out)
    cur = st.get("session") or {}
    this_id = cur.get("id") if cur.get("id_source") != "latest_activity" else None
    several = len(harnesses) > 1
    for sid, v in shown:
        tag = _c("  (this session)", _ACCENT) if sid == this_id else ""
        agent = (f"  {_HARNESS_LABELS.get(v.get('harness'), v.get('harness') or '-')}"
                 if several else "")
        print(f"    {_usd(v['usd_equivalent'])}  {(v.get('project') or '-')[:18].ljust(18)}"
              f"  {sid[:8]}  {_span(v['first_activity'], v['last_activity'])}"
              f"  {v['messages']} msgs{agent}{tag}", file=out)


def _usd(usd: float) -> str:
    return f"{'~$' + format(usd, ',.2f'):>11}"


def _span(first: float | None, last: float | None) -> str:
    """'Sep 24 17:55 to 22:40', local time; the end carries its date only when it
    differs from the start's."""
    if not first or not last:
        return ""
    a = datetime.fromtimestamp(first, tz=timezone.utc).astimezone()
    b = datetime.fromtimestamp(last, tz=timezone.utc).astimezone()
    end = b.strftime("%H:%M") if a.date() == b.date() else b.strftime("%b %d %H:%M")
    return f"{a.strftime('%b %d %H:%M')} to {end}"
