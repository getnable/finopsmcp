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


def _and_list(items: list) -> str:
    """Join client names for prose: ['Cursor'] -> 'Cursor'; ['A','B'] -> 'A and B'."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


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
    _line(green("✓") + bold("  7-day free trial, all features unlocked."))
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


_VALUE_MOMENT_TIMEOUT = 30  # seconds; hard wall-clock cap on the first-run scan


def _run_capped(fn, timeout: float):
    """Run fn() in a daemon thread; return its result, or None if it does not
    finish within `timeout` seconds. Returns on time even when fn pins the event
    loop or blocks on I/O, which a plain asyncio timeout cannot interrupt. The
    abandoned thread is a daemon and dies with the process."""
    import threading

    box: dict = {}

    def _w():
        try:
            box["v"] = fn()
        except Exception:
            box["v"] = None

    t = threading.Thread(target=_w, daemon=True)
    t.start()
    t.join(timeout=timeout)
    return box.get("v")


async def _any_llm_configured() -> bool:
    """Fast (vault/env, no network) check for a connected model provider, so the
    value moment only spends time fetching the token bill when there is one."""
    try:
        from .connectors.saas.openai_usage import is_configured as _oai
        from .connectors.saas.anthropic_usage import is_configured as _ant
        from .connectors.saas.openrouter import is_configured as _or
        return bool(await _oai() or await _ant() or await _or())
    except Exception:
        return False


def _value_moment_body(demo: bool = False) -> bool:
    """Scan and print a real dollar figure. Wrapped by _show_value_moment, which
    owns the demo-env lifecycle."""
    if not demo:
        _line(dim("  Scanning your account, this can take up to ~30s..."))

    import asyncio
    from . import server  # heavy import, only at the value-moment step

    _quiet_logs()

    # Headline first, on its own wall-clock cap. The user came for this number, so
    # the optional idle/AI scans below must never block or blank it. _run_capped's
    # daemon-thread join returns on time even when a call pins the event loop (an
    # asyncio timeout alone would not fire, the bug that used to hang setup and
    # blanked this scan when an un-guarded tool reached for real AWS in demo).
    summary = _run_capped(lambda: asyncio.run(server.get_cost_summary()), _VALUE_MOMENT_TIMEOUT)

    # Optional extras, real accounts only. There is no demo idle/AI dataset, and
    # calling those tools in demo would reach for real AWS, so skip them in demo.
    # Each runs on its own cap so a slow scan can't hold the headline hostage.
    idle = None
    ai_plan = None
    llm = None
    if not demo and isinstance(summary, dict) and not summary.get("error"):
        idle = _run_capped(lambda: asyncio.run(server.list_idle_resources()), 12)
        ai_plan = _run_capped(lambda: asyncio.run(server.optimize_ai_spend()), 18)
        # AI-native users connect a model provider too, and the token bill is their
        # biggest number and the thing no cloud dashboard shows. Surface it in the
        # same value moment instead of only the cloud bill. Gated on a fast config
        # check so cloud-only users never wait on a token fetch.
        if _run_capped(lambda: asyncio.run(_any_llm_configured()), 3):
            llm = _run_capped(lambda: asyncio.run(server.get_llm_costs(days=30)), 15)

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
    _header = "nable on sample data, last 30 days" if demo else "nable scanned your account, last 30 days"
    _line(green("✓") + bold("  " + _header))
    _blank()
    _line(f"  {dim('Total spend')}      {bold('$' + format(total, ',.0f'))}")
    # The token bill (OpenAI/Anthropic/Bedrock/gateways), the AI-native hero number,
    # shown alongside the cloud bill when a model provider is connected.
    if isinstance(llm, dict):
        llm_total = llm.get("total_usd") or 0
        if llm_total >= 1:
            _line(f"  {dim('AI / LLM spend')}   {bold('$' + format(llm_total, ',.0f'))}  {dim('OpenAI, Anthropic, Bedrock')}")
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


def _llm_ambient_provider() -> str | None:
    """Return 'OpenAI' or 'Anthropic' when a model-provider key is already in the
    shell environment, else None. AI-native startups' biggest cost is the token
    bill, and they usually have OPENAI_API_KEY/ANTHROPIC_API_KEY exported already,
    so this is their fastest path to a real first number, no cloud account needed."""
    import os
    if os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_ADMIN_KEY"):
        return "OpenAI"
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_ADMIN_KEY"):
        return "Anthropic"
    return None


def _llm_value_moment() -> bool:
    """Print the user's real LLM/token bill as the first number. Mirrors the cloud
    value moment but reads get_llm_costs, so an AI-native account sees its actual
    spend (OpenAI + Anthropic + gateways) instead of a cloud summary that would be
    empty for them. Returns True only when a real dollar figure was shown."""
    import asyncio
    from . import server

    _quiet_logs()
    result = _run_capped(lambda: asyncio.run(server.get_llm_costs(days=30)), _VALUE_MOMENT_TIMEOUT)
    if not isinstance(result, dict) or result.get("error"):
        return False
    total = result.get("total_usd") or 0.0
    if total <= 0:
        return False

    by_provider = result.get("by_provider") or {}
    top_spenders = result.get("top_spenders") or []

    _blank()
    _line(_rule())
    _blank()
    _line(green("✓") + bold("  nable scanned your AI spend, last 30 days"))
    _blank()
    _line(f"  {dim('Total AI spend')}   {bold('$' + format(total, ',.0f'))}")
    if by_provider:
        prov, amt = sorted(by_provider.items(), key=lambda kv: kv[1], reverse=True)[0]
        _line(f"  {dim('Top provider')}     {prov}  {cyan('$' + format(amt, ',.0f'))}")
    if top_spenders:
        m = top_spenders[0]
        _line(f"  {dim('Top model')}        {m.get('model', '')}  {cyan('$' + format(m.get('cost_usd', 0), ',.0f'))}")
    _blank()
    return True


def _connect_llm_provider() -> bool:
    """Prompt for an OpenAI or Anthropic key, store it, and show the token bill.
    The AI-native segment's first real number is its model spend, not a cloud bill."""
    from .setup_wizard import setup_saas_api_key
    try:
        pick = input("  Which? 1) OpenAI  2) Anthropic  [1]: ").strip() or "1"
    except (KeyboardInterrupt, EOFError):
        return False
    _blank()
    try:
        if pick == "2":
            setup_saas_api_key("Anthropic", [
                ("ANTHROPIC_API_KEY", "API Key (sk-ant-...)", True),
                ("ANTHROPIC_ADMIN_KEY", "Admin Key for org usage data (optional)", True),
                ("ANTHROPIC_ORGANIZATION_ID", "Organization ID (optional)", False),
            ])
        else:
            setup_saas_api_key("OpenAI", [
                ("OPENAI_API_KEY", "API Key (sk-...)", True),
                ("OPENAI_ADMIN_KEY", "Admin/Org Key for billing data (sk-admin-..., optional)", True),
                ("OPENAI_ORG_ID", "Organization ID (org-..., optional)", False),
            ])
    except (KeyboardInterrupt, EOFError):
        return False
    except Exception:
        return False
    shown = _llm_value_moment()
    if not shown:
        _llm_admin_key_hint("Anthropic" if pick == "2" else "OpenAI")
    return shown


def _llm_admin_key_hint(provider: str) -> None:
    """A connected LLM key that returns no org billing data is almost always a
    regular (non-admin) key. Org cost data needs an ADMIN key, so say that with the
    exact next step instead of dead-ending the user on an empty bill, the single
    most likely confusion on the AI-native connect path."""
    _blank()
    if provider == "OpenAI":
        _line(dim("  Key works, but no org billing data came back. OpenAI cost data needs an"))
        _line(dim("  ") + bold("admin key") + dim(" (sk-admin-...), not a regular key."))
        _line(dim("  Create one:  ") + cyan("https://platform.openai.com/settings/organization/admin-keys"))
        _line(dim("  Then run:    ") + cyan("finops setup openai") + dim("  and paste it as the Admin key."))
    elif provider == "Anthropic":
        _line(dim("  Key works, but no org usage data came back. Anthropic cost data needs an"))
        _line(dim("  ") + bold("admin key") + dim(" plus your Organization ID."))
        _line(dim("  Create one in the Anthropic Console under Settings -> Admin keys, then run:"))
        _line(dim("  ") + cyan("finops setup anthropic") + dim("  and paste them."))
    _blank()


# ── Full onboarding flow (finops welcome) ──────────────────────────────────────

_AMBIENT_AWS_TIMEOUT = 3.0  # seconds; cap on the first-run ambient credential probe


def _oneclick_aws_url() -> str | None:
    """Return the one-click read-only-key CloudFormation URL when it's published,
    else None. Gated on quick_create_available() so the welcome flow never shows a
    dead link, and lights up automatically once the template goes live. This is
    the fast path for the no-local-creds user, the segment that otherwise stalls
    out the 5-10 minute onboarding."""
    try:
        from .security.iam_setup import quick_create_available, quick_create_url
        if quick_create_available():
            return quick_create_url()
    except Exception:
        pass
    return None


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
    _line(f"  {amber('2')}  {bold('Connect editor')}   {dim('writing your MCP config')}")
    _line(f"  {dim('3')}  {dim('Connect a cloud')}  {dim('AWS, Azure, or GCP')}")
    _blank()
    _line(_rule())
    _blank()

    # Step 2: auto-configure every MCP client we can find (Claude Desktop, Cursor)
    # and surface the exact command for Claude Code. Honest about what got wired,
    # so a Cursor/Claude Code user is never told "you're set up" with nothing written.
    _line(bold("Step 2, Connecting nable to your editor"))
    _blank()
    client_result = {"configured": [], "manual": []}
    try:
        from .setup_wizard import _configure_mcp_clients
        client_result = _configure_mcp_clients()
    except (KeyboardInterrupt, EOFError):
        _line(dim("  Skipped. Run 'finops setup claude' later."))
    except Exception:
        _line(dim("  Could not auto-configure. Run 'finops setup claude' later."))
    _blank()
    _line(_rule())
    _blank()

    # Step 3: see a number. Zero-config AWS first, then a menu, and a demo
    # fallback so nobody ever leaves the terminal without seeing value.
    _line(bold("Step 3, See your first number"))
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
        try:
            from .setup_wizard import _emit_provider_connected, _emit_step
        except Exception:
            def _emit_provider_connected(*a, **k): pass
            def _emit_step(*a, **k): pass
        _emit_step("welcome_ambient_detected")
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
            _emit_step("welcome_ambient_accepted")
            shown = _show_value_moment(demo=False)
            if shown:
                # A confirmed read with ambient creds is a real connection. The
                # ambient path never calls setup_aws_account, so without this the
                # activation metric misses everyone who connects via an existing
                # profile, SSO, or the default chain. auth_method marks it ambient.
                _emit_provider_connected("ambient")
        else:
            _emit_step("welcome_ambient_declined")

    # No cloud creds shown yet: AI-native users usually have a model-provider key
    # in their env, and the token bill IS their biggest cost. Offer it as the fast
    # first number before falling back to the cloud connect menu.
    if not shown:
        _llm_prov = _llm_ambient_provider()
        if _llm_prov:
            _line(f"  {green('Found ' + _llm_prov + ' credentials')} in your environment.")
            _line(dim("  nable can show your AI / LLM bill with them right now."))
            _blank()
            ans = "y"
            try:
                ans = input(f"  Show your {_llm_prov} bill now? [Y/n]: ").strip().lower() or "y"
            except (KeyboardInterrupt, EOFError):
                ans = "n"
            _blank()
            if ans in ("y", "yes"):
                shown = _llm_value_moment()
                if shown:
                    try:
                        from .setup_wizard import _emit_provider_connected
                        _emit_provider_connected("ambient")
                    except Exception:
                        pass
                else:
                    # Key is present but returned no billing data: almost always a
                    # non-admin key. Point them at the admin key instead of leaving
                    # them staring at an empty AI bill.
                    _llm_admin_key_hint(_llm_prov)

    # No ambient creds, or they declined: offer the full connect menu.
    if not shown:
        # Lead with the one-click read-only key when it's published: a no-creds
        # user gets connected in two copy-pastes instead of hand-minting a key.
        _oneclick = _oneclick_aws_url()
        if _oneclick:
            _line(f"  {green('Fastest')}, one-click read-only AWS key, no local creds needed:")
            _line(f"    {cyan(_oneclick)}")
            _line(dim("    Click it, create the stack, then choose 1 below and paste the two outputs."))
            _blank()
        _line(f"  {dim('1)')} AWS          {dim('reads your existing AWS profile')}")
        _line(f"  {dim('2)')} Azure")
        _line(f"  {dim('3)')} GCP")
        _line(f"  {dim('4)')} OpenAI / Anthropic   {dim('paste an API key, see your token bill')}")
        _line(f"  {dim('5)')} Skip for now")
        _blank()
        choice = "5"
        try:
            choice = input("  Choice [5]: ").strip() or "5"
        except (KeyboardInterrupt, EOFError):
            choice = "5"
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
        elif choice == "4":
            # AI-native fast path: connect a model provider and show the token
            # bill, the number that actually matters for this segment.
            shown = _connect_llm_provider()

    # Never dead-end. If they skipped, declined, or the scan came up empty,
    # show nable on sample data so the value lands before they ever leave.
    if not shown:
        _blank()
        _line(bold("Here's nable on a sample bill") + dim("  ·  connect an account to see your own"))
        _show_value_moment(demo=True)
        _line(dim("  Ready for real numbers?  ") + cyan("finops setup aws") + dim("  (read-only, ~1 min)"))
        _oneclick = _oneclick_aws_url()
        if _oneclick:
            _line(dim("  Or one-click a read-only key:  ") + cyan(_oneclick))
        _blank()

    # Finish, honest about which clients are wired, and the restart cliff. MCP
    # clients only load servers at startup, so a user with the editor already open
    # sees no nable tools and assumes setup failed. Name the restart explicitly.
    _line(_rule())
    _blank()
    _configured = client_result.get("configured", [])
    _manual = client_result.get("manual", [])
    if _configured:
        _line(bold(green("You're set up.")) + "  " + bold("Fully quit and reopen " + _and_list(_configured)) + dim(" (not just close the window), then ask:"))
    else:
        _line(bold(green("nable is installed.")) + "  Add it to your editor with the command below, then ask:")
    _blank()
    _line(f"  {cyan(_MAGIC_Q)}")
    _blank()
    for _client, _cmd in _manual:
        _line(dim(f"  Using {_client}? Run:  ") + cyan(_cmd))
    if _manual:
        _blank()
    _line(dim("  You should see nable in your editor's MCP tool list. Not there? Run 'finops doctor'."))
    _blank()
    _line(f"  Docs    →  {cyan('https://getnable.com/docs')}")
    _line(f"  Support →  {cyan('hello@getnable.com')}")
    _blank()
