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

def _app_version() -> str:
    """Live package version, not a hardcoded constant that drifts stale."""
    try:
        from importlib.metadata import version
        return version("finops-mcp")
    except Exception:
        return "unknown"


def _fire_telemetry(event: str, properties: dict) -> None:
    """Send a PostHog event via the shared telemetry module.

    Delegates so the event uses the per-install anonymous UUID. The old path here
    hardcoded distinct_id="install" (a constant), which collapsed every install
    into a single PostHog person and made install counts uncountable.
    """
    try:
        from . import telemetry as _tel
        if _tel._is_opted_out():
            return
        props = {"version": _app_version(), **properties}
        t = threading.Thread(
            target=_tel._send_event,
            args=(_tel._get_install_id(), event, props),
            daemon=True,
        )
        t.start()
        # A first run that prints the welcome and exits immediately would kill
        # this daemon thread mid-POST, permanently uncounting the install
        # (the sentinel is already set). A short join lets it land.
        t.join(timeout=2)
    except Exception:
        pass


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

def _is_interactive_install() -> bool:
    """True only when a human is actually at the terminal.

    install_completed used to fire on ANY first CLI run, so piped invocations,
    `finops --help` in scripts, the cache-warm subprocess inside `finops
    upgrade` (capture_output, so stdin is a pipe), and fresh CI/uvx environments
    all counted as installs. A real first-run welcome is interactive, so we gate
    on stdin/stdout being a TTY and not running under CI.
    """
    try:
        from . import telemetry as _tel
        if _tel.is_ci():
            return False
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def show_welcome() -> None:
    """Print on the very first interactive CLI invocation, then never again."""
    if not _is_first_run():
        return

    # Non-interactive / automated first runs are not installs. Stay a no-op and
    # leave the sentinel unset so the first genuine human run still counts once.
    if not _is_interactive_install():
        return

    _mark_welcomed()
    # Banner first: _fire_telemetry briefly joins its sender thread, and on a
    # slow network that wait should happen behind visible output, not before it.
    _print_header()
    _fire_telemetry("install_completed", {"source": "finops_welcome"})

    _line(bold("Ask your AI about cloud costs:"))
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
    _line(dim("   Credentials stay on your machine. nable has no backend, so your data never touches our servers."))
    _line(dim("   nable sends anonymous usage pings (no cost data). Opt out: NABLE_NO_TELEMETRY=1"))
    _blank()


# ── The payoff: surface a real number right after connecting ───────────────────

_MAGIC_Q = '"What is driving my AWS bill this month?"'

# AWS service names that are AI/ML spend (Cost Explorer labels them many ways).
_AI_KEYWORDS = (
    "bedrock", "textract", "sagemaker", "comprehend", "rekognition", "kendra",
    "claude", "openai", "anthropic", "transcribe", "translate", "polly", "lex",
)


def _quiet_logs() -> None:
    """Silence the import-time and network INFO chatter so the value moment
    reads clean. Our output uses print(), never logging, so this is safe."""
    import logging
    logging.getLogger().setLevel(logging.ERROR)
    for name in ("botocore", "boto3", "urllib3", "httpx", "httpcore",
                 "posthog", "apscheduler", "finops"):
        logging.getLogger(name).setLevel(logging.ERROR)


def _show_value_moment(demo: bool = False) -> bool:
    """After connecting (or in --demo), scan the account and print a real dollar
    figure in the terminal, so setup pays off before the user opens Claude.

    Returns True if it printed a real number. Fully guarded: every failure or
    slowness is swallowed. This can never block or break onboarding."""
    # Demo mode rides on env flags the server reads. Set them only for the
    # duration of this call and restore after, so a demo fallback can never
    # leak demo mode into a later real scan in the same process.
    _demo_env_prev = None
    if demo:
        _demo_env_prev = {k: os.environ.get(k) for k in ("FINOPS_DEMO", "FINOPS_DEMO_MODE")}
        os.environ["FINOPS_DEMO"] = "1"
        os.environ["FINOPS_DEMO_MODE"] = "1"
    try:
        return _value_moment_body(demo)
    finally:
        if _demo_env_prev is not None:
            for _k, _v in _demo_env_prev.items():
                if _v is None:
                    os.environ.pop(_k, None)
                else:
                    os.environ[_k] = _v


def _value_moment_body(demo: bool = False) -> bool:
    """Scan and print a real dollar figure. Wrapped by _show_value_moment, which
    owns the demo-env lifecycle."""
    try:
        _quiet_logs()
        import asyncio
        from . import server  # heavy import, only at the value-moment step

        async def _run():
            async def _idle():
                try:
                    return await asyncio.wait_for(server.list_idle_resources(), timeout=12)
                except Exception:
                    return None

            async def _ai():
                try:
                    return await asyncio.wait_for(server.optimize_ai_spend(), timeout=18)
                except Exception:
                    return None

            # Run the scans concurrently so the AI optimizer adds no latency.
            summary, idle, ai_plan = await asyncio.gather(
                server.get_cost_summary(), _idle(), _ai(), return_exceptions=True,
            )
            return (
                None if isinstance(summary, Exception) else summary,
                None if isinstance(idle, Exception) else idle,
                None if isinstance(ai_plan, Exception) else ai_plan,
            )

        summary, idle, ai_plan = asyncio.run(asyncio.wait_for(_run(), timeout=35))
    except Exception:
        return False  # never block setup

    if not isinstance(summary, dict) or summary.get("error"):
        return False
    # Real tool returns grand_total_usd / grand_by_service; demo data returns
    # total_usd / by_service. Accept either.
    total = summary.get("grand_total_usd") or summary.get("total_usd") or 0.0
    by_svc = summary.get("grand_by_service") or summary.get("by_service") or {}
    if total <= 0 or not isinstance(by_svc, dict) or not by_svc:
        return False

    top = sorted(by_svc.items(), key=lambda kv: kv[1], reverse=True)
    top_name, top_val = top[0]
    ai_total = sum(v for k, v in by_svc.items()
                   if any(w in k.lower() for w in _AI_KEYWORDS))
    ai_share = round(ai_total / total * 100) if total else 0

    _blank()
    _line(_rule())
    _blank()
    _header = "nable on sample data — last 30 days" if demo else "nable scanned your account — last 30 days"
    _line(green("✓") + bold("  " + _header))
    _blank()
    _line(f"  {dim('Total spend')}      {bold('$' + format(total, ',.0f'))}")
    _line(f"  {dim('Top driver')}       {top_name}  {cyan('$' + format(top_val, ',.0f'))}")
    if ai_share >= 5:
        _line(f"  {dim('AI / ML share')}    {bold(str(ai_share) + '%')}  {dim('of your cloud bill')}")
    if isinstance(idle, dict):
        waste = idle.get("total_monthly_waste_usd") or 0
        if waste and waste >= 1:
            _line(f"  {dim('Idle / wasted')}    {amber('$' + format(waste, ',.0f') + '/mo')}  {dim('doing nothing')}")
    # Realizable AI savings (e.g. prompt caching). Only the addressable figure,
    # never the labeled routing ceiling, so onboarding never overpromises.
    if isinstance(ai_plan, dict) and not ai_plan.get("error"):
        ai_save = ai_plan.get("addressable_savings_monthly_usd") or 0
        if ai_save and ai_save >= 10:
            _line(f"  {dim('AI savings')}       {green('$' + format(ai_save, ',.0f') + '/mo')}  {dim('ready to capture')}")
    _blank()
    return True


# ── Full onboarding flow (finops welcome) ──────────────────────────────────────

_AMBIENT_AWS_TIMEOUT = 3.0  # seconds; cap on the first-run ambient credential probe


def run_welcome_flow(demo: bool = False) -> None:
    """
    Interactive onboarding shown when the user runs `finops welcome`.
    Auto-connects Claude, connects a cloud account, then pays off with a real
    cost number. `--demo` runs the whole thing on sample data, no account needed.
    """
    _print_header()

    if demo:
        _line(bold("Demo mode") + dim("  ·  nable on sample data, no account needed"))
        _show_value_moment(demo=True)
        _line(_rule())
        _blank()
        _line(bold(green("That is nable.")) + "  Run it on your own account:")
        _blank()
        _line(f"  {bold(cyan('uvx --from finops-mcp finops welcome'))}")
        _blank()
        _line(f"  Docs  →  {cyan('https://getnable.com/docs')}")
        _blank()
        return

    # Step indicators
    _line(bold("3 steps to your first cost insight:"))
    _blank()
    _line(f"  {green('1')}  {bold('Install')}          {green('done')}")
    _line(f"  {amber('2')}  {bold('Connect Claude')}   {dim('writing your MCP config')}")
    _line(f"  {dim('3')}  {dim('Connect a cloud')}  {dim('AWS, Azure, or GCP')}")
    _blank()
    _line(_rule())
    _blank()

    # Step 2: auto-configure Claude / Cursor (no manual command, no Enter)
    _line(bold("Step 2 — Connecting nable to Claude"))
    _blank()
    try:
        from .setup_wizard import _configure_claude_desktop
        _configure_claude_desktop()
    except (KeyboardInterrupt, EOFError):
        _line(dim("  Skipped. Run 'finops setup claude' later."))
    except Exception:
        _line(dim("  Could not auto-configure. Run 'finops setup claude' later."))
    _blank()
    _line(_rule())
    _blank()

    # Step 3: see a number. Zero-config AWS first, then a menu, and a demo
    # fallback so nobody ever leaves the terminal without seeing value.
    _line(bold("Step 3 — See your first number"))
    _blank()

    shown = False

    # Most dev machines already carry an AWS credential chain (env vars,
    # ~/.aws, SSO, instance profile). If so, the fastest path to value is a
    # read-only scan with those creds: no menu, no stored secrets. Ask first,
    # one keystroke. Never touch their account unprompted.
    aws_ambient = False
    try:
        import asyncio as _aio
        from .connectors.aws import AWSConnector
        # Hard timeout: the credential chain can reach for an EC2/ECS instance
        # profile (IMDS at 169.254.169.254) or refresh an expired SSO token,
        # which hangs for botocore's default connect timeout on a laptop where
        # IMDS is firewalled. Onboarding must never freeze, so cap it and treat
        # a timeout as "no ambient creds".
        aws_ambient = _aio.run(_aio.wait_for(AWSConnector().is_configured(), timeout=_AMBIENT_AWS_TIMEOUT))
    except Exception:
        aws_ambient = False

    if aws_ambient:
        _line(f"  {green('Found AWS credentials')} in your environment.")
        _line(dim("  nable can run a read-only cost scan with them right now."))
        _blank()
        ans = "y"
        try:
            ans = input("  Show your real AWS bill now? [Y/n]: ").strip().lower() or "y"
        except (KeyboardInterrupt, EOFError):
            ans = "n"
        _blank()
        if ans in ("y", "yes"):
            shown = _show_value_moment(demo=False)

    # No ambient creds, or they declined: offer the full connect menu.
    if not shown:
        _line(f"  {dim('1)')} AWS          {dim('reads your existing AWS profile')}")
        _line(f"  {dim('2)')} Azure")
        _line(f"  {dim('3)')} GCP")
        _line(f"  {dim('4)')} Skip for now")
        _blank()
        choice = "4"
        try:
            choice = input("  Choice [4]: ").strip() or "4"
        except (KeyboardInterrupt, EOFError):
            choice = "4"
        _blank()

        if choice == "1":
            from .setup_wizard import setup_aws_account
            setup_aws_account()
            shown = _show_value_moment(demo=False)
        elif choice == "2":
            from .setup_wizard import setup_azure
            setup_azure()
            shown = _show_value_moment(demo=False)
        elif choice == "3":
            from .setup_wizard import setup_gcp
            setup_gcp()
            shown = _show_value_moment(demo=False)

    # Never dead-end. If they skipped, declined, or the scan came up empty,
    # show nable on sample data so the value lands before they ever leave.
    if not shown:
        _blank()
        _line(bold("Here's nable on a sample bill") + dim("  ·  connect an account to see your own"))
        _show_value_moment(demo=True)
        _line(dim("  Ready for real numbers?  ") + cyan("finops setup aws") + dim("  (read-only, ~1 min)"))
        _blank()

    # Finish — one unambiguous next action
    _line(_rule())
    _blank()
    _line(bold(green("You're set up.")) + "  Open Claude (or Cursor) and ask:")
    _blank()
    _line(f"  {cyan(_MAGIC_Q)}")
    _blank()
    _line(dim("  Not seeing nable? Run 'finops doctor' to check the connection."))
    _blank()
    _line(f"  Docs    →  {cyan('https://getnable.com/docs')}")
    _line(f"  Support →  {cyan('hello@getnable.com')}")
    _blank()
