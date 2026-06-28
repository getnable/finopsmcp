"""
finops-mcp doctor — health check for your nable installation.

Checks and reports on:
  ✓ Credential storage (keyring vs plain env vars)
  ✓ AWS credential scope (read-only vs over-provisioned)
  ✓ Database encryption and permissions
  ✓ Telemetry posture (on by default, anonymous; how to opt out)
  ✓ Network path (direct to cloud APIs, no proxy)
  ✓ Recent audit log entries

Usage:
  finops-mcp doctor
  finops-mcp doctor --json
"""
from __future__ import annotations

from ._preflight import require_python
require_python()

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ── Check helpers ─────────────────────────────────────────────────────────────

def _check_python_version() -> dict:
    """Report the running Python and hard-fail below nable's 3.10 floor.

    On too-old Python, pip refuses to install finops-mcp at all (the cryptic
    "No matching distribution found"), so most users never reach this check.
    It exists for source runs and to make the active interpreter visible.
    """
    v = sys.version_info
    ver = f"{v[0]}.{v[1]}.{v[2]}"
    ok = (v[0], v[1]) >= (3, 10)
    if ok:
        detail = f"Python {ver} at {sys.executable}"
        rec = None
    else:
        detail = f"Python {ver} at {sys.executable}, nable requires Python 3.10 or newer"
        rec = ("Reinstall on Python 3.10+, e.g. "
               "uvx --python 3.12 --from finops-mcp finops welcome")
    return {
        "name": "Python version",
        "ok": ok,
        "detail": detail,
        "warnings": [],
        "recommendation": rec,
    }


def _check_keyring_storage() -> dict:
    """Verify credentials are in the OS keyring, not plain env vars."""
    sensitive_env_keys = [
        "AWS_SECRET_ACCESS_KEY",
        "AZURE_CLIENT_SECRET",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "DATADOG_API_KEY",
        "SNOWFLAKE_PASSWORD",
    ]
    leaked = [k for k in sensitive_env_keys if os.environ.get(k)]

    keyring_ok = False
    keyring_detail = "keyring library not installed"
    try:
        import keyring
        val = keyring.get_password("finops-mcp", "master-key")
        if val:
            keyring_ok = True
            keyring_detail = "master key found in OS keyring"
        else:
            keyring_detail = "keyring available but master key not stored yet"
    except Exception as e:
        keyring_detail = str(e)

    # Hard-fail only when keyring IS installed but the master key is missing.
    # When keyring is not installed at all, treat as a warning (ok: None) rather
    # than a failure — env-var-based credentials are a valid setup path.
    if keyring_ok:
        ok_val: bool | None = not leaked  # keyring installed + keys in env = warn
    elif "not installed" in keyring_detail:
        ok_val = None  # keyring optional dep not present; env vars are acceptable
    else:
        ok_val = None  # keyring present but master key not stored yet

    return {
        "name": "Credential storage",
        "ok": ok_val,
        "detail": keyring_detail,
        "warnings": (
            [f"Sensitive key in env var: {k}" for k in leaked]
            if leaked else []
        ),
        "recommendation": (
            "Run `finops setup aws` to store credentials in the OS keyring"
            if not keyring_ok else None
        ),
    }


def _check_aws_scope() -> dict:
    """Check if AWS credentials are scoped correctly (read-only)."""
    try:
        import boto3
        from botocore.config import Config as BotocoreConfig
    except ImportError:
        return {
            "name": "AWS credential scope",
            "ok": None,
            "detail": "boto3 not installed — AWS checks skipped",
        }

    # Use a short connect+read timeout so a broken network doesn't hang doctor
    _boto_cfg = BotocoreConfig(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1})

    try:
        sts = boto3.client("sts", config=_boto_cfg)
        identity = sts.get_caller_identity()
        account_id = identity["Account"]
        identity_arn = identity["Arn"]
    except Exception as e:
        return {
            "name": "AWS credential scope",
            "ok": None,
            "detail": f"No AWS credentials configured: {e}",
        }

    # Quick write-permission probe via DryRun
    warnings: list[str] = []
    try:
        ec2 = boto3.client("ec2", region_name="us-east-1", config=_boto_cfg)
        try:
            ec2.terminate_instances(
                InstanceIds=["i-00000000000000000"],
                DryRun=True,
            )
            # If DryRun succeeds (it never will for terminate) — over-provisioned
            warnings.append("ec2:TerminateInstances — key has write permissions")
        except Exception as e:
            err_str = str(e)
            if "DryRunOperation" in err_str:
                # DryRun succeeded = would have been allowed
                warnings.append("ec2:TerminateInstances is ALLOWED — over-provisioned!")
            elif "UnauthorizedOperation" in err_str or "AccessDenied" in err_str:
                pass  # correctly denied
    except Exception:
        pass

    # Check Cost Explorer access (the core requirement)
    ce_ok = False
    ce_unverified: str | None = None
    try:
        from datetime import date, timedelta
        ce = boto3.client("ce", region_name="us-east-1", config=_boto_cfg)
        end = date.today()
        start = end - timedelta(days=1)
        ce.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
        )
        ce_ok = True
    except Exception as e:
        err = str(e)
        if "AccessDenied" in err or "not authorized" in err:
            pass  # missing ce: permission, ce_ok stays False
        else:
            # Throttling, an expired/invalid token, a validation error, or a
            # network failure: we could not verify access. Never report the one
            # capability doctor exists to check as healthy on an unconfirmed error.
            ce_unverified = err.splitlines()[0][:160] if err else "unknown error"

    # Extended Cost Explorer actions nable uses for commitments and forecasting.
    # A credential can pass GetCostAndUsage but still miss these, which only
    # surfaces as a mid-query error (e.g. "no identity-based policy allows the
    # ce:GetSavingsPlansCoverage action"). Probe them up front so doctor catches
    # the gap and hands back the exact fix. Each probe is a cheap 1-day call.
    missing_ce: list[str] = []
    if ce_ok:
        from datetime import date, timedelta
        _end = date.today()
        _start = _end - timedelta(days=1)
        _tp = {"Start": _start.isoformat(), "End": _end.isoformat()}
        _probes = [
            ("ce:GetSavingsPlansCoverage",
             lambda: ce.get_savings_plans_coverage(TimePeriod=_tp, Granularity="DAILY")),
            ("ce:GetReservationUtilization",
             lambda: ce.get_reservation_utilization(TimePeriod=_tp, Granularity="DAILY")),
        ]
        for action, call in _probes:
            try:
                call()
            except Exception as e:
                # Only a real authorization denial counts as missing. Other
                # errors (DataUnavailable, validation) mean the action is allowed.
                if "AccessDenied" in str(e) or "not authorized" in str(e):
                    missing_ce.append(action)

    if not ce_ok and ce_unverified:
        ce_detail = f" · Cost Explorer: ? (could not verify: {ce_unverified})"
        rec = ("Could not verify Cost Explorer access (this is not a permissions denial). "
               "Check the credential or session is still valid, e.g. run: aws sso login, then retry: finops doctor")
    elif not ce_ok:
        ce_detail = " · Cost Explorer: ✗ (missing ce:GetCostAndUsage)"
        rec = "Grant ce:GetCostAndUsage on this credential, or run: finops setup aws --iam-template"
    elif missing_ce:
        ce_detail = f" · Cost Explorer: ✓ core, missing {', '.join(missing_ce)}"
        rec = ("Commitment and forecast queries will fail. Run: finops setup aws "
               "--iam-template, then apply the updated policy to this credential.")
    else:
        ce_detail = " · Cost Explorer: ✓"
        rec = None

    if warnings:
        rec = "Run `finops setup aws --iam-template` to generate a least-privilege IAM policy"

    return {
        "name": "AWS credential scope",
        "ok": ce_ok and not warnings and not missing_ce,
        "detail": f"Account {account_id} · {identity_arn}{ce_detail}",
        "warnings": warnings,
        "recommendation": rec,
    }


def _check_azure_permissions() -> dict:
    """Probe the Azure RBAC roles nable's Azure tools need.

    nable's Azure tools span three roles per subscription: Cost Management Reader
    (cost queries), Reader (Advisor + VM list), and Monitoring Reader (VM CPU for
    rightsizing). A missing role surfaces at query time as an empty result, which
    is confusing, so catch it here up front. Skips cleanly when Azure is not set up.
    """
    name = "Azure permissions"
    try:
        from .connectors.azure_detail import (
            _MGMT_BASE, _auth_headers, _get_access_token, _subscription_ids, is_configured,
        )
    except Exception:
        return {"name": name, "ok": None, "detail": "Azure helpers unavailable."}
    if not is_configured():
        return {"name": name, "ok": None, "detail": "Azure not configured — skipped."}
    try:
        import httpx
    except ImportError:
        return {"name": name, "ok": None, "detail": "httpx not installed — Azure checks skipped."}
    try:
        token = _get_access_token()
    except Exception as e:
        return {"name": name, "ok": None, "detail": f"Azure auth failed: {e}"}

    subs = _subscription_ids()
    if not subs:
        return {"name": name, "ok": None, "detail": "No Azure subscriptions configured."}
    sub = subs[0]
    headers = _auth_headers(token)
    warnings: list[str] = []

    # Reader probe: list VMs.
    reader_ok = None
    vms: list = []
    try:
        r = httpx.get(
            f"{_MGMT_BASE}/subscriptions/{sub}/providers/Microsoft.Compute/virtualMachines"
            f"?api-version=2023-09-01", headers=headers, timeout=15,
        )
        if r.status_code == 200:
            reader_ok, vms = True, r.json().get("value", [])
        elif r.status_code == 403:
            reader_ok = False
    except Exception:
        pass

    # Monitoring Reader probe: read a CPU metric on the first VM, if any.
    monitoring_ok = None
    if vms:
        vm_id = vms[0].get("id", "")
        try:
            from datetime import datetime, timedelta, timezone
            end = datetime.now(timezone.utc)
            start = end - timedelta(hours=6)
            # Use a 'Z' suffix, not isoformat()'s '+00:00'. A literal '+' in the
            # query string is decoded to a space by ARM, breaking the timespan and
            # returning 400 (a false 'missing Monitoring Reader' signal).
            ts = f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}/{end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
            mr = httpx.get(
                f"{_MGMT_BASE}{vm_id}/providers/Microsoft.Insights/metrics"
                f"?api-version=2023-10-01&metricnames=Percentage CPU"
                f"&timespan={ts}&interval=PT1H&aggregation=Average",
                headers=headers, timeout=15,
            )
            monitoring_ok = mr.status_code == 200
        except Exception:
            monitoring_ok = None

    if reader_ok is False:
        warnings.append(
            f"Missing 'Reader' role: VM rightsizing and Advisor return nothing. Grant: "
            f"az role assignment create --assignee <client-id> --role Reader "
            f"--scope /subscriptions/{sub}"
        )
    if monitoring_ok is False:
        warnings.append(
            f"Missing 'Monitoring Reader' role: VM CPU is unavailable, so rightsizing "
            f"cannot flag idle VMs. Grant: az role assignment create --assignee <client-id> "
            f"--role 'Monitoring Reader' --scope /subscriptions/{sub}"
        )

    if reader_ok and (monitoring_ok or not vms):
        detail = "Azure roles look good for cost, Advisor, and VM rightsizing."
        if not vms:
            detail += " (No VMs found, so Monitoring Reader was not fully verified.)"
        ok: bool | None = True
    elif reader_ok is None:
        detail = "Could not probe Azure roles (non-403 error)."
        ok = None
    else:
        detail = (
            "nable's Azure tools need three roles per subscription: Cost Management "
            "Reader (cost), Reader (Advisor + VM list), Monitoring Reader (VM CPU). "
            "See warnings for the missing ones."
        )
        ok = False

    return {"name": name, "ok": ok, "detail": detail, "warnings": warnings}


def _check_database() -> dict:
    """Verify the database exists, is readable, and has correct file permissions."""
    from .storage.db import data_dir
    import stat

    db_path = data_dir() / "finops.db"

    if not db_path.exists():
        return {
            "name": "Local database",
            "ok": True,
            "detail": "Not created yet — will be created on first query",
        }

    file_stat = db_path.stat()
    mode = oct(stat.S_IMODE(file_stat.st_mode))
    # Good permissions: owner has read+write, no group or world bits set
    owner_rw = bool(file_stat.st_mode & stat.S_IRUSR) and bool(file_stat.st_mode & stat.S_IWUSR)
    no_others = file_stat.st_mode & 0o177 == 0
    correct_perms = owner_rw and no_others

    size_kb = file_stat.st_size // 1024

    return {
        "name": "Local database",
        "ok": correct_perms,
        "detail": f"{db_path} · {size_kb} KB · permissions {mode}",
        "warnings": (
            [f"File permissions {mode} — should be 0o600 (owner read/write only)"]
            if not correct_perms else []
        ),
        "recommendation": (
            f"Run: chmod 600 {db_path}"
            if not correct_perms else None
        ),
    }


def _check_telemetry() -> dict:
    """Report nable's telemetry posture honestly."""
    # Only flag actual analytics SDK config vars, not trace propagation headers.
    # SENTRY-TRACE and BAGGAGE are OpenTelemetry headers injected by some IDEs/tools
    # and are not Sentry SDK configuration — skip them to avoid false positives.
    _ANALYTICS_SDK_VARS = {"SENTRY_DSN", "SENTRY_AUTH_TOKEN", "SENTRY_ORG", "AMPLITUDE_API_KEY", "SEGMENT_WRITE_KEY", "MIXPANEL_TOKEN"}
    analytics_vars = [v for v in os.environ if v.upper() in _ANALYTICS_SDK_VARS]
    warnings = [f"External analytics env var detected: {v}" for v in analytics_vars]
    # Ask the telemetry module for ground truth instead of guessing from an env
    # var. Telemetry ships with a default PostHog key, so it is ON unless the user
    # opted out (NABLE_NO_TELEMETRY=1, FINOPS_AIRGAP, or an empty key). Reporting
    # "off" here when it is actually on would be a trust violation for a tool whose
    # whole pitch is local-first.
    try:
        from . import telemetry as _tel
        telemetry_on = not _tel._is_opted_out()
    except Exception:
        telemetry_on = False
    if telemetry_on:
        detail = (
            "Anonymous usage telemetry is ON (default). It sends a random install ID, "
            "tool names, provider count, and plan tier to PostHog. It never sends cost "
            "figures, account IDs, or credentials. Opt out any time: export NABLE_NO_TELEMETRY=1. "
            "Cost queries always go straight from your machine to your cloud APIs."
        )
    else:
        detail = (
            "Usage telemetry is OFF. "
            "Cost queries go directly from your machine to your cloud provider APIs."
        )
    return {
        "name": "Telemetry",
        "ok": True,
        "detail": detail,
        "warnings": warnings,
    }


def _check_network() -> dict:
    """Check for proxies that might intercept cloud API traffic."""
    proxy_vars = {
        k: os.environ[k]
        for k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"]
        if k in os.environ
    }
    no_proxy = os.environ.get("NO_PROXY", os.environ.get("no_proxy", ""))

    if not proxy_vars:
        return {
            "name": "Network path",
            "ok": True,
            "detail": "Direct connection — no HTTP proxy detected",
        }

    return {
        "name": "Network path",
        "ok": None,  # not necessarily bad, just worth noting
        "detail": f"HTTP proxy configured: {list(proxy_vars.values())[0]}",
        "warnings": [
            "A proxy is in your environment. Cloud API requests will route through it.",
            "This is common in corporate environments and is usually fine.",
            f"NO_PROXY: {no_proxy}" if no_proxy else "Consider setting NO_PROXY for cloud endpoints if you control the proxy.",
        ],
    }


def _check_audit_log(last_n: int = 5) -> dict:
    """Return recent audit log entries from the vault."""
    try:
        from .storage.db import get_engine, audit_log
        from sqlalchemy import select
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                select(audit_log)
                .order_by(audit_log.c.ts.desc())
                .limit(last_n)
            ).fetchall()
        entries = [
            {
                "ts": str(r.ts),
                "operation": r.operation,
                "key_name": r.key_name,
                "user": r.client_user or "—",
            }
            for r in rows
        ]
        return {
            "name": "Audit log",
            "ok": True,
            "detail": f"{len(entries)} recent entries",
            "entries": entries,
        }
    except Exception as e:
        return {
            "name": "Audit log",
            "ok": None,
            "detail": f"Could not read audit log: {e}",
        }


# ── Full audit log CLI ────────────────────────────────────────────────────────

def print_audit_log(hours: int = 24, limit: int = 50, as_json: bool = False) -> None:
    """Print audit log entries to stdout."""
    try:
        from .storage.db import get_engine, audit_log
        from sqlalchemy import select
        engine = get_engine()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        with engine.connect() as conn:
            rows = conn.execute(
                select(audit_log)
                .where(audit_log.c.ts >= cutoff)
                .order_by(audit_log.c.ts.desc())
                .limit(limit)
            ).fetchall()
    except Exception as e:
        print(f"  ✗ Could not read audit log: {e}", file=sys.stderr)
        return

    if as_json:
        print(json.dumps(
            [dict(r._mapping) for r in rows],
            default=str, indent=2
        ))
        return

    print(f"\n  Audit log — last {hours}h ({len(rows)} entries)\n")
    if not rows:
        print("  (no entries)")
        return
    print(f"  {'Timestamp':<22} {'Operation':<16} {'Key':<30} {'User'}")
    print(f"  {'─'*22} {'─'*16} {'─'*30} {'─'*20}")
    for r in rows:
        ts = str(r.ts)[:19]
        print(f"  {ts:<22} {r.operation:<16} {r.key_name:<30} {r.client_user or '—'}")
    print()


# ── Doctor report ─────────────────────────────────────────────────────────────

def _check_path_and_install() -> dict:
    """Check that the finops and finops-mcp commands are in PATH."""
    import shutil
    import sys
    from pathlib import Path

    issues: list[str] = []
    recommendations: list[str] = []

    finops_in_path = shutil.which("finops") is not None
    mcp_in_path = shutil.which("finops-mcp") is not None

    scripts_dir = Path(sys.executable).parent

    if not finops_in_path:
        issues.append(f"'finops' not in PATH — shell cannot find the setup command")
        recommendations.append(f"Add to PATH: export PATH=\"{scripts_dir}:$PATH\"  (then add to ~/.zshrc)")
    if not mcp_in_path:
        issues.append(f"'finops-mcp' not in PATH — Claude Desktop may fail to start the MCP server")
        recommendations.append(
            f"Add to PATH: export PATH=\"{scripts_dir}:$PATH\"  or use uvx: run 'finops setup claude'"
        )

    return {
        "name": "PATH / install",
        "ok": len(issues) == 0,
        "detail": "Both 'finops' and 'finops-mcp' found in PATH" if not issues else issues[0],
        "warnings": issues[1:],
        "recommendation": recommendations[0] if recommendations else None,
    }


def _check_license() -> dict:
    """Report the active license tier so a user can confirm a key took effect.
    This is the terminal-side answer to 'did my FINOPS_LICENSE_KEY activate?'"""
    try:
        from .license import get_status
        s = get_status()
        mode = getattr(s, "mode", "free")
    except Exception as e:
        return {"name": "License", "ok": None,
                "detail": f"Could not read license status: {e}", "warnings": []}

    label = {"team": "Team", "pro": "Pro", "enterprise": "Enterprise",
             "trial": "Trial", "free": "Free"}.get(mode, mode)

    if mode in ("pro", "team", "enterprise"):
        email = getattr(s, "email", "") or ""
        detail = f"{label} plan active" + (f" ({email})" if email else "")
        return {"name": "License", "ok": True, "detail": detail, "warnings": []}
    if mode == "trial":
        days = getattr(s, "days_remaining", 0)
        return {"name": "License", "ok": True,
                "detail": f"Trial active, {days} day(s) left — all features unlocked.",
                "warnings": []}
    if mode == "invalid":
        return {"name": "License", "ok": False,
                "detail": ("FINOPS_LICENSE_KEY is set but invalid or expired. Re-check the "
                           "key (no extra spaces/quotes), or ask for a fresh one."),
                "warnings": []}
    # free
    return {"name": "License", "ok": None,
            "detail": ("Free tier. To activate a paid key, set FINOPS_LICENSE_KEY in your "
                       "nable MCP config env block (then fully restart your editor)."),
            "warnings": []}


def run_doctor(as_json: bool = False) -> int:
    """
    Run all health checks and print a report.
    Returns exit code: 0 = all ok, 1 = warnings/failures.
    """
    checks = [
        _check_python_version(),
        _check_path_and_install(),
        _check_license(),
        _check_keyring_storage(),
        _check_aws_scope(),
        _check_azure_permissions(),
        _check_database(),
        _check_telemetry(),
        _check_network(),
        _check_audit_log(),
    ]

    if as_json:
        print(json.dumps(checks, indent=2, default=str))
        failures = [c for c in checks if c.get("ok") is False]
        return 1 if failures else 0

    print()
    print("  nable · finops-mcp doctor")
    print(f"  {'─' * 58}")

    has_failure = False
    has_warning = False

    for c in checks:
        ok = c.get("ok")
        if ok is True:
            icon = "✓"
        elif ok is False:
            icon = "✗"
            has_failure = True
        else:
            icon = "·"  # unknown / not applicable

        print(f"  {icon} {c['name']:<28} {c['detail']}")

        for w in c.get("warnings", []):
            print(f"    ⚠  {w}")
            has_warning = True

        rec = c.get("recommendation")
        if rec:
            print(f"    → {rec}")

        # Print audit entries inline
        for entry in c.get("entries", []):
            print(f"    {entry['ts'][:19]}  {entry['operation']:<14} {entry['key_name']}")

    print(f"  {'─' * 58}")

    if has_failure:
        print("  Status: issues found — see recommendations above")
        print(f"  Docs:   https://getnable.com/docs")
        print()
        return 1
    elif has_warning:
        print("  Status: warnings only — review above")
        print(f"  Docs:   https://getnable.com/docs")
        print()
        return 0
    else:
        print("  Status: all checks passed")
        print("  Next:   restart Claude and ask \"What are my cloud costs this month?\"")
        print("          or run `finops tools` for more example questions")
        print(f"  Docs:   https://getnable.com/docs")
        print()
        return 0


def main(args: list[str] | None = None) -> None:
    import argparse
    p = argparse.ArgumentParser(
        prog="finops-mcp doctor",
        description="Health check for your nable (finops-mcp) installation",
    )
    p.add_argument("--json", action="store_true", help="Output results as JSON")
    p.add_argument(
        "--audit", action="store_true",
        help="Print audit log entries instead of running health checks",
    )
    p.add_argument(
        "--hours", type=int, default=24,
        help="Hours of audit log to show (default: 24)",
    )
    p.add_argument(
        "--limit", type=int, default=50,
        help="Max audit log entries to show (default: 50)",
    )
    parsed = p.parse_args(args if args is not None else sys.argv[1:])

    if parsed.audit:
        print_audit_log(hours=parsed.hours, limit=parsed.limit, as_json=parsed.json)
    else:
        code = run_doctor(as_json=parsed.json)
        sys.exit(code)
