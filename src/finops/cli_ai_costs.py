"""`nable ai-costs --by team`: AI spend split by who or what it was for.

The terminal view of the get_ai_cost_attribution tool: cost per project,
workspace, API key, team, user, tag or session, from OpenAI, Anthropic,
LiteLLM and Langfuse, each row labelled with its provider and source. A
provider that has no such field is named as such; one that could not be read
is printed as not read, never as a $0.00 row. --json prints the full result.
"""
from __future__ import annotations

import json
import sys

EXIT_OK = 0
EXIT_ERROR = 1

# Dimensions plus the words people use for them; ai_attribution maps the
# aliases (customer, feature and agent read request or trace tags).
_CHOICES = ("project", "workspace", "api_key", "team", "user", "tag", "session",
            "customer", "feature", "agent")
_ROW_LIMIT = 50

_BOLD = "\033[1m"
_DIM = "\033[2m"
_WARN = "\033[38;5;208m"
_RST = "\033[0m"


def _c(s: str, color: str) -> str:
    return s if not sys.stdout.isatty() else f"{color}{s}{_RST}"


def add_parser(sub) -> None:
    p = sub.add_parser(
        "ai-costs",
        help="AI spend by project, workspace, API key, team, user or tag",
        description="Split AI/LLM spend by who or what it was for, from OpenAI, "
                    "Anthropic, LiteLLM and Langfuse. Each row names its provider and "
                    "whether the figure is billed, logged or estimated.",
    )
    p.add_argument("--by", dest="by", choices=_CHOICES, default="project",
                   help="what to split by (default project); customer, feature and "
                        "agent read request or trace tags")
    p.add_argument("--provider", choices=("openai", "anthropic", "litellm", "langfuse"),
                   help="ask only this provider")
    p.add_argument("--days", type=int, default=30, metavar="N",
                   help="lookback window in days (default 30)")
    p.add_argument("--json", action="store_true", help="machine-readable output on stdout")
    p.set_defaults(cmd="ai-costs")


def _usd(v: float) -> str:
    return f"${v:,.2f}"


def render(result: dict, days: int) -> str:
    """The human table for a get_ai_cost_attribution() result."""
    dim = result.get("dimension", "")
    lines = [_c(f"AI cost by {dim.replace('_', ' ')}, last {days} days "
                f"({result.get('period', '')})", _BOLD), ""]
    if result.get("dimension_note"):
        lines += [_c(result["dimension_note"], _DIM), ""]

    groups = result.get("groups") or []
    shown = groups[:_ROW_LIMIT]
    if shown:
        width = min(40, max(len(str(g["group"])) for g in shown))
        for g in shown:
            label = str(g["group"])
            if len(label) > width:
                label = label[:width - 3] + "..."
            est = "  (estimated)" if g.get("source") == "estimated" else ""
            lines.append(f"  {label:<{width}}  {g['provider']:<9}  "
                         f"{_usd(g['cost_usd']):>12}{est}")
        if len(groups) > len(shown):
            lines.append(_c(f"  ... {len(groups) - len(shown)} smaller groups not shown "
                            f"(use --json for all)", _DIM))
        lines.append("")

    for p, s in (result.get("by_provider") or {}).items():
        total = s.get("total_usd")
        amount = _usd(total) if total is not None else "tags overlap, no total"
        n = s.get("group_count", 0)
        lines.append(f"  {p}: {amount} ({s.get('source')}, {n} group{'' if n == 1 else 's'})")
        if s.get("note"):
            lines.append(_c(f"    {s['note']}", _DIM))
    if result.get("total_usd") is not None and len(result.get("by_provider") or {}) > 1:
        lines.append(f"  Total: {_usd(result['total_usd'])}")

    for reason in (result.get("not_available") or {}).values():
        lines.append(_c(f"  {reason}", _DIM))
    for p, why in (result.get("failed_providers") or {}).items():
        lines.append(_c(f"  Not read: {p} ({why})", _WARN))
    if result.get("partial"):
        lines.append(_c("  Partial: some spend could not be read or priced; the groups "
                        "above are not the whole AI bill.", _WARN))
    # The failed providers are already listed line by line above.
    note = (result.get("note") or "").split("Not read:")[0].strip()
    if note:
        lines.append(f"  {note}")
    if result.get("not_covered"):
        lines.append(_c(f"  {result['not_covered']}", _DIM))
    return "\n".join(lines).rstrip() + "\n"


def run(args) -> int:
    from .connectors import ai_attribution

    days = max(1, int(getattr(args, "days", 30) or 30))
    result = ai_attribution.get_ai_cost_attribution(
        getattr(args, "by", "project"), provider=getattr(args, "provider", None), days=days)

    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, default=str))
        return EXIT_ERROR if result.get("error") and not result.get("groups") else EXIT_OK

    if result.get("error") and not result.get("groups"):
        print(result["error"], file=sys.stderr)
        for reason in (result.get("not_available") or {}).values():
            print(f"  {reason}", file=sys.stderr)
        for p, why in (result.get("failed_providers") or {}).items():
            print(f"  Not read: {p} ({why})", file=sys.stderr)
        return EXIT_ERROR

    sys.stdout.write(render(result, days))
    return EXIT_OK
