"""
`nable scan` - the terminal front door.

One command, existing local AWS credentials, the recoverable dollars on your
account in under 60 seconds. No MCP client, no config, no LLM call, no secrets
typed, and NO paid API calls: the default scan reads only free AWS APIs
(Describe*, Compute Optimizer, CloudWatch GetMetricStatistics), so a tool we
market as free never puts a charge on the user's own AWS bill.

The spend breakdown (month-to-date total + top services + % of bill) lives
behind the opt-in `--spend` flag, because it needs Cost Explorer, which AWS
meters at $0.01 per request. `--spend` discloses that cost before calling.

Output contract (the design doc is the source of truth):

    nable scan · profile default                       <- first print, <2s, no network
    account 352112345678 · this account only           <- after STS returns
    scanning 4 regions ...
      us-east-1 ......... 3 findings
      eu-west-1 ......... 1 finding
    ────────────────────────────────────────────
    $2,140/mo recoverable
      $1,200/mo  3 idle RDS instances (db.r5.xlarge), us-east-1
      ...

    (with --spend, a headline is added above the recoverable line:)
    $48,210 on AWS this month. Top: Bedrock $19.2k · EC2 $11.4k · S3 $4.1k

Exit codes (pinned contract; argparse owns 2 for usage errors):
    0  success, including partial WITH results (banner shown)
    3  credentials expired (prints the exact refresh command)
    4  permission denied everywhere (prints the IAM actions needed)
    5  partial with no usable results
    6  no credentials found

Failure states never stack-trace; every one ends with a docs link. Telemetry
events (cli_scan_started / _completed / _failed) carry only event name, error
class and flags: no dollar figures, no account IDs, and they honor
NABLE_NO_TELEMETRY. The terminal event is sent synchronously before exit so
slow-account runs never lose their completion mark.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time

DOCS_LINE = "docs: https://getnable.com/docs/cli"
_FINDING_FLOOR_USD = 25.0  # findings below this monthly value stay out of v1 output
_MAX_FINDINGS_SHOWN = 5
_SCAN_DEADLINE_S = 45.0
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d+$")  # validates region names; filters CE NoRegion/global

# Exit codes. argparse exits 2 on usage errors; never reuse it here.
EXIT_OK = 0
EXIT_EXPIRED = 3
EXIT_DENIED = 4
EXIT_PARTIAL_EMPTY = 5
EXIT_NO_CREDS = 6


# ── tiny ANSI layer (self-contained: importing wizard helpers would be a cycle) ──

def _tty() -> bool:
    return sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m" if _tty() else s


def _dim(s: str) -> str:
    return f"\033[2m{s}\033[0m" if _tty() else s


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m" if _tty() else s


def _usd(v: float) -> str:
    if v >= 1000:
        return f"${v:,.0f}"
    return f"${v:,.2f}" if v < 100 else f"${v:,.0f}"


def _short_usd(v: float) -> str:
    return f"${v / 1000:.1f}k" if v >= 10_000 else _usd(v)


# ── telemetry (name + error class + flags only; never dollars or account IDs) ──

def _emit(event: str, props: dict, wait: bool) -> None:
    try:
        from . import telemetry

        payload = {"command": "scan", **props}
        if wait:
            telemetry._send_event(telemetry._get_install_id(), event, payload)
        else:
            threading.Thread(
                target=telemetry._send_event,
                args=(telemetry._get_install_id(), event, payload),
                daemon=True,
            ).start()
    except Exception:
        pass  # telemetry must never break the scan


# ── failure rendering: problem + cause + exact fix + docs link, never a trace ──

def _fail(out, code: int, lines: list[str], error_class: str, t0: float) -> int:
    for line in lines:
        print(line, file=out)
    print(_dim(DOCS_LINE), file=out)
    _emit(
        "cli_scan_failed",
        {"error_class": error_class, "duration_s": round(time.time() - t0, 1)},
        wait=True,
    )
    return code


def _finish(code: int, lingering: bool) -> int:
    """Return normally, or hard-exit when the deadline abandoned live threads.

    A timed-out scan leaves boto3 worker threads blocked in the C layer; they
    are non-daemon, so a normal return hangs at interpreter shutdown waiting for
    them (up to the full per-region duration). Output and telemetry are already
    flushed by the caller before this runs, so os._exit is safe and instant.
    Gated on the engine's real-abandonment flag, so mocked-report unit tests
    (which have no live threads) take the normal return path.
    """
    if lingering:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    return code


def _classify_boto_error(exc: Exception) -> str:
    """Map a botocore exception to one of our typed failure classes."""
    name = type(exc).__name__
    if name in ("NoCredentialsError", "CredentialRetrievalError", "PartialCredentialsError"):
        return "no-creds"
    if name in ("SSOTokenLoadError", "UnauthorizedSSOTokenError", "TokenRetrievalError"):
        return "expired"
    code = ""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = (resp.get("Error") or {}).get("Code", "")
    if code in ("ExpiredToken", "ExpiredTokenException", "RequestExpired", "InvalidClientTokenId"):
        return "expired"
    if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation"):
        return "denied"
    return "other"


# ── Cost Explorer: at most 2 queries, one page each (CE bills $0.01/request) ──

def _spend_snapshot(session) -> dict | None:
    """Month-to-date total + by-service + by-region from CE. None if denied."""
    from datetime import date

    ce = session.client("ce", region_name="us-east-1")
    today = date.today()
    start = today.replace(day=1).isoformat()
    end = today.isoformat()
    if start == end:  # first of the month: CE needs a non-empty window
        return {"period": start, "total": 0.0, "services": [], "regions": {}}

    def _grouped(dimension: str) -> list[tuple[str, float]]:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": dimension}],
            # No pagination follow-up: one request per dimension keeps the
            # documented "at most $0.06 per scan" promise true on wide accounts.
        )
        rows: list[tuple[str, float]] = []
        for result in resp.get("ResultsByTime", []):
            for group in result.get("Groups", []):
                key = group["Keys"][0]
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                rows.append((key, amount))
        return rows

    services = _grouped("SERVICE")
    regions = _grouped("REGION")
    total = sum(v for _, v in services)
    services.sort(key=lambda kv: kv[1], reverse=True)
    return {
        "period": f"{start} to {end}",
        "total": total,
        "services": services[:3],
        "regions": dict(regions),
    }


def _pick_regions(spend: dict | None, session) -> list[str]:
    """All opted-in regions, deadline-bounded by the caller.

    We do NOT cap or drop regions: without the (paid) CE spend data there is no
    free way to know which regions carry cost, and capping to an arbitrary N
    risks skipping the exact region holding the waste. Empty regions scan fast,
    and run_deep_audit's 45s deadline bounds the worst case. When `--spend` did
    fetch CE region data, order by spend descending so a budget cutoff does the
    valuable regions first.
    """
    from .analyzers.optimizer import _discover_regions

    discovered = _discover_regions(session)
    if spend and spend.get("regions"):
        weight = {r: v for r, v in spend["regions"].items() if _REGION_RE.match(r)}
        discovered.sort(key=lambda r: weight.get(r, 0.0), reverse=True)
    return discovered


# ── demo: same output path on the StreamCo dataset; the engine is never faked ──

def _demo_payload() -> tuple[dict, dict]:
    from . import demo_data

    cs = demo_data.cost_summary()
    services = sorted(cs["by_service"].items(), key=lambda kv: kv[1], reverse=True)
    spend = {
        "period": cs["period"],
        "total": cs["total_usd"],
        "services": services[:3],
        "regions": {},
    }
    findings = [
        {
            "waste_type": "idle_nat_gateway",
            "description": "4 NAT gateways with no traffic in 30 days",
            "region": "us-east-1",
            "estimated_monthly_savings": 12960.0,
        },
        {
            "waste_type": "unattached_ebs",
            "description": "212 unattached EBS volumes (48 TB, gp2)",
            "region": "us-east-1",
            "estimated_monthly_savings": 4680.0,
        },
        {
            "waste_type": "old_snapshots",
            "description": "1,900 EBS snapshots older than a year",
            "region": "us-west-2",
            "estimated_monthly_savings": 3120.0,
        },
        {
            "waste_type": "idle_rds",
            "description": "3 idle RDS instances (db.r5.xlarge, <2% CPU)",
            "region": "eu-west-1",
            "estimated_monthly_savings": 2840.0,
        },
        {
            "waste_type": "oversized_ec2",
            "description": "9 EC2 instances under 8% peak CPU (m5.2xlarge)",
            "region": "us-east-1",
            "estimated_monthly_savings": 2210.0,
        },
    ]
    total = sum(f["estimated_monthly_savings"] for f in findings)
    report = {
        "account_id": "demo",
        "regions_scanned": ["us-east-1", "us-west-2", "eu-west-1"],
        "regions_timed_out": [],
        "total_findings": len(findings),
        "total_estimated_monthly_savings": total,
        "total_estimated_annual_savings": total * 12,
        "findings": findings,
        "errors": [],
    }
    return spend, report


# ── rendering ──────────────────────────────────────────────────────────────────

def _render(out, spend, report, *, demo: bool, ce_denied: bool):
    demo_tag = _dim(" (demo data)") if demo else ""
    print("─" * 60, file=out)

    recoverable = float(report.get("total_estimated_monthly_savings") or 0.0)

    if spend and spend["total"] > 0:
        top = " · ".join(f"{name} {_short_usd(v)}" for name, v in spend["services"])
        print(
            _bold(f"{_usd(spend['total'])} on AWS this month.") + f" Top: {top}{demo_tag}",
            file=out,
        )
        if recoverable >= _FINDING_FLOOR_USD:
            pct = f" ({recoverable / spend['total'] * 100:.1f}% of spend)" if spend["total"] else ""
            print(_green(_bold(f"{_usd(recoverable)}/mo recoverable{pct}")) + demo_tag, file=out)
    else:
        if ce_denied:
            print(
                _dim(
                    "spend summary unavailable (missing ce:GetCostAndUsage; "
                    "run `nable iam-template` to fix)"
                ),
                file=out,
            )
        if recoverable >= _FINDING_FLOOR_USD:
            print(_green(_bold(f"{_usd(recoverable)}/mo recoverable")) + demo_tag, file=out)

    findings = [
        f
        for f in report.get("findings", [])
        if float(f.get("estimated_monthly_savings") or 0) >= _FINDING_FLOOR_USD
    ][:_MAX_FINDINGS_SHOWN]

    if recoverable < _FINDING_FLOOR_USD:
        # The proud state: a clean account is a result, not an apology.
        print(_green("no material waste found, nice") + demo_tag, file=out)
    else:
        for f in findings:
            monthly = float(f.get("estimated_monthly_savings") or 0)
            desc = f.get("description") or f.get("waste_type", "finding")
            region = f.get("region", "")
            print(f"  {_usd(monthly) + '/mo':>12}  {desc}" + (f", {region}" if region else ""), file=out)

    timed_out = report.get("regions_timed_out") or []
    if timed_out:
        done = len(report.get("regions_scanned") or [])
        print(
            _dim(f"scanned {done} of {done + len(timed_out)} regions "
                 f"(budget hit; unscanned: {', '.join(timed_out)})"),
            file=out,
        )
    if not (spend and spend.get("total")):
        print(_dim("run `nable scan --spend` for the spend breakdown (uses Cost Explorer, ~$0.02)"), file=out)
    print(_dim(DOCS_LINE), file=out)


def _json_payload(spend, report, *, demo, profile, account_id, duration_s):
    recoverable = float(report.get("total_estimated_monthly_savings") or 0.0)
    return {
        "schema_version": 1,
        "command": "scan",
        "demo": demo,
        "profile": profile,
        "account_id": account_id,
        "spend": (
            {
                "period": spend["period"],
                "month_to_date_usd": round(spend["total"], 2),
                "top_services": [
                    {"service": name, "usd": round(v, 2)} for name, v in spend["services"]
                ],
            }
            if spend
            else None
        ),
        "recoverable": {
            "monthly_usd": round(recoverable, 2),
            "annual_usd": round(recoverable * 12, 2),
            "pct_of_spend": (
                round(recoverable / spend["total"] * 100, 2)
                if spend and spend["total"]
                else None
            ),
        },
        "findings": report.get("findings", [])[:_MAX_FINDINGS_SHOWN * 4],
        "scan": {
            "regions_scanned": report.get("regions_scanned", []),
            "regions_timed_out": report.get("regions_timed_out", []),
            "errors": report.get("errors", []),
            "duration_s": round(duration_s, 1),
            "partial": bool(report.get("regions_timed_out")),
        },
    }


# ── the command ────────────────────────────────────────────────────────────────

def run(args) -> int:
    t0 = time.time()
    as_json = bool(getattr(args, "json", False))
    demo = bool(getattr(args, "demo", False)) or os.getenv("FINOPS_DEMO") == "1"
    want_spend = bool(getattr(args, "spend", False))
    profile = getattr(args, "profile", None) or os.getenv("AWS_PROFILE") or "default"
    if getattr(args, "profile", None):
        os.environ["AWS_PROFILE"] = args.profile
    import logging
    if getattr(args, "debug", False):
        logging.basicConfig(level=logging.DEBUG)
    else:
        # Per-region check failures (a least-privilege user missing ELB/ECR/ECS
        # describe perms, a region with no snapshots API, etc.) are expected and
        # are pure noise in a CLI whose whole value is a clean result. Keep the
        # analyzers' warnings out of stderr unless --debug asked for them.
        logging.getLogger("finops.analyzers").setLevel(logging.ERROR)

    # Human output goes to stdout; in --json mode the progress chrome moves to
    # stderr so stdout stays a single parseable document.
    out = sys.stderr if as_json else sys.stdout

    # First print: no network, within 2s of process start.
    print(f"{_bold('nable scan')} {_dim('· profile ' + profile)}", file=out)
    _emit("cli_scan_started", {"demo": demo}, wait=False)

    if demo:
        demo_spend, report = _demo_payload()
        # Demo mirrors real behavior: the spend headline only appears with --spend.
        spend = demo_spend if want_spend else None
        print(_dim("account demo · StreamCo demo dataset (demo data)"), file=out)
        _render(out, spend, report, demo=True, ce_denied=False)
        if as_json:
            print(json.dumps(_json_payload(
                spend, report, demo=True, profile=profile, account_id="demo",
                duration_s=time.time() - t0,
            ), indent=2))
        _emit("cli_scan_completed", {"demo": True, "duration_s": round(time.time() - t0, 1)}, wait=True)
        return EXIT_OK

    # ── pre-flight typed probes: these drive exit codes, never engine strings ──
    try:
        import boto3
        import botocore.exceptions  # noqa: F401
    except ImportError:
        return _fail(out, 1, ["boto3 is not installed; reinstall with `pip install finops-mcp`"], "other", t0)

    try:
        session = boto3.Session()
        if session.get_credentials() is None:
            return _fail(out, EXIT_NO_CREDS, [
                "no AWS credentials found on this machine",
                "  looked in: env vars, ~/.aws/credentials, ~/.aws/config (SSO), instance metadata",
                "  fix: `aws configure` or `aws sso login`, then rerun",
                "  or try it on sample data right now: `nable scan --demo`",
            ], "no-creds", t0)
        sts = session.client("sts")
        ident = sts.get_caller_identity()
        account_id = ident["Account"]
    except Exception as exc:
        klass = _classify_boto_error(exc)
        if klass == "expired":
            return _fail(out, EXIT_EXPIRED, [
                "your AWS session has expired",
                f"  fix: `aws sso login --profile {profile}`  (or refresh your temporary credentials)",
            ], "expired", t0)
        if klass == "no-creds":
            return _fail(out, EXIT_NO_CREDS, [
                "no usable AWS credentials found",
                "  fix: `aws configure` or `aws sso login`, then rerun",
                "  or try sample data: `nable scan --demo`",
            ], "no-creds", t0)
        if klass == "denied":
            return _fail(out, EXIT_DENIED, [
                "this AWS identity cannot call sts:GetCallerIdentity",
                "  fix: `nable iam-template` prints the read-only policy nable needs",
            ], "permission", t0)
        return _fail(out, 1, [f"could not reach AWS: {exc}"], "other", t0)

    # Scope is always labeled, never detected: no organizations API, no
    # permission trap, never wrong. Org-aware payer detection waits for CUR.
    print(_dim(f"account {account_id} · this account only"), file=out)

    # ── spend snapshot: OPT-IN ONLY ──
    # The default scan makes zero paid API calls, so a free tool never charges
    # the user's own AWS account. `--spend` adds the Cost Explorer breakdown,
    # which AWS bills at ~$0.02 per scan; we disclose that before the call. The
    # flag is the consent, so no interactive prompt (would break --json/CI).
    spend = None
    ce_denied = False
    if want_spend:
        print(
            _dim("spend breakdown: 2 Cost Explorer calls, about $0.02 on your AWS bill"),
            file=out,
        )
        try:
            spend = _spend_snapshot(session)
        except Exception as exc:
            if _classify_boto_error(exc) == "denied":
                ce_denied = True
            elif _classify_boto_error(exc) == "expired":
                return _fail(out, EXIT_EXPIRED, [
                    "your AWS session expired mid-run",
                    f"  fix: `aws sso login --profile {profile}`, then rerun",
                ], "expired", t0)
            # any other CE hiccup: proceed without the spend headline

    override = getattr(args, "regions", None)
    if override:
        bad = [r for r in override if not _REGION_RE.match(r)]
        if bad:
            return _fail(out, 1, [f"not valid region name(s): {', '.join(bad)}"], "other", t0)
        regions = override
    else:
        regions = _pick_regions(spend, session)
    if not regions:
        return _fail(out, EXIT_DENIED, [
            "could not determine any scannable region",
            "  this identity lacks ec2:DescribeRegions",
            "  fix: `nable iam-template` prints the read-only policy nable needs",
        ], "permission", t0)

    print(f"scanning {len(regions)} region{'s' if len(regions) != 1 else ''} ...", file=out)

    from .analyzers.optimizer import run_deep_audit

    def _progress(region: str, count: int, done: int, total: int) -> None:
        # Only surface regions that actually found something; a 17-region account
        # printing a dozen "0 findings" lines is noise, not progress.
        if count:
            print(f"  {region:<18} {count} finding{'s' if count != 1 else ''}", file=out)

    report = run_deep_audit(
        account_id=account_id,
        regions=regions,
        progress_callback=_progress,
        deadline_seconds=_SCAN_DEADLINE_S,
    )

    if report.get("error"):
        return _fail(out, 1, [f"scan failed: {report['error']}"], "other", t0)

    scanned = report.get("regions_scanned") or []
    has_results = bool(scanned)
    lingering = bool(report.get("_threads_abandoned"))

    if not has_results:
        # every region timed out or failed: partial with nothing usable
        code = _fail(out, EXIT_PARTIAL_EMPTY, [
            "the scan hit its 45s budget before any region finished",
            "  try a narrower run: `nable scan --regions us-east-1`",
        ], "timeout", t0)
        return _finish(code, lingering)

    _render(out, spend, report, demo=False, ce_denied=ce_denied)
    if as_json:
        print(json.dumps(_json_payload(
            spend, report, demo=False, profile=profile, account_id=account_id,
            duration_s=time.time() - t0,
        ), indent=2))

    _emit("cli_scan_completed", {
        "demo": False,
        "spend": want_spend,
        "duration_s": round(time.time() - t0, 1),
        "partial": bool(report.get("regions_timed_out")),
        "ce_denied": ce_denied,
    }, wait=True)
    return _finish(EXIT_OK, lingering)


def add_parser(sub) -> None:
    """Register the scan subcommand on the wizard's argparse tree."""
    p = sub.add_parser(
        "scan",
        help="Find recoverable AWS spend in under a minute, free (AWS-first on-ramp; add providers with 'nable connect')",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output on stdout")
    p.add_argument("--demo", action="store_true", help="run on the StreamCo sample dataset")
    p.add_argument(
        "--spend", action="store_true",
        help="add a month-to-date spend breakdown (uses Cost Explorer, ~$0.02 on your AWS bill)",
    )
    p.add_argument("--debug", action="store_true", help="full tracebacks and per-check timing")
    p.add_argument("--profile", help="AWS profile to use (default: $AWS_PROFILE or 'default')")
    p.add_argument(
        "--regions", nargs="+", metavar="REGION",
        help="scan exactly these regions instead of the auto-discovered set",
    )
