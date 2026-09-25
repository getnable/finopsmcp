"""Seamless agent cost guardrail: a PreToolUse hook for AI coding agents.

`finops guard install` wires nable's advisory policy gate (policy.py) into
Claude Code so it runs automatically whenever an agent is about to execute an
infrastructure-mutating shell command (terraform destroy, kubectl delete,
aws ec2 terminate-instances, a commitment purchase, ...) or make the same
change through an MCP tool (HashiCorp Terraform, AWS API, Kubernetes servers;
the table is guard_mcp.py). The agent no longer has to remember to call
check_action_policy; the harness enforces the check.

Public entry points, for any harness adapter (guard_adapters.py for Cursor,
Codex, GitHub Copilot, Gemini CLI and Cline; run_hook below for Claude Code):
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

The cloud budget (budget_lens): a priced change that adds cost is also
checked against the budgets the user set (`set_budget`, budget.yml). When
month-to-date spend plus the change's cost for the rest of the period is over
a budget that applies to it, the policy gate gets cost_verdict "over_budget"
and the answer is "ask", or "deny" when the policy's on_budget_breach is
"deny" (nable.policy.yaml) or FINOPS_GUARD_STOP_ON_BUDGET=1; the env var wins
either way. Spend comes from the summary the budget checks write
(budget/summary.py), not the database; a summary older than 48 hours (or from
last month) is not used, and a verdict on a priced change says the budget went
unchecked.

History (the recent end of the decision ledger, guard_ledger.recent) can
turn an allow or a warn into an ask, never anything else:
  velocity cap   the priced monthly run-rate the guard let through in a
                 rolling window (60 min; 4x the per-action threshold unless
                 FINOPS_POLICY_VELOCITY_CAP_USD says otherwise), plus this
                 action, is over the cap
  loop detection the same creation (same verb, instance type, count,
                 template; local inputs unchanged) let through N-1 times in
                 M minutes already (3 in 10 by default): "this looks like a
                 retry loop"

The hook never executes anything itself and it fails open: any internal error
exits 0 so a guard bug can never break the user's agent. It answers before
it records (answer_first), waits at most 200 ms on the ledger's lock, and
asks rather than judges a command over 256 KB, so neither the ledger file
nor a padded command can run it past the harness timeout.

Strict mode (FINOPS_GUARD_STRICT=1) additionally asks on reversible
mutations (terraform apply, helm upgrade, kubectl apply/scale,
aws ec2 run-instances) with a nudge to cost the change first.

What happened afterwards (setup_wizard's `nable guard ...`):
  report [--session ID]   what it asked, blocked and let through, in dollars,
                          overall and per agent session
  verify-log              the hash chain, plus an anchor that notices records
                          cut from the end
  reconcile [--hours N] [--region R ...]
                          CloudTrail's creates, destroys and commitments
                          against the ledger (guard_reconcile.py)
  export [--since ...] [--format jsonl|cef]
                          the verified ledger for a SIEM, each record with its
                          chain hash
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

# The end of a verb or flag: whitespace, the end, or a shell operator right
# after it (`terraform destroy;echo`), never more word (destroy.tfplan).
_END = r"(?![^\s;&|)`])"

_ONE_WAY_CLASSIFIERS: list[tuple[str, str]] = [
    # `destroy` must end the word: a plan file called destroy.tfplan is a
    # file name, and `terraform apply destroy.tfplan` goes to the saved-plan
    # reader like any other apply. `-out destroy` names a file too.
    (rf"\bterraform\s+(?:\S+\s+)*destroy(?<!-out destroy){_END}", "delete_resource"),
    (rf"\btofu\s+(?:\S+\s+)*destroy(?<!-out destroy){_END}", "delete_resource"),
    # Terragrunt wraps terraform and fans out: `terragrunt run-all destroy` (or
    # `run --all destroy`, or the older `destroy-all`) tears down every module
    # under the directory in one command. It had no pattern at all, so the
    # widest destroy in the toolchain was the one the guard could not see.
    (rf"\bterragrunt\s+(?:\S+\s+)*destroy(?<!-out destroy)(?:-all)?{_END}",
     "delete_resource"),
    # destroy hidden behind the apply verb: `terraform apply -destroy` is destroy.
    # Must sit in the one-way list (checked first) or the two-way apply pattern
    # would classify it as a reversible mutation.
    ("apply-with-destroy-flag", "delete_resource"),
    # The same flag passed through terraform's own env hook:
    # `TF_CLI_ARGS_apply=-destroy terraform apply` is a destroy the apply
    # pattern below would otherwise wave through as a reversible mutation.
    ("tf-cli-args-destroy", "delete_resource"),
    # A workspace delete drops the workspace's state: whatever it managed is
    # orphaned, still running and still billed, with nothing left to destroy it.
    (rf"\b(?:terraform|tofu)\s+(?:\S+\s+)*workspace\s+delete{_END}", "delete_resource"),
    # `pulumi down` is pulumi's own alias for destroy.
    (rf"\bpulumi\s+(?:\S+\s+)*(?:destroy|down){_END}", "delete_resource"),
    # The rest of the IaC toolchain: AWS CDK (also as `npx cdk`), SAM, doctl.
    (rf"\bcdk\s+(?:\S+\s+)*destroy{_END}", "delete_resource"),
    (rf"\bsam\s+(?:\S+\s+)*delete{_END}", "delete_resource"),
    (rf"\bdoctl\s+(?:\S+\s+)*(?:delete|rm){_END}", "delete_resource"),
    (r"\beksctl\s+delete\b", "delete_resource"),
    # bucket/object wipes: `aws s3 rb` removes a bucket, `aws s3 rm --recursive`
    # empties one; gsutil is the GCP equivalent. Data deletion is a one-way door.
    (r"\baws\s+s3\s+r[mb]\b", "delete_resource"),
    # `aws s3 sync --delete` removes whatever the source does not have: synced
    # from an empty directory, it empties the bucket.
    ("s3-sync-delete", "delete_resource"),
    # Anchored at a token start, not \b: `-gsutil -gsutil ...` would otherwise
    # give every token a start and every start the whole run to scan.
    (r"(?<![\w-])gsutil\s+(?:-\S+\s+)*+r[mb]\b", "delete_resource"),
    # Helm's own aliases for uninstall are del, delete and un; flags such as
    # `-n prod` may come first.
    (rf"\bhelm\s+(?:\S+\s+)*(?:uninstall|delete|del|un){_END}", "delete_resource"),
    (rf"\bkubectl\s+(?:\S+\s+)*delete{_END}", "delete_resource"),
    # `kubectl drain` evicts every pod on the node; `replace --force` deletes
    # the object and creates it again, dropping whatever the old one held.
    (rf"\bkubectl\s+(?:\S+\s+)*drain{_END}", "delete_resource"),
    ("kubectl-replace-force", "delete_resource"),
    (r"\baws\s+ec2\s+terminate-instances\b", "terminate_instance"),
    ("spot-fleet-terminate", "terminate_instance"),
    (r"\baws\s+ec2\s+release-address\b", "release_ip"),
    (r"\baws\s+ec2\s+delete-snapshot\b", "snapshot_delete"),
    (r"\baws\s+(?:savingsplans\s+create-savings-plan|"
     r"ec2\s+purchase-reserved-instances-offering|"
     r"ec2\s+purchase-host-reservation|"
     r"rds\s+purchase-reserved-db-instances-offering)", "purchase_commitment"),
    # delete-*, and the batch forms (ecr batch-delete-image, dynamodb
    # batch-delete-item) that delete many at once.
    (r"\baws\s+\S+\s+(?:batch-)?delete-[a-z0-9-]+", "delete_resource"),
    # Deletes AWS does not spell delete-*: a KMS key scheduled for deletion
    # takes every byte encrypted under it along, an AMI deregistered cannot be
    # launched again, a closed account is gone with everything in it.
    (r"\baws\s+kms\s+schedule-key-deletion\b", "delete_resource"),
    (r"\baws\s+\S+\s+deregister-[a-z0-9-]+", "delete_resource"),
    (r"\baws\s+organizations\s+close-account\b", "delete_resource"),
    (r"\bgcloud\s+(?:\S+\s+)*delete\b", "delete_resource"),
    (r"\baz\s+(?:\S+\s+)*delete\b", "delete_resource"),
    # Heuristics. These cannot see what will actually run, so they only ever
    # ask: a Python one-liner that imports boto3 and calls a delete or a
    # terminate, and a base64 payload decoded straight into a shell.
    ("python-boto3-delete", "delete_resource"),
    ("base64-to-shell", "delete_resource"),
]

_TWO_WAY_CLASSIFIERS: list[tuple[str, str]] = [
    (r"\baws\s+ec2\s+stop-instances\b", "stop_idle"),
    (r"\bterraform\s+(?:\S+\s+)*apply\b", "infra_apply"),
    (r"\btofu\s+(?:\S+\s+)*apply\b", "infra_apply"),
    (r"\bterragrunt\s+(?:\S+\s+)*apply\b", "infra_apply"),
    (rf"\bhelm\s+(?:\S+\s+)*(?:install|upgrade){_END}", "infra_apply"),
    (rf"\bkubectl\s+(?:\S+\s+)*(?:apply|scale){_END}", "infra_apply"),
    (r"\baws\s+ec2\s+run-instances\b", "infra_apply"),
    # CloudFormation creates whatever the template holds, and an agent that
    # re-runs create-stack under a new name each time makes a new copy each
    # time. Unpriced: the template is not read here.
    (r"\baws\s+cloudformation\s+(?:create-stack|update-stack|deploy)\b", "infra_apply"),
    # Launches the pricers below can put a figure on. Unclassified, they could
    # never reach the policy's dollar threshold however large they were.
    (r"\baws\s+rds\s+create-db-instance\b", "infra_apply"),
    # Changes that resize what is already running, or launch capacity by other
    # verbs than run-instances. Reversible, but a class change to
    # db.r6g.16xlarge or a desired capacity of 100 is a bill like any launch.
    ("rds-class-change", "infra_apply"),
    ("ec2-type-change", "infra_apply"),
    (rf"\baws\s+ec2\s+(?:request-spot-instances|request-spot-fleet|create-fleet){_END}",
     "infra_apply"),
    (rf"\baws\s+autoscaling\s+(?:set-desired-capacity|create-auto-scaling-group){_END}",
     "infra_apply"),
    ("asg-capacity-change", "infra_apply"),
    (rf"\baws\s+eks\s+create-nodegroup{_END}", "infra_apply"),
    ("eks-nodegroup-scaling", "infra_apply"),
    (r"\bgcloud\s+(?:\S+\s+)*compute\s+instances\s+create\b", "infra_apply"),
    (r"\baz\s+(?:\S+\s+)*vm\s+create\b", "infra_apply"),
]


# AWS global options may sit anywhere in an aws command: between `aws` and the
# service name (`aws --profile prod ec2 terminate-instances`) or after it
# (`aws ec2 --region us-west-2 terminate-instances`). The aws entries in the
# tables above are anchored on `aws\s+ec2\s+terminate-instances`, so either
# placement used to make a terminate, a bucket wipe or a commitment purchase
# invisible to the guard, and the hook stayed silent on a one-way door. A
# profile flag is not an exotic input; it is what anyone with more than one
# account types by default.
#
# Rather than widen every pattern (and every future one) this strips the global
# options from each aws command first, so the tables stay readable and a new
# aws rule cannot forget.
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
_AWS_GLOBAL_OPT_RE = re.compile(
    rf" --(?:{_AWS_GLOBAL_WITH_VALUE})(?:=\S*| \S+)(?= |$)"
    rf"| --(?:{_AWS_GLOBAL_BOOLEAN})(?= |$)"
)
# One aws command: from `aws` to the end of its shell segment.
_AWS_SEGMENT_RE = re.compile(r"\baws [^;&|]*")


def _strip_aws_global_options(cmd: str) -> str:
    """`aws --profile p ec2 --region r terminate-instances` -> `aws ec2 terminate-instances`.

    Expects whitespace already collapsed to single spaces (see _normalize).
    Each aws segment is scanned once, so this stays linear however many
    there are."""
    if "--" not in cmd:
        return cmd
    return _AWS_SEGMENT_RE.sub(lambda m: _AWS_GLOBAL_OPT_RE.sub("", m.group(0)), cmd)


# The programs the tables above name. A case-insensitive filesystem (macOS's
# default, Windows) runs `TERRAFORM destroy` as terraform, so the program name
# is matched without regard to case; the verb after it is not, because the
# program itself rejects `terraform DESTROY`.
_PROGRAMS = ("aws|kubectl|terraform|tofu|terragrunt|helm|pulumi|gcloud|az|cdk|sam|doctl|"
             "eksctl|gsutil|base64|python3?")
_PROGRAM_RE = re.compile(rf"(?:{_PROGRAMS})(?![\w.-])")


def _lower_programs(cmd: str) -> str:
    """`TERRAFORM destroy` -> `terraform destroy`; every other word as written.

    Program names are found in a lowercased copy (one case-sensitive scan,
    much cheaper than an IGNORECASE one) and copied back where they differ."""
    low = cmd.lower()
    if low == cmd or len(low) != len(cmd):
        return cmd
    out, last = [], 0
    for m in _PROGRAM_RE.finditer(low):
        a, b = m.span()
        if (a == 0 or not (low[a - 1].isalnum() or low[a - 1] in "_.-")) and cmd[a:b] != low[a:b]:
            out += (cmd[last:a], low[a:b])
            last = b
    return "".join(out) + cmd[last:] if out else cmd

# `alias tf=terraform; tf destroy`: an alias defined on the same line is
# expanded where it is used later on that line. Only the first few aliases are
# expanded, so a command made of ten thousand of them stays linear.
_ALIAS_RE = re.compile(r"alias(?<![\w-]alias) ([\w.-]+)=([^\s;&|]+)")
_ALIAS_MAX = 4


def _expand_aliases(cmd: str) -> str:
    if "alias " not in cmd:
        return cmd
    pos = 0
    for _ in range(_ALIAS_MAX):
        m = _ALIAS_RE.search(cmd, pos)
        if m is None:
            break
        name, value = m.group(1), m.group(2)
        esc = re.escape(name)
        use = re.compile(rf"{esc}(?<![\w/.=-]{esc})(?![\w.=-])")
        cmd = cmd[:m.end()] + use.sub(lambda _m, v=value: v, cmd[m.end():])
        pos = m.end()
    return cmd


# Programs whose quoted arguments are data, not commands: a commit message or
# a search pattern that mentions `terraform destroy` asked the human to
# confirm a destroy nobody was running, and a guard that cries wolf on every
# docs commit gets uninstalled. `bash -c`, `sh -c` and `eval` are not here:
# their quoted argument is a command.
_DATA_PROGRAM_RE = re.compile(r"(?:echo|printf|grep|rg|ag|git)(?![\w.-])")
_DATA_SEGMENT_RE = re.compile(
    r"\s*(?:(?:[A-Za-z_]\w*=\S*|sudo|command|time|nohup)\s+)*(?:\S*/)?"
    r"(?:(?:echo|printf|grep|egrep|fgrep|rg|ag)(?!\S)"
    r"|git(?:\s+-\S+(?:\s+[^\s-]\S*)?)*?\s+(?:commit|tag)(?!\S))")
# A quoted string (single, or double with escapes) or a shell operator.
_QUOTE_OR_OP_RE = re.compile(r"'[^']*+'|\"(?:[^\"\\]++|\\.)*+\"|&&|\|\||[;&|\n]")
# Data that is then run: `printf "terraform destroy" | sh`, `| xargs ...`,
# `$(...)`, backticks. Masking is off for the whole command when any of these
# appears; over-matching is the safe side.
_RUNS_DATA_RE = re.compile(
    r"\|\s*(?:sudo\s+)?(?:\S*/)?(?:(?:ba|z|da|k)?sh|xargs|source|eval|\.)(?!\S)|\$\(|`|<\(")
_MASK_MAX_TOKENS = 20_000


def _mask_quoted_data(command: str) -> str:
    """`git commit -m "docs: terraform destroy"` -> `git commit -m ""`.

    Quoted arguments of echo, printf, grep, rg, ag and `git commit|tag` are
    blanked, one shell segment at a time, so what follows a `;` or `&&` is
    still judged. Past _MASK_MAX_TOKENS quotes and operators the command is
    left as it is: bounded work, and over-matching is the safe side."""
    if ('"' not in command and "'" not in command) or not _DATA_PROGRAM_RE.search(command) \
            or _RUNS_DATA_RE.search(command):
        return command
    out: list[str] = []
    last = seg_start = 0
    data: bool | None = None
    for n, m in enumerate(_QUOTE_OR_OP_RE.finditer(command)):
        if n >= _MASK_MAX_TOKENS:
            return command
        tok = m.group(0)
        if tok[0] not in "'\"":
            seg_start, data = m.end(), None
            continue
        if data is None:
            data = _DATA_SEGMENT_RE.match(command, seg_start, m.start()) is not None
        if data:
            out += (command[last:m.start()], '""')
            last = m.end()
    return "".join(out) + command[last:] if out else command


def _normalize(command: str) -> str:
    """The form every classifier and pricer reads.

    Quotes do not change which program runs: `"aws" ec2 terminate-instances`
    is a terminate. Dropping them can only over-match, which is the safe side.
    Classification and pricing must read the SAME form: when only the
    classifier stripped AWS global options, `aws --region us-east-1 ec2
    run-instances --instance-type p4d.24xlarge --count 8` classified as a
    launch, found no price, and passed silently at six figures a month."""
    cmd = _mask_quoted_data(command)
    cmd = cmd.replace('"', "").replace("'", "")
    cmd = " ".join(cmd.split())  # normalize whitespace
    cmd = _lower_programs(cmd)
    cmd = _expand_aliases(cmd)
    return _strip_aws_global_options(cmd)


# ── Linear-time matching ──────────────────────────────────────────────────────
# The command is whatever the agent sends, and a hook that runs past the
# harness's timeout fails open: `terraform destroy # AAAA...` padded to 100 KB
# used to take the hook 18 s, and the destroy ran. So no pattern may cost more
# than linear time in the command, however it is padded.
#
# The costly shape is "program, any tokens, verb" (`terraform (?:\S+\s+)*
# destroy`) searched from every occurrence of the program: each failed start
# rescans the rest of the command. But any token the pattern could reach from
# a later occurrence it can also reach from the first one (the later program
# name is itself just a token to skip), so only the first start needs trying.
# And from there, "any tokens, then the verb" is "the verb at any token start
# after the program": one forward scan, no backtracking through the tokens.
_ANY_TOKENS = r"(?:\S+\s+)*"

# Linear is not the whole budget: every rule scans the command once, and a
# pattern that opens with an assertion (`\bterraform`, `(?<!\S)destroy`) makes
# Python's re try every position in turn, about ten times slower than a
# pattern that opens with its literal word and can skip ahead to it. So the
# leading assertion is moved behind the word: `\bterraform` is compiled as
# `terraform(?<=\bterraform)`, which matches exactly the same text.
_LEADING_ASSERTION_RE = re.compile(
    r"(\\b|\(\?<!\\S\)|\(\?<!\[[^\]]+\]\))([A-Za-z0-9_]+)(?![*+?{])")


def _fast(pattern: str) -> re.Pattern[str]:
    m = _LEADING_ASSERTION_RE.match(pattern)
    if m:
        guard, word = m.groups()
        behind = (f"(?<=\\b{word})" if guard == "\\b"
                  else f"{guard[:-1]}{word})")      # (?<!X) -> (?<!Xword)
        pattern = f"{word}{behind}{pattern[m.end():]}"
    return re.compile(pattern)


class _Rule:
    """A compiled classifier pattern with search() linear in the command.

    For a pattern HEAD + _ANY_TOKENS + TAIL, search() returns the TAIL match
    (its end() is where the whole pattern would end), or None."""

    def __init__(self, pattern: str) -> None:
        self.pattern = pattern
        head, sep, tail = pattern.partition(_ANY_TOKENS)
        if sep:
            self.head: re.Pattern[str] | None = _fast(head)
            self.tail = _fast(r"(?<!\S)" + tail)
        else:
            self.head, self.tail = None, _fast(pattern)

    def search(self, cmd: str) -> re.Match[str] | None:
        if self.head is None:
            return self.tail.search(cmd)
        m = self.head.search(cmd)
        return self.tail.search(cmd, m.end()) if m else None


class _VerbWithFlag:
    """PROGRAM ... VERB ... FLAG within one shell segment, e.g. `terraform
    apply -destroy` (destroy hidden behind the apply verb) or `aws s3 sync
    --delete`. Checked segment by segment, so each character is looked at a
    bounded number of times."""

    def __init__(self, pattern: str, tool: str, verb: str, flag: str,
                 flag_anywhere: bool = False) -> None:
        self.pattern = pattern
        self._tool = _fast(tool)
        self._verb = _fast(verb)
        self._flag = _fast(flag)
        # The flag may also sit between the program and the verb.
        self._flag_anywhere = flag_anywhere

    def search(self, cmd: str) -> re.Match[str] | None:
        for seg in re.split(r"[|;&]", cmd):
            tool = self._tool.search(seg)
            if tool is None:
                continue
            verb = self._verb.search(seg, tool.end())
            if verb is not None:
                flag = self._flag.search(seg, tool.end() if self._flag_anywhere else verb.end())
                if flag is not None:
                    return flag
        return None


class _TfCliArgsDestroy:
    """`TF_CLI_ARGS[_cmd]=...-destroy`: the flag passed through terraform's
    env hook. One scan per assignment token, never one per character."""

    pattern = "tf-cli-args-destroy"
    _assign = re.compile(r"\bTF_CLI_ARGS(?:_\w+)?=\S*")
    _flag = re.compile(r"--?destroy\b")

    def search(self, cmd: str) -> re.Match[str] | None:
        for m in self._assign.finditer(cmd):
            if self._flag.search(m.group(0)):
                return m
        return None


class _PythonBoto3Delete:
    """`python3 -c "import boto3; ...terminate_instances(...)"`: a one-liner
    that deletes through the SDK instead of the CLI. A heuristic, so it only
    ever asks. The code is not one shell segment (it has its own `;`), so the
    rest of the command after the first `python -c` is what is searched: any
    later `python -c` is inside that rest already."""

    pattern = "python-boto3-delete"
    _python = re.compile(r"(?<![\w.-])python(?:3(?:\.\d+)?)?(?: -\S+)*? -c ")
    _boto3 = re.compile(r"\bboto3\b")
    _delete = re.compile(r"\b(?:delete|terminate)_\w+|\.(?:delete|terminate)\(")

    def search(self, cmd: str) -> re.Match[str] | None:
        py = self._python.search(cmd)
        if py is None or self._boto3.search(cmd, py.end()) is None:
            return None
        return self._delete.search(cmd, py.end())


class _Base64ToShell:
    """`... | base64 -d | sh`: a script decoded straight into a shell, so the
    guard never sees the command. A heuristic, so it only ever asks."""

    pattern = "base64-to-shell"
    _decode = re.compile(r"\bbase64 [^;&|]*?(?<!\S)(?:-[A-Za-z]*[dD][A-Za-z]*|--decode)(?!\S)")
    _to_shell = re.compile(r"\| ?(?:sudo )?(?:\S*/)?(?:sh|bash|zsh|dash|ksh)(?!\S)")

    def search(self, cmd: str) -> re.Match[str] | None:
        decode = self._decode.search(cmd)
        return self._to_shell.search(cmd, decode.end()) if decode else None


_SPECIAL_RULES = {r.pattern: r for r in (
    _VerbWithFlag("apply-with-destroy-flag", r"\b(?:terraform|tofu|terragrunt)\s",
                  rf"(?<!\S)apply{_END}", rf"\s--?destroy(?:=(?i:1|t|true))?{_END}",
                  flag_anywhere=True),
    _VerbWithFlag("s3-sync-delete", rf"\baws\s+s3\s+sync{_END}", r"", rf"\s--delete{_END}"),
    _VerbWithFlag("kubectl-replace-force", r"\bkubectl\s", rf"(?<!\S)replace{_END}",
                  rf"\s--force(?:=true)?{_END}", flag_anywhere=True),
    _VerbWithFlag("spot-fleet-terminate", rf"\baws\s+ec2\s+cancel-spot-fleet-requests{_END}",
                  r"", rf"\s--terminate-instances{_END}"),
    _VerbWithFlag("rds-class-change", rf"\baws\s+rds\s+modify-db-instance{_END}", r"",
                  r"\s--(?:db-instance-class|multi-az)(?![^\s=])"),
    _VerbWithFlag("ec2-type-change", rf"\baws\s+ec2\s+modify-instance-attribute{_END}", r"",
                  rf"\s--instance-type(?![^\s=])|\s--attribute[\s=]instanceType{_END}"),
    _VerbWithFlag("asg-capacity-change",
                  rf"\baws\s+autoscaling\s+update-auto-scaling-group{_END}", r"",
                  r"\s--(?:desired-capacity|min-size|max-size)(?![^\s=])"),
    _VerbWithFlag("eks-nodegroup-scaling", rf"\baws\s+eks\s+update-nodegroup-config{_END}",
                  r"", r"\s--scaling-config(?![^\s=])"),
    _TfCliArgsDestroy(), _PythonBoto3Delete(), _Base64ToShell(),
)}


def _compile(table: list[tuple[str, str]]) -> list[tuple[Any, str]]:
    return [(_SPECIAL_RULES.get(p) or _Rule(p), a) for p, a in table]


_ONE_WAY_RULES = _compile(_ONE_WAY_CLASSIFIERS)
_TWO_WAY_RULES = _compile(_TWO_WAY_CLASSIFIERS)


def classify_command(command: str) -> tuple[str, str] | None:
    """Classify a shell command as ("one_way"|"two_way", action_type), or None
    when it is not an infrastructure mutation nable cares about. Linear in the
    length of the command (see _Rule)."""
    cmd = _normalize(command)
    for rule, action in _ONE_WAY_RULES:
        if rule.search(cmd):
            return ("one_way", action)
    for rule, action in _TWO_WAY_RULES:
        if rule.search(cmd):
            return ("two_way", action)
    return None


def _strict() -> bool:
    return os.getenv("FINOPS_GUARD_STRICT", "").strip().lower() in ("1", "true", "yes")


# ── Command cost estimation ────────────────────────────────────────────────────
# The gap this closes: `aws ec2 run-instances --instance-type p4d.24xlarge
# --count 8` (six figures a month at list) classified as a reversible
# in-policy mutation and the
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
# `--instance-type m5.large`, and the AttributeValue forms
# modify-instance-attribute takes: `Value=m5.large`, `{"Value": "m5.large"}`.
_INSTANCE_TYPE_RE = re.compile(
    r"--instance-type[=\s]+(?:\{?\s*Value\s*[=:]\s*)?([a-z0-9]+\.[a-z0-9]+)")
# An instance type inside a JSON or shorthand structure (a launch
# specification, fleet overrides), quotes already stripped.
_STRUCT_TYPE_RE = re.compile(r"\bInstanceType\s*[=:]\s*([a-z0-9]+\.[a-z0-9]+)")
# `--count 8` or the min:max form `--count 2:8`; price the max, because the
# guard's job is the ceiling a human is about to authorise, not the floor.
_COUNT_RE = re.compile(r"--count[=\s]+(\d+)(?::(\d+))?")
_RDS_CREATE_RE = re.compile(r"\baws\s+rds\s+create-db-instance\b")
_RDS_MODIFY_RE = re.compile(r"\baws\s+rds\s+modify-db-instance\b")
_EC2_MODIFY_RE = re.compile(r"\baws\s+ec2\s+modify-instance-attribute\b")
_SPOT_RE = re.compile(r"\baws\s+ec2\s+request-spot-instances\b")
_FLEET_RE = re.compile(r"\baws\s+ec2\s+create-fleet\b")
_NODEGROUP_CREATE_RE = re.compile(r"\baws\s+eks\s+create-nodegroup\b")
_SAVINGS_PLAN_RE = re.compile(r"\baws\s+savingsplans\s+create-savings-plan\b")
_RESERVED_RE = re.compile(r"\baws\s+ec2\s+purchase-reserved-instances-offering\b")
# JSON ({"Amount": 1200, ...}, quotes already stripped) and shorthand
# (Amount=1200,CurrencyCode=USD) spell the same thing.
_LIMIT_AMOUNT_RE = re.compile(r"--limit-price[=\s]+\S{0,200}?Amount\W{1,3}([\d.]+)")
_GCE_CREATE_RE = _Rule(r"\bgcloud\s+(?:\S+\s+)*compute\s+instances\s+create\b(?!-)")
_AZ_VM_CREATE_RE = _Rule(r"\baz\s+(?:\S+\s+)*vm\s+create\b")
_SHELL_BREAKS = ("&&", "||", ";", "|")

# The engines aws_prices.rds_hourly has rates for. Aurora bills per cluster
# instance at other rates and SQL Server, Oracle and Db2 carry
# licence-included rates the tables do not hold: those get no figure, not a
# MySQL price.
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


def _price_ec2(itype: str | None, count: int, *, basis: str = _ON_DEMAND_BASIS,
               lead: str = "") -> dict[str, Any] | None:
    """`count` instances of `itype` at the EC2 table's rate, or None."""
    if not itype:
        return None
    from .aws_prices import EC2_HOURLY
    hourly = EC2_HOURLY.get(itype)
    if not hourly:
        return None
    count = max(count, 1)
    monthly = hourly * count * _hours_per_month()
    return {
        "monthly_usd": round(monthly, 2),
        "hourly_usd": hourly,
        "instance_type": itype,
        "count": count,
        "basis": basis,
        "line": (f"{lead}{count}x {itype} at {_rate(hourly)}/hr ({basis}) "
                 f"is ~${monthly:,.0f}/mo"),
    }


def _int_flag(cmd: str, name: str) -> int:
    return int(_num(_flag(cmd, name)) or 0)


def _price_run_instances(cmd: str, **_: Any) -> dict[str, Any] | None:
    m = _INSTANCE_TYPE_RE.search(cmd)
    if not m:
        return None
    # It launches up to --max-count (or the max of `--count min:max`), so the
    # max is what a human is authorising; --min-count alone is the count.
    count = _int_flag(cmd, "max-count") or _int_flag(cmd, "min-count")
    cm = _COUNT_RE.search(cmd)
    if cm:
        count = max(count, int(cm.group(1)), int(cm.group(2) or 0))
    return _price_ec2(m.group(1), count or 1)


def _price_instance_type_change(cmd: str, **_: Any) -> dict[str, Any] | None:
    """modify-instance-attribute to a new type: the new type's full rate. The
    current type is not in the command, so nothing is subtracted, and the
    basis says so."""
    m = _INSTANCE_TYPE_RE.search(cmd)
    itype = m.group(1) if m else None
    if itype is None and re.search(r"--attribute[=\s]instanceType(?!\S)", cmd):
        itype = _flag(cmd, "value")
    return _price_ec2(itype, 1, lead="resized to ",
                      basis=f"{_ON_DEMAND_BASIS}, before subtracting the current type, "
                            "which the command does not name")


_SPOT_BASIS = (f"the {_ON_DEMAND_BASIS} as a ceiling; spot prices vary with demand and are "
               "usually well below it, so this is an estimate, not a quote")


def _price_spot(cmd: str, **_: Any) -> dict[str, Any] | None:
    m = _STRUCT_TYPE_RE.search(cmd)
    return _price_ec2(m.group(1) if m else None, _int_flag(cmd, "instance-count") or 1,
                      basis=_SPOT_BASIS, lead="spot request for ")


def _price_fleet(cmd: str, **_: Any) -> dict[str, Any] | None:
    """create-fleet with its capacity and ONE instance type on the command
    line. A fleet over several types launches whichever mix it can get, so
    that gets no figure rather than a guessed one."""
    types = set(_STRUCT_TYPE_RE.findall(cmd))
    cap = re.search(r"\bTotalTargetCapacity\s*[=:]\s*(\d+)", cmd)
    if len(types) != 1 or not cap:
        return None
    spot = re.search(r"\bDefaultTargetCapacityType\s*[=:]\s*spot\b", cmd)
    return _price_ec2(types.pop(), int(cap.group(1)), lead="fleet of ",
                      basis=_SPOT_BASIS if spot else _ON_DEMAND_BASIS)


def _price_nodegroup(cmd: str, **_: Any) -> dict[str, Any] | None:
    """create-nodegroup at its desired size, the nodes it starts with."""
    types = _flag(cmd, "instance-types")
    desired = re.search(r"\bdesiredSize\s*[=:]\s*(\d+)", cmd)
    if not types or not desired:
        return None
    most = re.search(r"\bmaxSize\s*[=:]\s*(\d+)", cmd)
    basis = _ON_DEMAND_BASIS + (f"; the group may scale to {most.group(1)} nodes"
                                if most and most.group(1) != desired.group(1) else "")
    return _price_ec2(types.split(",")[0], int(desired.group(1)), basis=basis,
                      lead="a node group of ")


def _price_rds(cmd: str, **_: Any) -> dict[str, Any] | None:
    cls = _flag(cmd, "db-instance-class")
    engine = (_flag(cmd, "engine") or "").lower()
    if not cls or engine not in _RDS_TABLE_ENGINES:
        return None
    from .aws_prices import rds_hourly
    hourly = rds_hourly(cls, engine)
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


def _price_rds_class_change(cmd: str, **_: Any) -> dict[str, Any] | None:
    """modify-db-instance to a new class: the new class's full rate. Neither
    the engine nor the current class is in the command, so the figure is the
    higher of the MySQL/MariaDB and PostgreSQL rates for the new class (the
    ceiling a human is authorising), nothing subtracted, and the basis says
    both."""
    cls = _flag(cmd, "db-instance-class")
    if not cls:
        return None
    from .aws_prices import rds_hourly
    rates = {"PostgreSQL": rds_hourly(cls, "postgres") or 0.0,
             "MySQL/MariaDB": rds_hourly(cls, "mysql") or 0.0}
    engine, hourly = max(rates.items(), key=lambda kv: kv[1])
    if not hourly:
        return None
    if len(set(rates.values())) == 1:
        engine = "MySQL, MariaDB and PostgreSQL alike"
    multi_az = _has_flag(cmd, "multi-az")
    monthly = hourly * (2 if multi_az else 1) * _hours_per_month()
    basis = (f"{_ON_DEMAND_BASIS}, the {engine} rate (the engine is not in the command), "
             "instance hours only, before subtracting the current class")
    return {
        "monthly_usd": round(monthly, 2),
        "hourly_usd": hourly,
        "instance_type": cls,
        "count": 2 if multi_az else 1,
        "basis": basis,
        "line": (f"resized to {cls}{' Multi-AZ' if multi_az else ''} at {_rate(hourly)}/hr"
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


# A plan file's `show -json` document, or why it could not be read.
_PLAN_CACHE: dict[tuple[str, float], dict[str, Any] | str] = {}


def _plan_read(cmd: str, cwd: str | None) -> tuple[str, str, dict[str, Any] | str] | None:
    """(tool, plan file as written, `show -json` document or the reason it
    could not be read) for `terraform|tofu apply <planfile>`, or None when
    there is no plan file to read (a plain apply, a file that is not there).

    Read once per plan file per process: pricing, the destroy check and the
    unreadable check all need it, and the hook must not pay for `show` twice."""
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
        if not plan_path.is_file():
            return None
        key = (str(plan_path.resolve()), plan_path.stat().st_mtime)
    except OSError:
        return None
    if key not in _PLAN_CACHE:
        name = (os.environ.get("TERRAFORM_BIN") or "terraform") if tool == "terraform" else "tofu"
        exe = shutil.which(name)
        why: dict[str, Any] | str
        if not exe:
            why = f"{name} is not on PATH"
        else:
            # env=child_env(): terraform loads the providers the directory
            # declares, and none of them get nable's decrypted vault (see
            # estimate_from_dir).
            from .security.vault import child_env
            try:
                r = subprocess.run([exe, "show", "-json", str(plan_path)], cwd=str(base),
                                   capture_output=True, text=True, check=False,
                                   timeout=_PLAN_SHOW_TIMEOUT_S, env=child_env())
                doc = json.loads(r.stdout) if r.returncode == 0 else None
                why = (doc if isinstance(doc, dict)
                       else f"`{tool} show -json` exited {r.returncode}" if r.returncode
                       else f"`{tool} show -json` did not return a plan")
            except subprocess.TimeoutExpired:
                why = f"`{tool} show -json` took longer than {_PLAN_SHOW_TIMEOUT_S:g} s"
            except ValueError:
                why = f"`{tool} show -json` did not return a plan"
            except (OSError, subprocess.SubprocessError) as exc:
                why = f"{tool} could not run: {type(exc).__name__}"
        _PLAN_CACHE[key] = why
    return tool, plan, _PLAN_CACHE[key]


def _read_saved_plan(cmd: str, cwd: str | None) -> tuple[str, str, dict[str, Any]] | None:
    """(tool, plan file as written, `show -json` document), or None when there
    is no plan file or it could not be read (see saved_plan_unreadable)."""
    read = _plan_read(cmd, cwd)
    if read is None or not isinstance(read[2], dict):
        return None
    return read[0], read[1], read[2]


def saved_plan_unreadable(command: str, *, cwd: str | None = None) -> str | None:
    """For `terraform apply <planfile>` whose plan file exists but could not be
    read: "could not read saved plan X (reason)". None otherwise.

    The plan is the only place a destroy or a GPU fleet applied from a file
    shows up, so a plan the guard cannot read is a plan nobody has checked:
    that asks, rather than passing silently because terraform was missing or
    `show` ran past its time."""
    try:
        read = _plan_read(_normalize(command), cwd)
    except Exception:
        return None
    if read is None or isinstance(read[2], dict):
        return None
    return f"could not read saved plan {read[1]} ({read[2]})"


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


_PRICERS: list[tuple[Any, Any]] = [
    (_RUN_INSTANCES_RE, _price_run_instances),
    (_RDS_CREATE_RE, _price_rds),
    (_RDS_MODIFY_RE, _price_rds_class_change),
    (_EC2_MODIFY_RE, _price_instance_type_change),
    (_SPOT_RE, _price_spot),
    (_FLEET_RE, _price_fleet),
    (_NODEGROUP_CREATE_RE, _price_nodegroup),
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

    This runs BEFORE command classification and applies to every tool call the
    hook sees, not just infrastructure ones. "Stop the agent because it is
    spending too much" means stop it, not stop it from touching Terraform.

    Which calls that is, exactly: in Claude Code, the Bash tool and every MCP
    tool (the installed matcher is ^(Bash|mcp__.*)$), known to the guard or
    not; in Cursor and Codex, shell commands. NOT Claude Code's built-in Edit,
    Write, Read, Glob, Grep, WebFetch, WebSearch or Task tools: widening the
    matcher to them would add the hook's start-up time to every file edit, so
    an agent over budget can still edit files until its next shell or MCP
    call. `nable guard doctor` says the same.

    Reads the local Claude Code session logs (ai_budget), so it needs no cloud
    account, no API key and no network. Returns None when no budget is set, when
    usage is under it, or on ANY error: a guard that cannot read its own budget
    must not take a position. session_id is the hook payload's, so a per-session
    cap is measured against the session making the call.
    """
    try:
        from .ai_budget import BUDGET_OVER, BUDGET_WARN, status
        st = status(session_id=session_id) if session_id else status()
        verdict = st.get("verdict")
        if verdict not in (BUDGET_OVER, BUDGET_WARN):
            return None
        if verdict == BUDGET_WARN and not _budget_note_due(session_id):
            return None
        budget = st.get("budget") or {}
        pct = st.get("pct_of_budget")
        over = (f"{pct * 100:.0f}% of" if isinstance(pct, (int, float))
                else "over" if verdict == BUDGET_OVER else "close to")
        if st.get("verdict_basis") == "session":
            sess = st.get("session") or {}
            detail = (f"~${sess.get('usd_equivalent', 0):,.2f} estimated this session, "
                      f"{over} its ${sess.get('cap_usd') or 0:,.2f} session cap")
            raise_it = "nable ai-budget --session-cap USD"
        elif st.get("verdict_basis") == "spend":
            detail = (f"~${st.get('est_usd_mtd_list_price', 0):,.0f} estimated this month, "
                      f"{over} your ${budget.get('spend_cap', 0):,.0f} cap")
            raise_it = "nable ai-budget --spend-cap USD"
        else:
            detail = (f"{st.get('billable_tokens_mtd', 0):,} tokens this month, "
                      f"{over} your {budget.get('monthly_tokens', 0):,} budget")
            raise_it = "nable ai-budget --tokens N"
        if verdict == BUDGET_WARN:
            # Close to the line: say so, alongside, without stopping anything.
            return {
                "decision": "warn",
                "action_type": "ai_budget",
                "reason": (f"nable guard: your agent is close to its AI budget. {detail}. "
                           "Nothing is stopped; this note shows at most every "
                           f"{_BUDGET_NOTE_EVERY_MIN} minutes. Raise it with `{raise_it}`."),
            }
        hard = _stop_on_budget()
        return {
            "decision": "deny" if hard else "ask",
            "action_type": "ai_budget",
            "reason": (
                f"nable guard: your agent is over its AI budget. {detail}. "
                + ("Stopped because FINOPS_GUARD_STOP_ON_BUDGET is on. "
                   f"Raise it with `{raise_it}`, or unset that variable to "
                   "downgrade this to a confirmation."
                   if hard else
                   f"Confirm to continue, or raise it with `{raise_it}`. "
                   "Set FINOPS_GUARD_STOP_ON_BUDGET=1 to make this a hard stop.")
            ),
        }
    except Exception:
        return None  # unreadable budget is not a reason to block anyone


# How often the "close to your AI budget" note may show in one session. Every
# tool call would be noise that teaches people to ignore the guard.
_BUDGET_NOTE_EVERY_MIN = 30


def _budget_note_due(session_id: str | None) -> bool:
    """True at most once per _BUDGET_NOTE_EVERY_MIN per session, remembered
    in a small file beside the ledger. When the file cannot be read or
    written the note shows: it never stops anything."""
    from datetime import UTC, datetime, timedelta

    from . import guard_ledger
    path = guard_ledger.ledger_path().with_name("guard-budget-note.json")
    key = session_id or "*"
    now = datetime.now(UTC)
    try:
        seen = json.loads(path.read_text())
        seen = seen if isinstance(seen, dict) else {}
    except (OSError, ValueError):
        seen = {}
    try:
        last = datetime.fromisoformat(seen[key])
        if now - last < timedelta(minutes=_BUDGET_NOTE_EVERY_MIN):
            return False
    except (KeyError, TypeError, ValueError):
        pass
    cutoff = now - timedelta(days=1)
    kept = {}
    for k, v in seen.items():
        with contextlib.suppress(TypeError, ValueError):
            if datetime.fromisoformat(v) > cutoff:
                kept[k] = v
    kept[key] = now.isoformat(timespec="seconds")
    with contextlib.suppress(OSError):
        path.write_text(json.dumps(kept))
    return True


def _with_budget_note(v: dict[str, Any] | None, note: dict[str, Any] | None
                      ) -> dict[str, Any] | None:
    """A verdict carrying the budget note: an allow or a warn becomes a warn
    whose reason includes it. An ask or a deny already stops for a human and
    is left as it is."""
    if note is None:
        return v
    if v is None:
        return note
    if v["decision"] not in ("allow", "warn"):
        return v
    text = note["reason"]
    if v.get("reason"):
        text = f"{v['reason']} {note['reason'].removeprefix('nable guard: ')}"
    return {**v, "decision": "warn", "reason": text}


# ── The cloud budget lens ──────────────────────────────────────────────────────
# The per-action threshold asks whether one change is big. The budget asks
# whether the month can afford it: a $280/mo launch is nothing on the 3rd and
# the thing that breaks the budget on the 28th. The lens reads the small JSON
# summary the budget checks write (budget/summary.py), never the database, and
# only for a priced change that adds cost.

_EC2_SERVICE = "Amazon Elastic Compute Cloud - Compute"
_RDS_SERVICE = "Amazon Relational Database Service"
# (provider, billing service) each pricer's command bills to; None where the
# command does not say (a saved plan can span providers).
_PRICER_SCOPE: dict[Any, tuple[str | None, str | None]] = {
    _price_run_instances: ("aws", _EC2_SERVICE),
    _price_rds: ("aws", _RDS_SERVICE),
    _price_rds_class_change: ("aws", _RDS_SERVICE),
    _price_instance_type_change: ("aws", _EC2_SERVICE),
    _price_spot: ("aws", _EC2_SERVICE),
    _price_fleet: ("aws", _EC2_SERVICE),
    _price_nodegroup: ("aws", _EC2_SERVICE),
    _price_savings_plan: ("aws", None),
    _price_reserved_instances: ("aws", None),
    _price_gce: ("gcp", "Compute Engine"),
    _price_az_vm: ("azure", "Virtual Machines"),
    _price_planfile: (None, None),
}
_BUDGETS_LISTED = 5


def _change_scope(command: str) -> dict[str, str]:
    """What the guard knows about where a change bills: provider and service
    from the command, team and account from FINOPS_GUARD_TEAM and
    FINOPS_GUARD_ACCOUNT (a command does not say which team it is for)."""
    scope: dict[str, str] = {}
    cmd = _normalize(command)
    for pattern, pricer in _PRICERS:
        if pattern.search(cmd):
            provider, service = _PRICER_SCOPE.get(pricer, (None, None))
            if provider:
                scope["provider"] = provider
            if service:
                scope["service"] = service
            break
    for env, key in (("FINOPS_GUARD_TEAM", "team"), ("FINOPS_GUARD_ACCOUNT", "account")):
        val = os.getenv(env, "").strip()
        if val:
            scope[key] = val
    return scope


def _budget_applies(b: dict[str, Any], scope: dict[str, str]) -> bool:
    kind = str(b.get("scope_type") or "total")
    if kind == "total":
        return True
    want = str(b.get("scope_value") or "")
    have = scope.get(kind)
    return have is not None and have.lower() == want.lower()


def _budget_scope_words(b: dict[str, Any]) -> str:
    kind = str(b.get("scope_type") or "total")
    return "total" if kind == "total" else f"{kind} {b.get('scope_value')}"


def budget_lens(command: str, est: dict[str, Any] | None, *,
                now: Any = None) -> dict[str, Any] | None:
    """The change against the budget figures on this machine, or None when the
    change is not priced or does not add cost.

    A dict with "state":
      over       spent this period plus the change's cost for the rest of it
                 is over at least one budget that applies; the one with the
                 largest overage is named (budget, spent_mtd, limit, ...)
      within     every budget that applies has room ("checked" names them)
      no_budget  the figures are current but no budget applies to this change
      stale      the figures are older than the limit (or from last month)
      absent     there are no figures on this machine
    It is also what the ledger records. Never raises: a summary it cannot read
    is "absent".
    """
    if not est:
        return None
    monthly = est.get("monthly_usd")
    one_off = est.get("total_usd") if monthly is None else None
    if not ((isinstance(monthly, (int, float)) and monthly > 0)
            or (isinstance(one_off, (int, float)) and one_off > 0)):
        return None
    from datetime import date, datetime

    from .budget import summary as _summary
    doc = _summary.read_summary()
    fresh = _summary.freshness(doc, now=now)
    when = {"as_of": fresh["as_of"], "age_hours": fresh["age_hours"]}
    if fresh["state"] != "fresh":
        out = {"state": fresh["state"], **when}
        if fresh["state"] == "stale":
            out["max_age_hours"] = fresh["max_age_hours"]
            if fresh["previous_month"]:
                out["previous_month"] = True
        return out
    when["spend_through"] = fresh["spend_through"]
    today = datetime.now().astimezone().date()
    scope = _change_scope(command)
    checked: list[str] = []
    over: list[dict[str, Any]] = []
    for b in _summary.current_budgets(doc or {}, today=today):
        if not _budget_applies(b, scope):
            continue
        checked.append(str(b.get("name")))
        end = date.fromisoformat(str(b["period_end"]))
        days_left = (end - today).days + 1
        if monthly is not None:
            # The change's run-rate for what is left of the period, today
            # included: it bills from the moment it exists.
            rest = float(monthly) * days_left * 24 / _hours_per_month()
        else:
            rest = float(one_off)
        spent, limit = float(b["spent"]), float(b["limit"])
        projected = spent + rest
        if projected > limit:
            over.append({
                "budget": str(b.get("name")),
                "scope": _budget_scope_words(b),
                "period": b.get("period") or "monthly",
                "spent_mtd": round(spent, 2),
                "limit": round(limit, 2),
                "change_monthly_usd": round(float(monthly), 2) if monthly is not None else None,
                "change_one_off_usd": round(float(one_off), 2) if one_off is not None else None,
                "days_left": days_left,
                "rest_of_period_usd": round(rest, 2),
                "projected_usd": round(projected, 2),
                "projected_overage_usd": round(projected - limit, 2),
            })
    if over:
        over.sort(key=lambda o: o["projected_overage_usd"], reverse=True)
        return {"state": "over", **over[0], **when,
                "others_over": [o["budget"] for o in over[1:_BUDGETS_LISTED + 1]]}
    if checked:
        return {"state": "within", "checked": checked[:_BUDGETS_LISTED], **when}
    return {"state": "no_budget", **when}


def _budget_hard_stop() -> tuple[bool, str, str]:
    """(hard stop?, why, how to undo it) for a change over a cloud budget.

    FINOPS_GUARD_STOP_ON_BUDGET decides when set, either way (one session or
    CI run); otherwise the policy's on_budget_breach. Default: ask."""
    env = os.getenv("FINOPS_GUARD_STOP_ON_BUDGET", "").strip().lower()
    if env in ("1", "true", "yes"):
        return True, "FINOPS_GUARD_STOP_ON_BUDGET is on", "unset it"
    if env in ("0", "false", "no"):
        return False, "", ""
    if load_policy().get("on_budget_breach") == "deny":
        return (True, "your policy sets on_budget_breach: deny",
                f"set on_budget_breach: ask in {_policy_file_shown()}")
    return False, "", ""


def _policy_file_shown() -> str:
    from .policy import policy_file_path
    try:
        return str(policy_file_path())
    except Exception:  # noqa: BLE001 - only a path in a sentence
        return "nable.policy.yaml"


def _budget_policy() -> dict[str, Any]:
    """The policy the gate judges a priced change with: the user's, with the
    over-budget answer the guard's env var sets, so the gate and the verdict
    cannot disagree about whether this is a stop."""
    pol = load_policy()
    hard, _, _ = _budget_hard_stop()
    return {**pol, "on_budget_breach": "deny" if hard else "ask"}


def _budget_reason(lens: dict[str, Any], *, hard: bool, why: str, undo: str = "") -> str:
    """The over-budget sentence: the budget, spend so far, the change's figure,
    the projected overage, and how fresh the spend is."""
    so_far = "this week" if lens.get("period") == "weekly" else "month to date"
    if lens.get("change_monthly_usd") is not None:
        days = lens["days_left"]
        change = (f"this change at ~{_usd(lens['change_monthly_usd'])}/mo adds "
                  f"~{_usd(lens['rest_of_period_usd'])} over the {days} "
                  f"day{'s' if days != 1 else ''} left")
    else:
        change = f"this change adds a one-off ~{_usd(lens['change_one_off_usd'])}"
    others = lens.get("others_over") or []
    more = (f" It is over {len(others)} other budget{'s' if len(others) != 1 else ''} "
            f"too ({', '.join(others)})." if others else "")
    fresh = f"Spend figure from {_summary_age(lens)} ago"
    if lens.get("spend_through"):
        fresh += f", cost data through {lens['spend_through']}"
    text = (f"This goes over the '{lens['budget']}' budget ({lens['scope']}): "
            f"{_usd(lens['spent_mtd'])} spent {so_far} of {_usd(lens['limit'])}, and "
            f"{change}, a projected ~{_usd(lens['projected_usd'])}, "
            f"~{_usd(lens['projected_overage_usd'])} over.{more} {fresh}.")
    if hard:
        return (f"{text} Stopped because {why}; raise the budget, or {undo or 'unset it'} "
                "to downgrade this to a confirmation.")
    return (f"{text} Confirm to proceed, or raise the budget. Set "
            "FINOPS_GUARD_STOP_ON_BUDGET=1, or on_budget_breach: deny in the policy file, "
            "to make this a hard stop.")


def _summary_age(lens: dict[str, Any]) -> str:
    from .budget.summary import age_words
    return age_words(lens.get("age_hours"))


def _budget_skip_note(lens: dict[str, Any] | None) -> str | None:
    """What a verdict on a priced change says when the budget went unchecked."""
    if not lens:
        return None
    if lens["state"] == "stale" and lens.get("previous_month"):
        return ("Budget not checked: nable's spend figure is from last month; "
                "`nable budget refresh` updates it.")
    if lens["state"] == "stale":
        return (f"Budget not checked: nable's spend figure is {_summary_age(lens)} old "
                f"(the guard uses figures up to {lens.get('max_age_hours', 48):g} hours "
                "old); `nable budget refresh` updates it.")
    if lens["state"] == "absent":
        return ("Budget not checked: there is no spend figure on this machine yet; "
                "`nable budget refresh` computes one.")
    return None


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
        v = _check_history(v, command, via=via, cwd=cwd)
    except Exception as exc:
        v = {**v, "_history_error": exc}
    # A priced change whose budget went unchecked says so, whenever the
    # verdict says anything at all; a silent allow stays silent (the ledger
    # still records the skip).
    note = _budget_skip_note(v.get("budget_check"))
    if note and v.get("decision") != "allow" and v.get("reason"):
        v = {**v, "reason": f"{v['reason']} {note}"}
    return v


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

    lens: dict[str, Any] | None = None

    def verdict(decision: str, body: str, *, strict: bool = False,
                est: dict[str, Any] | None = None) -> dict[str, Any]:
        head = "nable guard (strict)" if strict else "nable guard"
        v: dict[str, Any] = {"decision": decision, "action_type": action_type,
                             "door": door, "reason": f"{head}: {lead}{body}"}
        if est is not None:
            v["monthly_delta_usd"] = est["monthly_usd"]
            v["estimate"] = est
        if lens is not None:
            v["budget_check"] = lens
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
        else:
            unreadable = saved_plan_unreadable(command, cwd=cwd)
            if unreadable:
                return verdict("ask", f"{unreadable}; review it before applying.")

    if action_type == "infra_apply":
        # Reversible mutation. Zero friction by default; strict mode confirms,
        # and a production context always confirms: practitioners run agents
        # loose in staging but want a human nod before prod changes.
        #
        # Priceable commands additionally go through the policy's dollar
        # threshold: reversible does not mean cheap, and launching 8x
        # p4d.24xlarge is a six-figure monthly decision whichever door it is. The
        # estimate rides the same evaluate_action_gate as everything else, so
        # the user's FINOPS_POLICY_MAX_AUTO_USD and learned adjustments apply.
        #
        # And through the budget: what is left of it this month, not only the
        # per-action threshold (budget_lens).
        est = estimate_command_monthly_cost(command, cwd=cwd)
        lens = budget_lens(command, est)
        if est is not None:
            over = lens is not None and lens["state"] == "over"
            gate = evaluate_action_gate(action_type,
                                        monthly_delta_usd=est.get("monthly_usd") or 0.0,
                                        cost_verdict="over_budget" if over else None,
                                        policy=_budget_policy() if over else None)
            if gate.get("gate") != GATE_ALLOW:
                if gate.get("rule") == "over_budget" and lens is not None:
                    hard, why, undo = _budget_hard_stop()
                    return verdict("deny" if hard else "ask",
                                   f"{_cost_line(est)}. "
                                   f"{_budget_reason(lens, hard=hard, why=why, undo=undo)}",
                                   est=est)
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
    lens = budget_lens(command, est)
    over = lens is not None and lens["state"] == "over"
    cost = f"{_cost_line(est)}. " if est else ""
    if destroys:
        shown = ", ".join(destroys[:3]) + (f" and {len(destroys) - 3} more" if len(destroys) > 3 else "")
        cost = (f"the saved plan destroys {len(destroys)} "
                f"resource{'s' if len(destroys) != 1 else ''} ({shown}). ") + cost
    gate = evaluate_action_gate(action_type,
                                monthly_delta_usd=(est or {}).get("monthly_usd") or 0.0,
                                cost_verdict="over_budget" if over else None,
                                policy=_budget_policy() if over else None)
    if over and gate.get("rule") != "allowlist":
        # A commitment that breaks the budget: the budget sentence travels with
        # whatever else the gate said, and a hard stop makes it a deny.
        hard, why, undo = _budget_hard_stop()
        budget_txt = _budget_reason(lens, hard=hard, why=why, undo=undo)
        if hard:
            return verdict("deny", cost + budget_txt, est=est)
        if door == "one_way" and load_policy().get("escalate_one_way_doors", True):
            opening, closing = _one_way_sentence(command, action_type, cwd=cwd)
            closing = closing.replace("; confirm to proceed.", ".")
            return verdict("ask", ("" if via else f"{opening}. ") + cost + closing + " "
                           + budget_txt, est=est)
        return verdict("ask", cost + budget_txt, est=est)
    if gate.get("gate") == GATE_ESCALATE:
        if door == "one_way" and load_policy().get("escalate_one_way_doors", True):
            # Say what the command does and to what, in words: "'delete_resource'
            # is a one-way door" was the policy's vocabulary, not the human's.
            opening, closing = _one_way_sentence(command, action_type, cwd=cwd)
            return verdict("ask", ("" if via else f"{opening}. ") + cost + closing, est=est)
        return verdict("ask", cost + gate.get("reason", "a human must review this action."),
                       est=est)
    if gate.get("gate") == GATE_BLOCK:
        return verdict("deny", cost + gate.get("reason",
                                               "this action is not in your policy allowlist."),
                       est=est)
    return allowed(est)


# What a one-way command does, in words, by the rule that caught it: the
# first fragment found in that rule's pattern names it. Checked against the
# normalised command in the classifier's own order.
_ONE_WAY_PHRASES: list[tuple[str, str]] = [
    ("base64-to-shell", "runs a decoded script the guard cannot read"),
    ("python-boto3-delete", "looks like a Python one-liner that deletes or terminates "
                            "AWS resources"),
    ("workspace", "would delete a Terraform workspace, leaving what it manages "
                  "running with no state"),
    ("drain", "would evict every pod from a node"),
    ("kubectl-replace-force", "would delete and recreate Kubernetes resources"),
    ("kubectl", "would delete Kubernetes resources"),
    ("helm", "would uninstall a Helm release"),
    ("s3", "would delete stored data"),
    ("gsutil", "would delete stored data"),
    ("schedule-key-deletion", "would schedule a KMS key for deletion"),
    ("terraform", "would destroy infrastructure"),
    ("tofu", "would destroy infrastructure"),
    ("pulumi", "would destroy infrastructure"),
    ("cdk", "would destroy infrastructure"),
    ("sam", "would destroy infrastructure"),
    ("apply-with-destroy-flag", "would destroy infrastructure"),
    ("tf-cli-args-destroy", "would destroy infrastructure"),
]
_ACTION_PHRASES = {
    "terminate_instance": "would terminate EC2 instances",
    "release_ip": "would release an Elastic IP address",
    "snapshot_delete": "would delete a snapshot",
    "purchase_commitment": "would buy a commitment",
    "idle_cleanup": "would clean up idle resources",
}
# Tools whose target is the directory they run in.
_DIR_TOOLS_RE = re.compile(r"\b(?:terraform|tofu|terragrunt|pulumi|cdk|sam)\s")
_SHOWN_COMMAND_MAX = 100


def _one_way_sentence(command: str, action_type: str, *, cwd: str | None) -> tuple[str, str]:
    """("This would destroy infrastructure (`terraform destroy` in infra/)",
    "It cannot be undone; confirm to proceed.") for a one-way command."""
    norm = _normalize(command)
    what = _ACTION_PHRASES.get(action_type)
    if what is None:
        what = "would delete cloud resources"
        rule = next((r for r, _ in _ONE_WAY_RULES if r.search(norm)), None)
        if rule is not None:
            what = next((phrase for frag, phrase in _ONE_WAY_PHRASES
                         if frag in rule.pattern), what)
    shown = " ".join(command.split())
    if len(shown) > _SHOWN_COMMAND_MAX:
        shown = shown[:_SHOWN_COMMAND_MAX - 3] + "..."
    where = ""
    if _DIR_TOOLS_RE.search(norm):
        cd = _CD_PREFIX_RE.match(norm)
        chdir = re.search(r"-chdir=(\S+)", norm)
        d = (chdir.group(1) if chdir else cd.group(1) if cd
             else Path(cwd).name if cwd else "")
        where = f" in {d.rstrip('/')}/" if d else ""
    if action_type == "purchase_commitment":
        closing = "It cannot be cancelled once bought; confirm to proceed."
    elif what.startswith(("runs", "looks")):
        closing = "The guard cannot see exactly what it does; confirm to proceed."
    else:
        closing = "It cannot be undone; confirm to proceed."
    return f"This {what} (`{shown}`{where})", closing


# ── History: what the guard already let through ────────────────────────────────
# One verdict sees one command. An agent that launches ten $400/mo instances in
# an hour passes ten verdicts that are each correct and a total nobody agreed
# to. These checks read the recent end of the decision ledger (guard_ledger
# .recent: a bounded read from the end of the file, no database) and can only
# tighten: an allow or a warn may become an ask, nothing else changes.

# What the guard let run without a human. An ask is not counted: the hook exits
# before the human answers, so the ledger cannot tell an approved ask from a
# declined one, and counting a declined six-figure ask would put every launch for
# the next hour behind a prompt about money that was never spent.
_LET_THROUGH = ("allow", "warn")
_HISTORY_LISTED = 5


def _check_history(v: dict[str, Any], command: str, *, via: str = "",
                   cwd: str | None = None) -> dict[str, Any]:
    """`v` upgraded to an ask when recent history says so, else `v` unchanged
    apart from its loop key. May raise; _verdict_for turns that into a
    fail-open."""
    if v.get("action_type") == "infra_apply":
        key = loop_key(command, cwd=cwd)
        if key is not None:
            v = {**v, "loop_key": key[0], "loop_label": key[1]}
    if v.get("decision") not in _LET_THROUGH:
        return v
    pol = load_policy()
    new = (v.get("estimate") or {}).get("monthly_usd")
    cap = velocity_cap(pol)
    vel_window = float(pol.get("velocity_window_minutes") or 0.0)
    velocity_on = isinstance(new, (int, float)) and new > 0 and cap > 0 and vel_window > 0
    loops = int(pol.get("loop_repeat_count") or 0)
    loop_window = float(pol.get("loop_window_minutes") or 0.0)
    loop_on = "loop_key" in v and loops > 1 and loop_window > 0
    if not (velocity_on or loop_on):
        return v
    from . import guard_ledger
    recent = guard_ledger.recent(max(vel_window if velocity_on else 0.0,
                                     loop_window if loop_on else 0.0))
    found: list[tuple[str, str]] = []
    if loop_on:
        why = _loop_reason(v, recent, repeats=loops, window=loop_window)
        if why:
            found.append(("loop", why))
    if velocity_on:
        since = _minutes_ago(vel_window)
        why = _velocity_reason(v, [r for r in recent if str(r.get("ts", "")) >= since],
                               new=float(new), cap=cap, window=vel_window)
        if why:
            found.append(("velocity", why))
    if not found:
        return v
    lead = f"{via}. " if via else ""
    return {**v, "decision": "ask",
            "reason": f"nable guard: {lead}" + " Also: ".join(why for _, why in found),
            "history": "+".join(name for name, _ in found)}


def _minutes_ago(minutes: float) -> str:
    """An ISO timestamp the ledger's `ts` strings compare against directly."""
    from datetime import UTC, datetime, timedelta
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(timespec="seconds")


# Loop detection keys. A retry loop is the same creation run again and again,
# so the key is the verb plus the arguments that decide WHAT gets created:
# the instance type and count, the template, the database class. Names are
# left out where each duplicate gets a fresh one (a stack called app-2, a
# database called db-3): that is the shape of an agent re-creating what it
# already made. Anything else classified as a creation is keyed on its whole
# normalised command.
_LOOP_ARGS: list[tuple[Any, tuple[str, ...]]] = [
    (_RUN_INSTANCES_RE, ("instance-type", "count", "image-id", "launch-template")),
    (re.compile(r"\baws\s+cloudformation\s+(?:create-stack|update-stack|deploy)\b"),
     ("template-file", "template-url", "template-body")),
    (_RDS_CREATE_RE, ("db-instance-class", "engine")),
    (_GCE_CREATE_RE, ("machine-type",)),
    (_AZ_VM_CREATE_RE, ("size", "count")),
]
_LOOP_FALLBACK_ARG = {"aws cloudformation": "stack-name"}
# Flags that change how a command runs, not what it creates.
_LOOP_NOISE_RE = re.compile(r"\s(?:-auto-approve|--auto-approve|-input=false|--yes|-y|"
                            r"--no-cli-pager|--no-color|-no-color)(?=\s|$)")
_LOOP_VALUE_MAX = 80
# Local files a creation reads. Their modification times go into the key (not
# the label): an agent that edits the template between runs is iterating, not
# looping, and asking it to stop would be noise.
_LOOP_FILE_ARGS = ("template-file", "template-body", "f", "filename", "values", "var-file")


def loop_key(command: str, *, cwd: str | None = None) -> tuple[str, str] | None:
    """(key, label) for a creating command, or None when there is nothing to key.

    The label is what the human reads ("aws ec2 run-instances --instance-type
    m5.2xlarge --count 8"); the key is a short hash of the label plus the
    modification times of the local files the command reads, so two runs match
    only when neither the command nor its inputs changed."""
    import hashlib

    cmd = _normalize(command)
    label = None
    for pattern, args in _LOOP_ARGS:
        m = pattern.search(cmd)
        if not m:
            continue
        parts = [m.group(0)]
        for name in args:
            val = _flag(cmd, name)
            if val is not None:
                parts.append(f"--{name} {_short(val)}")
        if len(parts) == 1:
            for prefix, name in _LOOP_FALLBACK_ARG.items():
                val = _flag(cmd, name)
                if m.group(0).startswith(prefix) and val is not None:
                    parts.append(f"--{name} {_short(val)}")
        label = " ".join(parts)
        break
    if label is None:
        label = _LOOP_NOISE_RE.sub("", f" {cmd}").strip()
        if not label:
            return None
    base = Path(cwd or os.getcwd())
    stamp = [str(base)] if _TF_APPLY_RE.search(cmd) or "terragrunt" in cmd else []
    if stamp:
        stamp.append(_dir_stamp(base, cmd))
    for name in _LOOP_FILE_ARGS:
        for val in re.findall(rf"(?<!\S)--?{re.escape(name)}(?:=|\s+)(?!-)(\S+)", cmd):
            stamp.append(_file_stamp(base, val))
    digest = hashlib.sha256("\0".join([label, *stamp]).encode()).hexdigest()[:16]
    return digest, label


def _short(val: str) -> str:
    """A flag value fit for a label: an inline template body becomes a hash."""
    if len(val) <= _LOOP_VALUE_MAX:
        return val
    import hashlib
    return "sha256:" + hashlib.sha256(val.encode()).hexdigest()[:12]


def _file_stamp(base: Path, val: str) -> str:
    raw = val[len("file://"):] if val.startswith("file://") else val
    try:
        p = base / Path(raw).expanduser()
        if p.is_dir():
            return f"{p}:{max((c.stat().st_mtime_ns for c in p.iterdir() if c.is_file()), default=0)}"
        return f"{p}:{p.stat().st_mtime_ns}"
    except (OSError, ValueError):
        return val


def _dir_stamp(base: Path, cmd: str) -> str:
    """Newest *.tf / *.tfvars / *.hcl in the directory a Terraform apply runs
    in (after a leading `cd` or -chdir=)."""
    cd = _CD_PREFIX_RE.match(cmd)
    if cd:
        base = base / Path(cd.group(1)).expanduser()
    chdir = re.search(r"-chdir=(\S+)", cmd)
    if chdir:
        base = base / Path(chdir.group(1)).expanduser()
    try:
        newest = max((p.stat().st_mtime_ns for p in base.iterdir()
                      if p.suffix in (".tf", ".tfvars", ".hcl", ".json") and p.is_file()),
                     default=0)
    except OSError:
        newest = 0
    return f"{base}:{newest}"


def _loop_reason(v: dict[str, Any], recent: list[dict[str, Any]], *, repeats: int,
                 window: float) -> str | None:
    """Loop detection: this creation, identical to ones let through in the
    window often enough to look like an agent retrying the same thing."""
    from datetime import datetime

    since = _minutes_ago(window)
    same = [r for r in recent
            if r.get("loop_key") == v["loop_key"] and r.get("decision") in _LET_THROUGH
            and str(r.get("ts", "")) >= since]
    if len(same) + 1 < repeats:
        return None
    first = datetime.fromisoformat(same[0]["ts"])
    span = max(1, -(-int((datetime.now(first.tzinfo) - first).total_seconds()) // 60))
    times = ", ".join(str(r.get("ts", ""))[11:16] for r in same[-_HISTORY_LISTED:])
    return (f"this looks like a retry loop: {len(same) + 1} identical `{v['loop_label']}` in "
            f"{span} minute{'s' if span != 1 else ''} (the guard let the earlier ones "
            f"through at {times} UTC). Each run can create another copy. Confirm to run "
            "it again, or check what the earlier runs left behind first.")


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
        budget_check        a priced change that adds cost, against the cloud
                            budgets (budget_lens): "over", "within",
                            "no_budget", "stale" or "absent", with the figures
        harness             as passed in

    `session_id` is the hook payload's, so a per-session AI budget cap is
    measured against the session making the call.
    """
    try:
        # The AI budget stop comes first and is not conditioned on the command:
        # an agent burning through its budget should be stopped whatever it is
        # doing.
        budget_hit = check_budget_gate(session_id)
        note = budget_hit if budget_hit and budget_hit["decision"] == "warn" else None
        if budget_hit is not None and note is None:
            v = {**budget_hit, "harness": harness}
        elif len(command) > MAX_JUDGED_CHARS:
            v = {**_oversize_verdict(command), "harness": harness}
        else:
            hit = classify_command(command)
            if hit is None:
                # Not an infra command: nothing to record, but a budget note
                # still shows (unrecorded; it is not a decision about this call).
                return {**note, "harness": harness} if note else None
            v = {**_verdict_for(command, hit, cwd=cwd), "harness": harness}
        history_error = v.pop("_history_error", None)
        if record:
            if history_error is not None:
                _record_fail_open(history_error, harness=harness, tool=tool, command=command,
                                  check="history", session_id=session_id)
            _record(v, tool=tool, command=command, session_id=session_id)
        v = _with_budget_note(v, note)
        return None if v["decision"] == "allow" else v
    except Exception as exc:
        if record:
            _record_fail_open(exc, harness=harness, tool=tool, command=command,
                              session_id=session_id)
        return None


_SEVERITY = {"deny": 3, "ask": 2, "warn": 1, "allow": 0}

# The longest command the guard judges. Every check is linear in the command,
# but linear in ten megabytes still runs past the harness's hook timeout, and a
# timed-out hook fails open: padding a destroy with a long comment was a way to
# switch the guard off. A model's single tool call is far shorter than this, so
# a command this long is either generated by something else or built to be
# long, and a human should look at it.
MAX_JUDGED_CHARS = 256 * 1024


def _oversize_verdict(command: str) -> dict[str, Any]:
    return {"decision": "ask", "action_type": "oversize_command", "door": None,
            "reason": (f"nable guard: this command is {len(command) / 1024:,.0f} KB, longer "
                       f"than the {MAX_JUDGED_CHARS // 1024} KB the guard can check before "
                       "its hook times out. A human should read it before it runs.")}



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

    The AI budget stop comes first and applies to every MCP tool, known or
    not: an agent over its budget must not keep spending through a tool the
    guard does not otherwise judge, and a budget stop on one is recorded
    under the tool's name. Under budget, an unknown MCP tool is judged only on
    the command lines in its arguments (`{"command": "terraform destroy"}` to
    a shell server, guard_mcp.command_strings); with none it returns None and
    is not recorded: the guard never asks about a tool it does not
    understand. Recording and fail-open as gate_command.
    """
    summary = tool_name
    try:
        from .guard_mcp import argument_text, translate

        budget_hit = check_budget_gate(session_id)
        note = budget_hit if budget_hit and budget_hit["decision"] == "warn" else None
        if note is not None:
            budget_hit = None
        change = _budget_change(tool_name, arguments)
        actions = [] if change is not None else translate(tool_name, arguments)
        if not actions and budget_hit is None and change is None:
            return {**note, "harness": harness, "mcp_tool": tool_name} if note else None
        if actions:
            summary = actions[0].command

        if change is not None:
            summary = change.pop("summary")
            worst: dict[str, Any] | None = change
            if budget_hit is not None:
                # Over budget and raising it: the budget's own verdict (a deny
                # under the hard stop) with both facts in one line.
                worst = {**change,
                         "decision": "deny" if budget_hit["decision"] == "deny" else "ask",
                         "reason": f"{change['reason']} "
                                   f"{budget_hit['reason'].removeprefix('nable guard: ')}"}
        elif budget_hit is not None:
            worst = {**budget_hit}
        else:
            context = argument_text(arguments)
            worst = None
            for act in actions:
                if len(act.command) > MAX_JUDGED_CHARS:
                    worst, summary = _oversize_verdict(act.command), act.command
                    break
                hit = act.hit or classify_command(act.command)
                if hit is None:
                    continue
                v = _verdict_for(act.command, hit, context=f"{act.command} {context}",
                                 via=(f"{tool_name} would {act.summary}" if act.summary
                                      else f"{tool_name} amounts to `{act.command}`"))
                history_error = v.pop("_history_error", None)
                if history_error is not None and record:
                    _record_fail_open(history_error, harness=harness, tool=tool_name,
                                      command=act.command, check="history",
                                      session_id=session_id)
                if worst is None or _SEVERITY[v["decision"]] > _SEVERITY[worst["decision"]]:
                    worst, summary = v, act.command
            if worst is None:
                return {**note, "harness": harness, "mcp_tool": tool_name} if note else None
        worst = {**worst, "harness": harness, "mcp_tool": tool_name}
        if record:
            _record(worst, tool=tool_name, command=summary, session_id=session_id)
        worst = _with_budget_note(worst, note)
        return None if worst["decision"] == "allow" else worst
    except Exception as exc:
        if record:
            _record_fail_open(exc, harness=harness, tool=tool_name, command=summary,
                              session_id=session_id)
        return None


# The nable MCP tool that sets the agent's own AI budget. An agent stopped by
# the budget could call it to raise its own cap: no prompt, no record. Any
# argument that sets or clears a cap, or switches the lens, is treated as a
# possible raise, since telling a raise from a cut needs the current budget
# and the hook should not have to read it. Reversible, and a human decides.
_BUDGET_TOOL = "set_ai_budget"
_BUDGET_CAP_ARGS = ("mode", "plan_cost", "spend_cap", "monthly_tokens", "session_cap",
                    "every_session")


def _budget_change(tool_name: str, arguments: Any) -> dict[str, Any] | None:
    """An ask for a set_ai_budget call (under any server prefix) that changes
    a cap, or None."""
    if not (tool_name == _BUDGET_TOOL or tool_name.endswith("__" + _BUDGET_TOOL)):
        return None
    args = arguments if isinstance(arguments, dict) else {}
    changed = {k: args[k] for k in _BUDGET_CAP_ARGS
               if args.get(k) is not None and args.get(k) is not False}
    if not changed:
        return None
    shown = ", ".join(f"{k}={v}" for k, v in changed.items())
    return {"decision": "ask", "action_type": "ai_budget_change", "door": None,
            "reason": (f"nable guard: the agent is changing its own AI budget ({shown}); "
                       "a human should confirm."),
            "summary": f"{_BUDGET_TOOL} {shown}"}


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


# Ledger writes held back while a hook answers (see answer_first).
_PENDING: list[dict[str, Any]] | None = None


@contextlib.contextmanager
def answer_first():
    """Hold ledger writes until the block exits.

    A hook's job is the verdict; the ledger line is the receipt. Inside this
    block the verdict is computed and written to the harness first, and the
    ledger is written after, so a slow or locked ledger file can delay a
    receipt but never the answer. Nested use is a no-op: the outermost block
    writes."""
    global _PENDING
    if _PENDING is not None:
        yield
        return
    _PENDING = []
    try:
        yield
    finally:
        pending, _PENDING = _PENDING, None
        with contextlib.suppress(Exception):
            from . import guard_ledger
            for entry in pending:
                guard_ledger.append(entry)


def _append(entry: dict[str, Any]) -> None:
    from . import guard_ledger
    if _PENDING is not None:
        _PENDING.append(entry)
    else:
        guard_ledger.append(entry)


def _session_field(session_id: Any) -> dict[str, Any]:
    """The agent session a record belongs to, redacted like everything else
    (a session id is not a secret, but the ledger never trusts a string)."""
    from . import guard_ledger
    if not session_id or not isinstance(session_id, str):
        return {}
    return {"session": guard_ledger.redact(session_id, limit=128)}


def _record(v: dict[str, Any], *, tool: str, command: str,
            session_id: str | None = None) -> None:
    """Append one verdict to the ledger. Cheap, and never raises: a ledger
    problem is a missing line, never a lost verdict."""
    with contextlib.suppress(Exception):
        from . import guard_ledger
        est = v.get("estimate") or {}
        _append({
            "harness": v.get("harness"),
            **_session_field(session_id),
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
            # What loop detection matches on: a hash of the creation and its
            # inputs, and the human form of it (redacted like the command).
            **({"loop_key": v["loop_key"],
                "loop_label": guard_ledger.redact(v.get("loop_label"), limit=200)}
               if v.get("loop_key") else {}),
            # What the budget lens found for a priced change: the figures
            # behind an over-budget stop, or why the budget went unchecked.
            **({"budget_check": v["budget_check"]} if v.get("budget_check") else {}),
            "policy_version": _policy_version(),
            "nable_version": __version__,
            # Known only for a deny: the call never ran. An ask is the human's
            # to answer after the hook has exited, and an allow may still meet
            # the harness's own permission prompt.
            "outcome": "not_run" if v["decision"] == "deny" else None,
        })


def _record_fail_open(exc: BaseException, *, harness: str, tool: Any, command: Any,
                      check: str | None = None, session_id: Any = None) -> None:
    """A guard error let a call through unexamined; that is a verdict too.

    `check` names the part that failed when the rest of the verdict stood (a
    history check that could not read the ledger): the call was judged on
    policy alone, and the verdict itself is recorded next to this line."""
    with contextlib.suppress(Exception):
        from . import guard_ledger
        _append({
            "harness": harness,
            **_session_field(session_id),
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
    with answer_first():
        return _run_hook(stdin or sys.stdin, stdout or sys.stdout)


def _run_hook(stdin: Any, stdout: Any) -> int:
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
            _flush(stdout)
            return 0
        json.dump({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": verdict["decision"],
                "permissionDecisionReason": verdict["reason"],
            }
        }, stdout)
        _flush(stdout)
        return 0
    except Exception as exc:
        # Still exit 0 with nothing on stdout: availability beats judgement.
        # But a fail-open is exactly what an audit should be able to count.
        _record_fail_open(exc, harness="claude-code", tool=tool, command=None)
        return 0


def _flush(stdout: Any) -> None:
    """The verdict leaves the process before the ledger is written."""
    with contextlib.suppress(Exception):
        stdout.flush()


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


def hook_pin(cmd: str) -> str | None:
    """How a hook command is pinned.

    "pinned"   the uvx form at exactly this release
    "other"    the uvx form pinned to some other release
    "unpinned" the uvx form with no version (resolves latest on every call)
    None       not the uvx form: a binary path is fixed by whatever was
               installed there, so it has no pin to speak of

    Read from uvx's arguments (guard_adapters.uvx_pin), not its spelling: a
    leading `uvx --from` was all this used to see, so `uvx finops-mcp ...`,
    `uvx --python 3.12 --from ...`, an absolute uvx path and `uv tool run`
    read as a binary path and were never flagged or re-pinned."""
    from .guard_adapters import uvx_pin
    return uvx_pin(cmd)


def hook_release(cmd: str) -> str | None:
    """The finops-mcp release a uvx hook command is pinned to, or None."""
    from .guard_adapters import uvx_release
    return uvx_release(cmd)


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
    return 30 if cmd.startswith("uvx") or hook_pin(cmd) is not None else 10


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


def pinned_elsewhere_hook_command(path: Path) -> str | None:
    """Our installed hook command, when it is the uvx form pinned to a release
    other than this one. None when the hook is absent, current, unpinned, or a
    binary path."""
    for _entry, h in _read_our_hooks(path):
        if hook_pin(h["command"]) == "other":
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
        hooks = entry.get("hooks") if isinstance(entry, dict) else None
        if not isinstance(hooks, list):
            # Not ours, or not a shape we write ("hooks" a string, missing,
            # null): someone's data, left exactly as found. Iterating a string
            # here used to rewrite it as a list of its characters.
            kept.append(entry)
            continue
        inner = [h for h in hooks
                 if not (isinstance(h, dict) and _is_our_command(h.get("command")))]
        if len(inner) == len(hooks):
            kept.append(entry)          # nothing of ours in it, untouched
            continue
        removed = True
        if inner:
            entry["hooks"] = inner
            kept.append(entry)
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
# write. Cursor (beforeShellExecution + beforeMCPExecution) and Codex
# (PreToolUse on Bash and mcp__*) see shell commands and MCP tool calls once
# installed by this release; the rest see shell commands only
# (guard_adapters.py). Codex, Gemini CLI, Cline and the Copilot cloud agent
# cannot pause to ask.
_FAMILY_LABELS = {"aws": "AWS", "kubernetes": "Kubernetes", "terraform": "Terraform"}
_ADAPTER_SURFACES = {"cursor": ("Cursor", "shell commands"),
                     "codex": ("Codex CLI", "shell commands (an ask becomes a deny)"),
                     "copilot": ("GitHub Copilot",
                                 "shell commands (an ask becomes a deny in the cloud agent)"),
                     "gemini": ("Gemini CLI", "shell commands (an ask becomes a deny)"),
                     "cline": ("Cline", "shell commands (an ask or a deny stops the task)")}
_ADAPTER_MCP = ("cursor", "codex")      # adapters whose hook can also see MCP calls


def _adapter_rows() -> list[dict[str, Any]]:
    """Hook state for every other harness from guard_adapters, [] when this
    build has no adapters or they cannot answer. Read-only."""
    try:
        from . import guard_adapters as ga  # type: ignore[attr-defined]
        found = set(ga.detected())
        sees_mcp = getattr(ga, "sees_mcp", None)
        pin_state = getattr(ga, "pin_state", None)
        rows = []
        for name in _ADAPTER_SURFACES:
            for scope, is_global in (("project", False), ("global", True)):
                st = ga.state(name, is_global)
                row = {"harness": name, "scope": scope,
                       "path": str(ga.hooks_path(name, is_global)),
                       "installed": st != "absent", "runs": st == "installed",
                       "present": name in found}
                if name in _ADAPTER_MCP and sees_mcp is not None and st == "installed":
                    row["mcp"] = bool(sees_mcp(name, is_global))
                if pin_state is not None and st == "installed":
                    row["pin"] = pin_state(name, is_global)
                rows.append(row)
        return rows
    except Exception:
        return []


def doctor() -> dict[str, Any]:
    """Which surfaces the guard actually covers on this machine, and what it
    does not. Read-only apart from the ledger anchor (guard_ledger.check),
    which a clean check moves forward: it inspects settings files and the ledger, runs
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
        elif r.get("pin") == "other":
            fix(f"nable guard install{flag}", "pins the hook to this release instead of "
                f"another release ({hook_release(r['command']) or 'unknown'})")

    for name, (label, what) in _ADAPTER_SURFACES.items():
        mine = [r for r in adapter_rows if r["harness"] == name]
        for r in mine:
            flag = " --global" if r["scope"] == "global" else ""
            if r["installed"] and not r["runs"]:
                # The repair names the broken scope: a bare install would add a
                # project hook and leave the dead global one where it is.
                gaps.append(f"{label} ({r['scope']}): the hooked command no longer exists")
                fix(f"nable guard install --harness {name}{flag}", "repairs the dead hook in place")
            elif r.get("pin") in ("unpinned", "other"):
                fix(f"nable guard install --harness {name}{flag}",
                    "pins the hook to this release instead of "
                    + ("the newest PyPI release on every call" if r["pin"] == "unpinned"
                       else "another release"))
        if any(r["runs"] for r in mine):
            if any(r.get("mcp") for r in mine):
                what = what.replace("shell commands", "shell commands and MCP tool calls", 1)
            covered.append(f"{label}: {what}")
            stale = [r for r in mine if r["runs"] and r.get("mcp") is False]
            if stale and not any(r.get("mcp") for r in mine):
                gaps.append(f"{label}: MCP tool calls (the hook only sees shell commands)")
                flag = " --global" if stale[0]["scope"] == "global" else ""
                fix(f"nable guard install --harness {name}{flag}", "widens the hook to MCP tools")
            continue
        if any(r["installed"] for r in mine):
            continue                    # a dead hook: its repair is listed above
        present = any(r["present"] for r in mine) or _harness_present(name)
        if present:
            if adapter_rows:
                gaps.append(f"{label}: on this machine, no working guard hook")
                fix(f"nable guard install --harness {name}")
            else:
                gaps.append(f"{label}: on this machine, and this nable has no hook for it")
    gaps.append("commands inside scripts the agent runs (the guard sees `bash deploy.sh`, "
                "not what is in it)")
    gaps.append("MCP servers outside the recognised table (the guard stays silent on them "
                "unless an argument is itself an aws, kubectl, terraform or similar "
                "command line, or the AI budget stop applies)")
    gaps.append("the AI budget stop on Claude Code's built-in tools (Edit, Write, Read, "
                "WebFetch, Task and the like): in Claude Code it covers Bash and MCP "
                "tool calls only")

    ledger = guard_ledger.check()
    if not ledger["ok"]:
        fix("nable guard verify-log", f"the decision ledger breaks at line "
            f"{ledger.get('broken_at')}: a record was edited, removed, reordered or torn")
    for warning in ledger["warnings"]:
        fix("nable guard verify-log", warning)
    if ledger["clean"]:
        guard_ledger.save_anchor(ledger)
    lost = guard_ledger.unrecorded()
    ledger["unrecorded"] = lost
    if lost["count"]:
        gaps.append(f"{lost['count']} verdict(s) answered but not recorded, last at "
                    f"{lost['last']}: the ledger file was locked by another process or "
                    "replaced by something that is not a file")
        fix(f"check what holds {ledger['path']} (lsof), then remove {lost['path']}",
            "records the guard could not write")
    fixes = [f"{cmd}  ({'; '.join(why)})" if why else cmd for cmd, why in todo.items()]
    fixes.append("give agents read-only cloud credentials; keep write access behind a human")

    return {
        "ok": bool(live) and ledger["clean"],
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
    signal guard_adapters uses too (CODEX_HOME for Codex, COPILOT_HOME for
    Copilot, GEMINI_CLI_HOME for Gemini CLI, ~/Documents/Cline for Cline)."""
    if name == "codex":
        return Path(os.getenv("CODEX_HOME") or Path.home() / ".codex").is_dir()
    if name == "copilot":
        return Path(os.getenv("COPILOT_HOME") or Path.home() / ".copilot").is_dir()
    if name == "gemini":
        return (Path(os.getenv("GEMINI_CLI_HOME") or Path.home()) / ".gemini").is_dir()
    if name == "cline" and (Path.home() / "Documents" / "Cline").is_dir():
        return True
    return (Path.home() / f".{name}").is_dir()
