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
    1  an unexpected error (prints how to report it) or a bad --regions value
    3  credentials expired (prints the exact refresh command)
    4  permission denied everywhere (prints the IAM actions needed)
    5  partial with no usable results: nothing finished, or most checks could
       not run, so the findings cannot stand in for the account
    6  no credentials found, or AWS rejected the ones it found
    7  local AWS config does not resolve (unknown profile, unparseable config,
       no region). Distinct from 6: the machine HAS a setup, it just is wrong.
    130  cancelled with Ctrl-C

With --json, every failure also prints one document on stdout,
{"error": {"class", "exit_code", "message"}}, so a script reading stdout gets
an answer instead of nothing.

Failure states never stack-trace; every one ends with a docs link. Telemetry
events (cli_scan_started / _completed / _failed) carry only event name, error
class and flags: no dollar figures, no account IDs. Telemetry is OPT-IN
(NABLE_TELEMETRY=1); nothing is sent otherwise. The terminal event is sent
synchronously before exit so slow-account runs never lose their completion mark.

`--dry-run` prints every API call and IAM permission a scan would make and
returns before reading credentials, so the question "what will this touch?"
can be answered on a machine that has granted nothing.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time

DOCS_LINE = "docs: https://getnable.com/docs/cli"

# Staleness check state. The network call runs in a daemon thread started at
# scan start so it overlaps the AWS work and costs the user nothing; the exit
# paths read it with a short join. Module-level rather than passed around
# because every exit path needs it and threading it through six signatures is
# how one of them ends up forgetting.
_stale_thread: "threading.Thread | None" = None
_stale_note: str | None = None
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
# Local AWS config is wrong (missing profile, unparseable config, no region).
# Distinct from no-creds: the machine HAS a setup, it just does not resolve.
EXIT_CONFIG = 7
EXIT_CANCELLED = 130  # the shell convention for SIGINT

# Set by run() for the duration of a --json scan, so every failure path also
# answers on stdout. Module-level for the same reason as the staleness state:
# every exit path needs it.
_json_mode = False

# Where the credentials came from, in words, and the fix when AWS rejects them.
# Env-var keys win over every profile in botocore's chain, so a scan run with
# AWS_ACCESS_KEY_ID exported is not "profile default", whatever AWS_PROFILE says.
_ENV_KEYS_LABEL = "credentials from AWS_ACCESS_KEY_ID in your environment"
_ENV_KEYS_FIX = ("  fix: replace AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY with a working key, "
                 "or unset them (and AWS_SESSION_TOKEN) to use your AWS profiles")


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

def _staleness_line(timeout: float = 0.4) -> str | None:
    """The one-line "you are on an old build" note, if the check has an answer.

    Started in the background at scan start, read here. Short timeout because a
    failing scan must stay fast: if PyPI has not answered by now, the note is
    dropped rather than made to wait. It is advice, not the result.
    """
    global _stale_thread, _stale_note
    try:
        if _stale_thread is not None:
            _stale_thread.join(timeout)
        return _stale_note
    except Exception:
        return None


def _fail(out, code: int, lines: list[str], error_class: str, t0: float,
          exc: Exception | None = None, props: dict | None = None,
          json_error: bool = True, docs_line: bool = True) -> int:
    for line in lines:
        print(line, file=out)
    if _json_mode and json_error:
        # --json promises stdout is one parseable document. A failure used to
        # print nothing there at all, so a script saw empty output and a code.
        # The class is one of our fixed names; the message is the human line.
        print(json.dumps({"error": {
            "class": error_class, "exit_code": code,
            "message": lines[0].strip() if lines else error_class,
        }}), file=sys.stdout)

    # Staleness FIRST among the follow-ups, because on an old build it is very
    # often the actual answer and the message above is not. Measured 2026-08-14:
    # 19 of 20 scans in 48 hours failed, and the ones carrying a version were on
    # 0.8.201 and 0.8.202 telling the user "boto3 is not installed; reinstall
    # with pip install finops-mcp". boto3 was installed. The real cause was a
    # boto3/botocore skew, diagnosed properly in 0.8.207, and the advice they
    # were given was a no-op: `pip install` without -U on an installed package
    # prints "Requirement already satisfied" and changes nothing. They were sent
    # in a circle by a build that predates the fix.
    #
    # A version gap does not prove THIS failure is fixed upstream, so the wording
    # claims only what is true: there is a newer build, here is how to get it.
    stale = _staleness_line()
    if stale:
        print(file=out)
        print(_bold("  " + stale), file=out)

    if docs_line:
        print(_dim(DOCS_LINE), file=out)
    # version + exception CLASS NAME only (never the message: messages carry
    # paths and account details). Without these, a month of real failures was
    # one opaque "other" bucket nobody could diagnose remotely. `site` is the
    # caller's line, stamped here so every _fail call, present and future, is
    # locatable: line numbers drift across releases, but every event also
    # carries the version, and the pair pins the exact statement. `props` is for
    # extra diagnosis a specific site can add; same rule applies: names and
    # version strings only, never a message, never a path.
    from . import __version__ as _v
    _site = ""
    try:
        _site = f"cli_scan:{sys._getframe(1).f_lineno}"
    except Exception:
        pass
    _emit(
        "cli_scan_failed",
        {"error_class": error_class, "duration_s": round(time.time() - t0, 1),
         "version": _v, "exc_type": type(exc).__name__ if exc else "",
         "site": _site, **(props or {})},
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


def _threads_lingering() -> bool:
    """True when a non-daemon worker thread is still alive, which would make a
    normal return wait at interpreter shutdown."""
    main = threading.main_thread()
    return any(t is not main and not t.daemon and t.is_alive()
               for t in threading.enumerate())


def _split_regions(values: list[str] | None) -> list[str]:
    """--regions as people type it. The flag took space-separated values only,
    so `--regions us-east-1,us-west-2` (how the AWS CLI and most tools spell a
    list) failed as one invalid region, and `US-EAST-1` failed on case.
    Repeating the flag adds to the list (argparse action="extend")."""
    out: list[str] = []
    for v in values or []:
        for r in re.split(r"[,\s]+", v.strip().lower()):
            if r and r not in out:
                out.append(r)
    return out


def _classify_boto_error(exc: Exception) -> str:
    """Map a botocore exception to one of our typed failure classes.

    Every class here has to earn a distinct FIX line. A class that cannot tell the
    user what to do next is worth nothing, and "other" is the bucket we are trying
    to empty: it was 100% of observed scan failures while covering four unrelated
    causes, which made the telemetry useless for diagnosing any of them.
    """
    name = type(exc).__name__
    # Local config problems. These fail instantly, before any network, and used to
    # fall through to "other" with a raw botocore string and no fix line.
    if name == "ProfileNotFound":
        return "profile-missing"
    if name in ("ConfigParseError", "ConfigNotFound"):
        return "config-broken"
    if name in ("NoRegionError",):
        return "no-region"
    if name in ("NoCredentialsError", "CredentialRetrievalError", "PartialCredentialsError"):
        return "no-creds"
    if name in ("SSOTokenLoadError", "UnauthorizedSSOTokenError", "TokenRetrievalError"):
        return "expired"
    code = ""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = (resp.get("Error") or {}).get("Code", "")
    if code in ("ExpiredToken", "ExpiredTokenException", "RequestExpired"):
        return "expired"
    # NOT expired. InvalidClientTokenId means the access key ID does not exist and
    # SignatureDoesNotMatch means the secret is wrong; neither is fixed by
    # re-authenticating, so sending the user to `aws sso login` wastes their time
    # and hides a typo'd or revoked key.
    if code in ("InvalidClientTokenId", "SignatureDoesNotMatch", "AuthFailure",
                "UnrecognizedClientException", "InvalidAccessKeyId"):
        return "bad-creds"
    if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation"):
        return "denied"
    # Cannot reach AWS at all: proxy, VPN, TLS interception, DNS, offline. These
    # were 97% of real-world scan failures ("other", duration 0s) before they
    # were classified, because a corporate machine fails the first STS call
    # instantly and none of these exception names were mapped.
    if name in ("EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError",
                "ProxyConnectionError", "ConnectionClosedError", "SSLError"):
        return "network"
    return "other"


def _available_profiles() -> list[str]:
    """Profiles boto3 can actually see, for the "you meant one of these" hint.
    Best-effort: a broken config is one of the cases we are reporting on, and it
    makes this raise too."""
    try:
        # Parse the config files directly. boto3.Session().available_profiles
        # cannot be used here: constructing the session honors AWS_PROFILE, so on
        # the exact failure we are reporting (that profile does not exist) it
        # raises ProfileNotFound and we would tell the user they have no profiles
        # while staring at the one they meant.
        import botocore.session
        return sorted(botocore.session.Session().full_config.get("profiles", {}))
    except Exception:
        return []


# ── Cost Explorer: at most 2 queries, one page each (CE bills $0.01/request) ──

def _spend_window(today) -> tuple[str, str, str]:
    """The CE TimePeriod to ask for, plus a label for what it covers.

    Split out of _spend_snapshot so the month boundary is testable on any day of
    the year. It used to live inline and only ever ran correctly on days 2-31:
    on the 1st, month-to-date is an empty window that Cost Explorer rejects, and
    the code returned a hand-made zero. So for one day in thirty nable told
    people their cloud bill was $0.00 and named no services, which is worse than
    an error because it looks like an answer. Ask for the month that just closed
    instead and say so: it is complete, it is the number they want on the 1st,
    and it is never a lie. CE end dates are exclusive, so `end` is safe to leave
    at the first of the current month.
    """
    from datetime import timedelta

    first_of_month = today.replace(day=1)
    if today == first_of_month:
        prev_start = (first_of_month - timedelta(days=1)).replace(day=1)
        return prev_start.isoformat(), first_of_month.isoformat(), "last month"
    return first_of_month.isoformat(), today.isoformat(), "month-to-date"


def _region_catalog(session) -> dict[str, str]:
    """Every region AWS knows, with its opt-in status for this account, from
    ec2:DescribeRegions (AllRegions=True). Raises when the call fails; the
    caller decides what that means."""
    ec2 = session.client("ec2", region_name="us-east-1")
    resp = ec2.describe_regions(AllRegions=True)
    return {r["RegionName"]: r.get("OptInStatus", "opt-in-not-required")
            for r in resp.get("Regions", [])}


def _spend_snapshot(session) -> dict | None:
    """Month-to-date total + by-service + by-region from CE. None if denied."""
    from datetime import date

    ce = session.client("ce", region_name="us-east-1")
    start, end, covers = _spend_window(date.today())

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
        "covers": covers,
        "total": total,
        "services": services[:3],
        "regions": dict(regions),
        # No groups at all is "Cost Explorer had nothing for this window",
        # which is not a $0 bill. New accounts, and CE in its first 24 hours,
        # answer exactly this way.
        "has_data": bool(services),
    }


# Regions most AWS accounts concentrate spend in. Absent paid Cost Explorer data
# to rank by, scan these first so a deadline cutoff trims the empty long tail,
# not the region actually holding the waste (usually us-east-1).
_DEFAULT_REGION_PRIORITY = [
    "us-east-1", "us-west-2", "us-east-2", "eu-west-1",
    "eu-central-1", "eu-west-2", "ap-southeast-1", "ap-southeast-2",
    "ap-northeast-1", "ap-south-1", "us-west-1", "ca-central-1",
]


def _pick_regions(spend: dict | None, session) -> list[str]:
    """All opted-in regions, ordered so the caller's deadline trims the tail.

    We do NOT cap or drop regions: without the (paid) CE spend data there is no
    free way to know which regions carry cost, and capping to an arbitrary N
    risks skipping the exact region holding the waste. Empty regions scan fast,
    and run_deep_audit's deadline bounds the worst case. Ordering is what keeps
    that deadline from cutting off the region that matters: with `--spend` CE
    data, scan by spend descending; without it, fall back to a default prior
    (the regions most accounts spend the most in) so the long tail is trimmed,
    never us-east-1.
    """
    from .analyzers.optimizer import _discover_regions

    discovered = _discover_regions(session)
    if spend and spend.get("regions"):
        weight = {r: v for r, v in spend["regions"].items() if _REGION_RE.match(r)}
        discovered.sort(key=lambda r: weight.get(r, 0.0), reverse=True)
    else:
        rank = {r: i for i, r in enumerate(_DEFAULT_REGION_PRIORITY)}
        discovered.sort(key=lambda r: rank.get(r, len(_DEFAULT_REGION_PRIORITY)))
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

def _render_extra(out, b) -> None:
    """Render one AI/GCP/Azure provider block. A non-ok block is a quiet note,
    never a failure of the whole scan."""
    if b.status != "ok":
        print(f"  {_dim(b.label + ':')} {_dim(b.note or b.status)}", file=out)
        return
    bits = []
    if b.spend_usd is not None:
        tag = _dim(" [estimated]") if b.estimated else ""
        bits.append(f"{_usd(b.spend_usd)}/mo" + tag)
    if b.recoverable_usd:
        tag = _dim(" [early]") if b.early_recoverable else ""
        bits.append(_green(f"{_usd(b.recoverable_usd)}/mo recoverable") + tag)
    head = "   ".join(bits) if bits else _dim("connected")
    print(_bold(b.label) + f"   {head}", file=out)
    if b.detail:
        print(f"      {_dim(b.detail)}", file=out)
    if b.note:
        print(f"      {_dim(b.note)}", file=out)


# One line per kind of waste, the way `--demo` reads, instead of one line per
# resource. A real finding carries `detail` (a sentence about one resource) and
# no `description`, so a first real scan printed the internal key,
# `unattached_ebs_volume, us-east-1`, once per volume, and the five lines it had
# room for did not add up to the headline above them.
_WASTE_LABELS: dict[str, tuple[str, str]] = {
    "unattached_ebs_volume": ("unattached EBS volume", "unattached EBS volumes"),
    "unattached_ebs": ("unattached EBS volume", "unattached EBS volumes"),
    "gp2_should_migrate_to_gp3": ("gp2 volume cheaper as gp3", "gp2 volumes cheaper as gp3"),
    "old_snapshots": ("old EBS snapshot", "old EBS snapshots"),
    "old_unmanaged_snapshot": ("old EBS snapshot", "old EBS snapshots"),
    "idle_nat_gateway": ("idle NAT gateway", "idle NAT gateways"),
    "idle_load_balancer": ("idle load balancer", "idle load balancers"),
    "unassociated_elastic_ip": ("unused Elastic IP", "unused Elastic IPs"),
    "idle_ec2_low_cpu": ("idle EC2 instance", "idle EC2 instances"),
    "oversized_ec2": ("oversized EC2 instance", "oversized EC2 instances"),
    "compute_optimizer_overprovisioned_ec2": ("oversized EC2 instance", "oversized EC2 instances"),
    "idle_rds": ("idle RDS instance", "idle RDS instances"),
    "rds_idle_no_connections": ("idle RDS instance", "idle RDS instances"),
    "rds_overprovisioned": ("oversized RDS instance", "oversized RDS instances"),
    "compute_optimizer_overprovisioned_rds": ("oversized RDS instance", "oversized RDS instances"),
    "excessive_rds_backup_retention": ("RDS instance keeping extra backups",
                                       "RDS instances keeping extra backups"),
    "lambda_zero_invocations": ("Lambda function never invoked", "Lambda functions never invoked"),
    "lambda_memory_overprovisioned": ("Lambda function with unused memory",
                                      "Lambda functions with unused memory"),
    "compute_optimizer_overprovisioned_lambda": ("Lambda function with unused memory",
                                                 "Lambda functions with unused memory"),
    "ecs_overprovisioned_cpu": ("ECS service with unused CPU", "ECS services with unused CPU"),
    "dynamodb_overprovisioned_capacity": ("DynamoDB table with unused capacity",
                                          "DynamoDB tables with unused capacity"),
    "ecr_old_untagged_images": ("ECR repo with old untagged images",
                                "ECR repos with old untagged images"),
    "s3_suboptimal_storage_class": ("S3 bucket in a costlier storage class",
                                    "S3 buckets in a costlier storage class"),
    "s3_incomplete_multipart_uploads": ("S3 bucket holding abandoned uploads",
                                        "S3 buckets holding abandoned uploads"),
    "log_group_infinite_retention": ("log group kept forever", "log groups kept forever"),
    "cloudtrail_data_events_enabled": ("CloudTrail trail logging data events",
                                       "CloudTrail trails logging data events"),
    "duplicate_cloudtrail_management_events": ("duplicate CloudTrail trail",
                                               "duplicate CloudTrail trails"),
    "cloudtrail_stopped_but_s3_bucket_costs_persist": ("stopped trail still storing logs",
                                                       "stopped trails still storing logs"),
    "data_transfer_cost": ("data transfer line", "data transfer lines"),
}


def _group_findings(findings: list[dict]) -> list[dict]:
    """Findings summed by waste_type, largest first. A group of one keeps the
    finding's own description when it has one (the demo's do)."""
    groups: dict[str, dict] = {}
    for f in findings:
        wt = f.get("waste_type") or "finding"
        g = groups.setdefault(wt, {"n": 0, "monthly": 0.0, "regions": set(),
                                   "description": f.get("description")})
        g["n"] += 1
        g["monthly"] += float(f.get("estimated_monthly_savings") or 0)
        if f.get("region"):
            g["regions"].add(f["region"])
    rows = []
    for wt, g in groups.items():
        if g["n"] == 1 and g["description"]:
            desc = g["description"]
        else:
            one, many = _WASTE_LABELS.get(wt, (wt.replace("_", " "), wt.replace("_", " ")))
            desc = f"{g['n']} {one if g['n'] == 1 else many}"
        regions = sorted(g["regions"])
        where = regions[0] if len(regions) == 1 else (f"{len(regions)} regions" if regions else "")
        rows.append({"description": desc, "region": where, "monthly": g["monthly"], "n": g["n"]})
    rows.sort(key=lambda r: -r["monthly"])
    return rows


_DENIED_CODES = ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation")


def _failed_check_lines(report: dict) -> list[str]:
    """What could not be read, in two kinds: checks that never ran anywhere,
    and checks that ran but left something unread (one region denied, the AMI
    list behind the snapshot filter, a NAT gateway's traffic metric). Both
    mean the findings above can be missing something, so both are said."""
    failed = report.get("checks_failed") or []
    if not failed:
        return []
    ran = set(report.get("checks_run") or ())
    not_run = sorted({f.get("check", "?") for f in failed
                      if not f.get("partial")} - ran)
    partly: dict[str, list[str]] = {}
    for f in failed:
        check = f.get("check", "?")
        if check in not_run:
            continue
        code = f.get("error_code", "")
        if f.get("partial"):
            n, unit = f.get("count", 0), f.get("unit", "resources")
            if n == 1 and unit.endswith("s"):
                unit = unit[:-1]
            what = f"{n} {unit} unread, {code}"
        else:
            what = f"{code} in {f.get('region', '?')}"
        bits = partly.setdefault(check, [])
        if what not in bits:
            bits.append(what)
    lines = []
    hint = ""
    if any(f.get("error_code") in _DENIED_CODES for f in failed):
        hint = " (`nable scan --dry-run --json` prints the policy)"
    if not_run:
        lines.append(f"{len(not_run)} check(s) could not run and were not counted: "
                     f"{', '.join(not_run)}{hint}")
        hint = ""
    if partly:
        detail = "; ".join(f"{c} ({', '.join(b[:3])}{', ...' if len(b) > 3 else ''})"
                           for c, b in sorted(partly.items()))
        lines.append(f"{len(partly)} check(s) could not fully run and may be missing "
                     f"findings: {detail}{hint}")
    return lines


def _render(out, spend, report, *, demo: bool, ce_denied: bool, extra_blocks=None,
            spend_requested: bool = False, spend_note: str | None = None):
    extra_blocks = extra_blocks or []
    demo_tag = _dim(" (demo data)") if demo else ""
    print("─" * 60, file=out)

    total_spend = 0.0
    total_recoverable = 0.0
    providers_ok = 0
    _has_aws = report is not None

    if _has_aws:
        providers_ok += 1
        recoverable = float(report.get("total_estimated_monthly_savings") or 0.0)
        total_recoverable += recoverable

        if spend and spend["total"] > 0:
            total_spend += spend["total"]
            top = " · ".join(f"{name} {_short_usd(v)}" for name, v in spend["services"])
            # "this month" is a lie on the 1st, when the snapshot falls back to the
            # month that just closed. Say which window the number covers.
            when = "last month" if spend.get("covers") == "last month" else "this month"
            print(
                _bold(f"{_usd(spend['total'])} on AWS {when}.") + f" Top: {top}{demo_tag}",
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
                        "`nable scan --dry-run --spend --json` prints the policy with it)"
                    ),
                    file=out,
                )
            elif spend is not None and not spend.get("has_data", True):
                print(_dim(f"Cost Explorer returned no data for this period "
                           f"({spend.get('period', '')})"), file=out)
            elif spend_note:
                print(_dim(spend_note), file=out)
            if recoverable >= _FINDING_FLOOR_USD:
                print(_green(_bold(f"{_usd(recoverable)}/mo recoverable")) + demo_tag, file=out)

        findings = report.get("findings") or []
        groups = _group_findings(findings)
        shown = [g for g in groups if g["monthly"] >= _FINDING_FLOOR_USD][:_MAX_FINDINGS_SHOWN]
        incomplete = bool(report.get("checks_failed") or report.get("regions_timed_out")
                          or report.get("regions_unlisted"))

        if recoverable < _FINDING_FLOOR_USD:
            if findings:
                # Below the line this summary shows, but not nothing: a $20/mo
                # account with $4.61/mo of findings used to read "no material
                # waste found, nice" and hide all of them.
                n = len(findings)
                print(f"{n} small finding{'s' if n != 1 else ''}, {_usd(recoverable)}/mo total"
                      + demo_tag + _dim(" · `nable scan --json` lists every one"), file=out)
            elif incomplete:
                # Nothing found is only a verdict on what was read.
                print("no waste found in what could be read" + demo_tag, file=out)
            else:
                # The proud state: a clean account is a result, not an apology.
                print(_green("no material waste found, nice") + demo_tag, file=out)
        else:
            for g in shown:
                region = g["region"]
                print(f"  {_usd(g['monthly']) + '/mo':>12}  {g['description']}"
                      + (f", {region}" if region else ""), file=out)
            rest_n = sum(g["n"] for g in groups) - sum(g["n"] for g in shown)
            rest_usd = sum(g["monthly"] for g in groups) - sum(g["monthly"] for g in shown)
            if rest_n > 0:
                # Without this the lines above never sum to the headline.
                print(_dim(f"  {_usd(rest_usd) + '/mo':>12}  {rest_n} more finding"
                           f"{'s' if rest_n != 1 else ''} · `nable scan --json` lists every one"),
                      file=out)

        if report.get("regions_unlisted"):
            print(_dim(f"could not list this account's regions (ec2:DescribeRegions: "
                       f"{report['regions_unlisted']}); scanned "
                       f"{', '.join(report.get('regions_scanned') or [])} only"), file=out)
        timed_out = report.get("regions_timed_out") or []
        if timed_out:
            done = len(report.get("regions_scanned") or [])
            print(
                _dim(f"scanned {done} of {done + len(timed_out)} regions "
                     f"(reached the {_SCAN_DEADLINE_S}s time limit; skipped: {', '.join(timed_out)})"),
                file=out,
            )
        for line in _failed_check_lines(report):
            # Without this line "no material waste found" reads as a verdict on
            # checks that never ran.
            print(_dim(line), file=out)

    # ── extra providers (AI / GCP / Azure), the cross-provider frame ──
    for b in extra_blocks:
        _render_extra(out, b)
        if b.status == "ok":
            providers_ok += 1
            if b.spend_usd:
                total_spend += b.spend_usd
            if b.recoverable_usd:
                total_recoverable += b.recoverable_usd

    # Unified summary only when the scan spans more than the AWS block, so an
    # AWS-only run stays byte-identical to v1.
    if extra_blocks:
        # Dedup cloud-native AI (Bedrock/Vertex): under --spend it is counted in
        # BOTH the AI block and the cloud spend total, so subtract it from the
        # grand total once. On the default path the AI block excludes it, so this
        # is 0 and the total is unchanged.
        cloud_native_ai = sum(
            amt
            for b in extra_blocks if b.family == "ai"
            for prov, amt in b.by_provider.items() if prov in ("bedrock", "vertex")
        )
        if cloud_native_ai and total_spend > cloud_native_ai:
            total_spend -= cloud_native_ai

        print("─" * 60, file=out)
        parts = []
        if total_spend > 0:
            parts.append(f"{_usd(total_spend)}/mo visible")
        parts.append(_green(f"{_usd(total_recoverable)}/mo recoverable"))
        print(
            _bold(" · ".join(parts))
            + _dim(f"  across {providers_ok} provider{'s' if providers_ok != 1 else ''}"),
            file=out,
        )

    if _has_aws and not spend_requested and not (spend and spend.get("total")):
        print(_dim("run `nable scan --spend` for the spend breakdown (uses Cost Explorer, ~$0.02)"), file=out)

    # A scan that worked still deserves to know it is running an old build, but
    # quietly: this is dim, one line, below the result. On the failure path the
    # same note is bold and above the docs line, because there it is often the
    # answer rather than a footnote. Demo mode never reaches here with a real
    # scan, so a `--demo` run is not nagged.
    if not demo:
        stale = _staleness_line()
        if stale:
            print(_dim("  " + stale), file=out)

    print(_dim(DOCS_LINE), file=out)


def _json_payload(spend, report, *, demo, profile, account_id, duration_s, extra_blocks=None,
                  credentials: str = "profile"):
    extra_blocks = extra_blocks or []
    report = report or {}
    recoverable = float(report.get("total_estimated_monthly_savings") or 0.0)
    findings = report.get("findings") or []
    spend_has_data = bool(spend) and spend.get("has_data", True)
    return {
        "schema_version": 1,
        "command": "scan",
        "demo": demo,
        # None when the keys came from the environment: they belong to no profile.
        "profile": profile if credentials == "profile" else None,
        "credentials": credentials,
        "account_id": account_id,
        "spend": (
            {
                "period": spend["period"],
                # On the 1st this is last month's closed total, not month-to-date;
                # `covers` says which, so a consumer never has to guess.
                "covers": spend.get("covers", "month-to-date"),
                # null, not 0, when Cost Explorer returned nothing: no data is
                # not a $0 bill.
                "has_data": spend_has_data,
                "month_to_date_usd": round(spend["total"], 2) if spend_has_data else None,
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
        # Every finding. This was capped at 20 while the text output promised
        # `nable scan --json` "lists every one", and the listed ones then did
        # not add up to recoverable.monthly_usd.
        "total_findings": len(findings),
        "unpriced_findings": sum(
            1 for f in findings if f.get("estimated_monthly_savings") is None),
        "findings": findings,
        "scan": {
            "regions_scanned": report.get("regions_scanned", []),
            "regions_timed_out": report.get("regions_timed_out", []),
            "regions_unlisted": report.get("regions_unlisted"),
            "errors": report.get("errors", []),
            "checks_run": report.get("checks_run"),
            "checks_failed": report.get("checks_failed", []),
            "duration_s": round(duration_s, 1),
            # A check that could not read is as partial as a region that timed
            # out: the findings list is missing whatever it would have found.
            "partial": bool(report.get("regions_timed_out") or report.get("checks_failed")
                            or report.get("regions_unlisted")),
        },
        "providers": [
            {
                "family": b.family,
                "status": b.status,
                "spend_usd": round(b.spend_usd, 2) if b.spend_usd is not None else None,
                "recoverable_usd": round(b.recoverable_usd, 2) if b.recoverable_usd is not None else None,
                "estimated": b.estimated,
                "note": b.note,
            }
            for b in extra_blocks
        ],
    }


# ── the command ────────────────────────────────────────────────────────────────

def _crash_site(exc: BaseException) -> str:
    """Where inside nable an unexpected exception was raised, as
    `analyzers/optimizer.py:612`. Relative to the package, so it carries no
    home directory or username, and with the version it pins the statement."""
    tb = exc.__traceback__
    site = ""
    while tb is not None:
        fname = tb.tb_frame.f_code.co_filename.replace("\\", "/")
        if "/finops/" in fname:
            site = f"{fname.rsplit('/finops/', 1)[1]}:{tb.tb_lineno}"
        tb = tb.tb_next
    return site


def main(args) -> int:
    """`nable scan` as the CLI runs it. run() gives every failure it anticipates
    its own exit code and fix line; this is for the ones it does not. Without it
    an unexpected exception reached the user as a Python traceback, which the
    module promises never happens, and the run sent cli_scan_started with no
    terminal event, so it could not be counted as a failure at all."""
    t0 = time.time()
    try:
        return run(args, t0)
    except KeyboardInterrupt:
        # Ctrl-C used to print a KeyboardInterrupt traceback and then sit for
        # 6 to 9 seconds while interpreter shutdown joined region workers still
        # blocked in boto3 calls. The pools are already cancelled on the way
        # out (their finally blocks); anything still running is abandoned.
        as_json = bool(getattr(args, "json", False))
        print("\nscan cancelled", file=sys.stderr if as_json else sys.stdout)
        if as_json:
            print(json.dumps({"error": {"class": "cancelled", "exit_code": EXIT_CANCELLED,
                                        "message": "scan cancelled"}}))
        return _finish(EXIT_CANCELLED, _threads_lingering())
    except Exception as exc:
        if getattr(args, "debug", False):
            raise
        out = sys.stderr if getattr(args, "json", False) else sys.stdout
        return _fail(out, 1, [
            f"nable scan stopped on an unexpected error ({type(exc).__name__})",
            "  this is most likely a bug in nable rather than your AWS setup",
            "  `nable scan --debug` prints the full trace; please include it in a report:",
            "  https://github.com/getnable/finopsmcp/issues/new",
        ], "crash", t0, exc=exc, props={"crash_site": _crash_site(exc)})


def run(args, t0: float | None = None) -> int:
    global _json_mode
    t0 = time.time() if t0 is None else t0
    as_json = bool(getattr(args, "json", False))
    _json_mode = as_json

    # --dry-run answers "what will this touch?" before anything is touched, and
    # returns before credentials are read, before any client is built, and before
    # a single network call. Asked for by a security-minded reader who could not
    # evaluate the tool without it, which is the correct reason to want it.
    if getattr(args, "dry_run", False):
        from .scan_manifest import iam_policy, render_dry_run
        want = bool(getattr(args, "spend", False))
        if as_json:
            print(json.dumps({
                "dry_run": True,
                "executed": False,
                "iam_policy": iam_policy(want),
                "cost_explorer_called": want,
            }, indent=2))
        else:
            print(render_dry_run(want))
        return EXIT_OK

    demo = bool(getattr(args, "demo", False)) or os.getenv("FINOPS_DEMO") == "1"
    want_spend = bool(getattr(args, "spend", False))
    # Where the profile name came from, read BEFORE --profile is exported:
    # the missing-profile message used to read AWS_PROFILE back after setting
    # it, and told people who typed --profile that their environment set it.
    flag_profile = getattr(args, "profile", None)
    env_profile = os.environ.get("AWS_PROFILE") or os.environ.get("AWS_DEFAULT_PROFILE")
    profile = flag_profile or env_profile or "default"
    # Env-var keys outrank any profile botocore could pick, unless a profile
    # is passed to the session itself, which --profile now does.
    env_keys = bool(os.environ.get("AWS_ACCESS_KEY_ID")) and not flag_profile and not demo
    cred_label = _ENV_KEYS_LABEL if env_keys else f"profile {profile}"
    if flag_profile:
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
    print(f"{_bold('nable scan')} {_dim('· ' + cred_label)}", file=out)
    # version here too: without it, started and failed cannot be joined per
    # release, so "is the new build better" is unanswerable. 119 starts in
    # 14 days carried no version while every failure did.
    from . import __version__ as _sv
    _emit("cli_scan_started", {"demo": demo, "version": _sv}, wait=False)

    # Kick the staleness check off here, immediately after the first print, so
    # the PyPI round trip overlaps the AWS work instead of adding to it. The
    # exit paths join it briefly and drop the note if it has not answered.
    #
    # server.py has run this check since it was written; the CLI never has, and
    # the CLI is where a stale build hurts most. `nable scan` is the first thing
    # a new user runs, and on a pinned config it can be nine releases behind
    # without anything on screen saying so. update_check already handles the
    # hard parts (airgap, no-telemetry, memoisation, a 2s cap), so this is a
    # call, not a second implementation.
    def _check_staleness() -> None:
        global _stale_note
        try:
            from .update_check import staleness_note
            _stale_note = staleness_note()
        except Exception:
            _stale_note = None

    global _stale_thread
    _stale_thread = threading.Thread(target=_check_staleness, daemon=True)
    _stale_thread.start()

    if demo:
        demo_spend, report = _demo_payload()
        # Demo mirrors real behavior: the spend headline only appears with --spend.
        spend = demo_spend if want_spend else None
        from .scan_assembler import demo_extra_blocks
        extra = demo_extra_blocks(want_spend)
        print(_dim("account demo · StreamCo demo dataset (demo data)"), file=out)
        _render(out, spend, report, demo=True, ce_denied=False, extra_blocks=extra)
        if as_json:
            print(json.dumps(_json_payload(
                spend, report, demo=True, profile=profile, account_id="demo",
                duration_s=time.time() - t0, extra_blocks=extra,
            ), indent=2))
        _emit("cli_scan_completed", {
            "demo": True, "providers": len(extra) + 1,
            "duration_s": round(time.time() - t0, 1),
        }, wait=True)
        return EXIT_OK

    # ── pre-flight typed probes: these drive exit codes, never engine strings ──
    #
    # This used to be a bare `except ImportError` reporting "boto3 is not
    # installed". It caught EVERY ImportError raised anywhere inside boto3's own
    # import chain (a half-installed wheel, a broken transitive dep, an
    # architecture mismatch on the interpreter) and told all of them to reinstall
    # a package that was already there. It also passed no exception to _fail, so
    # exc_type arrived empty and the failures were undiagnosable: on 2026-08-05,
    # 14 failures across 6 machines on current versions all landed here with
    # nothing to go on. Separate "absent" from "present but will not import", and
    # always hand the exception over so the class name is recorded.
    try:
        import boto3  # noqa: F401
        import botocore.exceptions  # noqa: F401
    except BaseException as exc:            # noqa: BLE001 - a probe reports, never swallows
        import importlib.util

        try:
            installed = importlib.util.find_spec("boto3") is not None
        except BaseException:               # a broken meta-path finder counts as unknown
            installed = False

        # The import chain that can take boto3 down: a version skew anywhere in
        # it produces "cannot import name X". Reproduced 2026-08-06: boto3
        # 1.43.66 over a stale botocore 1.34.0 (a distro-owned copy pip will not
        # upgrade) dies exactly this way. Recording the four version strings
        # turns a remote "ImportError" into a named conflict. Versions only:
        # no paths, no messages.
        deps: dict[str, str] = {}
        try:
            from importlib.metadata import version as _pkg_version
            for _pkg in ("boto3", "botocore", "s3transfer", "urllib3"):
                try:
                    deps[f"{_pkg}_version"] = _pkg_version(_pkg)
                except Exception:
                    deps[f"{_pkg}_version"] = "absent"
        except Exception:
            pass

        if not installed:
            # boto3 is a hard dependency and the published wheel declares it, so
            # "absent" means this environment installed the package without its
            # dependencies. Name that, because "reinstall" alone sends people to
            # repeat the command that already skipped them.
            from .install_health import install_shape, missing_core_dependencies
            others = [d for d in missing_core_dependencies() if d not in ("boto3", "botocore")]
            # WHICH kind of install skipped the dependencies. Without this the
            # event says "boto3 absent" and stops, which is where 13 of these
            # left me on 2026-08-14: the cause was un-nameable from the data, so
            # nothing could be fixed. Categories only, never paths.
            deps.update({k: str(v) for k, v in install_shape().items()})
            lines = ["nable is installed but its dependencies are not."]
            if others:
                lines.append(f"  also missing: {', '.join(others)}")
            lines += [
                "  usually a `pip install --no-deps`, a pruned container layer, "
                "or a partial copy of site-packages",
                "  fix: `pip install --upgrade --force-reinstall finops-mcp`",
                "  or run isolated, no cleanup needed: `uvx --python 3.12 nable scan`",
            ]
            cls = "missing_dep"
        else:
            _b3 = deps.get("boto3_version", "?")
            _bc = deps.get("botocore_version", "?")
            lines = [
                f"boto3 is installed but will not import ({type(exc).__name__}).",
                f"  found: boto3 {_b3} with botocore {_bc}; a mismatched pair "
                "(often a system-owned copy pip cannot upgrade) fails exactly here",
                "  fix: `pip install --upgrade --force-reinstall boto3 botocore`",
                "  or run isolated, no cleanup needed: `uvx --python 3.12 nable scan`",
            ]
            cls = "broken_dep"
        return _fail(out, 1, lines, cls, t0, exc, props=deps)

    # Connection-aware: what else is configured besides AWS? Detection uses the
    # same connected_families() the MCP server reads, so connecting on either
    # surface is immediately visible to the other.
    from .tool_surface import connected_families
    try:
        _fams = connected_families()
    except Exception:
        _fams = frozenset()
    _extra_fams = _fams & {"llm", "gcp", "azure"}

    # No AWS credentials at all, but other providers connected: scan those instead
    # of failing. A pure AI-startup box may have OPENAI_API_KEY and no ~/.aws.
    try:
        _no_aws_creds = boto3.Session().get_credentials() is None
    except Exception:
        _no_aws_creds = False
    if _no_aws_creds and _extra_fams:
        from .scan_assembler import gather_extra_providers
        print(_dim("no AWS credentials found · scanning your other connected providers"), file=out)
        blocks, abandoned = gather_extra_providers(_fams, spend=want_spend)
        if not any(b.status in ("ok", "no_data") for b in blocks):
            # Nothing answered. The common case: a profile-based account in
            # accounts.yaml puts "aws" (and so "llm") in connected_families with
            # no usable credentials behind it, the AI block drops itself as a
            # false positive, and the scan used to exit 0 with no providers and
            # no findings: a clean result for an account nothing was read from.
            for b in blocks:
                _render_extra(out, b)
            code = _fail(out, EXIT_NO_CREDS, [
                "no AWS credentials found, and no other connected provider answered",
                "  looked in: env vars, ~/.aws/credentials, ~/.aws/config (SSO), instance metadata",
                "  fix: `aws configure sso` (company SSO) or `aws configure` (access key)",
                "  then: `nable connect` waits and connects the moment they appear",
            ], "no-creds", t0, props={"n_extra": len(blocks)})
            return _finish(code, abandoned)
        _render(out, None, None, demo=False, ce_denied=False, extra_blocks=blocks)
        if as_json:
            print(json.dumps(_json_payload(
                None, None, demo=False, profile=profile, account_id=None,
                duration_s=time.time() - t0, extra_blocks=blocks,
            ), indent=2))
        _emit("cli_scan_completed", {
            "demo": False, "no_aws": True,
            "providers": len([b for b in blocks if b.status == "ok"]),
            "duration_s": round(time.time() - t0, 1),
        }, wait=True)
        return _finish(EXIT_OK, abandoned)

    try:
        # --profile goes to the session itself: only then does botocore let it
        # outrank keys exported in the environment.
        session = boto3.Session(profile_name=flag_profile) if flag_profile else boto3.Session()
        if session.get_credentials() is None:
            return _fail(out, EXIT_NO_CREDS, [
                "no AWS credentials found on this machine",
                "  looked in: env vars, ~/.aws/credentials, ~/.aws/config (SSO), instance metadata",
                "  fix: `aws configure sso` (company SSO) or `aws configure` (access key)",
                "  then: `nable connect` waits and connects the moment they appear",
                # Someone with no credentials is often deciding whether to grant
                # any, not failing to use the tool. Point them at the manifest
                # rather than only at how to hand over access.
                "  evaluating first? `nable scan --dry-run` lists every API call "
                "and permission a scan needs, without running one",
            ], "no-creds", t0)
        sts = session.client("sts")
        ident = sts.get_caller_identity()
        account_id = ident["Account"]
    except Exception as exc:
        klass = _classify_boto_error(exc)
        if klass == "expired":
            return _fail(out, EXIT_EXPIRED, [
                "your AWS session has expired",
                ("  fix: refresh AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY and AWS_SESSION_TOKEN "
                 "in your environment, or unset them to use your AWS profiles") if env_keys else
                f"  fix: `aws sso login --profile {profile}`  (or refresh your temporary credentials)",
            ], "expired", t0, exc=exc)
        if klass == "no-creds":
            return _fail(out, EXIT_NO_CREDS, [
                "no usable AWS credentials found",
                "  fix: `aws configure sso` (company SSO) or `aws configure` (access key)",
                "  then: `nable connect` waits and connects the moment they appear",
            ], "no-creds", t0, exc=exc)
        if klass == "denied":
            return _fail(out, EXIT_DENIED, [
                "this AWS identity cannot call sts:GetCallerIdentity",
                "  fix: `nable scan --dry-run --json` prints the exact least-privilege",
                "       policy for the calls this scan makes, ready to paste",
            ], "permission", t0)
        if klass == "profile-missing":
            # The single most common instant failure: AWS_PROFILE is exported in
            # the user's shell (normal for anyone with more than one account) and
            # does not resolve. This used to print a raw botocore string with no
            # fix line at all, which is why people retried and left.
            found = _available_profiles()
            lines = [f"AWS profile {profile!r} is not configured on this machine"]
            if flag_profile:
                lines.append(f"  you asked for it with --profile {flag_profile}")
            elif env_profile == profile:
                lines.append(f"  AWS_PROFILE={env_profile} is set in your environment")
            if found:
                lines.append(f"  profiles nable can see: {', '.join(found)}")
                lines.append(f"  fix: `nable scan --profile {found[0]}`"
                             + ("" if flag_profile else ", or unset AWS_PROFILE"))
            else:
                lines.append("  nable cannot see any configured profiles")
                lines.append("  fix: `aws configure sso` (company SSO) or `aws configure` (access key)")
            return _fail(out, EXIT_CONFIG, lines, "profile-missing", t0)
        if klass == "config-broken":
            return _fail(out, EXIT_CONFIG, [
                "your AWS config could not be parsed",
                f"  {exc}",
                "  fix: open that file and check for an unclosed [section] header or a stray line",
            ], "config-broken", t0, exc=exc)
        if klass == "no-region":
            return _fail(out, EXIT_CONFIG, [
                "no AWS region is configured",
                "  fix: `export AWS_DEFAULT_REGION=us-east-1` (or set `region` in ~/.aws/config)",
            ], "no-region", t0, exc=exc)
        if klass == "bad-creds":
            if env_keys:
                return _fail(out, EXIT_NO_CREDS, [
                    "AWS rejected these credentials",
                    (f"  {_ENV_KEYS_LABEL}: the access key is unknown, revoked, "
                     "or the secret does not match"),
                    _ENV_KEYS_FIX,
                ], "bad-creds", t0, exc=exc)
            return _fail(out, EXIT_NO_CREDS, [
                "AWS rejected these credentials",
                f"  profile {profile!r}: the access key is unknown, revoked, or the secret does not match",
                "  fix: `aws sts get-caller-identity` to confirm, then `aws configure` to replace them",
            ], "bad-creds", t0, exc=exc)
        if klass == "network":
            return _fail(out, 1, [
                "cannot reach AWS from this machine",
                f"  {type(exc).__name__}: the request never got an answer",
                "  fix: check VPN / proxy. Behind a corporate proxy, set HTTPS_PROXY;",
                "  with TLS interception, point AWS_CA_BUNDLE at your company root cert",
            ], "network", t0, exc=exc)
        # Genuinely unclassified. Keep the engine string so it is at least
        # reportable, and say what to do with it.
        return _fail(out, 1, [
            f"could not reach AWS: {exc}",
            "  fix: `nable scan --debug` prints the full traceback",
            "  if that does not explain it, please open an issue with the output",
        ], "other", t0, exc=exc)

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
    spend_note = None
    if want_spend:
        if "llm" in _extra_fams:
            # The AI block reads Bedrock spend through Cost Explorer too under
            # --spend: one call to find the Bedrock services, one for their
            # detail. Saying "2 calls" while making 4 is the one place a
            # disclosure must not round down.
            print(_dim("spend breakdown: 2 Cost Explorer calls, plus up to 2 for Bedrock "
                       "AI spend, about $0.02 to $0.04 on your AWS bill"), file=out)
        else:
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
                ], "expired", t0, exc=exc)
            else:
                # Any other Cost Explorer failure: carry on without the spend
                # headline, but say so rather than printing nothing.
                from .analyzers.waste import error_code
                spend_note = (f"Cost Explorer could not be read ({error_code(exc)}); "
                              "no spend breakdown this run")

    override = _split_regions(getattr(args, "regions", None))
    regions_unlisted = None
    if override:
        bad = [r for r in override if not _REGION_RE.match(r)]
        if bad:
            return _fail(out, 1, [
                f"not valid region name(s): {', '.join(bad)}",
                "  fix: region codes, not names, e.g. `nable scan --regions us-east-1 eu-west-1`",
            ], "bad-region-arg", t0, props={"n_bad": len(bad)})
        # Shaped like a region is not a region: `eu-west-9` passed the pattern,
        # 14 of 16 checks then failed on an endpoint that does not exist, and
        # the scan still printed "nice" and exited 0. Ask AWS which exist.
        try:
            catalog = _region_catalog(session)
        except Exception as exc:  # noqa: BLE001 - unvalidated, and the line below says so
            from .analyzers.waste import error_code
            catalog = None
            print(_dim(f"could not check region names (ec2:DescribeRegions: "
                       f"{error_code(exc)}); scanning them as given"), file=out)
        if catalog:
            unknown = [r for r in override if r not in catalog]
            disabled = [r for r in override if catalog.get(r) == "not-opted-in"]
            if unknown or disabled:
                enabled = sorted(r for r, s in catalog.items() if s != "not-opted-in")
                lines = []
                if unknown:
                    lines.append(f"not an AWS region: {', '.join(unknown)}")
                if disabled:
                    lines.append(f"not enabled for this account: {', '.join(disabled)} "
                                 "(an opt-in region)")
                lines.append(f"  regions this account can scan: {', '.join(enabled)}")
                return _fail(out, 1, lines, "bad-region-arg", t0,
                             props={"n_bad": len(unknown) + len(disabled)})
        regions = override
    else:
        regions = _pick_regions(spend, session)
        regions_unlisted = getattr(regions, "error_code", None)
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
        regions=list(regions),
        progress_callback=_progress,
        deadline_seconds=_SCAN_DEADLINE_S,
        # Only for --profile: without it the audit keeps its own session, which
        # honours AWS_ROLE_ARNS exactly as before.
        session=session if flag_profile else None,
    )
    if regions_unlisted and isinstance(report, dict) and not report.get("error"):
        report["regions_unlisted"] = regions_unlisted

    if report.get("error"):
        # Send the TYPE, never the message: the message interpolates the
        # exception and carries paths and account ids. Without this the event
        # said error_class="other", exc_type="" and named no cause at all.
        return _fail(out, 1, [f"scan failed: {report['error']}"], "other", t0,
                     props={"exc_type": report.get("error_type", "") or "unknown"})

    scanned = report.get("regions_scanned") or []
    has_results = bool(scanned)
    lingering = bool(report.get("_threads_abandoned"))

    # Every check raised (typically AccessDenied on every read). Regions were
    # "scanned" only in the sense that we asked; nothing was read, so zero
    # findings here is not a clean account and must not exit 0 as one.
    checks_failed = report.get("checks_failed") or []
    if has_results and checks_failed and not report.get("checks_run"):
        codes = sorted({f.get("error_code", "") for f in checks_failed})
        denied = all(c in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation")
                     for c in codes)
        # Same rule as the time-limit branch below: AWS reading nothing is one
        # row, not the whole scan, when another provider did answer.
        extra_blocks, extra_abandoned = ([], False)
        if _extra_fams:
            from .scan_assembler import gather_extra_providers
            extra_blocks, extra_abandoned = gather_extra_providers(_fams, spend=want_spend)
        lingering = lingering or extra_abandoned
        if any(b.status == "ok" for b in extra_blocks):
            print(_dim(f"AWS: every check failed ({', '.join(codes)}); "
                       "showing your other providers"), file=out)
            _render(out, None, None, demo=False, ce_denied=False, extra_blocks=extra_blocks)
            if as_json:
                # The report rides along for scan.errors and scan.partial; its
                # findings are empty because nothing was read.
                print(json.dumps(_json_payload(
                    None, report, demo=False, profile=profile, account_id=account_id,
                    duration_s=time.time() - t0, extra_blocks=extra_blocks,
                ), indent=2))
            _emit("cli_scan_completed", {
                "demo": False, "aws_all_checks_failed": True,
                "providers": len([b for b in extra_blocks if b.status == "ok"]),
                "duration_s": round(time.time() - t0, 1),
            }, wait=True)
            return _finish(EXIT_OK, lingering)
        if as_json:
            print(json.dumps(_json_payload(
                spend, report, demo=False, profile=profile, account_id=account_id,
                duration_s=time.time() - t0,
            ), indent=2))
        code = _fail(out, EXIT_DENIED if denied else EXIT_PARTIAL_EMPTY, [
            f"every check failed ({', '.join(codes)}); nothing in this account was read",
            "  fix: `nable scan --dry-run --json` prints the exact least-privilege",
            "       policy for the calls this scan makes, ready to paste",
        ], "permission" if denied else "all-checks-failed", t0,
            props={"n_failed": len(checks_failed)},
            json_error=False)  # the result document above already says it
        return _finish(code, lingering)

    if not has_results:
        # AWS produced nothing (hit the time limit). Don't blank the whole cross-provider
        # frame: if other providers are connected, still gather and show them, with
        # AWS degraded to a note. AWS failing is one row, not the whole scan.
        extra_blocks, extra_abandoned = ([], False)
        if _extra_fams:
            from .scan_assembler import gather_extra_providers
            extra_blocks, extra_abandoned = gather_extra_providers(_fams, spend=want_spend)
        lingering = lingering or extra_abandoned
        if any(b.status == "ok" for b in extra_blocks):
            print(_dim("AWS: hit the 45s time limit, no regions finished; showing your other providers"), file=out)
            _render(out, None, None, demo=False, ce_denied=False, extra_blocks=extra_blocks)
            if as_json:
                print(json.dumps(_json_payload(
                    None, None, demo=False, profile=profile, account_id=account_id,
                    duration_s=time.time() - t0, extra_blocks=extra_blocks,
                ), indent=2))
            _emit("cli_scan_completed", {
                "demo": False, "aws_timeout": True,
                "providers": len([b for b in extra_blocks if b.status == "ok"]),
                "duration_s": round(time.time() - t0, 1),
            }, wait=True)
            return _finish(EXIT_OK, lingering)
        # truly nothing usable anywhere
        code = _fail(out, EXIT_PARTIAL_EMPTY, [
            "the scan hit its 45s time limit before any region finished",
            "  try a narrower run: `nable scan --regions us-east-1`",
        ], "timeout", t0)
        return _finish(code, lingering)

    # Cross-provider frame: gather AI/GCP/Azure alongside the AWS block. Each has
    # its own timeout and degrades to a note; free-by-default holds (only --spend
    # touches Cost Explorer / the BigQuery export / cloud-native AI).
    extra_blocks, extra_abandoned = ([], False)
    if _extra_fams:
        from .scan_assembler import gather_extra_providers
        extra_blocks, extra_abandoned = gather_extra_providers(_fams, spend=want_spend)
    lingering = lingering or extra_abandoned

    _render(out, spend, report, demo=False, ce_denied=ce_denied, extra_blocks=extra_blocks,
            spend_requested=want_spend, spend_note=spend_note)
    if as_json:
        print(json.dumps(_json_payload(
            spend, report, demo=False, profile=profile, account_id=account_id,
            duration_s=time.time() - t0, extra_blocks=extra_blocks,
            credentials="environment" if env_keys else "profile",
        ), indent=2))

    # Most checks could not run: a nonexistent region, or a policy missing most
    # of the scan. What little came back cannot stand in for the account, so
    # this is partial with no usable result, not a success with a banner.
    ran = set(report.get("checks_run") or ())
    not_run = {f.get("check") for f in checks_failed if not f.get("partial")} - ran
    if not_run and len(not_run) > len(ran):
        code = _fail(out, EXIT_PARTIAL_EMPTY, [
            (f"{len(not_run)} of {len(not_run) + len(ran)} checks could not run; "
             "these results do not cover the account"),
            ("  fix: `nable scan --dry-run --json` prints the policy a scan needs, "
             "and `--regions` takes regions this account has enabled"),
        ], "most-checks-failed", t0, props={"n_failed": len(not_run)}, json_error=False,
            docs_line=False)
        return _finish(code, lingering)

    _emit("cli_scan_completed", {
        "demo": False,
        "spend": want_spend,
        "duration_s": round(time.time() - t0, 1),
        "partial": bool(report.get("regions_timed_out") or checks_failed),
        "ce_denied": ce_denied,
    }, wait=True)
    return _finish(EXIT_OK, lingering)


def add_parser(sub) -> None:
    """Register the scan subcommand on the wizard's argparse tree."""
    p = sub.add_parser(
        "scan",
        help="Find spend and recoverable waste across your connected cloud and AI providers, free",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output on stdout")
    p.add_argument("--demo", action="store_true", help="run on the StreamCo sample dataset")
    p.add_argument(
        "--spend", action="store_true",
        help="add a month-to-date spend breakdown (uses Cost Explorer, ~$0.02 on your AWS bill)",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="list every API call and IAM permission a scan needs, and exit")
    p.add_argument("--debug", action="store_true", help="full tracebacks and per-check timing")
    p.add_argument("--profile", help="AWS profile to use (default: $AWS_PROFILE or 'default')")
    p.add_argument(
        "--regions", nargs="+", action="extend", metavar="REGION",
        help="scan exactly these regions instead of the auto-discovered set "
             "(space or comma separated; repeat the flag to add more)",
    )
