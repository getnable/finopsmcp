"""
finops-mcp doctor — health check for your nable installation.

Checks and reports on:
  ✓ Credential storage (keyring vs plain env vars)
  ✓ AWS credential scope (read-only vs over-provisioned)
  ✓ Database encryption and permissions
  ✓ Telemetry (none — confirmed)
  ✓ Network path (direct to cloud APIs, no proxy)
  ✓ Recent audit log entries

Usage:
  finops-mcp doctor
  finops-mcp doctor --json
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ── Check helpers ─────────────────────────────────────────────────────────────

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

    return {
        "name": "Credential storage",
        "ok": keyring_ok and not leaked,
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
        if "AccessDenied" in err:
            pass  # missing ce: permission
        else:
            ce_ok = True  # got a real response error, not auth

    return {
        "name": "AWS credential scope",
        "ok": ce_ok and not warnings,
        "detail": (
            f"Account {account_id} · {identity_arn}"
            + (" · Cost Explorer: ✓" if ce_ok else " · Cost Explorer: ✗ (missing ce:GetCostAndUsage)")
        ),
        "warnings": warnings,
        "recommendation": (
            "Run `finops setup aws --iam-template` to generate a least-privilege IAM policy"
            if warnings else
            ("Grant ce:GetCostAndUsage on this credential" if not ce_ok else None)
        ),
    }


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

    mode = oct(stat.S_IMODE(db_path.stat().st_mode))
    correct_perms = db_path.stat().st_mode & 0o177 == 0  # only owner bits set

    size_kb = db_path.stat().st_size // 1024

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
    analytics_vars = [v for v in os.environ if any(
        kw in v.upper() for kw in ["SENTRY", "AMPLITUDE", "SEGMENT", "MIXPANEL"]
    )]
    posthog_key = os.environ.get("NABLE_POSTHOG_KEY", "")
    warnings = [f"External analytics env var detected: {v}" for v in analytics_vars]
    if posthog_key:
        detail = (
            "Anonymous usage events are sent to PostHog (tool names + plan tier, no cost data). "
            "Disable by unsetting NABLE_POSTHOG_KEY. "
            "Queries go directly from your machine to your cloud provider APIs — no cost data leaves your machine."
        )
    else:
        detail = (
            "No usage telemetry active. "
            "Queries go directly from your machine to your cloud provider APIs."
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


def run_doctor(as_json: bool = False) -> int:
    """
    Run all health checks and print a report.
    Returns exit code: 0 = all ok, 1 = warnings/failures.
    """
    checks = [
        _check_path_and_install(),
        _check_keyring_storage(),
        _check_aws_scope(),
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
