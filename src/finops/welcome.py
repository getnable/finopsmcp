"""
nable first-run welcome screen.

Displays once — on the first invocation of any nable CLI command.
Sentinel file: ~/.config/finops/.welcomed

Inspired by the Claude Code / Vercel CLI welcome pattern:
  - Rich ANSI colour when the terminal supports it
  - Falls back to plain text on dumb terminals or when piped
  - Never blocks; never requires input
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ── Colour helpers ─────────────────────────────────────────────────────────────

_USE_COLOR = (
    sys.stdout.isatty()
    and os.environ.get("NO_COLOR", "") == ""
    and os.environ.get("TERM", "") != "dumb"
)


def _c(code: str, text: str) -> str:
    """Wrap text in an ANSI escape sequence, or return plain text."""
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(t: str) -> str:   return _c("1", t)
def dim(t: str) -> str:    return _c("2", t)
def green(t: str) -> str:  return _c("32", t)
def cyan(t: str) -> str:   return _c("36", t)
def yellow(t: str) -> str: return _c("33", t)
def white(t: str) -> str:  return _c("97", t)


# ── Sentinel ───────────────────────────────────────────────────────────────────

_SENTINEL = Path.home() / ".config" / "finops" / ".welcomed"


def _is_first_run() -> bool:
    return not _SENTINEL.exists()


def _mark_welcomed() -> None:
    try:
        _SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        _SENTINEL.touch()
    except Exception:
        pass  # read-only FS, container, etc. — just skip


# ── Layout ─────────────────────────────────────────────────────────────────────

_W = 58  # total inner width


def _rule(char: str = "─") -> str:
    return dim(char * _W)


def _line(content: str = "") -> None:
    print(f"  {content}")


def show_welcome() -> None:
    """
    Print the first-run welcome screen and mark the sentinel.
    No-op if the sentinel already exists.
    """
    if not _is_first_run():
        return

    _mark_welcomed()
    print()

    # ── Logo block ──────────────────────────────────────────────────────────
    _line(bold(white("◆  nable")) + bold("  ·  Cloud Cost Intelligence"))
    _line(dim("   for Claude, Cursor, Windsurf, and any MCP client"))
    print()
    _line(_rule())
    print()

    # ── Value prop ──────────────────────────────────────────────────────────
    _line(bold("Ask your AI about cloud costs in plain English:"))
    print()
    quotes = [
        '"What drove our AWS costs up 40% this month?"',
        '"Which team is spending the most on Datadog?"',
        '"Show me EC2 rightsizing opportunities."',
        '"Create a Jira ticket for any anomaly over $500."',
    ]
    for q in quotes:
        _line(f"  {cyan(q)}")
    print()
    _line(_rule())
    print()

    # ── Sources ──────────────────────────────────────────────────────────────
    _line(bold("Connected sources:"))
    sources = [
        ("Cloud",    "AWS · Azure · GCP · Kubernetes"),
        ("AI / LLM", "OpenAI · Anthropic · Datadog · Langfuse"),
        ("SaaS",     "Snowflake · GitHub · Stripe · Vercel · more"),
    ]
    for label, items in sources:
        _line(f"  {dim(label + ':')}  {items}")
    print()
    _line(_rule())
    print()

    # ── Trial callout ────────────────────────────────────────────────────────
    _line(green("✓") + bold("  1-month free trial — all features unlocked."))
    _line(dim("   No credit card required."))
    print()

    # ── Docs ─────────────────────────────────────────────────────────────────
    _line(f"  Docs  →  {cyan('https://nable.sh/docs')}")
    print()
    _line(_rule())
    print()

    # ── Next step ────────────────────────────────────────────────────────────
    _line(bold("Getting started:") + "  connect your first provider below.")
    print()
