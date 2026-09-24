"""Seamless agent cost guardrail: a PreToolUse hook for AI coding agents.

`finops guard install` wires nable's advisory policy gate (policy.py) into
Claude Code so it runs automatically whenever an agent is about to execute an
infrastructure-mutating shell command (terraform destroy, kubectl delete,
aws ec2 terminate-instances, a commitment purchase, ...) or make the same
change through an MCP tool (HashiCorp Terraform, AWS API, Kubernetes servers;
the table is guard_mcp.py). The agent no longer has to remember to call
check_action_policy; the harness enforces the check.

Public entry points, for any harness adapter (guard_adapters.py for Cursor and
Codex, run_hook below for Claude Code):
  gate_command(command, *, harness)                 a shell command
  gate_mcp_call(tool_name, arguments, *, harness)   an MCP tool call
Both return None (no opinion: stay silent) or a verdict dict whose
"decision" is "ask", "deny" or "warn"; see gate_command for the full shape.

Verdict mapping (advisory, propose-only stays intact):
  escalate -> "ask"   the human sees the command plus the policy reason
  block    -> "deny"  the agent is told why and proposes something else
  allow    -> silent  zero friction, the command runs as normal
  allow, but priced near the auto threshold
           -> "warn"  runs as normal, with the figure shown alongside

History (the recent end of the decision ledger, guard_ledger.recent) can
turn an allow or a warn into an ask, never anything else:
  velocity cap   the priced monthly run-rate the guard let through in a
                 rolling window (60 min; 4x the per-action threshold unless
                 FINOPS_POLICY_VELOCITY_CAP_USD says otherwise), plus this
                 action, is over the cap

The hook never executes anything itself and it fails open: any internal error
exits 0 so a guard bug can never break the user's agent.

Strict mode (FINOPS_GUARD_STRICT=1) additionally asks on reversible
mutations (terraform apply, helm upgrade, kubectl apply/scale,
aws ec2 run-instances) with a nudge to cost the change first.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .policy import (
    GATE_ALLOW,
    GATE_BLOCK,
    GATE_ESCALATE,
    evaluate_action_gate,
    load_policy,
    velocity_cap,
)

# ── Command classification ─────────────────────────────────────────────────────
# Ordered: first match wins. Maps shell commands to the policy action types in
# policy.py. Over-matching is tolerable (worst case an unnecessary confirm);
# missing a one-way door is not, so patterns are deliberately broad.

_ONE_WAY_CLASSIFIERS: list[tuple[str, str]] = [
    (r"\bterraform\s+(?:\S+\s+)*destroy\b", "delete_resource"),
    (r"\btofu\s+(?:\S+\s+)*destroy\b", "delete_resource"),
    # Terragrunt wraps terraform and fans out: `terragrunt run-all destroy` (or
    # `run --all destroy`, or the older `destroy-all`) tears down every module
    # under the directory in one command. It had no pattern at all, so the
    # widest destroy in the toolchain was the one the guard could not see.
    (r"\bterragrunt\s+(?:\S+\s+)*destroy\b", "delete_resource"),
    # destroy hidden behind the apply verb: `terraform apply -destroy` is destroy.
    # Must sit in the one-way list (checked first) or the two-way apply pattern
    # would classify it as a reversible mutation.
    (r"\b(?:terraform|tofu|terragrunt)\s+(?:\S+\s+)*apply\b[^|;&]*\s-destroy\b",
     "delete_resource"),
    # The same flag passed through terraform's own env hook:
    # `TF_CLI_ARGS_apply=-destroy terraform apply` is a destroy the apply
    # pattern below would otherwise wave through as a reversible mutation.
    (r"\bTF_CLI_ARGS(?:_\w+)?=\S*-destroy\b", "delete_resource"),
    (r"\bpulumi\s+(?:\S+\s+)*destroy\b", "delete_resource"),
    (r"\beksctl\s+delete\b", "delete_resource"),
    # bucket/object wipes: `aws s3 rb` removes a bucket, `aws s3 rm --recursive`
    # empties one; gsutil is the GCP equivalent. Data deletion is a one-way door.
    (r"\baws\s+s3\s+r[mb]\b", "delete_resource"),
    (r"\bgsutil\s+(?:-\S+\s+)*r[mb]\b", "delete_resource"),
    (r"\bhelm\s+(?:uninstall|delete)\b", "delete_resource"),
    (r"\bkubectl\s+(?:\S+\s+)*delete\b", "delete_resource"),
    (r"\baws\s+ec2\s+terminate-instances\b", "terminate_instance"),
    (r"\baws\s+ec2\s+release-address\b", "release_ip"),
    (r"\baws\s+ec2\s+delete-snapshot\b", "snapshot_delete"),
    (r"\baws\s+(?:savingsplans\s+create-savings-plan|"
     r"ec2\s+purchase-reserved-instances-offering|"
     r"ec2\s+purchase-host-reservation|"
     r"rds\s+purchase-reserved-db-instances-offering)", "purchase_commitment"),
    (r"\baws\s+\S+\s+delete-[a-z0-9-]+", "delete_resource"),
    (r"\bgcloud\s+(?:\S+\s+)*delete\b", "delete_resource"),
    (r"\baz\s+(?:\S+\s+)*delete\b", "delete_resource"),
]

_TWO_WAY_CLASSIFIERS: list[tuple[str, str]] = [
    (r"\baws\s+ec2\s+stop-instances\b", "stop_idle"),
    (r"\bterraform\s+(?:\S+\s+)*apply\b", "infra_apply"),
    (r"\btofu\s+(?:\S+\s+)*apply\b", "infra_apply"),
    (r"\bterragrunt\s+(?:\S+\s+)*apply\b", "infra_apply"),
    (r"\bhelm\s+(?:install|upgrade)\b", "infra_apply"),
    (r"\bkubectl\s+(?:apply|scale)\b", "infra_apply"),
    (r"\baws\s+ec2\s+run-instances\b", "infra_apply"),
    # Launches the pricers below can put a figure on. Unclassified, they could
    # never reach the policy's dollar threshold however large they were.
    (r"\baws\s+rds\s+create-db-instance\b", "infra_apply"),
    (r"\bgcloud\s+(?:\S+\s+)*compute\s+instances\s+create\b", "infra_apply"),
    (r"\baz\s+(?:\S+\s+)*vm\s+create\b", "infra_apply"),
]


# AWS global options sit between `aws` and the service name, so
# `aws --profile prod ec2 terminate-instances` does not match a pattern anchored
# on `aws\s+ec2`. Every aws entry in the tables above was anchored that way,
# while the gcloud, az and kubectl entries already allowed intervening tokens.
# The result: adding --profile, --region, --output, --no-cli-pager or an
# --endpoint-url to a terminate, a bucket wipe or a commitment purchase made it
# invisible to the guard, and the hook stayed silent on a one-way door. A
# profile flag is not an exotic input; it is what anyone with more than one
# account types by default.
#
# Rather than widen eight patterns (and every future one) this strips the global
# options first, so the tables stay readable and a new aws rule cannot forget.
#
# Matched from an explicit list rather than "any token": `(?:\S+\s+)*` would also
# swallow a service name, so `aws s3 ls` could be read as a later verb's
# preamble. The value-taking and boolean forms are separated because
# `--profile prod` consumes the next token and `--no-cli-pager` does not;
# treating them alike either eats the service name or leaves a stray value.
_AWS_GLOBAL_WITH_VALUE = (
    "endpoint-url|output|query|profile|region|color|ca-bundle|cli-read-timeout|"
    "cli-connect-timeout|cli-binary-format"
)
_AWS_GLOBAL_BOOLEAN = (
    "debug|no-verify-ssl|no-paginate|no-sign-request|no-cli-pager|"
    "cli-auto-prompt|no-cli-auto-prompt"
)
_AWS_GLOBAL_OPTS_RE = re.compile(
    r"\b(aws)\s+(?:"
    rf"(?:--(?:{_AWS_GLOBAL_WITH_VALUE})(?:=\S+|\s+\S+))"
    rf"|(?:--(?:{_AWS_GLOBAL_BOOLEAN}))"
    r")(?:\s+(?:"
    rf"(?:--(?:{_AWS_GLOBAL_WITH_VALUE})(?:=\S+|\s+\S+))"
    rf"|(?:--(?:{_AWS_GLOBAL_BOOLEAN}))"
    r"))*\s+"
)


def _strip_aws_global_options(cmd: str) -> str:
    """`aws --profile p --region r ec2 terminate-instances` -> `aws ec2 terminate-instances`."""
    return _AWS_GLOBAL_OPTS_RE.sub(r"\1 ", cmd)


def _normalize(command: str) -> str:
    """The form every classifier and pricer reads.

    Quotes do not change which program runs: `"aws" ec2 terminate-instances`
    is a terminate. Dropping them can only over-match, which is the safe side.
    Classification and pricing must read the SAME form: when only the
    classifier stripped AWS global options, `aws --region us-east-1 ec2
    run-instances --instance-type p4d.24xlarge --count 8` classified as a
    launch, found no price, and passed silently at ~$191k/mo."""
    cmd = command.replace('"', "").replace("'", "")
    cmd = " ".join(cmd.split())  # normalize whitespace
    return _strip_aws_global_options(cmd)


def classify_command(command: str) -> tuple[str, str] | None:
    """Classify a shell command as ("one_way"|"two_way", action_type), or None
    when it is not an infrastructure mutation nable cares about."""
    cmd = _normalize(command)
    for pattern, action in _ONE_WAY_CLASSIFIERS:
        if re.search(pattern, cmd):
            return ("one_way", action)
    for pattern, action in _TWO_WAY_CLASSIFIERS:
        if re.search(pattern, cmd):
            return ("two_way", action)
    return None


def _strict() -> bool:
    return os.getenv("FINOPS_GUARD_STRICT", "").strip().lower() in ("1", "true", "yes")


# ── Command cost estimation ────────────────────────────────────────────────────
# The gap this closes: `aws ec2 run-instances --instance-type p4d.24xlarge
# --count 8` (~$191k/mo) classified as a reversible in-policy mutation and the
# guard stayed silent, while policy.py's dollar threshold sat unreachable
# because nothing on the shell path ever computed a dollar figure. Reversible
# is not the same as cheap.
#
# Every pricer below reads prices the repo already holds and returns None for
# anything it cannot price from them. An "ask" without a figure is the guard's
# old behaviour; an ask with a made-up figure is worse than that, because the
# human decides on the number. Deliberately NOT priced:
#   - `kubectl scale --replicas N`: what a replica costs depends on its
#     requests and the node it lands on, neither of which is in the command.
#   - a Reserved Instance purchase without --limit-price: the offering id fixes
#     type, term and price, and looking it up needs the network.
#   - `terraform apply` without a saved plan: there is nothing to read yet.

_ON_DEMAND_BASIS = "on-demand us-east-1 list price"

_RUN_INSTANCES_RE = re.compile(r"\baws\s+ec2\s+run-instances\b")
_INSTANCE_TYPE_RE = re.compile(r"--instance-type[=\s]+([a-z0-9]+\.[a-z0-9]+)")
# `--count 8` or the min:max form `--count 2:8`; price the max, because the
# guard's job is the ceiling a human is about to authorise, not the floor.
_COUNT_RE = re.compile(r"--count[=\s]+(\d+)(?::(\d+))?")
_RDS_CREATE_RE = re.compile(r"\baws\s+rds\s+create-db-instance\b")
_SAVINGS_PLAN_RE = re.compile(r"\baws\s+savingsplans\s+create-savings-plan\b")
_RESERVED_RE = re.compile(r"\baws\s+ec2\s+purchase-reserved-instances-offering\b")
# JSON ({"Amount": 1200, ...}, quotes already stripped) and shorthand
# (Amount=1200,CurrencyCode=USD) spell the same thing.
_LIMIT_AMOUNT_RE = re.compile(r"--limit-price[=\s]+\S*?Amount\W{1,3}([\d.]+)")
_GCE_CREATE_RE = re.compile(r"\bgcloud\s+(?:\S+\s+)*compute\s+instances\s+create\b(?!-)")
_AZ_VM_CREATE_RE = re.compile(r"\baz\s+(?:\S+\s+)*vm\s+create\b")
_SHELL_BREAKS = ("&&", "||", ";", "|")

# The engines _RDS_HOURLY's rates are for. Aurora bills per cluster instance
# at other rates and SQL Server, Oracle and Db2 carry licence-included rates
# the table does not hold: those get no figure, not a MySQL price.
_RDS_TABLE_ENGINES = ("mysql", "postgres", "mariadb")


def _flag(cmd: str, name: str) -> str | None:
    """The value of `--name value` or `--name=value`, or None."""
    m = re.search(rf"(?<!\S)--{re.escape(name)}(?:=|\s+)(?!-)(\S+)", cmd)
    return m.group(1) if m else None


def _has_flag(cmd: str, name: str) -> bool:
    """A boolean flag is present (`--multi-az`, never `--no-multi-az`)."""
    return re.search(rf"(?<!\S)--{re.escape(name)}(?![\w=-])", cmd) is not None


def _num(raw: str | None) -> float | None:
    try:
        v = float(raw) if raw is not None else None
    except ValueError:
        return None
    return v if v is not None and v > 0 else None


def _rate(hourly: float) -> str:
    """$32.77, $0.171, $0.0104: enough digits that the rate is the table's."""
    return f"${hourly:,.2f}" if round(hourly, 2) == hourly else f"${hourly:,.4f}".rstrip("0")


def _hours_per_month() -> float:
    from .aws_prices import HOURS_PER_MONTH
    return HOURS_PER_MONTH


def _price_run_instances(cmd: str, **_: Any) -> dict[str, Any] | None:
    m = _INSTANCE_TYPE_RE.search(cmd)
    if not m:
        return None
    itype = m.group(1)
    from .aws_prices import EC2_HOURLY
    hourly = EC2_HOURLY.get(itype)
    if not hourly:
        return None
    count = 1
    cm = _COUNT_RE.search(cmd)
    if cm:
        count = max(int(cm.group(1)), int(cm.group(2) or 0)) or 1
    monthly = hourly * count * _hours_per_month()
    return {
        "monthly_usd": round(monthly, 2),
        "hourly_usd": hourly,
        "instance_type": itype,
        "count": count,
        "basis": _ON_DEMAND_BASIS,
        "line": (f"{count}x {itype} at {_rate(hourly)}/hr ({_ON_DEMAND_BASIS}) "
                 f"is ~${monthly:,.0f}/mo"),
    }


def _price_rds(cmd: str, **_: Any) -> dict[str, Any] | None:
    cls = _flag(cmd, "db-instance-class")
    engine = (_flag(cmd, "engine") or "").lower()
    if not cls or engine not in _RDS_TABLE_ENGINES:
        return None
    from .aws_prices import RDS_HOURLY
    hourly = RDS_HOURLY.get(cls)
    if not hourly:
        return None
    # Multi-AZ runs a standby of the same class: twice the instance hours,
    # the same rule the Terraform estimator applies to aws_db_instance.
    multi_az = _has_flag(cmd, "multi-az")
    monthly = hourly * (2 if multi_az else 1) * _hours_per_month()
    basis = f"{_ON_DEMAND_BASIS}, instance hours only; storage and I/O not included"
    return {
        "monthly_usd": round(monthly, 2),
        "hourly_usd": hourly,
        "instance_type": cls,
        "count": 2 if multi_az else 1,
        "basis": basis,
        "line": (f"{cls} {engine}{' Multi-AZ' if multi_az else ''} at {_rate(hourly)}/hr"
                 f"{' x2 for the standby' if multi_az else ''} ({basis}) "
                 f"is ~${monthly:,.0f}/mo"),
    }


def _price_savings_plan(cmd: str, **_: Any) -> dict[str, Any] | None:
    hourly = _num(_flag(cmd, "commitment"))
    if hourly is None:
        return None
    monthly = hourly * _hours_per_month()
    # The commitment is exact; the term is not in the command. It is fixed by
    # the offering id, and resolving that is a network call, so both terms are
    # stated rather than one guessed.
    basis = ("--commitment is dollars per hour for the whole term; the offering "
             "id fixes the term (1 or 3 years), which the guard cannot look up offline")
    upfront = _num(_flag(cmd, "upfront-payment-amount"))
    return {
        "monthly_usd": round(monthly, 2),
        "commitment_hourly_usd": hourly,
        "term_totals_usd": {"1yr": round(monthly * 12, 2), "3yr": round(monthly * 36, 2)},
        "basis": basis,
        "line": (f"a {_rate(hourly)}/hr Savings Plan commitment is ${monthly:,.0f}/mo, "
                 f"${monthly * 12:,.0f} over a 1-year term or ${monthly * 36:,.0f} over 3 years"
                 + (f", ${upfront:,.0f} of it up front" if upfront else "")
                 + f" ({basis})"),
    }


def _price_reserved_instances(cmd: str, **_: Any) -> dict[str, Any] | None:
    m = _LIMIT_AMOUNT_RE.search(cmd)
    ceiling = _num(m.group(1)) if m else None
    if ceiling is None:
        return None
    count = int(_num(_flag(cmd, "instance-count")) or 1)
    basis = ("the --limit-price ceiling on the whole order; the offering id fixes "
             "type, term and price, which the guard cannot look up offline")
    return {
        "monthly_usd": None,           # a one-off order ceiling, not a monthly rate
        "total_usd": round(ceiling, 2),
        "count": count,
        "basis": basis,
        "line": (f"{count} Reserved Instance{'s' if count != 1 else ''}, the order capped "
                 f"at ${ceiling:,.0f} ({basis})"),
    }


def _leading_names(cmd: str, verb_end: int) -> int:
    """How many positional names follow the verb before the first flag.

    `gcloud compute instances create vm-1 vm-2 --machine-type ...` makes two.
    Names written after flags cannot be told from flag values without the
    flag's schema, so they are not counted and the line says "each"."""
    n = 0
    for tok in cmd[verb_end:].split():
        if tok.startswith("-") or tok in _SHELL_BREAKS:
            break
        n += 1
    return n


def _price_table_vm(cmd: str, *, flag: str, table: dict[str, float], count: int,
                    basis: str) -> dict[str, Any] | None:
    size = _flag(cmd, flag)
    if not size:
        return None
    # Azure sizes are case-insensitive on the CLI (standard_d4s_v3 works).
    each = table.get(size) or {k.lower(): v for k, v in table.items()}.get(size.lower())
    if not each:
        return None
    monthly = each * count
    return {
        "monthly_usd": round(monthly, 2),
        "instance_type": size,
        "count": count,
        "basis": basis,
        "line": f"{count}x {size} at ${each:,.2f}/mo each ({basis}) is ~${monthly:,.0f}/mo",
    }


def _price_gce(cmd: str, **_: Any) -> dict[str, Any] | None:
    from .connectors.kubernetes import _GKE_MONTHLY
    m = _GCE_CREATE_RE.search(cmd)
    return _price_table_vm(
        cmd, flag="machine-type", table=_GKE_MONTHLY,
        count=max(1, _leading_names(cmd, m.end())),
        basis="on-demand monthly, nable's Compute Engine node price table")


def _price_az_vm(cmd: str, **_: Any) -> dict[str, Any] | None:
    from .connectors.kubernetes import _AKS_MONTHLY
    return _price_table_vm(
        cmd, flag="size", table=_AKS_MONTHLY,
        count=int(_num(_flag(cmd, "count")) or 1),
        basis="pay-as-you-go monthly, nable's Azure VM price table")


_TF_APPLY_RE = re.compile(r"\b(terraform|tofu)\s+((?:-chdir=\S+\s+)?)apply\b")
_CD_PREFIX_RE = re.compile(r"^cd\s+(\S+)\s*(?:&&|;)")
# `terraform apply` flags that take their value as the NEXT token, so that
# token is not mistaken for the plan file.
_TF_VALUE_FLAGS = ("-var", "-var-file", "-target", "-replace", "-state",
                   "-state-out", "-backup", "-parallelism", "-lock-timeout")
# The hook's own timeout is 10s (30s for uvx) and a timed-out hook fails open
# with no verdict at all. Reading a plan loads provider schemas, which is
# usually one to three seconds; past five, no figure beats no guard.
_PLAN_SHOW_TIMEOUT_S = 5.0


def _planfile_arg(cmd: str, verb_end: int) -> str | None:
    skip = False
    for tok in cmd[verb_end:].split():
        if tok in _SHELL_BREAKS:
            break
        if skip:
            skip = False
        elif tok.startswith("-"):
            skip = tok in _TF_VALUE_FLAGS
        else:
            return tok
    return None


_PLAN_CACHE: dict[tuple[str, float], dict[str, Any] | None] = {}


def _read_saved_plan(cmd: str, cwd: str | None) -> tuple[str, str, dict[str, Any]] | None:
    """(tool, plan file as written, `show -json` document) for `terraform|tofu
    apply <planfile>`, or None.

    Only when the plan file exists and the binary is on PATH; anything else (a
    plain apply, a missing file, a slow or failing `show`) is None. Read once
    per plan file per process: both pricing and the destroy check need it,
    and the hook must not pay for `show` twice."""
    import shutil
    import subprocess

    m = _TF_APPLY_RE.search(cmd)
    if not m:
        return None
    tool = m.group(1)
    plan = _planfile_arg(cmd, m.end())
    if not plan:
        return None
    base = Path(cwd or os.getcwd())
    cd = _CD_PREFIX_RE.match(cmd)
    if cd:
        base = base / Path(cd.group(1)).expanduser()
    chdir = re.search(r"-chdir=(\S+)", m.group(2))
    if chdir:
        base = base / Path(chdir.group(1)).expanduser()
    plan_path = base / Path(plan).expanduser()
    try:
        key = (str(plan_path.resolve()), plan_path.stat().st_mtime)
    except OSError:
        return None
    if key not in _PLAN_CACHE:
        _PLAN_CACHE[key] = None
        exe = shutil.which((os.environ.get("TERRAFORM_BIN") or "terraform")
                           if tool == "terraform" else "tofu")
        if exe and plan_path.is_file():
            # env=child_env(): terraform loads the providers the directory
            # declares, and none of them get nable's decrypted vault (see
            # estimate_from_dir).
            from .security.vault import child_env
            try:
                r = subprocess.run([exe, "show", "-json", str(plan_path)], cwd=str(base),
                                   capture_output=True, text=True, check=False,
                                   timeout=_PLAN_SHOW_TIMEOUT_S, env=child_env())
                if r.returncode == 0:
                    doc = json.loads(r.stdout)
                    _PLAN_CACHE[key] = doc if isinstance(doc, dict) else None
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
    doc = _PLAN_CACHE[key]
    return (tool, plan, doc) if doc is not None else None


def saved_plan_destroys(command: str, *, cwd: str | None = None) -> list[str]:
    """Addresses a saved plan deletes outright, for `terraform apply <planfile>`.

    `terraform plan -destroy -out plan.out` then `terraform apply plan.out` is
    a destroy wearing the apply verb, and the classifier can only see the verb.
    The plan cannot lie about it. Replacements (delete-then-create) are not
    counted: they are routine in ordinary applies, and asking on each one is
    the kind of noise that gets a guard uninstalled."""
    try:
        read = _read_saved_plan(_normalize(command), cwd)
        if read is None:
            return []
        out = []
        for rc in read[2].get("resource_changes") or []:
            actions = (rc.get("change") or {}).get("actions") or []
            if "delete" in actions and "create" not in actions:
                out.append(str(rc.get("address") or rc.get("type") or "?"))
        return out
    except Exception:
        return []


def _price_planfile(cmd: str, *, cwd: str | None = None, **_: Any) -> dict[str, Any] | None:
    """`terraform apply plan.out` applies exactly the saved plan, so the plan
    can be priced before it runs, through the same estimator as `nable
    estimate`."""
    read = _read_saved_plan(cmd, cwd)
    if read is None:
        return None
    tool, plan, doc = read
    from .connectors.terraform_estimate import estimate_plan
    result = estimate_plan(doc)
    if not result["lines"]:
        return None                    # nothing in the plan is priceable
    monthly = float(result["monthly_delta_usd"])
    unpriced = len(result["unpriced"])
    basis = (f"`{tool} show -json {plan}`, {_ON_DEMAND_BASIS}"
             + (f"; {unpriced} resource{'s' if unpriced != 1 else ''} in the plan not priced"
                if unpriced else ""))
    return {
        "monthly_usd": round(monthly, 2),
        "plan": plan,
        "priced_resources": len(result["lines"]),
        "unpriced_resources": unpriced,
        "basis": basis,
        "line": (f"{plan} changes the bill by {'+' if monthly >= 0 else '-'}"
                 f"${abs(monthly):,.0f}/mo ({basis})"),
    }


_PRICERS: list[tuple[re.Pattern[str], Any]] = [
    (_RUN_INSTANCES_RE, _price_run_instances),
    (_RDS_CREATE_RE, _price_rds),
    (_SAVINGS_PLAN_RE, _price_savings_plan),
    (_RESERVED_RE, _price_reserved_instances),
    (_GCE_CREATE_RE, _price_gce),
    (_AZ_VM_CREATE_RE, _price_az_vm),
    (_TF_APPLY_RE, _price_planfile),
]


def estimate_command_monthly_cost(command: str, *, cwd: str | None = None) -> dict[str, Any] | None:
    """A local, list-price estimate for a shell command, or None.

    `cwd` is the directory the command will run in (the agent session's), used
    to find a saved Terraform plan; it defaults to this process's.

    Returns {monthly_usd, basis, line, ...} where `line` is the sentence the
    human reads and `basis` says where the number came from. `monthly_usd` is
    None when the only honest figure is a one-off total (`total_usd`, e.g. a
    Reserved Instance order ceiling). Anything unpriceable returns None: an
    unknown type must degrade to the guard's existing behaviour, never to an
    invented figure.
    """
    cmd = _normalize(command)
    for pattern, pricer in _PRICERS:
        if pattern.search(cmd):
            try:
                return pricer(cmd, cwd=cwd)
            except Exception:
                return None            # a pricing bug must not cost the verdict
    return None


def _cost_line(est: dict[str, Any]) -> str:
    return est["line"]


def _prod_context(command: str) -> bool:
    """Does this command look aimed at production?

    Word-boundary match so 'product' never trips it. Patterns are overridable
    (comma-separated regexes) via FINOPS_GUARD_PROD_PATTERNS; set it to 'off'
    to disable production-context confirmation entirely. Over-matching costs an
    unnecessary confirm, which is the tolerable failure direction here.
    """
    raw = os.getenv("FINOPS_GUARD_PROD_PATTERNS", "")
    if raw.strip().lower() == "off":
        return False
    patterns = [p.strip() for p in raw.split(",") if p.strip()] or [r"\bprod(uction)?\b"]
    for pat in patterns:
        try:
            if re.search(pat, command, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def _stop_on_budget() -> bool:
    """Whether a blown AI budget HARD-STOPS the agent (deny) or just asks.

    The user is asked once, at `nable guard install`, and the answer is stored on
    the budget as `on_breach`. The env var overrides it for a single session or a
    CI run. Default is notify: a tool that silently halts your agent gets
    uninstalled before anyone finds the setting that caused it, so stopping has
    to be something you chose.
    """
    env = os.getenv("FINOPS_GUARD_STOP_ON_BUDGET", "").strip().lower()
    if env in ("1", "true", "yes"):
        return True
    if env in ("0", "false", "no"):
        return False
    try:
        from .ai_budget import get_budget
        return (get_budget().get("on_breach") or "notify") == "stop"
    except Exception:
        return False


def check_budget_gate(session_id: str | None = None) -> dict[str, Any] | None:
    """Stop the agent when its own token spend is over the budget the user set.

    This runs BEFORE command classification and applies to every tool call, not
    just infrastructure ones. "Stop the agent because it is spending too much"
    means stop it, not stop it from touching Terraform.

    Reads the local Claude Code session logs (ai_budget), so it needs no cloud
    account, no API key and no network. Returns None when no budget is set, when
    usage is under it, or on ANY error: a guard that cannot read its own budget
    must not take a position. session_id is the hook payload's, so a per-session
    cap is measured against the session making the call.
    """
    try:
        from .ai_budget import BUDGET_OVER, status
        st = status(session_id=session_id) if session_id else status()
        if st.get("verdict") != BUDGET_OVER:
            return None
        budget = st.get("budget") or {}
        pct = st.get("pct_of_budget")
        over = f"{pct * 100:.0f}% of" if isinstance(pct, (int, float)) else "over"
        if st.get("verdict_basis") == "session":
            sess = st.get("session") or {}
            detail = (f"~${sess.get('usd_equivalent', 0):,.2f} estimated this session, "
                      f"{over} its ${sess.get('cap_usd') or 0:,.2f} session cap")
        elif st.get("verdict_basis") == "spend":
            detail = (f"~${st.get('est_usd_mtd_list_price', 0):,.0f} estimated this month, "
                      f"{over} your ${budget.get('spend_cap', 0):,.0f} cap")
        else:
            detail = (f"{st.get('billable_tokens_mtd', 0):,} tokens this month, "
                      f"{over} your {budget.get('monthly_tokens', 0):,} budget")
        hard = _stop_on_budget()
        return {
            "decision": "deny" if hard else "ask",
            "action_type": "ai_budget",
            "reason": (
                f"nable guard: your agent is over its AI budget. {detail}. "
                + ("Stopped because FINOPS_GUARD_STOP_ON_BUDGET is on. "
                   "Raise it with `nable ai-budget set`, or unset that variable to "
                   "downgrade this to a confirmation."
                   if hard else
                   "Confirm to continue, or raise it with `nable ai-budget set`. "
                   "Set FINOPS_GUARD_STOP_ON_BUDGET=1 to make this a hard stop.")
            ),
        }
    except Exception:
        return None  # unreadable budget is not a reason to block anyone


# A priced change allowed by policy but at or above this share of the auto
# threshold gets a "warn": the same 80% line ai_budget draws for the agent's
# own spend, applied to what the agent is about to launch.
_WARN_AT = 0.80


def _verdict_for(command: str, hit: tuple[str, str], *, context: str | None = None,
                 via: str = "", cwd: str | None = None) -> dict[str, Any]:
    """The policy verdict, then what the ledger's recent history adds to it.

    A history check that fails leaves the policy verdict standing and puts the
    exception under "_history_error" for the caller to record as a fail-open:
    a guard that cannot read its own ledger must not take a position."""
    v = _policy_verdict(command, hit, context=context, via=via, cwd=cwd)
    try:
        return _check_history(v, command, via=via)
    except Exception as exc:
        return {**v, "_history_error": exc}


def _policy_verdict(command: str, hit: tuple[str, str], *, context: str | None = None,
                    via: str = "", cwd: str | None = None) -> dict[str, Any]:
    """The policy verdict for one already-classified action. Always a dict:
    "allow" is a verdict too (the ledger records it, with its figure), and the
    public entry points turn it into None for their callers.

    Shared by the shell and MCP entry points so a destroy is judged the same
    whichever door the agent used. `command` is the shell form, which is what
    gets priced; `context` is the text searched for a production context
    (defaults to the command); `via` prefixes the reason with what an MCP call
    amounts to, since the human never saw a command.
    """
    door, action_type = hit
    lead = f"{via}. " if via else ""

    def verdict(decision: str, body: str, *, strict: bool = False,
                est: dict[str, Any] | None = None) -> dict[str, Any]:
        head = "nable guard (strict)" if strict else "nable guard"
        v: dict[str, Any] = {"decision": decision, "action_type": action_type,
                             "door": door, "reason": f"{head}: {lead}{body}"}
        if est is not None:
            v["monthly_delta_usd"] = est["monthly_usd"]
            v["estimate"] = est
        return v

    def allowed(est: dict[str, Any] | None) -> dict[str, Any]:
        v = verdict("allow", "allowed by policy", est=est)
        del v["reason"]                # silent: there is nothing to tell anyone
        return v

    destroys: list[str] = []
    if action_type == "infra_apply":
        # A saved plan that deletes resources is a one-way door whatever verb
        # applies it; saved_plan_destroys reads the plan rather than the verb.
        destroys = saved_plan_destroys(command, cwd=cwd)
        if destroys:
            door, action_type = "one_way", "delete_resource"

    if action_type == "infra_apply":
        # Reversible mutation. Zero friction by default; strict mode confirms,
        # and a production context always confirms: practitioners run agents
        # loose in staging but want a human nod before prod changes.
        #
        # Priceable commands additionally go through the policy's dollar
        # threshold: reversible does not mean cheap, and launching 8x
        # p4d.24xlarge is a ~$191k/mo decision whichever door it is. The
        # estimate rides the same evaluate_action_gate as everything else, so
        # the user's FINOPS_POLICY_MAX_AUTO_USD and learned adjustments apply.
        est = estimate_command_monthly_cost(command, cwd=cwd)
        if est is not None:
            gate = evaluate_action_gate(action_type,
                                        monthly_delta_usd=est.get("monthly_usd") or 0.0)
            if gate.get("gate") != GATE_ALLOW:
                return verdict(
                    "ask" if gate.get("gate") == GATE_ESCALATE else "deny",
                    f"{_cost_line(est)}. "
                    f"{gate.get('reason', 'a human must review this action.')}",
                    est=est)
        cost = f" {_cost_line(est)}." if est else ""
        if _strict():
            return verdict("ask", "this changes infrastructure and therefore the "
                           f"bill.{cost} Cost it first (ask nable to "
                           "estimate_change_cost) or confirm to proceed.",
                           strict=True, est=est)
        if _prod_context(context if context is not None else command):
            return verdict("ask", "this mutates infrastructure in what looks like a "
                           f"PRODUCTION context.{cost} Confirm to proceed, or cost it "
                           "first (ask nable to estimate_change_cost).", est=est)
        if est is not None:
            # Allowed, but close to the line: say so without stopping anyone.
            # "warn" never changes the permission flow; the hook shows the
            # figure and the call proceeds exactly as an allow would.
            cap = float(load_policy().get("max_auto_monthly_usd", 500.0))
            monthly = est.get("monthly_usd") or 0.0
            if cap > 0 and monthly >= _WARN_AT * cap:
                return verdict("warn", f"{_cost_line(est)}, {monthly / cap:.0%} of your "
                               f"${cap:,.0f}/mo auto threshold. Proceeding without a prompt.",
                               est=est)
        return allowed(est)

    # One-way doors escalate whatever they cost, but the human deciding on a
    # Savings Plan should see the commitment in the same breath as the question.
    est = estimate_command_monthly_cost(command, cwd=cwd)
    cost = f"{_cost_line(est)}. " if est else ""
    if destroys:
        shown = ", ".join(destroys[:3]) + (f" and {len(destroys) - 3} more" if len(destroys) > 3 else "")
        cost = (f"the saved plan destroys {len(destroys)} "
                f"resource{'s' if len(destroys) != 1 else ''} ({shown}). ") + cost
    gate = evaluate_action_gate(action_type,
                                monthly_delta_usd=(est or {}).get("monthly_usd") or 0.0)
    if gate.get("gate") == GATE_ESCALATE:
        return verdict("ask", cost + gate.get("reason", "a human must review this action."),
                       est=est)
    if gate.get("gate") == GATE_BLOCK:
        return verdict("deny", cost + gate.get("reason",
                                               "this action is not in your policy allowlist."),
                       est=est)
    return allowed(est)


# ── History: what the guard already let through ────────────────────────────────
# One verdict sees one command. An agent that launches ten $400/mo instances in
# an hour passes ten verdicts that are each correct and a total nobody agreed
# to. These checks read the recent end of the decision ledger (guard_ledger
# .recent: a bounded read from the end of the file, no database) and can only
# tighten: an allow or a warn may become an ask, nothing else changes.

# What the guard let run without a human. An ask is not counted: the hook exits
# before the human answers, so the ledger cannot tell an approved ask from a
# declined one, and counting a declined $191k ask would put every launch for
# the next hour behind a prompt about money that was never spent.
_LET_THROUGH = ("allow", "warn")
_HISTORY_LISTED = 5


def _check_history(v: dict[str, Any], command: str, *, via: str = "") -> dict[str, Any]:
    """`v` upgraded to an ask when recent history says so, else `v` unchanged.
    May raise; _verdict_for turns that into a fail-open."""
    if v.get("decision") not in _LET_THROUGH:
        return v
    pol = load_policy()
    new = (v.get("estimate") or {}).get("monthly_usd")
    cap = velocity_cap(pol)
    window = float(pol.get("velocity_window_minutes") or 60.0)
    if not (isinstance(new, (int, float)) and new > 0 and cap > 0 and window > 0):
        return v
    from . import guard_ledger
    recent = guard_ledger.recent(window)
    reason = _velocity_reason(v, recent, new=float(new), cap=cap, window=window)
    if reason is None:
        return v
    lead = f"{via}. " if via else ""
    return {**v, "decision": "ask", "reason": f"nable guard: {lead}{reason}",
            "history": "velocity"}


def _usd(x: float) -> str:
    return f"${x:,.0f}"


def _listed(recs: list[dict[str, Any]]) -> str:
    shown = []
    for r in recs[-_HISTORY_LISTED:]:
        cmd = str(r.get("command") or r.get("action_type") or "?")
        cmd = cmd if len(cmd) <= 70 else cmd[:67] + "..."
        shown.append(f"~{_usd(r['monthly_usd'])}/mo `{cmd}` at {str(r.get('ts', ''))[11:16]} UTC")
    more = len(recs) - len(shown)
    return "; ".join(shown) + (f"; and {more} earlier" if more > 0 else "")


def _velocity_reason(v: dict[str, Any], recent: list[dict[str, Any]], *, new: float,
                     cap: float, window: float) -> str | None:
    """The velocity cap: priced monthly run-rate let through in the window,
    plus this action, over the cap."""
    counted = [r for r in recent
               if r.get("decision") in _LET_THROUGH
               and isinstance(r.get("monthly_usd"), (int, float)) and r["monthly_usd"] > 0]
    total = sum(r["monthly_usd"] for r in counted)
    if total + new <= cap:
        return None
    n = len(counted)
    est = v.get("estimate") or {}
    head = (f"{_cost_line(est)}. On top of ~{_usd(total)}/mo already let through in the "
            f"last {window:g} minutes ({n} action{'s' if n != 1 else ''}: {_listed(counted)}), "
            f"that is ~{_usd(total + new)}/mo in {window:g} minutes"
            if counted else f"{_cost_line(est)}, on its own")
    return (f"{head}, over your {_usd(cap)}/mo velocity cap per {window:g} minutes. "
            "Confirm to proceed, or raise FINOPS_POLICY_VELOCITY_CAP_USD.")


def gate_command(command: str, session_id: str | None = None, *, harness: str = "claude-code",
                 cwd: str | None = None, tool: str = "shell",
                 record: bool = True) -> dict[str, Any] | None:
    """Evaluate a shell command against the policy gate. PUBLIC ENTRY POINT.

    This and gate_mcp_call are what every harness adapter calls (the Claude
    Code hook below; Cursor and Codex adapters in guard_adapters.py). `harness`
    names the calling agent harness and is echoed back in the verdict. `cwd`
    is the directory the agent will run the command in, when the harness
    says (a saved Terraform plan is found relative to it). `tool` is the
    harness's name for its shell tool ("Bash" in Claude Code), for the ledger.

    Every verdict on an infrastructure action, allows included, is appended
    to the decision ledger (guard_ledger.py) unless `record` is False, which
    is for a human asking what the guard would do (`nable guard check`), not
    an agent doing it. Never raises: an internal error is recorded as a
    fail-open and returns None, so an adapter cannot be broken by the guard.

    Returns None when the guard has no opinion (not infra, or an in-policy
    reversible action), else a verdict dict:

        decision            "ask" (a human confirms), "deny" (do not run), or
                            "warn" (proceed, but show the reason: a priced
                            change allowed by policy yet near its threshold)
        reason              one line for the human, starting "nable guard"
        action_type, door   the policy.py vocabulary (e.g. delete_resource, one_way)
        monthly_delta_usd   present only when the action was priced
        estimate            the pricing basis behind that figure, when priced
        harness             as passed in

    `session_id` is the hook payload's, so a per-session AI budget cap is
    measured against the session making the call.
    """
    try:
        # The AI budget stop comes first and is not conditioned on the command:
        # an agent burning through its budget should be stopped whatever it is
        # doing.
        budget_hit = check_budget_gate(session_id)
        if budget_hit is not None:
            v = {**budget_hit, "harness": harness}
        else:
            hit = classify_command(command)
            if hit is None:
                return None
            v = {**_verdict_for(command, hit, cwd=cwd), "harness": harness}
        history_error = v.pop("_history_error", None)
        if record:
            if history_error is not None:
                _record_fail_open(history_error, harness=harness, tool=tool, command=command,
                                  check="history")
            _record(v, tool=tool, command=command)
        return None if v["decision"] == "allow" else v
    except Exception as exc:
        if record:
            _record_fail_open(exc, harness=harness, tool=tool, command=command)
        return None


_SEVERITY = {"deny": 3, "ask": 2, "warn": 1, "allow": 0}


def gate_mcp_call(tool_name: str, arguments: dict[str, Any] | None, *,
                  harness: str = "claude-code", session_id: str | None = None,
                  record: bool = True) -> dict[str, Any] | None:
    """Evaluate an MCP tool call against the policy gate. PUBLIC ENTRY POINT.

    `tool_name` is the harness's full name (`mcp__<server>__<tool>` in Claude
    Code). Known infra-mutating tools (guard_mcp.MCP_RULES: Terraform, AWS,
    Kubernetes) are translated to the shell command they amount to and judged
    exactly like it, prices included. Returns the same verdict shape as
    gate_command, with `mcp_tool` added; a batch call returns its most severe
    verdict.

    Unknown MCP tools return None before anything else runs, the AI budget
    stop included: the guard never asks about a tool it does not understand,
    and does not record it either. Recording and fail-open as gate_command.
    """
    summary = tool_name
    try:
        from .guard_mcp import argument_text, translate

        actions = translate(tool_name, arguments)
        if not actions:
            return None
        summary = actions[0].command

        budget_hit = check_budget_gate(session_id)
        if budget_hit is not None:
            worst: dict[str, Any] | None = {**budget_hit}
        else:
            context = argument_text(arguments)
            worst = None
            for act in actions:
                hit = act.hit or classify_command(act.command)
                if hit is None:
                    continue
                v = _verdict_for(act.command, hit, context=f"{act.command} {context}",
                                 via=(f"{tool_name} would {act.summary}" if act.summary
                                      else f"{tool_name} amounts to `{act.command}`"))
                history_error = v.pop("_history_error", None)
                if history_error is not None and record:
                    _record_fail_open(history_error, harness=harness, tool=tool_name,
                                      command=act.command, check="history")
                if worst is None or _SEVERITY[v["decision"]] > _SEVERITY[worst["decision"]]:
                    worst, summary = v, act.command
            if worst is None:
                return None
        worst = {**worst, "harness": harness, "mcp_tool": tool_name}
        if record:
            _record(worst, tool=tool_name, command=summary)
        return None if worst["decision"] == "allow" else worst
    except Exception as exc:
        if record:
            _record_fail_open(exc, harness=harness, tool=tool_name, command=summary)
        return None


# ── Decision ledger ───────────────────────────────────────────────────────────

def _policy_version() -> str:
    """A short fingerprint of the effective policy, so a reviewer can tell
    which verdicts were made under which knobs (threshold, allowlist)."""
    import hashlib
    try:
        blob = json.dumps(load_policy(), sort_keys=True, default=str)
    except Exception:
        return "unknown"
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def _record(v: dict[str, Any], *, tool: str, command: str) -> None:
    """Append one verdict to the ledger. Cheap, and never raises: a ledger
    problem is a missing line, never a lost verdict."""
    with contextlib.suppress(Exception):
        from . import guard_ledger
        est = v.get("estimate") or {}
        guard_ledger.append({
            "harness": v.get("harness"),
            "tool": tool,
            "command": guard_ledger.redact(command),
            "door": v.get("door"),
            "action_type": v.get("action_type"),
            "decision": v["decision"],
            "monthly_usd": est.get("monthly_usd"),
            "total_usd": est.get("total_usd"),
            "basis": est.get("basis"),
            "reason": guard_ledger.redact(v.get("reason"), limit=600) if v.get("reason") else None,
            # Which history check turned this into an ask, when one did.
            **({"history": v["history"]} if v.get("history") else {}),
            "policy_version": _policy_version(),
            "nable_version": __version__,
            # Known only for a deny: the call never ran. An ask is the human's
            # to answer after the hook has exited, and an allow may still meet
            # the harness's own permission prompt.
            "outcome": "not_run" if v["decision"] == "deny" else None,
        })


def _record_fail_open(exc: BaseException, *, harness: str, tool: Any, command: Any,
                      check: str | None = None) -> None:
    """A guard error let a call through unexamined; that is a verdict too.

    `check` names the part that failed when the rest of the verdict stood (a
    history check that could not read the ledger): the call was judged on
    policy alone, and the verdict itself is recorded next to this line."""
    with contextlib.suppress(Exception):
        from . import guard_ledger
        guard_ledger.append({
            "harness": harness,
            "tool": tool if isinstance(tool, str) else None,
            "command": guard_ledger.redact(command) if command else None,
            "decision": "fail_open",
            "error": type(exc).__name__,
            **({"check": check} if check else {}),
            "policy_version": _policy_version(),
            "nable_version": __version__,
            "outcome": None,
        })


# ── Claude Code hook protocol ──────────────────────────────────────────────────

def run_hook(stdin: Any = None, stdout: Any = None) -> int:
    """PreToolUse hook body: JSON in on stdin, optional JSON verdict on stdout.

    Handles the Bash tool and MCP tools (`mcp__*`); everything else exits 0
    with no output. Fails open by design: any error or unknown payload exits 0
    with no output so the guard can never break the user's agent.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    tool: Any = None
    try:
        payload = json.load(stdin)
        tool = payload.get("tool_name")
        tool_input = payload.get("tool_input") or {}
        session_id = payload.get("session_id") or None
        if tool == "Bash":
            command = tool_input.get("command") or ""
            if not command:
                return 0
            verdict = gate_command(command, session_id=session_id, harness="claude-code",
                                   cwd=payload.get("cwd"), tool="Bash")
        elif isinstance(tool, str) and tool.startswith("mcp__"):
            verdict = gate_mcp_call(tool, tool_input, harness="claude-code",
                                    session_id=session_id)
        else:
            return 0
        if not verdict:
            return 0
        if verdict["decision"] == "warn":
            # No permissionDecision: the call goes through the normal
            # permission flow untouched, with the figure shown alongside.
            json.dump({"systemMessage": verdict["reason"]}, stdout)
            return 0
        json.dump({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": verdict["decision"],
                "permissionDecisionReason": verdict["reason"],
            }
        }, stdout)
        return 0
    except Exception as exc:
        # Still exit 0 with nothing on stdout: availability beats judgement.
        # But a fail-open is exactly what an audit should be able to count.
        _record_fail_open(exc, harness="claude-code", tool=tool, command=None)
        return 0


# ── Installer ──────────────────────────────────────────────────────────────────

# Marker used to find our hook in settings regardless of how the command is
# prefixed (bare, absolute path, or uvx wrapper).
_HOOK_MARKER = "guard hook"
_HOOK_CMD = "finops guard hook"

# Which tool calls Claude Code sends the hook. Its documented matcher rules: a
# value of only letters, digits, `_`, `-`, spaces, `,` and `|` is a list of
# exact names; anything else is an UNANCHORED JavaScript regex. So the obvious
# "Bash|mcp__.*" would also match BashOutput, KillBash and any tool with "Bash"
# anywhere in its name, spawning the hook for nothing. Anchored, it is exactly
# the Bash tool plus every MCP tool (`mcp__<server>__<tool>`); run_hook then
# returns at once for any MCP tool guard_mcp does not recognise.
_HOOK_MATCHER = "^(Bash|mcp__.*)$"
_LEGACY_MATCHER = "Bash"          # what every release before MCP coverage wrote


def matcher_covers(matcher: Any, tool_name: str) -> bool:
    """Would Claude Code run a hook with this matcher for `tool_name`?

    Mirrors the documented rules closely enough to report coverage: empty or
    "*" matches everything, a plain-word value is an exact list split on `|`
    or `,`, anything else is an unanchored regex. Python's `re` stands in for
    JavaScript's, which agree on every pattern this module writes."""
    if matcher in (None, "", "*"):
        return True
    if not isinstance(matcher, str):
        return False
    if re.fullmatch(r"[A-Za-z0-9_\-\s,|]*", matcher):
        return tool_name in {m.strip() for m in re.split(r"[|,]", matcher)}
    try:
        return re.search(matcher, tool_name) is not None
    except re.error:
        return False


def hook_surfaces(path: Path) -> dict[str, bool]:
    """Which tool surfaces our installed hook actually sees in this file."""
    covered = {"bash": False, "mcp": False}
    for entry, _h in _read_our_hooks(path):
        m = entry.get("matcher")
        covered["bash"] |= matcher_covers(m, "Bash")
        covered["mcp"] |= matcher_covers(m, "mcp__server__call_aws")
    return covered

# The uvx form is pinned to the release that wrote it. Unpinned, `uvx --from
# finops-mcp` resolves the newest PyPI release on every agent tool call, so
# whoever can publish to that project name can run code on every machine with
# the guard installed, on the next Bash call, with no install step and no
# prompt. A security hook is the last thing that should auto-update from the
# network. Pinned, the code that runs is the code the user chose to install;
# `nable guard install` from a newer release moves the pin forward in place.
_PYPI_NAME = "finops-mcp"
_UVX_HOOK_CMD = f"uvx --from {_PYPI_NAME}=={__version__} finops guard hook"
_UVX_FROM_RE = re.compile(r"^uvx\s+--from[=\s]+['\"]?([^\s'\"]+)")


def hook_pin(cmd: str) -> str | None:
    """How a hook command is pinned.

    "pinned"   the uvx form at exactly this release
    "other"    the uvx form pinned to some other release
    "unpinned" the uvx form with no version (resolves latest on every call)
    None       not the uvx form: a binary path is fixed by whatever was
               installed there, so it has no pin to speak of
    """
    m = _UVX_FROM_RE.match(cmd.strip())
    if not m:
        return None
    spec = m.group(1)
    if not re.fullmatch(rf"{re.escape(_PYPI_NAME)}==[A-Za-z0-9.+-]+", spec):
        return "unpinned"
    return "pinned" if spec == f"{_PYPI_NAME}=={__version__}" else "other"


def _is_ephemeral(path: str) -> bool:
    """True when `path` lives somewhere that is not ours to depend on.

    `uvx nable guard install` runs from uv's content-addressed archive cache
    (…/uv/archive-v0/<hash>/bin/finops). That path is real while the command is
    running and is a trap to persist: uv garbage-collects it on `uv cache clean`
    or `uv cache prune`, and the hash changes on the next release. Baking it into
    settings.json produces a hook that works today, dies silently later (Claude
    Code fails open on a hook that cannot execute), and still reports itself as
    installed. The user believes they are guarded and they are not.
    """
    import tempfile
    p = Path(path)
    if "archive-v0" in p.parts:          # uv's content-addressed store
        return True
    roots = [os.getenv("UV_CACHE_DIR"),
             str(Path.home() / ".cache" / "uv"),
             str(Path.home() / "Library" / "Caches" / "uv"),
             tempfile.gettempdir()]
    for r in roots:
        if not r:
            continue
        try:
            if p.resolve().is_relative_to(Path(r).resolve()):
                return True
        except (OSError, ValueError):
            continue
    return False


def _hook_command() -> str:
    """The command Claude Code should run for the hook, resolved to something
    that will still exist tomorrow.

    A uvx user has no `finops` on PATH afterwards, so a bare command would fail
    with command-not-found on every Bash call. A persistent binary is best. An
    ephemeral one is worse than none, because it fails open and lies about it, so
    those fall through to the uvx form, which re-resolves at run time (to this
    release, see _UVX_HOOK_CMD, not to whatever PyPI has that day)."""
    import shutil
    found = shutil.which("finops")
    if found and not _is_ephemeral(found):
        # Quote in case the path has spaces (framework installs on macOS do not,
        # but user venvs can).
        return f'"{found}" guard hook' if " " in found else f"{found} guard hook"
    return _UVX_HOOK_CMD


def _settings_path(global_scope: bool) -> Path:
    if global_scope:
        return Path.home() / ".claude" / "settings.json"
    return Path.cwd() / ".claude" / "settings.json"


def _refuse(path: Path, what: str):
    """Never guess at a settings file we do not understand. Someone's editor
    config is not ours to repair, and a wrong repair silently breaks their agent."""
    return SystemExit(f"  {path} {what}; fix it first, nothing was changed.")


def _load_settings(path: Path) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except Exception:
            raise _refuse(path, "exists but is not valid JSON")
        # Valid JSON is not the same as the shape we expect. A top-level array or
        # string parses fine and then blows up on .setdefault deeper in, after we
        # may already have decided to write.
        if not isinstance(data, dict):
            raise _refuse(path, f"contains a JSON {type(data).__name__}, not an object")
        return data
    return {}


def _hook_list(settings: dict, path: Path, *, create: bool):
    """The PreToolUse list, or None when there is not one and we are not creating.

    Tolerates the keys being absent or explicitly null (a real state: some tools
    write "hooks": null). Refuses when they hold a value of the wrong type, for
    the same reason _load_settings does."""
    hooks = settings.get("hooks")
    if hooks is None:
        if not create:
            return None
        hooks = settings["hooks"] = {}
    elif not isinstance(hooks, dict):
        raise _refuse(path, f"has a 'hooks' value that is a {type(hooks).__name__}, not an object")

    pre = hooks.get("PreToolUse")
    if pre is None:
        if not create:
            return None
        pre = hooks["PreToolUse"] = []
    elif not isinstance(pre, list):
        raise _refuse(path, f"has a 'hooks.PreToolUse' value that is a "
                            f"{type(pre).__name__}, not an array")
    return pre


def _is_our_command(cmd: Any) -> bool:
    return isinstance(cmd, str) and _HOOK_MARKER in cmd and "finops" in cmd


def _our_hooks(pre: Any):
    """Every (entry, hook) pair in a PreToolUse list that is the guard's own.

    Tolerant of entries that are not the shape we write: someone else's data
    is skipped, never inspected further or rewritten."""
    for entry in pre if isinstance(pre, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            continue
        for h in entry["hooks"]:
            if isinstance(h, dict) and _is_our_command(h.get("command")):
                yield entry, h


def _read_our_hooks(path: Path) -> list[tuple[dict, dict]]:
    """Read-only view of our hooks in a settings file; [] on anything odd."""
    try:
        s = json.loads(path.read_text())
        return list(_our_hooks(((s.get("hooks") or {}).get("PreToolUse")) or []))
    except Exception:
        return []


def _command_runs(cmd: str) -> bool:
    """Does the program a hook command names still exist?"""
    import shutil
    exe = cmd[1:cmd.index('"', 1)] if cmd.startswith('"') else cmd.split()[0]
    # The uvx form resolves its (pinned) release at run time; it is healthy if
    # uv exists.
    probe = "uvx" if exe == "uvx" else exe
    return bool(shutil.which(probe) or Path(probe).exists())


def _timeout_for(cmd: str) -> int:
    # uvx resolves an environment per call; give the cold-cache case room.
    # Timeouts fail open in Claude Code, so a slow first call cannot block.
    return 30 if cmd.startswith("uvx") else 10


def is_installed(path: Path) -> bool:
    """Read-only predicate, so any structural surprise means "not installed"
    rather than a traceback. Answering False on a file we cannot parse is safe:
    the caller's next move is to install, which refuses loudly on the same file."""
    return bool(_read_our_hooks(path))


def broken_hook_command(path: Path) -> str | None:
    """Our installed hook command, when the thing it invokes is not runnable.

    Returns None when the hook is absent or healthy. A hook Claude Code cannot
    execute is skipped silently and fails open, so "installed" on its own is not
    a safe thing to report."""
    try:
        for _entry, h in _read_our_hooks(path):
            cmd = h["command"]
            return None if _command_runs(cmd) else cmd
    except Exception:
        return None
    return None


def unpinned_hook_command(path: Path) -> str | None:
    """Our installed hook command, when it is the uvx form with no version.

    That form resolves the newest release from PyPI on every agent tool call
    (see _UVX_HOOK_CMD). Returns None when the hook is absent, pinned, or a
    binary path."""
    for _entry, h in _read_our_hooks(path):
        if hook_pin(h["command"]) == "unpinned":
            return h["command"]
    return None


def _stale(cmd: str) -> bool:
    """Should install() rewrite this existing hook command in place?

    Dead, unpinned, or pinned to a release other than the one running the
    install. Re-running install is an explicit choice of release, so the pin
    follows it; a healthy binary-path hook is never touched."""
    return not _command_runs(cmd) or hook_pin(cmd) in ("unpinned", "other")


def _widen_matcher(pre: list, entry: dict, hook: dict) -> bool:
    """Move our hook from the Bash-only matcher earlier releases wrote to one
    that also covers MCP tools. Returns True when something changed.

    Only the exact "Bash" we wrote is upgraded: any other matcher is a choice
    someone made by hand, and it stays theirs. When our hook shares that entry
    with someone else's, widening the entry would start running THEIR hook on
    every MCP call, so ours moves to an entry of its own instead."""
    if entry.get("matcher") != _LEGACY_MATCHER:
        return False
    if all(isinstance(h, dict) and _is_our_command(h.get("command")) for h in entry["hooks"]):
        entry["matcher"] = _HOOK_MATCHER
        return True
    entry["hooks"].remove(hook)
    pre.append({"matcher": _HOOK_MATCHER, "hooks": [hook]})
    return True


def install(global_scope: bool = False) -> Path:
    """Idempotently add the guard hook to Claude Code settings. Returns the path.

    Idempotent is not the same as "do nothing when an entry exists". `guard
    status` tells anyone whose hook binary has vanished to re-run install, and
    the 0.8.195 changelog told every uvx user the same. Both were promises this
    function did not keep: it returned early on any existing entry, so the dead
    hook stayed dead and the telemetry counted it as "repaired". Our own entry
    is now rewritten in place when it is stale, or when its matcher predates
    MCP coverage, keeping its position and every other hook in the file
    exactly as found."""
    path = _settings_path(global_scope)
    settings = _load_settings(path)
    pre = _hook_list(settings, path, create=True)
    ours = list(_our_hooks(pre))
    if ours:
        changed = False
        for entry, h in ours:
            changed = _widen_matcher(pre, entry, h) or changed
            if not _stale(h["command"]):
                continue
            cmd = _hook_command()
            h["command"] = cmd
            old = h.get("timeout")
            h["timeout"] = max(old, _timeout_for(cmd)) if isinstance(old, int) else _timeout_for(cmd)
            changed = True
        if not changed:
            return path
    else:
        cmd = _hook_command()
        pre.append({
            "matcher": _HOOK_MATCHER,
            "hooks": [{"type": "command", "command": cmd, "timeout": _timeout_for(cmd)}],
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    return path


def uninstall(global_scope: bool = False) -> bool:
    """Remove the guard hook. Returns True when something was removed."""
    path = _settings_path(global_scope)
    settings = _load_settings(path)
    pre = _hook_list(settings, path, create=False)
    if not pre:
        return False
    removed = False
    kept = []
    for entry in pre:
        if not isinstance(entry, dict):
            kept.append(entry)          # not ours; leave it exactly as found
            continue
        inner = [h for h in (entry.get("hooks") or [])
                 if not (isinstance(h, dict)
                         and _HOOK_MARKER in (h.get("command") or "")
                         and "finops" in (h.get("command") or ""))]
        if len(inner) != len(entry.get("hooks") or []):
            removed = True
        if inner or not entry.get("hooks"):
            entry["hooks"] = inner
            if inner:
                kept.append(entry)
        else:
            removed = True
    if removed:
        settings["hooks"]["PreToolUse"] = kept
        if not kept:
            del settings["hooks"]["PreToolUse"]
        if not settings.get("hooks"):
            settings.pop("hooks", None)
        path.write_text(json.dumps(settings, indent=2) + "\n")
    return removed


# ── Doctor ─────────────────────────────────────────────────────────────────────

SEATBELT = (
    "The guard is a seatbelt, not a security boundary. It checks what an agent "
    "sends through the hooks listed here, and an agent can route around it: a "
    "script that runs the command inside it, a tool or MCP server the guard does "
    "not recognise, an edit to its own settings file. Give agents read-only cloud "
    "credentials, and keep write access behind a human: a separate profile, role "
    "or pipeline the agent cannot assume."
)

# What each harness's hook can see. Claude Code's comes from the matcher we
# write; Cursor's beforeShellExecution and Codex's PreToolUse-on-Bash see shell
# commands only (guard_adapters.py), and Codex cannot pause to ask.
_FAMILY_LABELS = {"aws": "AWS", "kubernetes": "Kubernetes", "terraform": "Terraform"}
_ADAPTER_SURFACES = {"cursor": ("Cursor", "shell commands"),
                     "codex": ("Codex CLI", "shell commands (an ask becomes a deny)")}


def _adapter_rows() -> list[dict[str, Any]]:
    """Cursor and Codex hook state from guard_adapters, [] when this build has
    no adapters or they cannot answer. Read-only."""
    try:
        from . import guard_adapters as ga  # type: ignore[attr-defined]
        found = set(ga.detected())
        rows = []
        for name in _ADAPTER_SURFACES:
            for scope, is_global in (("project", False), ("global", True)):
                st = ga.state(name, is_global)
                rows.append({"harness": name, "scope": scope,
                             "path": str(ga.hooks_path(name, is_global)),
                             "installed": st != "absent", "runs": st == "installed",
                             "present": name in found})
        return rows
    except Exception:
        return []


def doctor() -> dict[str, Any]:
    """Which surfaces the guard actually covers on this machine, and what it
    does not. Read-only: it inspects settings files and the ledger, runs
    nothing, and calls no cloud API."""
    from . import guard_ledger
    from .guard_mcp import MCP_RULES

    rows: list[dict[str, Any]] = []
    for scope, is_global in (("project", False), ("global", True)):
        p = _settings_path(is_global)
        ours = _read_our_hooks(p)
        row: dict[str, Any] = {"harness": "claude-code", "scope": scope, "path": str(p),
                               "installed": bool(ours)}
        if ours:
            cmd = ours[0][1]["command"]
            surf = hook_surfaces(p)
            row.update(command=cmd, runs=_command_runs(cmd), pin=hook_pin(cmd) or "binary",
                       bash=surf["bash"], mcp=surf["mcp"])
        rows.append(row)
    adapter_rows = _adapter_rows()
    rows += adapter_rows

    families: dict[str, list[str]] = {}
    for rule in MCP_RULES:
        families.setdefault(rule.family, []).extend(rule.names)
    n_tools = sum(len(v) for v in families.values())

    covered: list[str] = []
    gaps: list[str] = []
    todo: dict[str, list[str]] = {}    # one line per command, however many reasons

    def fix(cmd: str, why: str = "") -> None:
        todo.setdefault(cmd, [])
        if why:
            todo[cmd].append(why)
    live = [r for r in rows if r["harness"] == "claude-code" and r.get("runs")]
    if any(r.get("bash") for r in live):
        covered.append("Claude Code: Bash commands")
    if any(r.get("mcp") for r in live):
        covered.append(f"Claude Code: MCP tool calls ({n_tools} recognised tools: "
                       + ", ".join(_FAMILY_LABELS.get(f, f) for f in sorted(families)) + ")")
    if not live:
        gaps.append("Claude Code: no working guard hook")
        fix("nable guard install", "this project; add --global for every project")
    for r in rows:
        flag = " --global" if r["scope"] == "global" else ""
        if r["harness"] != "claude-code" or not r["installed"]:
            continue
        if not r["runs"]:
            gaps.append(f"Claude Code ({r['scope']}): the hooked command no longer exists")
            fix(f"nable guard install{flag}", "repairs the dead hook in place")
            continue
        if not r.get("mcp") and not any(x.get("mcp") for x in live):
            # Claude Code runs project and user hooks alike, so one scope that
            # sees MCP covers it; only a machine where none does has a gap.
            gaps.append(f"Claude Code ({r['scope']}): MCP tool calls (the hook only sees Bash)")
            fix(f"nable guard install{flag}", "widens the hook to MCP tools")
        if r.get("pin") == "unpinned":
            fix(f"nable guard install{flag}", "pins the hook to this release instead of "
                "the newest PyPI release on every call")

    for name, (label, what) in _ADAPTER_SURFACES.items():
        mine = [r for r in adapter_rows if r["harness"] == name]
        if any(r["runs"] for r in mine):
            covered.append(f"{label}: {what}")
            continue
        present = any(r["present"] for r in mine) or _harness_present(name)
        if present:
            if adapter_rows:
                gaps.append(f"{label}: on this machine, no working guard hook")
                fix(f"nable guard install --harness {name}")
            else:
                gaps.append(f"{label}: on this machine, and this nable has no hook for it")
    gaps.append("commands inside scripts the agent runs (the guard sees `bash deploy.sh`, "
                "not what is in it)")
    gaps.append("MCP servers outside the recognised table (the guard stays silent on them)")

    ledger = guard_ledger.verify()
    if not ledger["ok"]:
        fix("nable guard verify-log", f"the decision ledger breaks at line "
            f"{ledger.get('broken_at')}: a record was edited, removed, reordered or torn")
    fixes = [f"{cmd}  ({'; '.join(why)})" if why else cmd for cmd, why in todo.items()]
    fixes.append("give agents read-only cloud credentials; keep write access behind a human")

    return {
        "ok": bool(live) and ledger["ok"],
        "surfaces": rows,
        "covered": covered,
        "not_covered": gaps,
        "mcp_tools": families,
        "ledger": ledger,
        "recommendations": fixes,
        "seatbelt": SEATBELT,
        "version": __version__,
    }


def _harness_present(name: str) -> bool:
    """Is this agent installed here at all? Its user config directory is the
    signal guard_adapters uses too (CODEX_HOME for Codex)."""
    if name == "codex":
        return Path(os.getenv("CODEX_HOME") or Path.home() / ".codex").is_dir()
    return (Path.home() / f".{name}").is_dir()
