"""
nable first-run welcome screen + guided onboarding command.

show_welcome()       - prints once on first CLI run, then never again
run_welcome_flow()   - full interactive onboarding, always runs (finops welcome)
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

# ── Colour helpers ─────────────────────────────────────────────────────────────

_USE_COLOR = (
    sys.stdout.isatty()
    and os.environ.get("NO_COLOR", "") == ""
    and os.environ.get("TERM", "") != "dumb"
)


def _c(code: str, text: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(t: str) -> str:   return _c("1", t)
def dim(t: str) -> str:    return _c("2", t)
def green(t: str) -> str:  return _c("32", t)
def cyan(t: str) -> str:   return _c("36", t)
def yellow(t: str) -> str: return _c("33", t)
def amber(t: str) -> str:  return _c("33", t)
def white(t: str) -> str:  return _c("97", t)


# ── Telemetry ──────────────────────────────────────────────────────────────────

_POSTHOG_TOKEN = "phc_zcaQqoAXrSghjtbE6VB83p4RjfmcpqezKWV9GdZy4dPv"
_POSTHOG_ENDPOINT = "https://us.i.posthog.com/capture/"
_VERSION = "0.8.36"


def _fire_telemetry(event: str, properties: dict) -> None:
    """Send a PostHog event. Fire-and-forget: never raises."""
    if os.environ.get("NABLE_NO_TELEMETRY", "") == "1":
        return

    def _send() -> None:
        try:
            import httpx
            payload = {
                "api_key": _POSTHOG_TOKEN,
                "event": event,
                "distinct_id": "install",
                "properties": properties,
            }
            httpx.post(_POSTHOG_ENDPOINT, json=payload, timeout=5)
        except Exception:
            pass

    t = threading.Thread(target=_send, daemon=True)
    t.start()


# ── Sentinel ───────────────────────────────────────────────────────────────────

_SENTINEL = Path.home() / ".config" / "finops" / ".welcomed"


def _is_first_run() -> bool:
    return not _SENTINEL.exists()


def _mark_welcomed() -> None:
    try:
        _SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        _SENTINEL.touch()
    except Exception:
        pass


# ── Layout helpers ─────────────────────────────────────────────────────────────

_W = 58


def _rule(char: str = "─") -> str:
    return dim(char * _W)


def _line(content: str = "") -> None:
    print(f"  {content}")


def _blank() -> None:
    print()


# ── Shared header ──────────────────────────────────────────────────────────────

def _print_header() -> None:
    _blank()
    _line(bold(white("◆  nable")) + bold("  ·  Cloud Cost Intelligence"))
    _line(dim("   for Claude, Cursor, Windsurf, and any MCP client"))
    _blank()
    _line(_rule())
    _blank()


# ── One-time welcome (auto-shown on first run) ─────────────────────────────────

def show_welcome() -> None:
    """Print on the very first CLI invocation, then never again."""
    if not _is_first_run():
        return

    _mark_welcomed()
    _fire_telemetry("install_completed", {"source": "finops_welcome", "version": _VERSION})
    _print_header()

    _line(bold("Ask your AI about cloud costs in plain English:"))
    _blank()
    for q in [
        '"What drove our AWS costs up 40% this month?"',
        '"Which team is spending the most on Datadog?"',
        '"Show me EC2 rightsizing opportunities."',
        '"Create a Jira ticket for any anomaly over $500."',
    ]:
        _line(f"  {cyan(q)}")

    _blank()
    _line(_rule())
    _blank()
    _line(bold("Connected sources:"))
    for label, items in [
        ("Cloud",    "AWS · Azure · GCP · Kubernetes"),
        ("AI / LLM", "OpenAI · Anthropic · Datadog · Langfuse"),
        ("SaaS",     "Snowflake · GitHub · Stripe · Vercel · more"),
    ]:
        _line(f"  {dim(label + ':')}  {items}")

    _blank()
    _line(_rule())
    _blank()
    _line(green("✓") + bold("  7-day free trial — all features unlocked."))
    _line(dim("   No credit card required."))
    _blank()
    _line(f"  Docs  →  {cyan('https://getnable.com/docs')}")
    _blank()
    _line(_rule())
    _blank()
    _line(bold("Getting started:") + "  connect your first provider below.")
    _line(dim("   Credentials stay on your machine — never sent anywhere."))
    _blank()


# ── Full onboarding flow (finops welcome) ──────────────────────────────────────

def run_welcome_flow() -> None:
    """
    Interactive onboarding shown when the user runs `finops welcome`.
    Walks through the 3-step getting-started flow and then launches
    the account setup wizard.
    """
    _print_header()

    # Step indicators
    _line(bold("3 steps to your first cost insight:"))
    _blank()
    _line(f"  {green('1')}  {bold('Install')}          {dim('pip install finops-mcp')}  {green('done')}")
    _line(f"  {amber('2')}  {bold('Connect Claude')}   add nable to your MCP client")
    _line(f"  {dim('3')}  {dim('Connect a cloud')}  {dim('your AWS, Azure, or GCP account')}")
    _blank()
    _line(_rule())
    _blank()

    # Step 2: Claude Desktop / Cursor config
    _line(bold("Step 2 — Connect to Claude, Cursor, or Windsurf"))
    _blank()
    _line("  Run this in your terminal:")
    _blank()
    _line(f"  {bold(cyan('finops setup claude'))}")
    _blank()
    _line(dim("  This writes the MCP server config for you. Restart Claude after."))
    _blank()

    try:
        input(f"  {dim('Press Enter once Claude is set up, or Ctrl-C to skip...')}  ")
    except (KeyboardInterrupt, EOFError):
        _blank()
        _line(dim("  Skipped. Run 'finops setup claude' later."))
        _blank()

    _blank()
    _line(_rule())
    _blank()

    # Step 3: cloud account
    _line(bold("Step 3 — Connect your first cloud account"))
    _blank()
    _line(f"  {dim('1)')} AWS          {dim('finops setup aws')}")
    _line(f"  {dim('2)')} Azure        {dim('finops setup azure')}")
    _line(f"  {dim('3)')} GCP          {dim('finops setup gcp')}")
    _line(f"  {dim('4)')} Skip for now")
    _blank()

    choice = ""
    try:
        choice = input("  Choice [1]: ").strip() or "1"
    except (KeyboardInterrupt, EOFError):
        _blank()

    _blank()

    if choice == "1":
        from .setup_wizard import setup_aws_account
        setup_aws_account()
    elif choice == "2":
        from .setup_wizard import setup_azure
        setup_azure()
    elif choice == "3":
        from .setup_wizard import setup_gcp
        setup_gcp()
    else:
        _line(dim("  No problem. Run 'finops setup aws' whenever you're ready."))
        _blank()

    # Finish
    _line(_rule())
    _blank()
    _line(bold(green("You're set up.")) + "  Open Claude and try:")
    _blank()
    q = '"What did we spend on AWS last month?"'
    _line(f"  {cyan(q)}")
    _blank()
    _line(f"  Docs    →  {cyan('https://getnable.com/docs')}")
    _line(f"  Support →  {cyan('chandanirving@gmail.com')}")
    _blank()
