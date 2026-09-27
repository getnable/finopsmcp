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

The org model (finops.org, read by guard_org.py only for a priced change or
an ask or deny): an ask or a deny on a priced change or a one-way door names
the owner of what the command touches ("Owned by payments
(#payments-oncall).", "Likely owned by ..." for a proposal); with
FINOPS_GUARD_TEAM unset, the confirmed owner of the working directory's repo
path is the team whose budgets apply; and a confirmed threshold for that team
or for an environment the command touches replaces the auto threshold and
the velocity cap. Any error there is a recorded fail-open (check "org") and
the call is judged as if there were no org model.

Change freezes (org model freeze facts, guard_org.freeze): while a freeze
covers what a command touches (the org, the team scope, a confirmed
environment, an account it names), a priced change or a one-way door asks,
or is denied when a person confirmed the freeze in deny mode, whatever the
thresholds say. A proposed freeze, or one from a repo nobody trusted, only
asks. The reason names the freeze, why, and when it ends.

One-time approvals (guard_approvals): Codex CLI, Gemini CLI, Cline and the
Copilot cloud agent cannot ask, so their asks are denies. Such a deny
carries an approval id, and `nable guard approve <id>`, run by a person in
their own terminal, lets the identical call (same command, directory and
harness) through once within 15 minutes, recorded as approved_out_of_band.
A deny from policy (on_budget_breach: deny, a freeze in deny mode, a pack's
deny rule, the allowlist) is never approvable and says so; nor is a change
to the guard itself.

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

The guard's own files (guard_paths: the org model, the policy file, the
installed packs, the off flag, the ledger, the budgets, the settings that
carry the hook): a shell command, an MCP call or one of Claude Code's file
tools (Write, Edit, MultiEdit, NotebookEdit) that writes to one asks, and so
does `nable pack install|update|remove|sign|keygen`. An agent could
otherwise loosen its own guard without a command the rules above would see.

Installed packs (guard_packs): their guard rules may tighten any verdict
(silence or an allow to an ask, an ask to a deny), never loosen one, and
their price books inform the figures shown for the SKUs they name ("at your
price book rate"): the verdict judges at the higher of the list price and the
book rate, so a price book can never make a launch look cheaper to the
guard. A pack the guard cannot load is a recorded fail-open.

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
  approve [ID] [--as WHO] a person lets one stopped call run once (above);
                          with no id, the approvals waiting
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import json
import os
import re
import string
import sys
from pathlib import Path, PurePath
from typing import Any, NamedTuple

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

# The end of a verb or flag: whitespace, the end, a shell operator or a
# redirection right after it (`terraform destroy;echo`, `destroy>log`), never
# more word (destroy.tfplan).
_END = r"(?![^\s;&|)`<>])"

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
    # gsutil's successor spells it `gcloud storage rm` (-r or not, like gsutil).
    (rf"\bgcloud\s+(?:\S+\s+)*storage\s+rm{_END}", "delete_resource"),
    # `aws s3 mv --recursive` out of a bucket empties it as surely as rm does.
    ("s3-mv-recursive", "delete_resource"),
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
             "eksctl|gsutil|base64|python3?|nable|finops")
_PROGRAM_RE = re.compile(rf"(?:{_PROGRAMS})(?![\w.-])")
# ASCII only. str.lower() changes the length of some text (`İ` lowers to two
# characters), and a lowered copy whose offsets no longer line up with the
# command cannot be copied back: `TERRAFORM destroy # İ` kept its capitals and
# passed. The program names are ASCII, so nothing else needs lowering.
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _lower_programs(cmd: str) -> str:
    """`TERRAFORM destroy` -> `terraform destroy`; every other word as written.

    Program names are found in a lowercased copy (one case-sensitive scan,
    much cheaper than an IGNORECASE one) and copied back where they differ."""
    low = cmd.translate(_ASCII_LOWER)
    if low == cmd:
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


# ── Reading the command the way the shell will ────────────────────────────────
# Every rule reads a flattened command: escapes and quotes dropped, whitespace
# collapsed. Dropping them can only over-match, and it is how `t\erraform
# de\stroy` and `"aws" ec2 terminate-instances` are seen for what they run.
#
# Normalizing may only ADD classifications. The command is read three ways
# (_readings): as written, with same-line aliases expanded, and with the
# quoted text of data programs blanked, and the most severe reading wins
# (_worst_reading). `alias terraform=echo; terraform destroy` is a destroy as
# written even though the expansion says echo; the reader cannot know which
# alias bash will honour, and asking is the safe side. The one thing that
# removes a classification is the blanking of a commit message or a search
# pattern that mentions `terraform destroy`, and only where the shell is
# certain not to run that text (_quoted_data_args, _only_in_data).

_ESCAPE_RE = re.compile(r"\\(.?)", re.S)


def _unfold(command: str) -> str:
    """Line continuations removed: `terraform destroy\\<newline> -auto-approve`
    is one command, and so is `kubectl delete\\<newline> ns prod`."""
    return command.replace("\\\n", "") if "\\\n" in command else command


def _flatten(cmd: str) -> str:
    """Escapes and quotes dropped, whitespace collapsed, program names in
    lower case.

    Every backslash goes, inside single quotes too: outside them `\\x` runs
    as `x`, and single-quoted text that another shell reads (`sh -c`) loses
    its escapes there. Over-matching is the safe side."""
    if "\\" in cmd:
        cmd = _ESCAPE_RE.sub(r"\1", cmd)
    cmd = cmd.replace('"', "").replace("'", "")
    return _lower_programs(" ".join(cmd.split()))


# One left-to-right reading of the shell's quoting: a backslash escape, an
# ANSI-C `$'...'` string, a single- or double-quoted string, a comment, or an
# operator. Every alternative either fails on its first character or matches,
# and an unterminated quote runs to the end instead of failing, so no quote is
# ever rescanned from a later start: `echo "` followed by 100 KB of `\"` used
# to rescan the rest of the command from every one of them.
_SHELL_LEX_RE = re.compile(
    r"\\."
    r"|\$'[^'\\]*+(?:\\.[^'\\]*+)*+(?:'|\\?\Z)"
    r"|'[^']*+(?:'|\Z)"
    r'|"[^"\\]*+(?:\\.[^"\\]*+)*+(?:"|\\?\Z)'
    r"|(?<![^\s;&|()])#[^\n]*+"
    r"|&&|\|\||\|&|[;&|\n()]",
    re.S)

# Programs whose quoted arguments are data, not commands: a commit message or
# a search pattern that mentions `terraform destroy` asked the human to
# confirm a destroy nobody was running, and a guard that cries wolf on every
# docs commit gets uninstalled. `bash -c`, `sh -c` and `eval` are not here:
# their quoted argument is a command.
_DATA_PROGRAM_RE = re.compile(r"(?:echo|printf|grep|rg|ag|git|gh)(?![\w.-])")
_DATA_SEGMENT_RE = re.compile(
    r"\s*(?:(?:[A-Za-z_]\w*=\S*|sudo|command|time|nohup)\s+)*(?:\S*/)?"
    r"(?:(?:echo|printf|grep|egrep|fgrep|rg|ag)(?!\S)"
    r"|git(?:\s+-\S+(?:\s+[^\s-]\S*)?)*?\s+(?:commit|tag)(?!\S)"
    # A pull request's or an issue's title, body or comment.
    r"|gh\s+(?:pr|issue)\s+(?:create|edit|comment|review|close)(?!\S))")
# Blanking is for commands short enough to check this carefully (and to hand
# to shlex); a longer one is judged as written.
_MASK_MAX_CHARS = 16 * 1024
# Anything that could run quoted text, or that makes the shell's reading less
# than certain, turns blanking off for the whole command: a shell or an
# interpreter named anywhere (quoted text can be piped, sourced or passed to
# it: `| env bash`, `| busybox sh`, `>(sh)`, `echo ... > x.sh; bash x.sh`),
# `$` and backticks (substitutions, `$'...'`), escaped quotes, process
# substitution, a PATH change, `.` or a program started by path (`./x.sh`
# may be a file this command just wrote), and the flags that make a search
# tool run a command (`rg --pre`, `ag --pager`).
_NO_MASK_RE = re.compile(
    r"(?<![\w.-])(?:(?:ba|z|da|k|mk|c|tc|r)?sh|fish|busybox|xargs|parallel|eval|source|exec|"
    r"env|alias|python[\d.]*|node(?:js)?|deno|bun|perl|ruby|php|lua|tclsh|awk|gawk|mawk|"
    r"nawk|osascript|pwsh|powershell)(?![\w-])"
    r"|\\[\"']|[$`]|<\(|>\(|(?<![\w-])PATH=|--pre(?![\w-])|--pager(?![\w-])"
    r"|(?:^|[;&|\n])\s*(?:[A-Za-z_]\w*=[^\s;&|]*\s+)*[./~]")
# What a data program's output may be piped into with its quoted text still
# data: programs that only read and print. Anything else (at, crontab, ssh,
# `docker run -i`) might run what it is fed.
_PIPE_SAFE = frozenset({"grep", "egrep", "fgrep", "rg", "ag", "head", "tail", "wc", "sort",
                        "uniq", "cut", "tr", "less", "more", "cat", "column", "fold", "fmt",
                        "nl", "rev"})
_NEXT_WORD_RE = re.compile(r"\s*([^\s;&|<>()]*)")
# Output redirections that cannot leave a script behind: a file descriptor or
# /dev/null. `echo "terraform destroy" > x.sh` is data only until x.sh runs.
_SAFE_REDIRECT_RE = re.compile(r">>?\|?(?:&[\d-]|\s*/dev/(?:null|stderr|stdout)(?![^\s;&|]))")


def _plain_ok(cmd: str, a: int, b: int) -> bool:
    """cmd[a:b] is unquoted text with no group braces and no redirection
    into a file."""
    chunk = cmd[a:b]
    if "{" in chunk or "}" in chunk:
        return False
    i = chunk.find(">")
    while i >= 0:
        if not _SAFE_REDIRECT_RE.match(cmd, a + i):
            return False
        i = chunk.find(">", i + 1)
    return True


def _quoted_data_args(cmd: str) -> list[tuple[int, int]]:
    """Spans (quotes included) of the quoted arguments of echo, printf, grep,
    rg, ag, `git commit|tag` and `gh pr|issue create|...` that the shell will
    not run, or [] when that is not certain for the whole command.

    Conservative on purpose: a comment, an escape, a subshell, a redirection
    into a file, a pipe into anything but a reader, an unterminated quote or
    anything _NO_MASK_RE names means nothing is blanked and the command is
    judged as written."""
    if (len(cmd) > _MASK_MAX_CHARS or ('"' not in cmd and "'" not in cmd)
            or not _DATA_PROGRAM_RE.search(cmd) or _NO_MASK_RE.search(cmd)):
        return []
    spans: list[tuple[int, int]] = []
    seg_start = last = 0
    data: bool | None = None
    fed = False                 # this pipeline carries a data program's quoted text
    for m in _SHELL_LEX_RE.finditer(cmd):
        start, end = m.span()
        if not _plain_ok(cmd, last, start):
            return []
        last = end
        tok = m.group(0)
        if tok[0] in "'\"":
            if len(tok) < 2 or tok[-1] != tok[0]:
                return []       # unterminated
            if data is None:
                data = _DATA_SEGMENT_RE.match(cmd, seg_start, start) is not None
            if data:
                spans.append((start, end))
                fed = True
        elif tok[0] in "\\#$()":
            return []           # an escape, a comment, $'...', a subshell
        elif tok[0] in "&|" and tok not in ("&&", "||") and (
                cmd[start - 1:start] == ">" or cmd[end:end + 1] == ">"):
            continue            # part of a redirection: >&2, &>file, >|file
        elif tok in ("|", "|&"):
            seg_start, data = end, None
            if fed and _NEXT_WORD_RE.match(cmd, end).group(1) not in _PIPE_SAFE:
                return []
        else:                   # ; & && || newline: the next command in the list
            seg_start, data, fed = end, None, False
    if not _plain_ok(cmd, last, len(cmd)):
        return []
    return spans


class _Readings(NamedTuple):
    raw: str                    # as written: no alias expanded, nothing blanked
    expanded: str               # same-line aliases expanded
    masked: str                 # expanded, with quoted data blanked where that is certain
    data: tuple[str, ...]       # the quoted texts that were blanked
    shell: str                  # the command with its line continuations removed


@functools.lru_cache(maxsize=64)
def _readings(command: str) -> _Readings:
    cmd = _unfold(command)
    base = _flatten(cmd)
    raw = _strip_aws_global_options(base)
    expanded = _strip_aws_global_options(_expand_aliases(base)) if "alias " in base else raw
    spans = _quoted_data_args(cmd)
    if not spans:
        return _Readings(raw, expanded, expanded, (), cmd)
    out, last = [], 0
    for a, b in spans:
        out += (cmd[last:a], '""')
        last = b
    # _NO_MASK_RE turns blanking off for any `alias`: nothing to expand here.
    masked = _strip_aws_global_options(_flatten("".join(out) + cmd[last:]))
    return _Readings(raw, expanded, masked, tuple(cmd[a + 1:b - 1] for a, b in spans), cmd)


def _normalize(command: str) -> str:
    """The form every pricer reads, and the first reading every rule reads.

    Classification and pricing must read the SAME form: when only the
    classifier stripped AWS global options, `aws --region us-east-1 ec2
    run-instances --instance-type p4d.24xlarge --count 8` classified as a
    launch, found no price, and passed silently at six figures a month."""
    return _readings(command).masked


@functools.lru_cache(maxsize=16)
def _shell_words(cmd: str) -> frozenset[str] | None:
    """The words shlex reads in the command (comments dropped, operators
    split off), or None when it cannot read it."""
    import shlex
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        return frozenset(lex)
    except ValueError:
        return None


_DOOR_RANK = {"one_way": 2, "two_way": 1}


def _rank(hit: tuple[str, str] | None) -> int:
    return 0 if hit is None else _DOOR_RANK.get(hit[0], 1)


def _only_in_data(r: _Readings, hit: Any, judge: Any) -> bool:
    """Is `hit` all inside one blanked quoted argument? Only when that text
    gives the same result on its own and shlex agrees it is one word."""
    if not r.data:
        return False
    alone = [t for t in r.data if judge(_strip_aws_global_options(_flatten(t))) == hit]
    if not alone:
        return False
    words = _shell_words(r.shell)
    return words is not None and any(t in words for t in alone)


def _worst_reading(command: str, judge: Any) -> Any:
    """judge() over every reading of the command: the most severe result.

    A result that only the readings with nothing blanked give stands, unless
    it is all inside quoted data (_only_in_data)."""
    r = _readings(command)
    best = judge(r.masked)
    seen = {r.masked}
    for form in (r.expanded, r.raw):
        if form in seen:
            continue
        seen.add(form)
        hit = judge(form)
        if _rank(hit) > _rank(best) and not _only_in_data(r, hit, judge):
            best = hit
    return best


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


class _Lazy:
    """A pattern compiled the first time it is used. The hook imports this
    module on every Bash and MCP call; the patterns only some calls need (an
    MCP tool's name, a long one-liner, a substitution) cost nothing until
    one of those comes."""

    __slots__ = ("_args", "_re")

    def __init__(self, pattern: str, flags: int = 0) -> None:
        self._args = (pattern, flags)
        self._re: re.Pattern[str] | None = None

    def __getattr__(self, name: str) -> Any:
        if self._re is None:
            self._re = re.compile(*self._args)
        return getattr(self._re, name)


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


class _S3MoveOutOfBucket:
    """`aws s3 mv s3://bucket/prefix <dest> --recursive`: every object under the
    prefix is copied and then deleted from the bucket. An upload (a local
    source) is not a delete. Checked segment by segment."""

    pattern = "s3-mv-recursive"
    _mv = re.compile(rf"\baws\s+s3\s+mv{_END}")
    _recursive = re.compile(rf"(?<!\S)--recursive{_END}")
    # `aws s3 mv` flags that take no value; any other --flag consumes the next
    # token, so a flag's value is not read as the source.
    _BOOLEAN = frozenset({"--recursive", "--dryrun", "--quiet", "--follow-symlinks",
                          "--no-follow-symlinks", "--no-guess-mime-type", "--only-show-errors",
                          "--no-progress", "--ignore-glacier-warnings",
                          "--force-glacier-transfer", "--validate-same-s3-paths"})

    def search(self, cmd: str) -> re.Match[str] | None:
        if self._mv.search(cmd) is None:
            return None
        for seg in re.split(r"[|;&]", cmd):
            mv = self._mv.search(seg)
            if mv is None or self._recursive.search(seg, mv.end()) is None:
                continue
            skip = False
            for tok in seg[mv.end():].split():
                if skip:
                    skip = False
                elif tok.startswith("-"):
                    skip = "=" not in tok and tok not in self._BOOLEAN
                else:
                    if tok.lower().startswith("s3://"):
                        return mv
                    break
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
    # `(?:[^\s/|]*/)*`, not `(?:\S*/)?`: a run of `|||...` made every pipe in
    # it rescan the rest of the run for a `/`, quadratic in its length.
    _to_shell = re.compile(r"\| ?(?:sudo )?(?:[^\s/|]*/)*(?:sh|bash|zsh|dash|ksh)(?!\S)")

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
    _TfCliArgsDestroy(), _S3MoveOutOfBucket(), _PythonBoto3Delete(), _Base64ToShell(),
)}


def _compile(table: list[tuple[str, str]]) -> list[tuple[Any, str]]:
    return [(_SPECIAL_RULES.get(p) or _Rule(p), a) for p, a in table]


_ONE_WAY_RULES = _compile(_ONE_WAY_CLASSIFIERS)
_TWO_WAY_RULES = _compile(_TWO_WAY_CLASSIFIERS)


def classify_command(command: str) -> tuple[str, str] | None:
    """Classify a shell command as ("one_way"|"two_way", action_type), or None
    when it is not an infrastructure mutation nable cares about. Linear in the
    length of the command (see _Rule). Every reading of the command is judged
    and the most severe wins (_worst_reading)."""
    return _worst_reading(command, _classify_normalized)


def _classify_normalized(cmd: str) -> tuple[str, str] | None:
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


# An installed price book (a pack's price_books, finops.packs.price_override)
# holds the org's own rate for a SKU: an EDP discount, a private offer, a
# markup. A price book informs the figure shown and never loosens a verdict:
# thresholds, the velocity cap and budgets judge at the higher of the list
# price and the book rate (_gated). A book rate above list prices at it and
# says so ("at your price book rate"); one below list is shown beside the
# list figure the guard judges by. A book that prices a SKU the tables do not
# know is used as it is (without it there would be no figure at all). The
# ledger records which pack's rate it was. With none installed, or none for
# this SKU, pricing is the list price as before. Read through guard_packs,
# which caches the packs between hooks.

def _gated(provider: str, sku: str | None, list_rate: float | None, per: str = "hour"
           ) -> tuple[float | None, dict[str, Any] | None, bool]:
    """(the rate the guard judges by, the price book entry or None, whether
    that rate is the book's). A book rate of 0 is a rate, not "no price"."""
    book = _book(provider, sku, per)
    if book is None:
        return list_rate, None, False
    if list_rate is None or book["usd"] >= list_rate:
        return book["usd"], book, True
    return list_rate, book, False


def _below_list(book: dict[str, Any], units: float, per: str = "hr") -> str:
    """What a book rate below list would make the figure, said beside it."""
    return (f"; at your price book rate of {_rate(book['usd'])}/{per} ({book['pack']}) "
            f"it would be ~${book['usd'] * units:,.0f}/mo, but a price book can only raise "
            "the figure the guard judges by, so its threshold and budget checks use the "
            "list price")


def _book(provider: str, sku: str | None, per: str = "hour") -> dict[str, Any] | None:
    """The org's USD rate for `sku` from an installed price book, or None."""
    if not sku:
        return None
    try:
        from .guard_packs import rate
        return rate(provider, sku, per=per)
    except Exception:
        return None                    # list price, as with no price book


def _book_basis(basis: str, book: dict[str, Any], list_basis: str = _ON_DEMAND_BASIS) -> str:
    """`basis` with the list price it names replaced by the price book's rate."""
    return basis.replace(list_basis, f"on-demand rate in your price book ({book['pack']})")


def _book_field(book: dict[str, Any], judged: bool = True,
                book_monthly: float | None = None) -> dict[str, Any]:
    out = {"pack": book["pack"], "sku": book["sku"], "rate": book["rate"],
           "unit": book["unit"]}
    if not judged:
        # Below list: shown, not used for the verdict.
        out.update(below_list=True, monthly_usd=round(book_monthly or 0.0, 2))
    return out


def _price_ec2(itype: str | None, count: int, *, basis: str = _ON_DEMAND_BASIS,
               lead: str = "") -> dict[str, Any] | None:
    """`count` instances of `itype` at the EC2 table's rate (or the org's
    price book rate for it), or None."""
    if not itype:
        return None
    from .aws_prices import EC2_HOURLY
    hourly, book, judged = _gated("aws", itype, EC2_HOURLY.get(itype))
    if hourly is None:
        return None
    if book and judged:
        basis = _book_basis(basis, book)
    count = max(count, 1)
    hours = count * _hours_per_month()
    monthly = hourly * hours
    at = "at your price book rate of " if judged else "at "
    return {
        "monthly_usd": round(monthly, 2),
        "hourly_usd": hourly,
        "instance_type": itype,
        "count": count,
        "basis": basis,
        **({"price_book": _book_field(book, judged, book["usd"] * hours)} if book else {}),
        "line": (f"{lead}{count}x {itype} {at}{_rate(hourly)}/hr ({basis}) "
                 f"is ~${monthly:,.0f}/mo"
                 + (_below_list(book, hours) if book and not judged else "")),
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
    that gets no figure rather than a guessed one. So does a capacity counted
    in anything but instances: `TargetCapacityUnitType=vcpu` (or
    memory-mib) with 384 is 384 vCPUs, not 384 instances, and a
    WeightedCapacity makes each instance count for its weight."""
    types = set(_STRUCT_TYPE_RE.findall(cmd))
    cap = re.search(r"\bTotalTargetCapacity\s*[=:]\s*(\d+)", cmd)
    if len(types) != 1 or not cap:
        return None
    unit = re.search(r"\bTargetCapacityUnitType\s*[=:]\s*([\w-]+)", cmd)
    if (unit and unit.group(1).lower() != "units") or re.search(r"\bWeightedCapacity\b", cmd):
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
    if not cls:
        return None
    # A price book's rate for the class stands for whatever engine the org
    # priced it on, so it also prices an engine the list tables do not hold.
    from .aws_prices import rds_hourly
    listed = rds_hourly(cls, engine) if engine in _RDS_TABLE_ENGINES else None
    hourly, book, judged = _gated("aws", cls, listed)
    if hourly is None:
        return None
    # Multi-AZ runs a standby of the same class: twice the instance hours,
    # the same rule the Terraform estimator applies to aws_db_instance.
    multi_az = _has_flag(cmd, "multi-az")
    hours = (2 if multi_az else 1) * _hours_per_month()
    monthly = hourly * hours
    basis = f"{_ON_DEMAND_BASIS}, instance hours only; storage and I/O not included"
    if book and judged:
        basis = _book_basis(basis, book)
    at = "at your price book rate of " if judged else "at "
    return {
        "monthly_usd": round(monthly, 2),
        "hourly_usd": hourly,
        "instance_type": cls,
        "count": 2 if multi_az else 1,
        "basis": basis,
        **({"price_book": _book_field(book, judged, book["usd"] * hours)} if book else {}),
        "line": (f"{cls} {engine or 'RDS'}{' Multi-AZ' if multi_az else ''} {at}"
                 f"{_rate(hourly)}/hr"
                 f"{' x2 for the standby' if multi_az else ''} ({basis}) "
                 f"is ~${monthly:,.0f}/mo"
                 + (_below_list(book, hours) if book and not judged else "")),
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
    multi_az = _has_flag(cmd, "multi-az")
    from .aws_prices import rds_hourly
    rates = {"PostgreSQL": rds_hourly(cls, "postgres") or 0.0,
             "MySQL/MariaDB": rds_hourly(cls, "mysql") or 0.0}
    engine, listed = max(rates.items(), key=lambda kv: kv[1])
    if len(set(rates.values())) == 1:
        engine = "MySQL, MariaDB and PostgreSQL alike"
    hourly, book, judged = _gated("aws", cls, listed or None)
    if hourly is None:
        return None
    if judged:
        basis = _book_basis(f"{_ON_DEMAND_BASIS}, instance hours only, before subtracting "
                            "the current class", book)
    else:
        basis = (f"{_ON_DEMAND_BASIS}, the {engine} rate (the engine is not in the command), "
                 "instance hours only, before subtracting the current class")
    hours = (2 if multi_az else 1) * _hours_per_month()
    monthly = hourly * hours
    at = "at your price book rate of " if judged else "at "
    return {
        "monthly_usd": round(monthly, 2),
        "hourly_usd": hourly,
        "instance_type": cls,
        "count": 2 if multi_az else 1,
        "basis": basis,
        **({"price_book": _book_field(book, judged, book["usd"] * hours)} if book else {}),
        "line": (f"resized to {cls}{' Multi-AZ' if multi_az else ''} {at}{_rate(hourly)}/hr"
                 f"{' x2 for the standby' if multi_az else ''} ({basis}) "
                 f"is ~${monthly:,.0f}/mo"
                 + (_below_list(book, hours) if book and not judged else "")),
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
                    basis: str, provider: str) -> dict[str, Any] | None:
    size = _flag(cmd, flag)
    if not size:
        return None
    # Azure sizes are case-insensitive on the CLI (standard_d4s_v3 works).
    listed = table.get(size)
    if listed is None:
        listed = {k.lower(): v for k, v in table.items()}.get(size.lower())
    each, book, judged = _gated(provider, size, listed, per="month")
    if each is None:
        return None
    if judged:
        basis = f"monthly rate in your price book ({book['pack']})"
    monthly = each * count
    at = "at your price book rate of " if judged else "at "
    below = ""
    if book and not judged:
        below = (f"; at your price book rate of ${book['usd']:,.2f}/mo each ({book['pack']}) "
                 f"it would be ~${book['usd'] * count:,.0f}/mo, but a price book can only "
                 "raise the figure the guard judges by, so its threshold and budget checks "
                 "use the list price")
    return {
        "monthly_usd": round(monthly, 2),
        "instance_type": size,
        "count": count,
        "basis": basis,
        **({"price_book": _book_field(book, judged, book["usd"] * count)} if book else {}),
        "line": (f"{count}x {size} {at}${each:,.2f}/mo each ({basis}) is ~${monthly:,.0f}/mo"
                 + below),
    }


def _price_gce(cmd: str, **_: Any) -> dict[str, Any] | None:
    from .connectors.kubernetes import _GKE_MONTHLY
    m = _GCE_CREATE_RE.search(cmd)
    return _price_table_vm(
        cmd, flag="machine-type", table=_GKE_MONTHLY,
        count=max(1, _leading_names(cmd, m.end())), provider="gcp",
        basis="on-demand monthly, nable's Compute Engine node price table")


def _price_az_vm(cmd: str, **_: Any) -> dict[str, Any] | None:
    from .connectors.kubernetes import _AKS_MONTHLY
    return _price_table_vm(
        cmd, flag="size", table=_AKS_MONTHLY,
        count=int(_num(_flag(cmd, "count")) or 1), provider="azure",
        basis="pay-as-you-go monthly, nable's Azure VM price table")


_TF_APPLY_RE = re.compile(r"\b(terraform|tofu)\s+((?:-chdir=\S+\s+)?)apply\b")
# `cd DIR` or `pushd DIR` as a command of its own, anywhere before the apply:
# `git pull && cd infra && terraform apply tfplan`, `(cd infra && ...)`,
# `export X=1 && cd infra && ...`. Only a leading `cd` used to count, and the
# saved-plan destroy check looked for the plan in the wrong directory.
_CD_RE = re.compile(
    r"(?:^|[;&|(])\s*(?:cd|pushd)(?:\s+-[A-Za-z@]+)*(?:\s+([^\s;&|()]+))?(?=\s*(?:$|[;&|()]))")
# `terraform apply` flags that take their value as the NEXT token, so that
# token is not mistaken for the plan file.
_TF_VALUE_FLAGS = ("-var", "-var-file", "-target", "-replace", "-state",
                   "-state-out", "-backup", "-parallelism", "-lock-timeout")
# The hook's own timeout is 10s (30s for uvx) and a timed-out hook fails open
# with no verdict at all. Reading a plan loads provider schemas, which is
# usually one to three seconds; past five, no figure beats no guard.
_PLAN_SHOW_TIMEOUT_S = 5.0
_ARG_END_RE = re.compile(r"[;&|()<>]")


def _cd_target(cmd: str, upto: int, cwd: str | None) -> tuple[Path, str | None]:
    """Where a command at offset `upto` runs: `cwd` after every `cd` and
    `pushd` before it, and those moves as written (None when there are none)."""
    base = Path(cwd or os.getcwd())
    moves: list[str] = []
    for m in _CD_RE.finditer(cmd, 0, upto):
        d = m.group(1)
        if d is None or d == "~":
            base, moves = Path.home(), ["~"]
        elif d != "-":
            base = base / Path(d).expanduser()
            moves.append(d)
    return base, (str(PurePath(*moves)) if moves else None)


def _planfile_arg(cmd: str, verb_end: int) -> str | None:
    skip = False
    for tok in cmd[verb_end:].split():
        end = _ARG_END_RE.search(tok)
        if end is not None:
            # An operator or a redirection ends the arguments: `tfplan)`,
            # `tfplan;`, `tfplan>log` name the plan; `2>&1` and `|` do not.
            head = tok[:end.start()]
            if head and not skip and not head.startswith("-") and not (
                    end.group() in "<>" and head.isdigit()):
                return head
            return None
        if skip:
            skip = False
        elif tok.startswith("-"):
            skip = tok in _TF_VALUE_FLAGS
        else:
            return tok
    return None


# A plan file's `show -json` document, or why it could not be read.
_PLAN_CACHE: dict[tuple[str, float], dict[str, Any] | str] = {}


def _plan_read(cmd: str, cwd: str | None, start: int = 0
               ) -> tuple[str, str, dict[str, Any] | str] | None:
    """(tool, plan file as written, `show -json` document or the reason it
    could not be read) for the first `terraform|tofu apply <planfile>` at or
    after `start`, or None when that apply names no plan file.

    A plan file named but not there is a reason too: the guard cannot see
    what it would apply, so it asks.

    Read once per plan file per process: pricing, the destroy check and the
    unreadable check all need it, and the hook must not pay for `show` twice."""
    import shutil
    import subprocess

    m = _TF_APPLY_RE.search(cmd, start)
    if not m:
        return None
    tool = m.group(1)
    plan = _planfile_arg(cmd, m.end())
    if not plan:
        return None
    base, _ = _cd_target(cmd, m.start(), cwd)
    chdir = re.search(r"-chdir=(\S+)", m.group(2))
    if chdir:
        base = base / Path(chdir.group(1)).expanduser()
    try:
        plan_path = base / Path(plan).expanduser()
        if not plan_path.is_file():
            return tool, plan, f"no such file in {base}"
        key = (str(plan_path.resolve()), plan_path.stat().st_mtime)
    except (OSError, RuntimeError) as exc:
        return tool, plan, f"could not open it: {type(exc).__name__}"
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


def _read_saved_plan(cmd: str, cwd: str | None, start: int = 0
                     ) -> tuple[str, str, dict[str, Any]] | None:
    """(tool, plan file as written, `show -json` document), or None when there
    is no plan file or it could not be read (see saved_plan_unreadable)."""
    read = _plan_read(cmd, cwd, start)
    if read is None or not isinstance(read[2], dict):
        return None
    return read[0], read[1], read[2]


def saved_plan_unreadable(command: str, *, cwd: str | None = None) -> str | None:
    """For `terraform apply <planfile>` whose plan file could not be found or
    read: "could not read saved plan X (reason)". None otherwise.

    The plan is the only place a destroy or a GPU fleet applied from a file
    shows up, so a plan the guard cannot read is a plan nobody has checked:
    that asks, rather than passing silently because terraform was missing,
    `show` ran past its time or the plan was looked for in the wrong place."""
    try:
        read = _plan_read(_segmented(command), cwd)
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
        read = _read_saved_plan(_segmented(command), cwd)
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


def _price_planfile(cmd: str, *, cwd: str | None = None, whole: str | None = None,
                    at: int = 0, **_: Any) -> dict[str, Any] | None:
    """`terraform apply plan.out` applies exactly the saved plan, so the plan
    can be priced before it runs, through the same estimator as `nable
    estimate`. `whole` and `at` are the full command and where this segment
    starts in it, so a `cd` earlier in the command is followed."""
    read = _read_saved_plan(cmd, cwd) if whole is None else _read_saved_plan(whole, cwd, at)
    if read is None:
        return None
    tool, plan, doc = read
    from .connectors.terraform_estimate import estimate_plan
    result = estimate_plan(doc)
    if not result["lines"]:
        return None                    # nothing in the plan is priceable
    # A price book may raise the figure judged, never lower it (_gated).
    shown = float(result["monthly_delta_usd"])
    monthly = float(result.get("gate_monthly_delta_usd", shown))
    unpriced = len(result["unpriced"])
    books = result.get("price_books") or {}
    booked = (f"; {books['resources']} resource{'s' if books['resources'] != 1 else ''} "
              f"at your price book rate ({', '.join(books['packs'])})" if books else "")
    if books and round(shown, 2) != round(monthly, 2):
        booked += (f", which would make it {'+' if shown >= 0 else '-'}${abs(shown):,.0f}/mo; "
                   "a price book can only raise the figure the guard judges by, so the "
                   "list price stands where it is higher")
    basis = (f"`{tool} show -json {plan}`, {_ON_DEMAND_BASIS}{booked}"
             + (f"; {unpriced} resource{'s' if unpriced != 1 else ''} in the plan not priced"
                if unpriced else ""))
    return {
        "monthly_usd": round(monthly, 2),
        "plan": plan,
        "priced_resources": len(result["lines"]),
        "unpriced_resources": unpriced,
        "basis": basis,
        **({"price_book": {"pack": ", ".join(books["packs"])}} if books else {}),
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

# ── One command at a time ─────────────────────────────────────────────────────
# `aws ec2 run-instances --instance-type t3.micro && aws ec2 run-instances
# --instance-type p4d.24xlarge --count 8` is two launches. The pricers used to
# read the whole line, find the first --instance-type and price the t3.micro;
# so each shell command is priced on its own and the figures are added up.

# The operators between shell commands in a normalized command.
_COMMAND_BREAK_RE = re.compile(r"&&|\|\||\|&|[;&|]")
_QUOTED_OPS = str.maketrans({";": " ", "&": " ", "|": " ", "\n": " "})


def _price_rewrite(command: str, split_quoted: bool) -> str:
    """The command with every unquoted newline made a `;`, so that it still
    separates commands once whitespace is collapsed; and unless `split_quoted`,
    the operators inside quotes spaced out, so `--query 'a | b'` stays in its
    command. Both readings are priced (a `sh -c 'x; y'` wants its `;`)."""
    def one(m: re.Match[str]) -> str:
        tok = m.group(0)
        if tok == "\n":
            return " ; "
        if not split_quoted and len(tok) > 1 and tok[0] in "'\"$":
            return tok.translate(_QUOTED_OPS)
        return tok
    command = _unfold(command)
    if "\n" not in command and "'" not in command and '"' not in command:
        return command
    return _SHELL_LEX_RE.sub(one, command)


@functools.lru_cache(maxsize=16)
def _segmented(command: str, split_quoted: bool = False, masked: bool = True) -> str:
    """The normalized command with the breaks between its shell commands
    intact (see _price_rewrite). `masked` picks the reading with quoted data
    blanked (_readings)."""
    r = _readings(_price_rewrite(command, split_quoted))
    return r.masked if masked else r.expanded


def _commands_in(form: str):
    """(command, offset) for each shell command in a _segmented form."""
    pos = 0
    for m in _COMMAND_BREAK_RE.finditer(form):
        yield form[pos:m.start()], pos
        pos = m.end()
    yield form[pos:], pos


def _price_form(form: str, cwd: str | None) -> tuple[list[dict[str, Any]], int]:
    """(figures, how many changes had none) for each command in `form`: the
    first pricer whose command it is prices it, and a command it cannot price
    does not stop the others."""
    parts: list[dict[str, Any]] = []
    unpriced = 0
    for seg, at in _commands_in(form):
        for pattern, pricer in _PRICERS:
            if pattern.search(seg):
                try:
                    est = pricer(seg, cwd=cwd, whole=form, at=at)
                except Exception:
                    est = None         # a pricing bug must not cost the verdict
                if est is None:
                    unpriced += 1
                else:
                    parts.append(est)
                break
    return parts, unpriced


def _added_up(parts: list[dict[str, Any]], unpriced: int) -> dict[str, Any] | None:
    """One estimate for the whole command: a single figure as the pricer
    gave it, several added up (only what adds cost counts toward the total)."""
    if not parts:
        return None
    est = parts[0]
    if len(parts) > 1:
        monthly = [p["monthly_usd"] for p in parts
                   if isinstance(p.get("monthly_usd"), (int, float)) and p["monthly_usd"] > 0]
        one_off = [p["total_usd"] for p in parts
                   if p.get("monthly_usd") is None and isinstance(p.get("total_usd"), (int, float))]
        if monthly or one_off:
            total = round(sum(monthly), 2) if monthly else None
            said = ([f"~${total:,.0f}/mo"] if total is not None else []) + \
                   ([f"${sum(one_off):,.0f} in one-off orders"] if one_off else [])
            est = {"monthly_usd": total,
                   "basis": "; ".join(dict.fromkeys(str(p.get("basis")) for p in parts)),
                   "parts": parts,
                   "line": ("; ".join(p["line"] for p in parts)
                            + f"; {len(parts)} changes in this command, together "
                            + " plus ".join(said))}
            if one_off:
                est["total_usd"] = round(sum(one_off), 2)
            books = list(dict.fromkeys(p["price_book"]["pack"] for p in parts
                                       if p.get("price_book")))
            if books:
                est["price_book"] = {"pack": ", ".join(books)}
    if unpriced:
        est = {**est, "unpriced_changes": unpriced,
               "line": (f"{est['line']} ({unpriced} more change"
                        f"{'s' if unpriced != 1 else ''} in this command "
                        f"{'have' if unpriced != 1 else 'has'} no figure)")}
    return est


def _size(est: dict[str, Any]) -> float:
    return max(float(est.get("monthly_usd") or 0.0), 0.0) + float(est.get("total_usd") or 0.0)


def _pricing_forms(command: str):
    """The _segmented forms to price, in order of preference: with quoted data
    blanked first, as written only when that finds nothing."""
    seen: set[str] = set()
    for masked in (True, False):
        forms = [f for f in dict.fromkeys(_segmented(command, split, masked)
                                          for split in (False, True)) if f not in seen]
        seen.update(forms)
        if forms:
            yield forms


def estimate_command_monthly_cost(command: str, *, cwd: str | None = None) -> dict[str, Any] | None:
    """A local, list-price estimate for a shell command, or None.

    `cwd` is the directory the command will run in (the agent session's), used
    to find a saved Terraform plan; it defaults to this process's.

    Returns {monthly_usd, basis, line, ...} where `line` is the sentence the
    human reads and `basis` says where the number came from. `monthly_usd` is
    None when the only honest figure is a one-off total (`total_usd`, e.g. a
    Reserved Instance order ceiling). Each shell command in it is priced on
    its own and the figures added up ("parts" holds them); a command with no
    figure is counted in "unpriced_changes" and does not stop the others.
    Anything unpriceable returns None: an unknown type must degrade to the
    guard's existing behaviour, never to an invented figure.
    """
    try:
        r = _readings(command)
        if not any(p.search(f) for f in {r.masked, r.raw, r.expanded} for p, _ in _PRICERS):
            return None
        for forms in _pricing_forms(command):
            best: dict[str, Any] | None = None
            for form in forms:
                est = _added_up(*_price_form(form, cwd))
                if est is not None and (best is None or _size(est) > _size(best)):
                    best = est
            if best is not None:
                return best
        return None
    except Exception:
        return None                    # a pricing bug must not cost the verdict


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

    This applies to every tool call the hook sees, not just infrastructure
    ones. "Stop the agent because it is spending too much" means stop it, not
    stop it from touching Terraform. The call is still judged on its own, and
    the more severe of the two verdicts answers (_against_budget).

    Which calls that is, exactly: in Claude Code, the Bash tool and every MCP
    tool (the installed matcher is _HOOK_MATCHER), known to the guard or not;
    in Cursor and Codex, shell commands. NOT Claude Code's built-in Edit,
    Write, Read, Glob, Grep, WebFetch, WebSearch or Task tools. The file tools
    do reach the hook, but only to check for an edit to the guard's own files
    (guard_paths); guard_plugin answers every other edit before the guard is
    imported, so reading the budget there would add its cost to every file
    edit. An agent over budget can still edit files until its next shell or
    MCP call. `nable guard doctor` says the same.

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
        # What the figure leaves out or is late on (Cursor usage not read, or
        # read a while ago), said with it.
        lens = (st.get("session") if st.get("verdict_basis") == "session"
                else st.get("month_to_date")) or {}
        for note in lens.get("source_notes") or []:
            detail += f". {note.rstrip('.')}"
        if verdict == BUDGET_WARN:
            # Close to the line: say so, alongside, without stopping anything.
            return {
                "decision": "warn",
                "action_type": "ai_budget",
                "reason": (f"nable guard: your agent is close to its AI budget. {detail}. "
                           "Nothing is stopped; this note shows at most every "
                           f"{_BUDGET_NOTE_EVERY_MIN} minutes. The budget is yours to "
                           f"raise, in your own terminal: `{raise_it}`."),
            }
        # The remedy is addressed to the human: the reason reaches the agent
        # too, and an agent that runs `nable ai-budget` itself is asked about
        # it (_self_change).
        hard = _stop_on_budget()
        return {
            "decision": "deny" if hard else "ask",
            "action_type": "ai_budget",
            "reason": (
                f"nable guard: your agent is over its AI budget. {detail}. "
                + ("Stopped because FINOPS_GUARD_STOP_ON_BUDGET is on. You can raise the "
                   f"budget in your own terminal with `{raise_it}`, or unset that variable "
                   "to downgrade this to a confirmation."
                   if hard else
                   "Confirm to continue, or raise the budget in your own terminal with "
                   f"`{raise_it}`. Set FINOPS_GUARD_STOP_ON_BUDGET=1 to make this a hard stop.")
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


def _change_scope(command: str, *, team: str | None = None
                  ) -> dict[str, str | tuple[str, ...]]:
    """What the guard knows about where a change bills: provider and service
    from each shell command in it (a tuple when they differ), team and
    account from FINOPS_GUARD_TEAM and FINOPS_GUARD_ACCOUNT (a command does
    not say which team it is for). `team`, when given, is the team the org
    model scopes the working directory to (_OrgLens.team), and stands in for
    an unset FINOPS_GUARD_TEAM.

    A budget scoped to one of several services a command bills to is checked
    against the whole command's figure: more than lands in it, which is the
    safe side."""
    scope: dict[str, str | tuple[str, ...]] = {}
    found: dict[str, dict[str, None]] = {"provider": {}, "service": {}}
    for forms in _pricing_forms(command):
        for form in forms:
            for seg, _at in _commands_in(form):
                for pattern, pricer in _PRICERS:
                    if pattern.search(seg):
                        provider, service = _PRICER_SCOPE.get(pricer, (None, None))
                        if provider:
                            found["provider"][provider] = None
                        if service:
                            found["service"][service] = None
                        break
        if found["provider"] or found["service"]:
            break
    for key, vals in found.items():
        if vals:
            scope[key] = next(iter(vals)) if len(vals) == 1 else tuple(vals)
    for env, key in (("FINOPS_GUARD_TEAM", "team"), ("FINOPS_GUARD_ACCOUNT", "account")):
        val = os.getenv(env, "").strip()
        if val:
            scope[key] = val
    if team and "team" not in scope:
        scope["team"] = team
    return scope


def _budget_applies(b: dict[str, Any], scope: dict[str, str | tuple[str, ...]]) -> bool:
    kind = str(b.get("scope_type") or "total")
    if kind == "total":
        return True
    want = str(b.get("scope_value") or "").lower()
    have = scope.get(kind)
    if have is None:
        return False
    return any(h.lower() == want for h in ((have,) if isinstance(have, str) else have))


def _budget_scope_words(b: dict[str, Any]) -> str:
    kind = str(b.get("scope_type") or "total")
    return "total" if kind == "total" else f"{kind} {b.get('scope_value')}"


def _refresh_budget_summary(doc: dict[str, Any] | None, state: str) -> bool:
    """With FINOPS_GUARD_AUTO_REFRESH_BUDGET=1, have a stale or absent spend
    summary recomputed in the background (background_refresh), for the next
    priced change. Not when the summary lists no budget: nothing to check.
    True when a refresh is under way."""
    if state not in ("stale", "absent") or (doc is not None and not doc.get("budgets")):
        return False
    from . import background_refresh
    if not background_refresh.budget_auto_enabled():
        return False
    return background_refresh.maybe_start(background_refresh.BUDGET) is not None


def budget_lens(command: str, est: dict[str, Any] | None, *,
                now: Any = None, team: str | None = None) -> dict[str, Any] | None:
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
    is "absent". A stale or absent one carries "refreshing" when a background
    refresh is under way (_refresh_budget_summary). `team` is the org model's
    team scope when FINOPS_GUARD_TEAM is unset (_change_scope).
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
        if _refresh_budget_summary(doc, fresh["state"]):
            out["refreshing"] = True
        return out
    when["spend_through"] = fresh["spend_through"]
    today = datetime.now().astimezone().date()
    scope = _change_scope(command, team=team)
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


def _scoped_policy(org: Any, over: bool) -> dict[str, Any] | None:
    """The policy a priced change is gated with: the budget policy when it
    is over a budget, with the org model's thresholds for its team and
    environment when a human confirmed any (_OrgLens.policy). None means the
    gate's own default, exactly as before the org model."""
    base = _budget_policy() if over else None
    scoped = org.policy(base or load_policy())
    return scoped if scoped is not None else base


def _on_breach() -> tuple[str, str]:
    """(what a change over budget gets, which setting says so)."""
    env = os.getenv("FINOPS_GUARD_STOP_ON_BUDGET", "").strip().lower()
    if env in ("1", "true", "yes", "0", "false", "no"):
        return ("deny" if env in ("1", "true", "yes") else "ask"), "FINOPS_GUARD_STOP_ON_BUDGET"
    from .policy import BUDGET_BREACH_ACTIONS, _policy_file_keys
    if os.getenv("FINOPS_POLICY_ON_BUDGET_BREACH", "").strip().lower() in BUDGET_BREACH_ACTIONS:
        source = "FINOPS_POLICY_ON_BUDGET_BREACH"
    elif "on_budget_breach" in _policy_file_keys():
        source = "policy file"
    else:
        source = "default"
    return str(load_policy().get("on_budget_breach") or "ask"), source


def budget_status(org_team: str | None = None) -> dict[str, Any]:
    """Which cloud budgets the guard checks priced changes against, and how
    fresh its spend figure is: the doctor's view of budget_lens.

    enforced      budgets in the current period a change can land in: total,
                  provider and service ones (placed by the command), team and
                  account ones when FINOPS_GUARD_TEAM / FINOPS_GUARD_ACCOUNT
                  name them, or the org model scopes this directory to the
                  team (org_status)
    not_enforced  team and account budgets nothing places a change in
    state         "fresh", "stale", "no_data" or "absent" (budget.summary.freshness)
    on_breach     "ask" or "deny", and on_breach_source, the setting behind it

    `org_team` is the team the org model scopes this directory to, which a
    priced change here is checked as when FINOPS_GUARD_TEAM is unset.
    """
    from .budget import summary as _summary
    doc = _summary.read_summary()
    fresh = _summary.freshness(doc)
    on_breach, source = _on_breach()
    env = {"team": os.getenv("FINOPS_GUARD_TEAM", "").strip() or (org_team or ""),
           "account": os.getenv("FINOPS_GUARD_ACCOUNT", "").strip()}
    enforced: list[dict[str, Any]] = []
    not_enforced: list[dict[str, Any]] = []
    # With no cost data for the period every budget reads $0, and
    # current_budgets leaves those rows out; list them anyway so the doctor
    # names the budgets it cannot check rather than saying none is set.
    readable_only = fresh["state"] != "no_data"
    for b in _summary.current_budgets(doc, readable_only=readable_only) if doc else []:
        row = {"name": str(b.get("name")), "scope": _budget_scope_words(b),
               "spent": b.get("spent"), "limit": b.get("limit"), "pct_used": b.get("pct_used")}
        kind = str(b.get("scope_type") or "total")
        if kind in env and env[kind].lower() != str(b.get("scope_value") or "").lower():
            row["needs"] = f"FINOPS_GUARD_{kind.upper()}={b.get('scope_value')}"
            not_enforced.append(row)
        else:
            enforced.append(row)
    return {"state": fresh["state"], "as_of": fresh["as_of"], "age_hours": fresh["age_hours"],
            "previous_month": fresh["previous_month"], "spend_through": fresh["spend_through"],
            "max_age_hours": fresh["max_age_hours"], "enforced": enforced,
            "not_enforced": not_enforced, "on_breach": on_breach,
            "on_breach_source": source, "summary_path": str(_summary.summary_path())}


def org_status(cwd: str | None = None) -> dict[str, Any]:
    """The doctor's view of the org model (finops.org): whether one is
    loaded, where from, how many facts are confirmed and proposed, and the
    team the guard scopes priced changes in `cwd` to. Never raises.

    loaded       True when the org directory holds an org file or any fact applies
                 (legacy ones from tag_rules.yaml or accounts.yaml included)
    team         FINOPS_GUARD_TEAM, else the confirmed owner of this repo
                 path; team_source says which
    thresholds   the confirmed per-scope thresholds for that team, if any
    """
    out: dict[str, Any] = {"loaded": False}
    try:
        from . import guard_org
        from .org.model import KNOWN_FILES
        m = guard_org.load_model(cwd or os.getcwd())
        counts = m.status_counts()
        files = m.dir.is_dir() and any((m.dir / n).is_file() for n in KNOWN_FILES)
        out.update(loaded=bool(files or m.facts), dir=str(m.dir),
                   dir_source=m.dir_source, exists=m.dir.is_dir(),
                   confirmed=counts["confirmed"], proposed=counts["proposed"],
                   legacy=sum(1 for f in m.facts if f.origin == "legacy"),
                   warnings=len(m.warnings))
        env_team = os.getenv("FINOPS_GUARD_TEAM", "").strip()
        team, source = ((env_team, "FINOPS_GUARD_TEAM") if env_team
                        else guard_org.team_scope(m, cwd))
        out.update(team=team, team_source=source)
        # What the guard would apply: confirmed only, and a repo that is not
        # trusted may only lower a figure (guard_org.thresholds), never the
        # raw file contents.
        t = guard_org.thresholds(m, team, [])
        if t:
            out["thresholds"] = t
    except Exception as exc:  # noqa: BLE001 - the doctor reports it, never dies of it
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


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
    fix = ("it is being refreshed in the background" if lens.get("refreshing")
           else "`nable budget refresh` updates it")
    if lens["state"] == "stale" and lens.get("previous_month"):
        return f"Budget not checked: nable's spend figure is from last month; {fix}."
    if lens["state"] == "stale":
        return (f"Budget not checked: nable's spend figure is {_summary_age(lens)} old "
                f"(the guard uses figures up to {lens.get('max_age_hours', 48):g} hours "
                f"old); {fix}.")
    if lens["state"] == "absent":
        return ("Budget not checked: there is no spend figure on this machine yet; "
                + ("one is being computed in the background." if lens.get("refreshing")
                   else "`nable budget refresh` computes one."))
    if lens["state"] == "no_data":
        return ("Budget not checked: nable has no cost data for this budget period yet; "
                "sync cost data, then `nable budget refresh`.")
    return None


# A priced change allowed by policy but at or above this share of the auto
# threshold gets a "warn": the same 80% line ai_budget draws for the agent's
# own spend, applied to what the agent is about to launch.
_WARN_AT = 0.80


class _OrgLens:
    """One verdict's view of the org model (guard_org): the team scope, the
    per-scope thresholds and the owner of what the command touches, each
    computed at most once and only when asked, so an ordinary command never
    imports finops.org.

    Never raises. The first error switches the lens off and is kept in
    `error`; _verdict_for then judges the call again with the lens off (the
    guard as it was before the org model) and records the fail-open."""

    def __init__(self, command: str, cwd: str | None, *, on: bool = True) -> None:
        self.command, self.cwd, self.on = command, cwd, on
        self.error: BaseException | None = None
        self._memo: dict[str, Any] = {}

    def _get(self, name: str, fn: Any, default: Any) -> Any:
        if not self.on:
            return default
        if name not in self._memo:
            try:
                self._memo[name] = fn()
            except Exception as exc:  # noqa: BLE001 - kept, judged again, recorded
                self.error, self.on = exc, False
                return default
        return self._memo[name]

    def _model(self) -> Any:
        from . import guard_org
        return self._get("model", lambda: guard_org.load_model(self.cwd), None)

    def _subjects(self) -> list[str]:
        from . import guard_org
        m = self._model()
        return self._get("subjects", lambda: guard_org.subjects(self.command, self.cwd, m)
                         if m is not None else [], [])

    def team(self) -> tuple[str | None, str | None]:
        """(team, where it came from): FINOPS_GUARD_TEAM when set, else the
        confirmed owner of the working directory's repo path."""
        env = os.getenv("FINOPS_GUARD_TEAM", "").strip()
        if env:
            return env, "FINOPS_GUARD_TEAM"

        def find() -> tuple[str | None, str | None]:
            from . import guard_org
            m = self._model()
            return guard_org.team_scope(m, self.cwd) if m is not None else (None, None)
        return self._get("team", find, (None, None))

    def thresholds(self) -> dict[str, Any]:
        """Confirmed per-scope thresholds for the team and the environments
        the command touches, or {}."""
        def find() -> dict[str, Any]:
            from . import guard_org
            m = self._model()
            if m is None or not any(f.confirmed for f in m.by_kind("threshold")):
                return {}
            return guard_org.thresholds(m, self.team()[0],
                                        guard_org.confirmed_envs(m, self._subjects()))
        return self._get("thresholds", find, {})

    def policy(self, base: dict[str, Any]) -> dict[str, Any] | None:
        """`base` with the org model's thresholds in it, or None when the org
        model sets none (the caller keeps its own policy)."""
        t = self.thresholds()
        if not t:
            return None
        pol = dict(base)
        if "max_auto_monthly_usd" in t:
            pol["max_auto_monthly_usd"] = t["max_auto_monthly_usd"]
        if "velocity_cap_usd" in t:
            pol["velocity_cap_monthly_usd"] = t["velocity_cap_usd"]
        return pol

    def whose(self, name: str) -> str:
        """"for team payments (in /repo/nable.org/policy.yaml)": the scope
        whose confirmed threshold set `name` ("max_auto_monthly_usd" or
        "velocity_cap_usd") and the file it is in, or ""."""
        t = self.thresholds()
        if not t:
            return ""
        from . import guard_org
        # Name the file when the figure came from a repo's nable.org/, so a
        # person can see a repo set it; the user's own org dir goes unnamed.
        where = (t.get("files") or {}).get(name) or ""
        if "nable.org" not in Path(where).parts:
            t = {**t, "files": {}}
        return guard_org.whose(t, name)

    def owner(self) -> Any:
        def find() -> Any:
            from . import guard_org
            m = self._model()
            return guard_org.owner(m, self._subjects()) if m is not None else None
        return self._get("owner", find, None)

    def freeze(self) -> dict[str, Any] | None:
        """The change freeze in force over this command's scope
        (guard_org.freeze), or None."""
        def find() -> dict[str, Any] | None:
            from . import guard_org
            m = self._model()
            if m is None or not m.by_kind("freeze"):
                return None
            return guard_org.freeze(m, self.team()[0], self._subjects())
        return self._get("freeze", find, None)


def _with_freeze(v: dict[str, Any], org: _OrgLens) -> dict[str, Any]:
    """A priced change or a one-way door during a change freeze over its
    scope asks, or is denied when a person confirmed a freeze in deny mode,
    whatever the thresholds or a learned rule would let through. A freeze
    only ever tightens: a verdict already as strict keeps its decision and
    gains the freeze's sentence."""
    if v.get("door") != "one_way" and not v.get("estimate"):
        return v
    fz = org.freeze()
    if not fz:
        return v
    body = str(v.get("reason") or "").removeprefix("nable guard: ")
    if not body and v.get("estimate"):
        body = f"{_cost_line(v['estimate'])}."
    decision = fz["decision"]
    if _SEVERITY[v["decision"]] > _SEVERITY[decision]:
        decision = v["decision"]
    closing = ""
    if decision == "deny" and fz["decision"] == "deny":
        closing = (" It is denied until the freeze ends, by the org's policy; do not run it. "
                   "A person can end the freeze early: nable org reject " + fz["key"] + ".")
    elif decision == "ask" and "onfirm to proceed" not in body:
        closing = " Confirm to proceed."
    reason = f"nable guard: {fz['words']} {body}".rstrip() + closing
    field = {k: fz[k] for k in ("key", "subject", "reason", "end", "mode", "sure")}
    if fz.get("file") and "nable.org" in Path(fz["file"]).parts:
        field["file"] = fz["file"]
    return {**v, "decision": decision, "reason": reason, "freeze": field}


def _verdict_for(command: str, hit: tuple[str, str], *, context: str | None = None,
                 via: str = "", cwd: str | None = None) -> dict[str, Any]:
    """The policy verdict, then what the ledger's recent history adds to it,
    then the owner of what it touches when it asks or denies.

    A history check that fails leaves the policy verdict standing and puts the
    exception under "_history_error" for the caller to record as a fail-open:
    a guard that cannot read its own ledger must not take a position. An org
    model that fails the same way puts it under "_org_error", and the call is
    judged again as if there were no org model."""
    org = _OrgLens(command, cwd)
    v = _judged(command, hit, context=context, via=via, cwd=cwd, org=org)
    if org.error is not None:
        err = org.error
        v = _judged(command, hit, context=context, via=via, cwd=cwd,
                    org=_OrgLens(command, cwd, on=False))
        return {**v, "_org_error": err}
    v = _with_owner(v, org)
    v = _with_scope(v, org)
    if org.error is not None:
        v = {**v, "_org_error": org.error}
    elif org.on and org._memo.get("thresholds"):
        # The org model's thresholds were in force for this verdict: the
        # ledger keeps which figures, from which scope and which file.
        v = {**v, "org_thresholds": org._memo["thresholds"]}
    return v


def _with_scope(v: dict[str, Any], org: _OrgLens) -> dict[str, Any]:
    """An ask or a deny on a priced change or a one-way door keeps the scope
    it was judged in: the team whose thresholds apply (FINOPS_GUARD_TEAM, or
    the confirmed owner of the working directory's repo path) and the
    confirmed environments the command touches. The learning loop reads it
    from the ledger: a threshold it proposes names this scope, so a person's
    yes changes exactly the verdicts it was learned from."""
    if v.get("decision") not in ("ask", "deny") or not org.on:
        return v
    if v.get("door") != "one_way" and not v.get("estimate"):
        return v
    team = org.team()[0]

    def envs() -> list[str]:
        from . import guard_org
        m = org._model()
        return guard_org.confirmed_envs(m, org._subjects()) if m is not None else []
    found = org._get("envs", envs, [])
    return {**v, "scope": {"team": team, "envs": sorted(found)}}


def _with_owner(v: dict[str, Any], org: _OrgLens) -> dict[str, Any]:
    """An ask or a deny on a priced change or a one-way door names who owns
    what it touches, when the org model says: "Owned by payments
    (#payments-oncall).", or "Likely owned by ..." for a proposal. A citation
    changes no decision, so an unconfirmed owner may be shown, marked."""
    if v.get("decision") not in ("ask", "deny") or not v.get("reason"):
        return v
    if v.get("door") != "one_way" and not v.get("estimate"):
        return v
    r = org.owner()
    if r is None:
        return v
    try:
        from . import guard_org
        return {**v, "reason": f"{v['reason']} {guard_org.owner_words(r)}",
                "owner": guard_org.owner_field(r)}
    except Exception as exc:  # noqa: BLE001 - a citation never costs the verdict
        org.error = exc
        return v


def _judged(command: str, hit: tuple[str, str], *, context: str | None, via: str,
            cwd: str | None, org: _OrgLens) -> dict[str, Any]:
    """_verdict_for without the owner: policy, history, the budget note."""
    v = _policy_verdict(command, hit, context=context, via=via, cwd=cwd, org=org)
    try:
        v = _check_history(v, command, via=via, cwd=cwd, org=org)
    except Exception as exc:
        v = {**v, "_history_error": exc}
    v = _with_freeze(v, org)
    # A priced change whose budget went unchecked says so, whenever the
    # verdict says anything at all; a silent allow stays silent (the ledger
    # still records the skip).
    note = _budget_skip_note(v.get("budget_check"))
    if note and v.get("decision") != "allow" and v.get("reason"):
        v = {**v, "reason": f"{v['reason']} {note}"}
    return v


def _policy_verdict(command: str, hit: tuple[str, str], *, context: str | None = None,
                    via: str = "", cwd: str | None = None,
                    org: _OrgLens | None = None) -> dict[str, Any]:
    """The policy verdict for one already-classified action. Always a dict:
    "allow" is a verdict too (the ledger records it, with its figure), and the
    public entry points turn it into None for their callers.

    Shared by the shell and MCP entry points so a destroy is judged the same
    whichever door the agent used. `command` is the shell form, which is what
    gets priced; `context` is the text searched for a production context
    (defaults to the command); `via` prefixes the reason with what an MCP call
    amounts to, since the human never saw a command. `org` is the org
    model's view (team scope, per-scope thresholds); None judges without it.
    """
    door, action_type = hit
    lead = f"{via}. " if via else ""
    if org is None:
        org = _OrgLens(command, cwd, on=False)

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
        lens = budget_lens(command, est, team=org.team()[0] if est is not None else None)
        if est is not None:
            over = lens is not None and lens["state"] == "over"
            gate = evaluate_action_gate(action_type,
                                        monthly_delta_usd=est.get("monthly_usd") or 0.0,
                                        cost_verdict="over_budget" if over else None,
                                        policy=_scoped_policy(org, over))
            whose = org.whose("max_auto_monthly_usd")
            if gate.get("rule") == "threshold" and whose:
                # Say whose threshold it is when it is not the policy's own.
                gate = {**gate, "reason": re.sub(
                    r"your (\$[\d,]+) auto threshold",
                    lambda m: f"the {m.group(1)} auto threshold {whose} (org model)",
                    str(gate.get("reason") or ""), count=1)}
            if gate.get("gate") != GATE_ALLOW:
                if gate.get("rule") == "over_budget" and lens is not None:
                    hard, why, undo = _budget_hard_stop()
                    return verdict("deny" if hard else "ask",
                                   f"{_cost_line(est)}. "
                                   f"{_budget_reason(lens, hard=hard, why=why, undo=undo)}",
                                   est=est)
                # `rule` tells the learning loop an ask the threshold caused
                # (the only kind a threshold fact can stop) from the rest.
                return {**verdict(
                    "ask" if gate.get("gate") == GATE_ESCALATE else "deny",
                    f"{_cost_line(est)}. "
                    f"{gate.get('reason', 'a human must review this action.')}",
                    est=est), "rule": gate.get("rule")}
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
            pol = org.policy(load_policy()) or load_policy()
            cap = float(pol.get("max_auto_monthly_usd", 500.0))
            monthly = est.get("monthly_usd") or 0.0
            whose = org.whose("max_auto_monthly_usd")
            if cap > 0 and monthly >= _WARN_AT * cap:
                line = (f"the ${cap:,.0f}/mo auto threshold {whose} (org model)" if whose
                        else f"your ${cap:,.0f}/mo auto threshold")
                return verdict("warn", f"{_cost_line(est)}, {monthly / cap:.0%} of {line}. "
                               "Proceeding without a prompt.", est=est)
        return allowed(est)

    # One-way doors escalate whatever they cost, but the human deciding on a
    # Savings Plan should see the commitment in the same breath as the question.
    est = estimate_command_monthly_cost(command, cwd=cwd)
    lens = budget_lens(command, est, team=org.team()[0] if est is not None else None)
    over = lens is not None and lens["state"] == "over"
    cost = f"{_cost_line(est)}. " if est else ""
    if destroys:
        shown = ", ".join(destroys[:3]) + (f" and {len(destroys) - 3} more" if len(destroys) > 3 else "")
        cost = (f"The saved plan destroys {len(destroys)} "
                f"resource{'s' if len(destroys) != 1 else ''} ({shown}). ") + cost
    gate = evaluate_action_gate(action_type,
                                monthly_delta_usd=(est or {}).get("monthly_usd") or 0.0,
                                cost_verdict="over_budget" if over else None,
                                policy=(_scoped_policy(org, over) if est is not None
                                        else _budget_policy() if over else None))
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
    ("storage\\s+rm", "would delete stored data"),
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
    r = _readings(command)
    norm = r.masked
    what = _ACTION_PHRASES.get(action_type)
    if what is None:
        what = "would delete cloud resources"
        # The reading the classifier took it from: blanked, expanded, as written.
        rule = next((rule for form in (r.masked, r.expanded, r.raw)
                     for rule, _ in _ONE_WAY_RULES if rule.search(form)), None)
        if rule is not None:
            what = next((phrase for frag, phrase in _ONE_WAY_PHRASES
                         if frag in rule.pattern), what)
        elif _TF_APPLY_RE.search(norm):
            what = "would destroy infrastructure"      # a saved plan that deletes
    shown = " ".join(command.split())
    if len(shown) > _SHOWN_COMMAND_MAX:
        shown = shown[:_SHOWN_COMMAND_MAX - 3] + "..."
    where = ""
    tool = _DIR_TOOLS_RE.search(norm)
    if tool:
        seg = _segmented(command)
        at = _DIR_TOOLS_RE.search(seg)
        _, moved = _cd_target(seg, at.start() if at else len(seg), cwd)
        chdir = re.search(r"-chdir=(\S+)", norm)
        d = (chdir.group(1) if chdir else moved if moved
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
                   cwd: str | None = None, org: _OrgLens | None = None) -> dict[str, Any]:
    """`v` upgraded to an ask when recent history says so, else `v` unchanged
    apart from its loop key. May raise; _verdict_for turns that into a
    fail-open. The velocity cap is the org model's for this team or
    environment when a human confirmed one (`org`)."""
    if v.get("action_type") == "infra_apply":
        key = loop_key(command, cwd=cwd)
        if key is not None:
            v = {**v, "loop_key": key[0], "loop_label": key[1]}
    if v.get("decision") not in _LET_THROUGH:
        return v
    pol = load_policy()
    new = (v.get("estimate") or {}).get("monthly_usd")
    whose = ""
    if org is not None and isinstance(new, (int, float)) and new > 0:
        scoped = org.policy(pol)
        if scoped is not None and velocity_cap(scoped) != velocity_cap(pol):
            whose = (org.whose("velocity_cap_usd")
                     or org.whose("max_auto_monthly_usd"))
            pol = scoped
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
                               new=float(new), cap=cap, window=vel_window, whose=whose)
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
_LOOP_FILES_MAX = 16
_LOOP_DIR_ENTRIES_MAX = 256


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
        stamp.append(_dir_stamp(command, cwd))
    # Each file once, and at most _LOOP_FILES_MAX of them: `-f <big dir>`
    # repeated a thousand times used to stat every entry of the directory a
    # thousand times, seconds of hook time from a 29 KB command.
    files: dict[str, None] = {}
    for name in _LOOP_FILE_ARGS:
        for val in re.findall(rf"(?<!\S)--?{re.escape(name)}(?:=|\s+)(?!-)(\S+)", cmd):
            files[val] = None
    for val in list(files)[:_LOOP_FILES_MAX]:
        stamp.append(_file_stamp(base, val))
    if len(files) > _LOOP_FILES_MAX:
        stamp.append(f"+{len(files) - _LOOP_FILES_MAX} more files")
    digest = hashlib.sha256("\0".join([label, *stamp]).encode()).hexdigest()[:16]
    return digest, label


def _short(val: str) -> str:
    """A flag value fit for a label: an inline template body becomes a hash."""
    if len(val) <= _LOOP_VALUE_MAX:
        return val
    import hashlib
    return "sha256:" + hashlib.sha256(val.encode()).hexdigest()[:12]


def _newest_entry(d: Path, suffixes: tuple[str, ...] | None = None) -> int:
    """The newest modification time among the directory's first
    _LOOP_DIR_ENTRIES_MAX entries (those with one of `suffixes`, when given),
    and without `suffixes` the directory's own, which moves when a file is
    added or removed. A directory of a hundred thousand files is not listed
    in full inside a hook. With `suffixes` the directory's own time is left
    out: `terraform apply` writes its state beside the .tf files."""
    import itertools
    newest = 0 if suffixes else d.stat().st_mtime_ns
    with os.scandir(d) as it:
        for e in itertools.islice(it, _LOOP_DIR_ENTRIES_MAX):
            with contextlib.suppress(OSError):
                if (suffixes is None or e.name.endswith(suffixes)) and e.is_file():
                    newest = max(newest, e.stat().st_mtime_ns)
    return newest


def _file_stamp(base: Path, val: str) -> str:
    raw = val[len("file://"):] if val.startswith("file://") else val
    try:
        p = base / Path(raw).expanduser()
        if p.is_dir():
            return f"{p}:{_newest_entry(p)}"
        return f"{p}:{p.stat().st_mtime_ns}"
    except (OSError, ValueError):
        return val


def _dir_stamp(command: str, cwd: str | None) -> str:
    """Newest *.tf / *.tfvars / *.hcl / *.json in the directory a Terraform
    apply runs in (after any `cd` before it, and -chdir=)."""
    seg = _segmented(command)
    tf = _TF_APPLY_RE.search(seg) or re.search(r"\bterragrunt\s", seg)
    base, _ = _cd_target(seg, tf.start() if tf else len(seg), cwd)
    chdir = re.search(r"-chdir=(\S+)", seg)
    if chdir:
        base = base / Path(chdir.group(1)).expanduser()
    try:
        newest = _newest_entry(base, (".tf", ".tfvars", ".hcl", ".json"))
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
                     cap: float, window: float, whose: str = "") -> str | None:
    """The velocity cap: priced monthly run-rate let through in the window,
    plus this action, over the cap. `whose` names the org model scope that
    set the cap ("for team payments"), when one did."""
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
    if whose:
        return (f"{head}, over the {_usd(cap)}/mo velocity cap per {window:g} minutes "
                f"{whose} (org model). Confirm to proceed.")
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
        # The AI budget stop is not conditioned on the command: an agent
        # burning through its budget should be stopped whatever it is doing.
        # The command is still judged, so that a policy deny or a cloud budget
        # hard stop is not softened to the budget's ask, and so the ledger
        # holds what the command was.
        budget_hit = check_budget_gate(session_id)
        note = budget_hit if budget_hit and budget_hit["decision"] == "warn" else None
        stop = budget_hit if note is None else None
        v: dict[str, Any] | None
        if len(command) > MAX_JUDGED_CHARS:
            v = _oversize_verdict(command)
            forms: tuple[str, ...] = (command,)
        else:
            v = _self_change(command) or _protected_write(command, cwd) or _oversize_code(command)
            hit = classify_command(command)
            if hit is not None:
                v = _worse_of(v, _verdict_for(command, hit, cwd=cwd))
            forms = ()
        # Installed guard-rule packs may tighten any of that, never loosen it.
        v, pack_error, pack_problem = _with_packs(
            v, forms=forms, commands=() if forms else (command,))
        if record:
            _record_pack_trouble(pack_error, pack_problem, judged=v is not None or stop is not None,
                                 harness=harness, tool=tool, command=command,
                                 session_id=session_id)
        if v is None and stop is None:
            # Not an infra command: nothing to record, but a budget note
            # still shows (unrecorded; it is not a decision about this call).
            return {**note, "harness": harness} if note else None
        history_error = v.pop("_history_error", None) if v is not None else None
        org_error = v.pop("_org_error", None) if v is not None else None
        answer, recorded = _against_budget(v, stop)
        if record:
            answer, recorded = _out_of_band(answer, recorded, kind="shell", text=command,
                                            cwd=cwd, harness=harness, session_id=session_id,
                                            tool=tool)
        if record:
            if history_error is not None:
                _record_fail_open(history_error, harness=harness, tool=tool, command=command,
                                  check="history", session_id=session_id)
            if org_error is not None:
                _record_fail_open(org_error, harness=harness, tool=tool, command=command,
                                  check="org", session_id=session_id)
            if stop is not None:
                _record({**stop, "harness": harness}, tool=tool, command=command,
                        session_id=session_id)
            if recorded is not None:
                _record({**recorded, "harness": harness}, tool=tool, command=command,
                        session_id=session_id)
        answer = _with_budget_note({**answer, "harness": harness}, note)
        return None if answer["decision"] == "allow" else answer
    except Exception as exc:
        if record:
            _record_fail_open(exc, harness=harness, tool=tool, command=command,
                              session_id=session_id)
        return None


_SEVERITY = {"deny": 3, "ask": 2, "warn": 1, "allow": 0}


def _worse_of(change: dict[str, Any] | None, judged: dict[str, Any]) -> dict[str, Any]:
    """A command that both changes the guard (a self rule, a protected write)
    and is an infrastructure change (`rm ~/.finops/guard-off && terraform
    destroy`): the more severe verdict answers, with both reasons, so
    confirming the one is never a way past the other. On a tie the change's
    verdict answers, carrying the infrastructure verdict's reason when that
    one asks or denies too."""
    if change is None:
        return judged
    if _SEVERITY[judged["decision"]] > _SEVERITY[change["decision"]]:
        return _with_reason_of(judged, change)
    if judged["decision"] in ("ask", "deny"):
        return _with_reason_of(change, judged)
    return change


class _PackRuleHit(NamedTuple):
    rule: str
    pack: str
    verdict: str
    reason: str


def _pack_command_results(base: str, rules: Any, tighten: Any, command: str) -> list[Any]:
    """tighten() over the readings of one command line. The reading with
    quoted data blanked counts as it is; a rule that only the others (as
    written, aliases expanded, nothing blanked) match counts unless all it
    matches is inside one quoted data argument (_only_in_data): a commit
    message or a search pattern that names a command is not that command."""
    r = _readings(command)
    out = [tighten(base, rules, command=r.masked)]
    for rule in rules:
        if rule.matches_command(r.masked):
            continue

        def judge(form: str, rule: Any = rule) -> str | None:
            return rule.id if rule.matches_command(form) else None
        for form in dict.fromkeys((r.expanded, r.raw, command)):
            if judge(form) is not None and not _only_in_data(r, rule.id, judge):
                out.append(tighten(base, [rule], command=form))
                break
    return out


def _with_packs(v: dict[str, Any] | None, *, forms: Any = (), commands: Any = (),
                tool: str | None = None, args: Any = None
                ) -> tuple[dict[str, Any] | None, BaseException | None, BaseException | None]:
    """(v after the installed packs' guard rules, an error reading them, a
    pack with guard rules or a price book that is not loaded).

    finops.packs.content.tighten() over every reading of each of `commands`
    (_pack_command_results), over each of `forms` as it is (a command too
    long to read), and over the MCP call: the strictest answer wins, and it
    is never looser than `v`. A rule that matches without tightening is
    named in the ledger only. An error keeps `v` as it was: the caller
    records it as a fail-open (check "packs")."""
    try:
        from . import guard_packs
        st = guard_packs.state()
        problem = guard_packs.PackLoadProblem() if st["guard_problems"] else None
        rules = st["rules"]
        if not rules:
            return v, None, problem
        from .packs.rules import VERDICT_ORDER, tighten
        base = (v or {}).get("decision", "allow")
        if base not in VERDICT_ORDER:
            return v, None, problem
        hits: dict[tuple[str, str], _PackRuleHit] = {}
        worst = base
        results = [tighten(base, rules, command=f) for f in forms]
        for c in commands:
            results += _pack_command_results(base, rules, tighten, c)
        if tool is not None:
            results.append(tighten(base, rules, tool=tool, args=args))
        for t in results:
            if VERDICT_ORDER.index(t["verdict"]) > VERDICT_ORDER.index(worst):
                worst = t["verdict"]
            for h in t["rules"]:
                hits.setdefault((h["pack"], h["id"]),
                                _PackRuleHit(h["id"], h["pack"], h["verdict"], h["reason"]))
    except Exception as exc:
        return v, exc, None
    if not hits:
        return v, None, problem
    named = [f"{h.pack}:{h.rule}" for h in hits.values()]
    if worst == base:
        return ({**v, "pack_rules": named} if v is not None else v), None, problem
    said = " ".join(f"{h.reason.rstrip('. ')} (rule {h.rule} of pack {h.pack})."
                    for h in hits.values() if h.verdict == worst)
    closing = ("It was stopped by an installed pack; do not run it."
               if worst == "deny" else "Confirm to proceed.")
    if v is None or base in ("allow", "warn"):
        # The pack's reason leads: the guard's own view of the call was an allow.
        reason = f"nable guard: {said} {closing}"
        if v is not None and v.get("reason"):
            reason = f"{reason} {v['reason'].removeprefix('nable guard: ')}"
        out = {**(v or {"action_type": "pack_rule", "door": None}), "decision": worst,
               "reason": reason}
    else:
        out = {**v, "decision": worst,
               "reason": f"{v['reason']} {said} {closing if worst == 'deny' else ''}".rstrip()}
    out["pack_rules"] = named
    return out, None, problem


# A pack the guard cannot load is judged without, and that is a fail-open;
# recorded with every call the guard records anyway, and otherwise at most
# once in this many minutes, so a broken pack cannot fill the ledger.
_PACK_TROUBLE_EVERY_MIN = 10


def _record_pack_trouble(error: BaseException | None, problem: BaseException | None, *,
                         judged: bool, harness: str, tool: Any, command: Any,
                         session_id: Any) -> None:
    if error is not None:
        _record_fail_open(error, harness=harness, tool=tool, command=command, check="packs",
                          session_id=session_id)
    elif problem is not None and (judged or _pack_trouble_due()):
        _record_fail_open(problem, harness=harness, tool=tool, command=command, check="packs",
                          session_id=session_id)


def _pack_trouble_due() -> bool:
    import time

    from . import guard_ledger
    path = guard_ledger.ledger_path().with_name("guard-packs-note")
    try:
        if time.time() - path.stat().st_mtime < _PACK_TROUBLE_EVERY_MIN * 60:
            return False
    except OSError:
        pass
    with contextlib.suppress(OSError):
        path.write_text("The guard recorded that an installed pack could not be loaded.\n")
    return True


def _with_reason_of(primary: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    """`primary` with `other`'s reason after its own."""
    if not other.get("reason"):
        return {**primary}
    if not primary.get("reason"):
        return {**primary, "reason": other["reason"]}
    return {**primary, "reason": f"{primary['reason']} "
                                 f"{other['reason'].removeprefix('nable guard: ')}"}


def _against_budget(v: dict[str, Any] | None, stop: dict[str, Any] | None
                    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """(the answer, the verdict on the call as the ledger records it) for the
    verdict on the call and an AI budget stop, either possibly None.

    The more severe decision answers, and both reasons are given. On a tie
    the budget's verdict answers, except for an agent changing a budget or
    the guard itself, which is the change's verdict carrying the budget's
    reason. The ledger line for the call carries the decision that was
    answered: an allow the budget turned into an ask was not let through."""
    if stop is None or v is None:
        return (v or stop or {}), v
    if v.get("action_type") in _CHANGE_TYPES:
        worst = max(v["decision"], stop["decision"], key=_SEVERITY.__getitem__)
        answer = {**_with_reason_of(v, stop), "decision": worst}
    elif _SEVERITY[v["decision"]] > _SEVERITY[stop["decision"]]:
        answer = _with_reason_of(v, stop)
    else:
        answer = _with_reason_of(stop, v)
    recorded = {**v, "decision": answer["decision"]}
    if answer.get("reason"):
        recorded["reason"] = answer["reason"]
    return answer, recorded


# The harnesses whose hooks cannot ask (guard_approvals.DENY_ONLY, and
# Copilot under its cloud agent): cheap to test before importing anything.
_MAYBE_DENY_ONLY = ("codex", "gemini", "cline", "copilot")
# Asks a one-time approval never lifts: the agent's own AI budget (these
# harnesses show it without stopping), a change to the guard or its files (a
# person makes those in their own terminal), and a command too long for the
# guard to read (a person should read it, not approve an id). With
# _CHANGE_TYPES, defined further down.
_NO_OUT_OF_BAND = ("ai_budget", "oversize_command")
_POLICY_DENY_NOTE = (" This is denied by policy, not waiting for a person, so a one-time "
                     "approval (`nable guard approve`) cannot let it through.")


def _out_of_band(answer: dict[str, Any], recorded: dict[str, Any] | None, *, kind: str,
                 text: Any, cwd: str | None, harness: str, session_id: Any,
                 tool: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """(answer, recorded) for a harness that cannot ask (guard_approvals).

    An ask the identical call already had a person approve, once, from their
    own terminal becomes an allow, recorded with approved_out_of_band and
    who approved. Any other ask gets a pending approval: its id and the
    command a person runs join the reason, and the ledger line carries the
    id. A deny says a one-time approval cannot lift it. Elsewhere, and on
    any error, the verdict is left as it was: in these harnesses an ask is
    a deny either way."""
    if harness not in _MAYBE_DENY_ONLY or recorded is None or not answer.get("reason"):
        return answer, recorded
    decision = answer.get("decision")
    if decision not in ("ask", "deny"):
        return answer, recorded
    try:
        from . import guard_approvals as ga
        if not ga.deny_only(harness):
            return answer, recorded
        if decision == "deny":
            reason = answer["reason"].rstrip() + _POLICY_DENY_NOTE
            return {**answer, "reason": reason}, {**recorded, "reason": reason}
        if answer.get("action_type") in (*_NO_OUT_OF_BAND, *_CHANGE_TYPES):
            return answer, recorded
        body = ga.mcp_text(*text) if kind == "mcp" else str(text)
        hit = ga.take(kind, body, cwd=cwd, harness=harness)
        if hit is not None:
            granted = {"id": hit["id"], "by": hit.get("approved_by"),
                       "at": hit.get("approved_at")}
            said = (f"nable guard: allowed once, approved out of band by "
                    f"{hit.get('approved_by')} (approval {hit['id']}). "
                    f"{answer['reason'].removeprefix('nable guard: ')}")
            return ({**answer, "decision": "allow", "reason": said,
                     "approved_out_of_band": granted},
                    {**recorded, "decision": "allow", "reason": said,
                     "approved_out_of_band": granted})
        aid = ga.pending(kind, body, cwd=cwd, harness=harness,
                         session=session_id if isinstance(session_id, str) else None,
                         tool=tool if isinstance(tool, str) else None, verdict=answer)
    except Exception:  # noqa: BLE001 - no approval id is the deny it always was
        return answer, recorded
    if aid is None:
        return answer, recorded
    what = "call" if kind == "mcp" else "command"
    reason = (f"{answer['reason'].rstrip()} A person can approve it once with "
              f"`nable guard approve {aid}` in their own terminal (not the agent); the "
              f"identical {what} from the same directory is then allowed once within "
              f"{ga.EXPIRY_MINUTES} minutes.")
    return ({**answer, "reason": reason, "approval_id": aid},
            {**recorded, "reason": reason, "approval_id": aid})


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


def _unchecked_verdict(tool_name: str, why: str) -> dict[str, Any]:
    return {"decision": "ask", "action_type": "oversize_command", "door": None,
            "reason": (f"nable guard: {tool_name} carries command lines the guard did not "
                       f"check ({why}), and one of them names a cloud or IaC CLI. A human "
                       "should read it before it runs.")}


def gate_mcp_call(tool_name: str, arguments: dict[str, Any] | None, *,
                  harness: str = "claude-code", session_id: str | None = None,
                  record: bool = True, cwd: str | None = None) -> dict[str, Any] | None:
    """Evaluate an MCP tool call against the policy gate. PUBLIC ENTRY POINT.

    `tool_name` is the harness's full name (`mcp__<server>__<tool>` in Claude
    Code). Known infra-mutating tools (guard_mcp.MCP_RULES: Terraform, AWS,
    Kubernetes) are translated to the shell command they amount to and judged
    exactly like it, prices included. Returns the same verdict shape as
    gate_command, with `mcp_tool` added; a batch call returns its most severe
    verdict.

    The AI budget stop applies to every MCP tool, known or not: an agent over
    its budget must not keep spending through a tool the guard does not
    otherwise judge, and a budget stop on one is recorded under the tool's
    name. A known tool is judged as well, and the more severe verdict answers
    (_against_budget). An unknown MCP tool is judged only on the command
    lines in its arguments (`{"command": "terraform destroy"}` to a shell
    server, guard_mcp.scan_arguments); with none, and under budget, it
    returns None and is not recorded: the guard never asks about a tool it
    does not understand. One with more command lines than the guard judges,
    or one nested too deep, is asked about instead of passed. Recording and
    fail-open as gate_command. `cwd` is where the agent works: a one-time
    approval (guard_approvals) is bound to it, as a shell command's is.
    """
    summary = tool_name
    try:
        from .guard_mcp import argument_text, translate

        budget_hit = check_budget_gate(session_id)
        note = budget_hit if budget_hit and budget_hit["decision"] == "warn" else None
        stop = budget_hit if note is None else None
        change = _budget_change(tool_name, arguments) or _protected_mcp(tool_name, arguments)
        # A write to the guard's own files is judged with whatever else the
        # call does, so confirming the one is never a way past the other.
        judge_actions = change is None or change.get("action_type") in _JUDGED_WITH
        actions = translate(tool_name, arguments) if judge_actions else []
        if actions:
            summary = actions[0].command

        worst: dict[str, Any] | None = None
        if change is not None:
            summary = change.pop("summary")
            worst = change
        if judge_actions:
            context = argument_text(arguments)
            for act in actions:
                if len(act.command) > MAX_JUDGED_CHARS:
                    worst, summary = _oversize_verdict(act.command), act.command
                    break
                if act.unchecked:
                    v = _unchecked_verdict(tool_name, act.unchecked)
                else:
                    # A self rule on a command line in the arguments is the
                    # change already found; the command is judged for the rest.
                    v = _self_change(act.command) if change is None else None
                    if v is None:
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
                org_error = v.pop("_org_error", None)
                if org_error is not None and record:
                    _record_fail_open(org_error, harness=harness, tool=tool_name,
                                      command=act.command, check="org",
                                      session_id=session_id)
                if worst is not None and worst.get("action_type") in _JUDGED_WITH:
                    worst = _worse_of(worst, v)
                elif worst is None or _SEVERITY[v["decision"]] > _SEVERITY[worst["decision"]]:
                    worst, summary = v, act.command
        # Installed guard-rule packs: `mcp` rules on the call, `command` rules
        # on each command line it amounts to. They only tighten.
        commands = [act.command for act in actions if len(act.command) <= MAX_JUDGED_CHARS]
        worst, pack_error, pack_problem = _with_packs(
            worst, commands=tuple(dict.fromkeys(commands)), tool=tool_name, args=arguments)
        if record:
            _record_pack_trouble(pack_error, pack_problem,
                                 judged=worst is not None or stop is not None, harness=harness,
                                 tool=tool_name, command=summary, session_id=session_id)
        if worst is None and stop is None:
            return {**note, "harness": harness, "mcp_tool": tool_name} if note else None
        answer, recorded = _against_budget(worst, stop)
        if record:
            answer, recorded = _out_of_band(answer, recorded, kind="mcp",
                                            text=(tool_name, arguments),
                                            cwd=cwd if isinstance(cwd, str) else None,
                                            harness=harness, session_id=session_id,
                                            tool=tool_name)
            if stop is not None:
                _record({**stop, "harness": harness, "mcp_tool": tool_name}, tool=tool_name,
                        command=summary, session_id=session_id)
            if recorded is not None:
                _record({**recorded, "harness": harness, "mcp_tool": tool_name},
                        tool=tool_name, command=summary, session_id=session_id)
        answer = _with_budget_note({**answer, "harness": harness, "mcp_tool": tool_name}, note)
        return None if answer["decision"] == "allow" else answer
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
# The nable MCP tools that change the cloud budgets a priced change is checked
# against (budget_lens): raising or deleting one lifts the budget stop just as
# surely. Any call asks.
_CLOUD_BUDGET_TOOLS = ("set_budget", "delete_budget", "sync_budgets_from_yaml")
_CHANGE_TYPES = ("ai_budget_change", "budget_change", "guard_change", "org_change",
                 "pack_change", "protected_write", "learning_change")
# Verdicts on an MCP call that the infrastructure it amounts to is judged
# with: a change to the guard or its files, and code too long to read.
_JUDGED_WITH = (*_CHANGE_TYPES, "oversize_command")
_SHOWN_VALUE_MAX = 80


def _budget_change(tool_name: str, arguments: Any) -> dict[str, Any] | None:
    """An ask for a set_ai_budget call (under any server prefix) that changes
    a cap, or for any set_budget, delete_budget or sync_budgets_from_yaml
    call; None otherwise."""
    name = tool_name.rsplit("__", 1)[-1]
    args = arguments if isinstance(arguments, dict) else {}
    if name in _CLOUD_BUDGET_TOOLS:
        shown = ", ".join(f"{k}={str(v)[:_SHOWN_VALUE_MAX]}" for k, v in args.items()
                          if v is not None)
        summary = f"{name} {shown}".rstrip()
        return {"decision": "ask", "action_type": "budget_change", "door": None,
                "reason": ("nable guard: the agent is changing the cloud budgets the guard "
                           f"checks changes against ({summary}); a human should confirm."),
                "summary": summary}
    if name != _BUDGET_TOOL:
        return None
    changed = {k: args[k] for k in _BUDGET_CAP_ARGS
               if args.get(k) is not None and args.get(k) is not False}
    if not changed:
        return None
    shown = ", ".join(f"{k}={v}" for k, v in changed.items())
    return {"decision": "ask", "action_type": "ai_budget_change", "door": None,
            "reason": (f"nable guard: the agent is changing its own AI budget ({shown}); "
                       "a human should confirm."),
            "summary": f"{_BUDGET_TOOL} {shown}"}


# The same changes made from the shell: `nable ai-budget --spend-cap ...`,
# `nable budget ci-gate --budget-file ...` (it syncs the file's budgets
# first), and taking the guard out (`nable guard uninstall`, `nable guard
# off`, `nable uninstall`). An agent stopped by a budget could otherwise lift it, or remove
# the hook, in one command. `budget status` and `refresh` only read. And
# `nable org confirm|reject|set|trust` and `nable guard approve`, which record
# a person's decision. Every way to start the CLI counts: nable, finops and finops-mcp (a path in
# front, or a uvx pin like `finops-mcp@1.2` or `finops-mcp[aws]==1.2`), and
# `python -m finops.setup_wizard`, `-m finops.entry` or `-m finops.server`,
# and the program a command substitution finds (`$(which nable) guard off`,
# `"$(command -v nable)"`, `` `which finops` ``): a closing parenthesis,
# backtick or quote may sit between the name and the space.
_NABLE = (r"(?:(?<![\w-])(?:nable|finops|finops-mcp)(?:\[[\w,.-]*\])?(?:(?:@|==)[\w.+!*-]*)?"
          r"|(?<![\w-])-m\s*finops\.(?:setup_wizard|entry|server))[)`\"']*\s")


class _PythonApiCall:
    """Code that imports one of nable's modules and calls a function that
    changes what the guard allows, in a one-liner or a heredoc: `python3 -c
    "from finops.org import store; store.confirm(...)"` decides an org fact as
    surely as `nable org confirm`. (The org API also wants a decision the CLI
    built; this is the seatbelt.) The code is not one shell segment, so the
    rest of the command after the module's name is searched."""

    def __init__(self, pattern: str, module: str, names: str) -> None:
        self.pattern = pattern
        self._module = _Lazy(module)
        self._call = _Lazy(rf"(?<![\w])(?:{names})(?![\w])\s*\("
                           rf"|(?:\.|\bimport\s|,)\s*(?:{names})(?![\w])")

    def search(self, cmd: str) -> re.Match[str] | None:
        mod = self._module.search(cmd)
        return self._call.search(cmd, mod.end()) if mod else None


_SELF_RULES: dict[str, tuple[Any, str, str]] = {r.pattern: (r, action, what) for r, action, what in (
    (_VerbWithFlag("ai-budget-change", _NABLE, rf"(?<!\S)ai-budget{_END}",
                   r"\s--(?:plan-cost|spend-cap|tokens|session-cap|reset)(?![\w-])",
                   flag_anywhere=True),
     "ai_budget_change", "changing its own AI budget"),
    (_VerbWithFlag("cloud-budget-sync", _NABLE, rf"(?<!\S)budget{_END}",
                   r"\s--budget-file(?![\w-])", flag_anywhere=True),
     "budget_change", "changing the cloud budgets the guard checks changes against"),
    (_VerbWithFlag("guard-uninstall", _NABLE, rf"(?<!\S)guard{_END}", rf"\suninstall{_END}"),
     "guard_change", "removing the guard's own hook"),
    (_VerbWithFlag("guard-off", _NABLE, rf"(?<!\S)guard{_END}", rf"\soff{_END}"),
     "guard_change", "turning the guard off"),
    # A one-time approval lets a call the guard stopped through: a person's
    # decision, made in their own terminal (guard_approvals). The CLI refuses
    # without one unless --as names someone, so an agent could sign for them.
    (_VerbWithFlag("guard-approve", _NABLE, rf"(?<!\S)guard{_END}", rf"\sapprove{_END}"),
     "guard_change", "approving, for a person, a call the guard stopped"),
    (_PythonApiCall("guard-approve-api", r"finops(?:\.guard_approvals)?(?![\w.-])",
                    r"approve"),
     "guard_change", "approving, for a person, a call the guard stopped"),
    (_VerbWithFlag("nable-uninstall", _NABLE, rf"(?<!\S)uninstall{_END}", r""),
     "guard_change", "uninstalling nable, the guard's hook with it"),
    # Confirming, rejecting or setting an org fact is a person's decision, and
    # a confirmed fact can change which budget and threshold apply. The CLI
    # refuses without a terminal unless --as names someone, so an agent could
    # otherwise sign a person's name. Reading (status, review, questions,
    # export) stays silent.
    (_VerbWithFlag("org-decide", _NABLE, rf"(?<!\S)org{_END}",
                   rf"\s(?:confirm|reject|set|trust){_END}"),
     "org_change", "deciding an org model fact for a person"),
    (_VerbWithFlag("org-decide-module", r"(?<![\w.-])finops\.org\.cli(?![\w.])",
                   rf"(?<!\S)(?:confirm|reject|set|trust){_END}", r""),
     "org_change", "deciding an org model fact for a person"),
    (_PythonApiCall("org-decide-api", r"finops\.org(?![\w-])",
                    r"confirm|reject|set_fact|confirm_many|reject_many|import_legacy|trust"),
     "org_change", "deciding an org model fact for a person"),
    (_PythonApiCall("guard-off-api", r"finops(?:\.guard_plugin|\.guard)?(?![\w.-])",
                    r"set_off|uninstall"),
     "guard_change", "turning the guard off"),
    # Rolling a learned lesson back, or restoring one, is a person's call too
    # (learning.ledger takes the same HumanDecision), and it changes what
    # nable proposes and how it ranks. Reading (list, show, infer) is silent.
    (_VerbWithFlag("learn-decide", _NABLE, rf"(?<!\S)learn{_END}",
                   rf"\s(?:rollback|restore){_END}"),
     "learning_change", "rolling back or restoring a learned lesson for a person"),
    (_VerbWithFlag("learn-decide-module", r"(?<![\w.-])finops\.cli_learn(?![\w.])",
                   rf"(?<!\S)(?:rollback|restore){_END}", r""),
     "learning_change", "rolling back or restoring a learned lesson for a person"),
    (_PythonApiCall("learn-decide-api", r"finops\.(?:recommendations\.learning|cli_learn)"
                                        r"(?![\w-])", r"rollback|restore"),
     "learning_change", "rolling back or restoring a learned lesson for a person"),
    # The post hook's `ran` is how an ask counts as approved, and repeated
    # approvals are what a higher threshold is learned from. The harness runs
    # the post hook; an agent that runs it, with a payload it wrote (the
    # session and the command are in the readable ledger), turns a person's
    # "no" into a "yes". `--p`, `--po` and `--pos` are argparse's
    # abbreviations of --post, the guard parser's only --p option.
    (_VerbWithFlag("guard-post-hook", _NABLE, rf"(?<!\S)guard{_END}",
                   rf"(?<!\S)--p(?:o(?:st?)?)?{_END}"),
     "learning_change", "recording, for a person, how the guard's ask was answered"),
    (_PythonApiCall("guard-post-hook-api", r"guard_(?:outcome|plugin)(?![\w-])",
                    r"run_post|record|run_hook|hook_main"),
     "learning_change", "recording, for a person, how the guard's ask was answered"),
    (_PythonApiCall("guard-ledger-api", r"guard_ledger(?![\w-])", r"append"),
     "learning_change", "writing the guard's decision ledger, which records the answers"),
    # Installing a pack grants it capabilities (and may add code the broker
    # runs); updating one can change its rules; removing a guard-rule pack
    # takes its asks and denies away; a signature or a key made here is what
    # packs.trusted_keys would trust; a secret set for a pack hands it a
    # credential from nable's vault. Reading (list, audit, search, validate,
    # new, which writes only the directory it is given) stays silent.
    (_VerbWithFlag("pack-change", _NABLE,
                   rf"(?<!\S)pack(?:\s+-\S+)*\s+(?:install|update|remove|sign|keygen"
                   rf"|secret(?:\s+-\S+)*\s+(?:set|remove)){_END}",
                   r""),
     "pack_change", "changing the installed packs, which decide what the guard asks about"),
)}


def _self_change_normalized(cmd: str) -> tuple[str, str] | None:
    if "nable" not in cmd and "finops" not in cmd:
        return None
    for name, (rule, _action, _what) in _SELF_RULES.items():
        if rule.search(cmd):
            return ("self", name)
    return None


def _self_change(command: str) -> dict[str, Any] | None:
    """An ask for a shell command that changes a budget or removes the guard,
    or None. Every reading of the command counts (_worst_reading), so a
    commit message that mentions one does not ask and an alias does not
    hide one."""
    hit = _worst_reading(command, _self_change_normalized)
    if hit is None:
        return None
    _rule, action_type, what = _SELF_RULES[hit[1]]
    shown = " ".join(command.split())
    if len(shown) > _SHOWN_COMMAND_MAX:
        shown = shown[:_SHOWN_COMMAND_MAX - 3] + "..."
    return {"decision": "ask", "action_type": action_type, "door": None,
            "reason": f"nable guard: the agent is {what} (`{shown}`); a human should confirm."}


# ── Writes to the guard's own files ───────────────────────────────────────────
# The org model, the policy file, the installed packs, the off flag, the
# ledger and the harness settings that carry the hook decide what the guard
# allows (guard_paths.protected lists them). `nable org confirm` and `nable
# guard off` ask, but appending a confirmed threshold to nable.org/policy.yaml,
# `touch ~/.finops/guard-off` or `rm -rf ~/.finops/packs/...` used to change
# the same things in silence. So a shell command that writes, moves, deletes,
# links, truncates or changes the permissions of one asks. Reading one (cat,
# grep, less, git diff, cp FROM it) stays silent.
#
# Each shell command is read in the forms the price reader uses (_segmented:
# quotes and escapes dropped, aliases expanded, one command per segment, a
# commit message's quoted text blanked where the shell will not run it), so
# `"~/.fin""ops/guard-off"` and `t\ee` are what the shell will run. A path is
# resolved as the shell would open it: variables assigned earlier on the line
# and in the environment, `~`, a `cd` earlier in the command, braces, globs
# and symlinks (guard_paths.match). A command inside `$(...)`, backticks or
# `<(...)` is read as a command of its own. What it cannot see: a path built
# at run time (the output of `$(...)`, a loop variable) and a write inside a
# script the command runs; `nable guard doctor` lists those gaps.

# A cheap first look: nothing that can write a file is named.
_WRITE_HINT_RE = re.compile(
    r">|(?<![\w.-])(?:rm|rmdir|unlink|shred|mv|cp|ln|install|rsync|tee|sed|perl|ruby|awk|gawk|"
    r"chmod|chown|chgrp|chattr|setfacl|truncate|dd|touch|mkdir|patch|sponge|yq|sd|curl|wget|"
    r"n?vim?|ex|ed|emacs|nano|find|git|python[\d.]*|node|deno|bun|php|pwsh|powershell|"
    r"osascript|lua)(?![\w-])")
# Output redirections into a file: `>`, `>>`, `>|`, `&>`, `2>`, `<>` (not
# `>&2`). The target may be written right against the operator.
_REDIRECT_RE = re.compile(r"(?:\d+|&)?(?:>>?\|?|<>)(?![&>])\s*([^\s;&|<>()]+)")
_INPUT_RE = re.compile(r"\d*<(?:<-?|<<)?\s*[^\s;&|<>()]+")
_SPECIAL_FILES = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty")
_ASSIGN_RE = re.compile(r"[A-Za-z_]\w*=")
# Words that run the rest of the words as a command: the flags each takes a
# value for, so `sudo -u root rm x` is rm.
_WRAPPERS: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "--user",
                       "--group"}),
    "doas": frozenset({"-u", "-C"}), "env": frozenset({"-u", "-C", "--unset", "--chdir"}),
    "nice": frozenset({"-n"}), "ionice": frozenset({"-c", "-n", "-p"}),
    "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
    "xargs": frozenset({"-I", "-n", "-P", "-L", "-d", "-a", "-E", "-s"}),
    "stdbuf": frozenset(), "nohup": frozenset(), "time": frozenset(), "command": frozenset(),
    "exec": frozenset({"-a"}), "builtin": frozenset(), "setsid": frozenset(),
    "unbuffer": frozenset(), "caffeinate": frozenset(), "chronic": frozenset(),
    "then": frozenset(), "do": frozenset(), "else": frozenset(), "if": frozenset(),
    "while": frozenset(), "until": frozenset(), "!": frozenset(), "{": frozenset(),
    "eval": frozenset(),
}
# Wrappers whose first plain word is theirs, not the command's.
_WRAPPER_ARG = {"timeout": 1, "flock": 1}
# Project runners: `uv run python -c ...` runs python. The flags each takes a
# value for, so `uv run --with x python` is python.
_RUNNERS: dict[str, frozenset[str]] = {
    "uv": frozenset({"--with", "-w", "--python", "-p", "--project", "--directory", "--package",
                     "--extra", "--group", "--env-file", "--index", "--index-url",
                     "--default-index", "--extra-index-url", "--with-requirements",
                     "--with-editable", "--only-group", "--no-group", "--config-file",
                     "--cache-dir"}),
    "poetry": frozenset({"-C", "--directory", "-P", "--project"}),
    "pipenv": frozenset(), "pdm": frozenset({"-p", "--project"}),
    "hatch": frozenset({"-e", "--env"}),
    "conda": frozenset({"-n", "--name", "-p", "--prefix"}),
}
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "busybox"})
_DECLARE = frozenset({"export", "declare", "local", "readonly", "typeset"})
_DELETES = frozenset({"rm", "unlink", "shred", "rmdir", "srm", "trash", "trash-put", "gio"})
_PERMS = frozenset({"chmod", "chown", "chgrp", "chattr", "setfacl", "xattr"})
_WRITES = frozenset({"tee", "touch", "mkdir", "truncate", "sponge", "sd", "patch"})
_EDITORS = frozenset({"vi", "vim", "nvim", "ex", "ed", "emacs", "nano", "micro", "hx", "kak",
                      "joe", "mcedit"})
_COPIES = frozenset({"cp", "install", "rsync", "ln", "mv", "scp"})
_INTERPRETERS_RE = re.compile(r"(?:python[\d.]*|node(?:js)?|deno|bun|ruby|perl|php|pwsh|"
                              r"powershell|osascript|lua|tclsh)")
# Code that writes, deletes or runs something, in a one-liner or a heredoc.
_CODE_WRITE_RE = re.compile(
    r"write_(?:text|bytes)|\.write\s*\(|"
    r"unlink|remove|rmtree|rename|replace\s*\(|truncate|chmod|chown|symlink|copy|move\s*\(|"
    r"mkdir|makedirs|touch\s*\(|writeFile|appendFile|rmSync|rmdir|system\s*\(|subprocess|"
    r"popen|fopen|file_put_contents|Set-Content|Out-File|Remove-Item|Add-Content|New-Item|"
    r"Copy-Item|Move-Item|shutil|exec")
# open() with a mode that writes, the call's arguments read up to
# _OPEN_ARGS_MAX characters on (nested calls included: `open(os.path.
# expanduser('~/x'), 'w')`). The two halves are found separately and paired
# by position, so a run of `open(f, ` costs one pass, never one per call.
_OPEN_CALL_RE = _Lazy(r"open\s{0,8}\(")
_OPEN_MODE_RE = _Lazy(r",\s{0,8}(?:mode\s{0,8}=\s{0,8})?[rbt]{0,3}[wax+]")
_OPEN_ARGS_MAX = 512
_CODE_TOKEN_SPLIT_RE = re.compile(r"[^\w.~${}/\-]+")
_CODE_TOKENS_MAX = 400
# The code of an interpreter one-liner: `python3 -c CODE`, `node -e CODE`,
# `perl -ne CODE`, `ruby -e CODE`. Code longer than _CODE_MAX_CHARS asks,
# as a command over MAX_JUDGED_CHARS does: the checks on code are
# heuristics, and a human should read that much of it.
_ONE_LINER_RE = _Lazy(
    r"(?<![\w.-])(?:python[\d.]*|node(?:js)?|perl|ruby)(?:\s+-[\w=-]*+)*?"
    r"\s+(?:-[A-Za-z]*[ceE]|--eval|--print)(?=[\s'\"$])\s*")
_UNQUOTED_WORD_RE = _Lazy(r"\S*")
_CODE_MAX_CHARS = 16 * 1024


def _code_writes(form: str) -> bool:
    """Does the code in `form` write, delete or run something? Linear."""
    if _CODE_WRITE_RE.search(form):
        return True
    import bisect
    opens = [m.end() for m in _OPEN_CALL_RE.finditer(form)]
    if not opens:
        return False
    for m in _OPEN_MODE_RE.finditer(form, opens[0]):
        k = bisect.bisect_right(opens, m.start())
        if k and m.start() - opens[k - 1] <= _OPEN_ARGS_MAX:
            return True
    return False


def _oversize_code(command: str) -> dict[str, Any] | None:
    """An ask for an interpreter one-liner whose code is longer than
    _CODE_MAX_CHARS, else None. Each one's code is measured once."""
    if len(command) <= _CODE_MAX_CHARS:
        return None
    covered = 0
    for m in _ONE_LINER_RE.finditer(command):
        at = m.end()
        if at < covered or at >= len(command):
            continue
        word = (_SHELL_LEX_RE.match(command, at) if command[at] in "'\"$" else None) \
            or _UNQUOTED_WORD_RE.match(command, at)
        covered = word.end()
        if covered - at > _CODE_MAX_CHARS:
            return {"decision": "ask", "action_type": "oversize_command", "door": None,
                    "reason": (f"nable guard: this command runs {(covered - at) / 1024:,.0f} KB "
                               "of code in an interpreter one-liner, longer than the "
                               f"{_CODE_MAX_CHARS // 1024} KB the guard reads in one. A human "
                               "should read it before it runs.")}
    return None


def _next_plain(words: list[str], i: int, takes: frozenset[str]) -> int:
    """Index of the first word from `i` that is not a flag (or a flag's value)."""
    while i < len(words):
        w = words[i]
        if w == "--":
            return i + 1
        if not w.startswith("-") or w == "-":
            return i
        i += 2 if w in takes else 1
    return i


def _plain_args(words: list[str]) -> list[str]:
    """The words that are not flags; everything after `--` counts."""
    out, rest = [], False
    for w in words:
        if rest or not w.startswith("-") or w == "-":
            out.append(w)
        elif w == "--":
            rest = True
    return out


def _flag_value(words: list[str], shorts: tuple[str, ...], longs: tuple[str, ...]) -> str | None:
    """The value of a flag given as `-t DIR`, `-tDIR`, `--target DIR` or
    `--target=DIR`, or None."""
    for i, w in enumerate(words):
        for s in shorts:
            if w == s and i + 1 < len(words):
                return words[i + 1]
            if w.startswith(s) and len(w) > len(s) and not w.startswith("--"):
                return w[len(s):]
            # A cluster that ends in the flag takes the next word: `curl -so FILE`.
            if (len(w) > 2 and w[0] == "-" and w[1] != "-" and w[-1] == s[-1]
                    and w[1:].isalpha() and i + 1 < len(words)):
                return words[i + 1]
        for lg in longs:
            if w == lg and i + 1 < len(words):
                return words[i + 1]
            if w.startswith(lg + "="):
                return w[len(lg) + 1:]
    return None


def _has_short(words: list[str], letters: str, stop: str = "") -> bool:
    """A short flag cluster carries one of `letters` (`-pi` has i). Scanning a
    cluster stops at a letter in `stop`, whose value follows attached
    (perl's `-MFile::Find` is not -i)."""
    for w in words:
        if w == "--":
            return False
        if not w.startswith("-") or w.startswith("--") or len(w) < 2:
            continue
        for ch in w[1:]:
            if ch in letters:
                return True
            if ch in stop:
                break
    return False


# The paths one command's check looks at, however many readings name them,
# and the work it may do on them (guard_paths.meter: a unit is a realpath of
# a path of a dozen parts, or a directory listing of a few dozen names). A command that
# names more than this asks instead, so that one padded with thousands of
# `a* b* ...` or `cd a/a/a/...` still gets its answer in the hook's time.
_PATHS_MAX = 2048
_PATH_UNITS = 4096


class _TooManyPaths(Exception):
    pass


class _PathChecks:
    """What one command's paths were found to be, each looked at once."""

    __slots__ = ("left", "seen")

    def __init__(self) -> None:
        self.seen: dict[Any, Any] = {}
        self.left = _PATHS_MAX

    def once(self, key: Any, look: Any) -> Any:
        if key in self.seen:
            return self.seen[key]
        self.left -= 1
        if self.left < 0:
            raise _TooManyPaths
        out = self.seen[key] = look()
        return out


def _env_key(path: str, env: dict[str, str] | None) -> tuple[Any, ...] | None:
    """The values of the variables `path` reads, and of those theirs read:
    what a look at it depends on besides the path."""
    if "$" not in path or not env:
        return None
    from . import guard_paths
    out: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    todo = [path]
    for _ in range(4):                  # as deep as guard_paths expands them
        nxt = []
        for text in todo:
            for a, b in guard_paths._VAR_RE.findall(text):
                if (a or b) not in seen:
                    seen.add(a or b)
                    val = env.get(a or b)
                    out.append((a or b, val))
                    if val and "$" in val:
                        nxt.append(val)
        todo = nxt
    return tuple(out)


def _copy_targets(prog: str, args: list[str], base: str = "",
                  env: dict[str, str] | None = None, entries: list[Any] = (),
                  checks: _PathChecks | None = None) -> list[tuple[str, bool]]:
    """(path, ancestors too) for what cp, install, rsync, ln, scp and mv
    write: the destination, and the file each source lands as in it when the
    destination is a directory (a glob source, as each name it expands to).
    mv also takes its sources away, and ln links to them."""
    from . import guard_paths
    target = _flag_value(args, ("-t",), ("--target-directory",))
    plain = _plain_args(args)
    if target is not None:
        plain = [p for p in plain if p != target]
        srcs, dests = plain, [target]
    elif len(plain) >= 2 or (prog == "ln" and plain):
        srcs, dests = plain[:-1], plain[-1:]
        if prog == "ln" and len(plain) == 1:
            srcs, dests = plain, []
    else:
        return []
    recursive = _has_short(args, "rRa") or any(
        a in ("--recursive", "--archive", "--no-target-directory") for a in args)
    out: list[tuple[str, bool]] = []
    wanted: set[str] | None = None
    for d in dests:
        out.append((d, recursive and (_has_short(args, "T") or any(
            s.endswith(("/.", "/")) for s in srcs))))
        # `cp budget.yml budget.yml.bak` writes budget.yml.bak, not
        # budget.yml.bak/budget.yml: a source lands inside the destination
        # only when that is a directory (or cannot be told not to be one).
        if target is None and len(srcs) == 1 and not d.endswith("/") and prog != "scp":
            real = guard_paths.resolve(d.replace("\x00", " "), base or None, env=env)
            if real is not None and not os.path.isdir(real):
                continue
        for s in srcs:
            name = s.rstrip("/").rsplit("/", 1)[-1]
            if not name or name in (".", ".."):
                continue
            landed = [name]
            if guard_paths._GLOB_CHARS & set(name):
                # `cp config/*.yaml deploy/` lands budget.yaml in deploy/ only
                # when config/ holds one: the names the glob expands to that
                # could be (or lead to) a protected file.
                src = s.rstrip("/").replace("\x00", " ")
                names = (checks or _PathChecks()).once(
                    ("glob", src, base, _env_key(src, env)),
                    lambda src=src: guard_paths.glob_names(src, base or None, env=env))
                wanted = wanted if wanted is not None else _landing_names(entries)
                landed = sorted(wanted) if names is None else [
                    n for n in names if (n.casefold() if guard_paths._FOLD else n) in wanted]
            out += [(f"{d.rstrip('/')}/{n}", recursive) for n in landed]
    if prog == "mv" or (prog == "rsync" and "--remove-source-files" in args):
        out += [(s, True) for s in srcs]
    elif prog == "ln":
        out += [(s, False) for s in srcs]
    return out


def _landing_names(entries: list[Any]) -> set[str]:
    """The file names a copy could land as and change something protected:
    each part of each protected path, a budget file's name, nable.org."""
    from . import guard_paths
    out = {part for e in entries for part in e.path.split(os.sep) if part}
    return out | set(guard_paths.PROTECTED_NAMES) | {guard_paths.ORG_DIR_NAME}


def _git_targets(args: list[str], base: str) -> tuple[list[tuple[str, Any]], str]:
    """(targets, directory) for a git command: checkout and restore of paths,
    rm, mv and clean. `git -C dir` and `--work-tree=dir` move the directory."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        w = args[i]
        if w == "-C" and i + 1 < len(args):
            base = os.path.join(base, os.path.expanduser(args[i + 1]))
            i += 2
            continue
        if w.startswith("--work-tree="):
            base = os.path.join(base, os.path.expanduser(w.split("=", 1)[1]))
        i += 2 if w in ("-c", "--git-dir", "--work-tree", "--namespace") else 1
    if i >= len(args):
        return [], base
    sub, rest = args[i], args[i + 1:]
    if sub in ("checkout", "restore"):
        source = any(w in ("-s", "--source") or w.startswith("--source=") for w in rest)
        if sub == "restore" and (_has_short(rest, "S") or "--staged" in rest) and not (
                _has_short(rest, "W") or "--worktree" in rest):
            return [], base             # the index only
        rest = [w for j, w in enumerate(rest)
                if not (j and rest[j - 1] in ("-b", "-B", "--orphan", "-s", "--source"))]
        paths = _plain_args(rest)
        if sub == "checkout" and "--" in rest and any(
                not w.startswith("-") for w in rest[:rest.index("--")]):
            source = True               # `git checkout HEAD~3 -- .`
            paths = rest[rest.index("--") + 1:]
        elif sub == "checkout" and len(paths) > 1:
            source = True               # `git checkout HEAD~3 .`: a commit, then paths
        # A path named, and what git would put back under one (`git checkout
        # .` puts back every changed file under it, the org model's among
        # them; from another commit, every tracked one).
        mode = "tracked" if source else "changed"
        return [(p, False) for p in paths] + [(p, mode) for p in paths], base
    if sub == "stash" and (not rest or rest[0] in ("push", "save", "pop", "apply")
                           or rest[0].startswith("-")):
        # It takes back the changes to every tracked file in the repo (or
        # under the pathspecs after `--`), untracked ones too with -u or -a;
        # pop and apply put back whatever the stash holds.
        from . import guard_paths
        spec = rest[rest.index("--") + 1:] if "--" in rest else []
        root = guard_paths.git_root(base)
        if rest and rest[0] in ("pop", "apply"):
            mode = "tracked"
        elif _has_short(rest, "ua") or any(w in ("--include-untracked", "--all") for w in rest):
            mode = "exists"
        else:
            mode = "changed"
        return [(p, mode) for p in (spec or [str(root) if root else "."])], base
    if sub == "rm" and "--cached" in rest:
        # The index only: the files stay, but the next commit drops what is
        # tracked from the repo (nable.org/ with it) for everyone else.
        return [(p, "tracked") for p in _plain_args(rest)], base
    if sub in ("rm", "mv"):
        return [(p, True) for p in _plain_args(rest)], base
    if sub == "clean" and (_has_short(rest, "f") or "--force" in rest) and not (
            _has_short(rest, "n") or "--dry-run" in rest):
        # It deletes what is untracked, so only a protected file that is there.
        return [(p, "exists") for p in (_plain_args(rest) or ["."])], base
    return [], base


def _write_targets(prog: str, args: list[str], base: str,
                   env: dict[str, str] | None = None, entries: list[Any] = (),
                   checks: _PathChecks | None = None) -> tuple[list[tuple[str, Any]], str]:
    """(path, ancestors) for each path this command writes, and the
    directory its relative paths are from. `ancestors` is True when writing
    to a directory also changes what is in it (rm -r, mv, chmod -R), and
    "exists" when only what is there is touched (a clean of untracked files)."""
    if prog in _DELETES:
        return [(p, True) for p in _plain_args(args)], base
    if prog in _PERMS:
        return [(p, True) for p in _plain_args(args)], base
    if prog in _WRITES or prog in _EDITORS:
        return [(p, False) for p in _plain_args(args)], base
    if prog in _COPIES:
        return _copy_targets(prog, args, base, env, entries, checks), base
    if prog == "dd":
        return [(a[3:], False) for a in args if a.startswith("of=")], base
    if prog == "sed" and (_has_short(args, "i") or any(a.startswith("--in-place") for a in args)):
        return [(p, False) for p in _plain_args(args)], base
    if prog in ("perl", "ruby") and _has_short(args, "i", stop="eEMmIlx0CFKr"):
        return [(p, False) for p in _plain_args(args)], base
    if prog in ("awk", "gawk") and ("inplace" in args or "--include=inplace" in args):
        return [(p, False) for p in _plain_args(args)], base
    if prog == "yq" and (_has_short(args, "i") or "--inplace" in args):
        return [(p, False) for p in _plain_args(args)], base
    if prog == "curl":
        out = _flag_value(args, ("-o",), ("--output",))
        return ([(out, False)] if out else []), base
    if prog == "wget":
        out = _flag_value(args, ("-O",), ("--output-document",))
        into = _flag_value(args, ("-P",), ("--directory-prefix",))
        return [(p, False) for p in (out, into) if p], base
    if prog == "find" and any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir")
                              for a in args):
        # It deletes (or runs something on) what it finds: a protected file
        # that is there, under a starting point, with a name its -name tests
        # allow. `find . -name '*.pyc' -delete` finds none of them.
        names = tuple((args[i + 1], a == "-iname") for i, a in enumerate(args[:-1])
                      if a in ("-name", "-iname"))
        if len(names) > _FIND_NAMES_MAX:
            names = ()                  # read as no filter: anything it finds
        starts = []
        for a in args:
            if a.startswith(("-", "(", "!")):
                break
            starts.append((a, ("exists", names)))
        return starts or [(".", ("exists", names))], base
    if prog == "git":
        return _git_targets(args, base)
    return [], base


def _command_words(words: list[str], env: dict[str, str]) -> list[str]:
    """The words of the command a segment runs: assignments recorded in
    `env` and taken off, wrappers (sudo, env, timeout, xargs, ...) and
    `sh -c` taken off. [] for a segment that only assigns."""
    i = 0
    while i < len(words):
        w = words[i].lstrip("({")
        if not w:
            i += 1
            continue
        if _ASSIGN_RE.match(w):
            name, _, val = w.partition("=")
            env[name] = val
            i += 1
            continue
        prog = w.rsplit("/", 1)[-1]
        if prog in _DECLARE:
            for a in words[i + 1:]:
                if _ASSIGN_RE.match(a):
                    name, _, val = a.partition("=")
                    env[name] = val
            return []
        if prog in _WRAPPERS or prog == "flock":
            i = _next_plain(words, i + 1, _WRAPPERS.get(prog, frozenset()))
            i += _WRAPPER_ARG.get(prog, 0)
            continue
        if prog in _RUNNERS:
            j = _next_plain(words, i + 1, _RUNNERS[prog])
            if prog == "uv" and words[j:j + 2] == ["tool", "run"]:
                j += 1
            if j < len(words) and words[j] == "run":
                i = _next_plain(words, j + 1, _RUNNERS[prog])
                continue
        if prog in _SHELLS:
            j = i + 1
            if prog == "busybox" and j < len(words) and words[j].rsplit("/", 1)[-1] in _SHELLS:
                j += 1
            flag = next((n for n, a in enumerate(words[j:])
                         if a.startswith("-") and not a.startswith("--") and "c" in a[1:]), None)
            if flag is not None:
                i = j + flag + 1
                continue
        return [prog, *words[i + 1:]]
    return []


def _code_target(form: str, cwd: str | None, entries: list[Any]) -> Any:
    """For a command that runs an interpreter: a protected path the code
    names, when the code also writes, deletes or runs something."""
    if not _code_writes(form):
        return None
    from . import guard_paths
    by_name = {os.path.basename(e.path): e for e in entries}
    looked: set[str] = set()
    for tok in _CODE_TOKEN_SPLIT_RE.split(form):
        if not tok:
            continue
        name = tok.rstrip("/").rsplit("/", 1)[-1]
        if name in guard_paths.DISTINCTIVE_NAMES:
            return by_name.get(name) or guard_paths.Protected(tok, tok, f"{name}, one of the "
                                                                   "guard's own files")
        if ("/" in tok or tok.startswith("~")) and tok not in looked \
                and len(looked) < _CODE_TOKENS_MAX:
            looked.add(tok)
            hit = guard_paths.match(tok, cwd, entries=entries, ancestors=True)
            if hit is not None:
                return hit
    return None


# The entries of a protected tree find's -name tests are tried against
# before the tree is taken to hold a match, and the -name tests read.
_FIND_WALK_MAX = 2048
_FIND_NAMES_MAX = 64


def _find_names_reach(entry: Any, names: tuple[tuple[str, bool], ...]) -> bool:
    """Could `find ... -name PATTERN` reach `entry`? Its own name, or for a
    tree (`find nable.org -name '*.yaml' -exec sed -i ...`), any name inside
    it. A tree larger than _FIND_WALK_MAX entries is taken to hold one."""
    import fnmatch

    def hit(name: str) -> bool:
        return any(fnmatch.fnmatchcase(name.casefold(), n.casefold()) if ci
                   else fnmatch.fnmatchcase(name, n) for n, ci in names)
    if hit(os.path.basename(entry.path)):
        return True
    if not entry.tree:
        return False
    from . import guard_paths
    seen = 0
    for _dirpath, dirnames, filenames in os.walk(entry.path):
        seen += len(dirnames) + len(filenames) + 1
        guard_paths.charge(1 + (len(dirnames) + len(filenames)) // guard_paths._UNIT_ENTRIES)
        if seen > _FIND_WALK_MAX or any(hit(n) for n in (*dirnames, *filenames)):
            return True
    return False


def _spaced(form: str, entries: list[Any]) -> str:
    """A protected path with a space in it (`Application Support`) kept as
    one word: its spaces become NUL (never whitespace, never in a real path),
    which hit_of and cd turn back."""
    for e in entries:
        for p in (e.path, e.shown):
            if " " in p and p in form:
                form = form.replace(p, p.replace(" ", "\x00"))
    return form


def _git_would_change(path: str, mode: str, where: str, env: dict[str, str],
                      entries: list[Any]) -> Any:
    """The protected entry under `path` that a checkout, restore or stash
    there would change: one git tracks ("tracked"), and ("changed") has
    changed since HEAD last moved. Where git's records cannot be read, one
    that is there."""
    from . import guard_paths
    real = guard_paths.resolve(path, where, env=env)
    if real is None:
        return None
    root = guard_paths.git_root(real)
    for e in entries:
        if not (guard_paths.is_under(e.path, real)
                or (e.tree and guard_paths.is_under(real, e.path))):
            continue
        tracked = guard_paths.git_tracked(e.path, str(root), e.tree) if root else None
        if tracked is None:
            if os.path.lexists(e.path):
                return e
        elif tracked and (mode == "tracked" or guard_paths.git_changed(
                e.path, str(root), e.tree) is not False):
            return e
    return None


# What starts a command substitution (`$(...)`, a backtick) or a process
# substitution (`<(...)`, `>(...)`), and the parentheses that close one.
_SUBST_TOKEN_RE = _Lazy(r"\$\(|[<>]\(|[()`]")
_SUBST_OPENERS = ("$(", "<(", ">(")
# Between the bodies of substitutions judged together: a word no command has,
# which puts the directory back where the command's own `cd`s left it.
_BODY_BREAK = "\x01"
_BODY_BASES_MAX = 8


def _substitutions(form: str) -> tuple[str, list[str]]:
    """(`form` with the body of each substitution in it replaced by a plain
    word, those bodies with theirs replaced likewise). `x=$(touch F)` writes F
    as surely as `touch F` does, but read as words it is an assignment and a
    word `F)`. One pass: each character is copied once, however deep the
    nesting. An unterminated body runs to the end."""
    stack: list[list[Any]] = [[[], 0, None]]      # [parts, open parens, opener]
    bodies: list[str] = []
    last = 0
    for m in _SUBST_TOKEN_RE.finditer(form):
        top = stack[-1]
        top[0].append(form[last:m.start()])
        last = m.end()
        tok = m.group()
        if tok in _SUBST_OPENERS or (tok == "`" and top[2] != "`"):
            top[0].append(" _ ")
            stack.append([[], 0, tok])
        elif len(stack) > 1 and (tok == "`" or (tok == ")" and top[1] == 0 and top[2] != "`")):
            bodies.append("".join(stack.pop()[0]))
        else:
            top[1] = top[1] + 1 if tok == "(" else max(0, top[1] - (tok == ")"))
            top[0].append(tok)
    stack[-1][0].append(form[last:])
    while len(stack) > 1:
        bodies.append("".join(stack.pop()[0]))
    return "".join(stack[0][0]), bodies


def _protected_target(form: str, cwd: str | None, entries: list[Any],
                      checks: _PathChecks | None = None) -> Any:
    """The protected entry one normalized form of a command writes to, or None.
    The body of each substitution is judged as a command of its own, from
    every directory a `cd` in the command moves to."""
    # `>|` (write even under noclobber) is a redirection, not a pipe.
    form = _spaced(form, entries).replace(">|", "> ")
    base = cwd or os.getcwd()
    checks = checks or _PathChecks()
    if "(" not in form and "`" not in form:
        return _protected_commands(form, form, base, entries, {}, [], checks)
    outer, bodies = _substitutions(form)
    env: dict[str, str] = {}
    bases: list[str] = []
    hit = _protected_commands(outer, form, base, entries, env, bases, checks)
    if hit is not None or not bodies:
        return hit
    joined = f" ; {_BODY_BREAK} ; ".join(bodies)
    if not _WRITE_HINT_RE.search(joined):
        return None
    for b in dict.fromkeys([base, *bases][:_BODY_BASES_MAX]):
        hit = _protected_commands(joined, joined, b, entries, dict(env), [], checks)
        if hit is not None:
            return hit
    return None


def _protected_commands(form: str, code: str, base: str, entries: list[Any],
                        env: dict[str, str], bases: list[str], checks: _PathChecks) -> Any:
    """_protected_target over the commands of `form`, from `base`, with `env`
    (assignments, updated) and `bases` (each `cd`, appended). `code` is what
    an interpreter's code is read from."""
    from . import guard_paths
    start = base
    interpreter = False

    def hit_of(path: str, ancestors: Any = False, where: str | None = None) -> Any:
        path = path.replace("\x00", " ")
        if path in _SPECIAL_FILES or path.startswith("/dev/fd/") or not path:
            return None
        key = (path, repr(ancestors), where or base, _env_key(path, env))
        return checks.once(key, lambda: look(path, ancestors, where))

    def look(path: str, ancestors: Any, where: str | None) -> Any:
        if ancestors in ("changed", "tracked"):
            return _git_would_change(path, ancestors, where or base, env, entries)
        if isinstance(ancestors, tuple) or ancestors == "exists":
            # Only what is there: a clean of untracked files, a find -delete
            # (whose -name patterns, when it has any, must match a name in
            # it), a checkout or stash that puts files back.
            names = ancestors[1] if isinstance(ancestors, tuple) else ()
            real = guard_paths.resolve(path, where or base, env=env)
            if real is None:
                return None
            for e in entries:
                if e.tree and guard_paths.is_under(real, e.path) and os.path.lexists(real):
                    return e            # it starts inside a protected tree
                if (guard_paths.is_under(e.path, real) and os.path.lexists(e.path)
                        and (not names or _find_names_reach(e, names))):
                    return e
            return None
        return guard_paths.match(path, where or base, entries=entries, ancestors=ancestors,
                                 env=env)

    for seg, _at in _commands_in(form):
        if seg.strip() == _BODY_BREAK:
            base = start
            continue
        for m in _REDIRECT_RE.finditer(seg):
            hit = hit_of(m.group(1))
            if hit is not None:
                return hit
        seg = _INPUT_RE.sub(" ", _REDIRECT_RE.sub(" ", seg))
        words = _command_words(seg.split(), env)
        if not words:
            continue
        prog, args = words[0], words[1:]
        if prog in ("cd", "pushd"):
            plain = _plain_args(args)
            dest = plain[0] if plain else "~"
            if dest != "-":
                dest = dest.replace("\x00", " ")
                dest = checks.once(("cd", dest, base, _env_key(dest, env)),
                                   lambda d=dest, b=base: guard_paths.resolve(d, b, env=env))
                base = dest or base
                if len(bases) < _BODY_BASES_MAX:
                    bases.append(base)
            continue
        if _INTERPRETERS_RE.fullmatch(prog):
            interpreter = True
        targets, where = _write_targets(prog, args, base, env, entries, checks)
        for path, ancestors in targets:
            hit = hit_of(path, ancestors, where)
            if hit is not None:
                return hit
    if interpreter:
        return _code_target(code, start, entries)
    return None


def _protected_write(command: str, cwd: str | None = None) -> dict[str, Any] | None:
    """An ask for a shell command that writes to one of the guard's own
    files (guard_paths), or None. Every reading of the command counts; a
    path only in a commit message's quoted text does not."""
    if not _WRITE_HINT_RE.search(command) and not _WRITE_HINT_RE.search(
            command.replace("\\", "").replace('"', "").replace("'", "")):
        return None
    from . import guard_paths
    entries = guard_paths.protected(cwd)
    found: dict[str, Any] = {}
    checks = _PathChecks()

    def judge(form: str) -> tuple[str, str] | None:
        hit = _protected_target(form, cwd, entries, checks)
        if hit is None:
            return None
        found[hit.path] = hit
        return ("protected", hit.path)

    r = _readings(command)
    token = guard_paths.meter(_PATH_UNITS)
    try:
        for masked in (True, False):
            for split in (False, True):
                h = judge(_segmented(command, split, masked))
                if h is not None and (masked or not _only_in_data(r, h, judge)):
                    return _protected_verdict(found[h[1]], command=command)
    except (_TooManyPaths, guard_paths.TooMuch):
        return _too_many_paths_verdict()
    finally:
        guard_paths.unmeter(token)
    return None


def _too_many_paths_verdict() -> dict[str, Any]:
    return {"decision": "ask", "action_type": "oversize_command", "door": None,
            "reason": ("nable guard: this command names more paths than the guard checks for "
                       "its own files before its hook times out. A human should read it "
                       "before it runs.")}


def _protected_verdict(entry: Any, *, command: str | None = None,
                       tool: str | None = None) -> dict[str, Any]:
    if command is not None:
        shown = " ".join(command.split())
        if len(shown) > _SHOWN_COMMAND_MAX:
            shown = shown[:_SHOWN_COMMAND_MAX - 3] + "..."
        how = f"`{shown}`"
    else:
        how = f"its {tool} tool"
    return {"decision": "ask", "action_type": "protected_write", "door": None,
            "protected_path": entry.shown,
            "reason": (f"nable guard: the agent is changing {entry.what} ({entry.shown}) with "
                       f"{how}. That file decides what the guard allows, so a human should "
                       "confirm.")}


def gate_editor(tool_name: str, tool_input: Any, *, cwd: str | None = None,
                harness: str = "claude-code", session_id: str | None = None,
                record: bool = True) -> dict[str, Any] | None:
    """Claude Code's file tools (Write, Edit, MultiEdit, NotebookEdit): an
    ask when the file is one of the guard's own (guard_paths), else None and
    nothing recorded. guard_plugin answers the None case before the guard is
    imported; this is the same check, for a call that reaches the guard."""
    from . import guard_plugin
    path = None
    try:
        found = guard_plugin.editor_target(tool_name, tool_input, cwd)
        if found is None:
            return None
        entry, path = found
        v = {**_protected_verdict(entry, tool=tool_name), "harness": harness}
        if record:
            _record(v, tool=tool_name, command=f"{tool_name} {path}", session_id=session_id)
        return v
    except Exception as exc:
        if record:
            _record_fail_open(exc, harness=harness, tool=tool_name,
                              command=f"{tool_name} {path}" if path else None,
                              session_id=session_id)
        return None


# MCP tools: a command line in the arguments of any tool (a shell server's
# {"command": "rm ~/.finops/guard-off"}), and a path argument of a tool whose
# name says it writes (mcp__filesystem__write_file {"path": ...}). The name is
# read a word at a time (write_file, writeFile, createDirectory), so compute
# and output are not put.
_MCP_NAME_WORD_RE = _Lazy(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
_MCP_WRITE_WORDS = frozenset({
    "write", "overwrite", "edit", "create", "move", "mv", "rename", "delete", "del", "remove",
    "rm", "rmdir", "unlink", "patch", "append", "replace", "put", "upload", "copy", "cp",
    "save", "mkdir", "makedirs", "touch", "chmod", "chown", "trash", "insert"})
_MCP_DELETE_WORDS = frozenset({"move", "mv", "rename", "delete", "del", "remove", "rm",
                               "rmdir", "unlink", "trash"})
# A verb run into the noun it acts on: writefile, mkdirs, deletefiles.
_MCP_NOUN_RE = _Lazy(r"(?:s|d|file|files|dir|dirs|directory|directories|text|bytes|"
                     r"object|objects|blob|path|paths|content|contents|notebook|cell)?")
_MCP_PATH_KEYS = frozenset({"path", "paths", "file_path", "filepath", "file", "filename",
                            "file_name", "destination", "dest", "target", "target_path",
                            "source", "src", "new_path", "old_path", "notebook_path",
                            "directory", "dir"})
_MCP_WALK_MAX = 512
# A key the walk checks, in the JSON of the arguments it did not reach.
_MCP_CHECKED_KEY_RE = _Lazy(
    r'"(?:path|paths|file_path|filepath|file|filename|file_name|destination|dest|target|'
    r'target_path|source|src|new_path|old_path|notebook_path|directory|dir|command|commands|'
    r'cmd|script|args|arguments|argv|input|code|cli_command|shell)"\s*:', re.IGNORECASE)


def _mcp_name_says(name: str, verbs: frozenset[str]) -> bool:
    """Does one word of an MCP tool's name (or a verb with its noun run on)
    say it is one of `verbs`?"""
    for w in _MCP_NAME_WORD_RE.findall(name):
        w = w.lower()
        if w in verbs or any(w.startswith(v) and _MCP_NOUN_RE.fullmatch(w, len(v))
                             for v in verbs):
            return True
    return False


def _protected_mcp(tool_name: str, arguments: Any) -> dict[str, Any] | None:
    """An ask for an MCP call that writes one of the guard's own files (a
    path argument of a tool that writes, or a command line), or None. Metered
    as the shell check is."""
    from . import guard_paths
    token = guard_paths.meter(_PATH_UNITS)
    try:
        return _protected_mcp_walk(tool_name, arguments)
    except guard_paths.TooMuch:
        return {**_too_many_paths_verdict(), "summary": tool_name.rsplit("__", 1)[-1]}
    finally:
        guard_paths.unmeter(token)


def _protected_mcp_walk(tool_name: str, arguments: Any) -> dict[str, Any] | None:
    from . import guard_paths
    from .guard_mcp import _COMMAND_KEYS

    name = tool_name.rsplit("__", 1)[-1]
    writes = _mcp_name_says(name, _MCP_WRITE_WORDS)
    ancestors = _mcp_name_says(name, _MCP_DELETE_WORDS)
    entries: list[Any] | None = None
    stack: list[tuple[Any, str, int]] = [(arguments, "", 0)]
    seen = 0
    while stack and seen < _MCP_WALK_MAX:
        v, key, depth = stack.pop()
        seen += 1
        if isinstance(v, dict) and depth < 8:
            # Paths and command lines last onto the stack, so first off it.
            items = [(x, str(k).lower(), depth + 1) for k, x in v.items()]
            stack += sorted(items, key=lambda it: it[1] in _MCP_PATH_KEYS
                            or it[1] in _COMMAND_KEYS)
        elif isinstance(v, list) and depth < 8:
            stack += [(x, key, depth + 1) for x in reversed(v)]
        elif isinstance(v, str) and v.strip():
            if key in _COMMAND_KEYS and len(v) <= MAX_JUDGED_CHARS:
                hit = _self_change(v) or _protected_write(v) or _oversize_code(v)
                if hit is not None:
                    return {**hit, "summary": f"{name} {v.strip()[:_SHOWN_VALUE_MAX]}"}
            elif writes and key in _MCP_PATH_KEYS:
                if entries is None:
                    entries = guard_paths.protected()
                e = guard_paths.match(v.strip(), entries=entries, ancestors=ancestors)
                if e is not None:
                    return {**_protected_verdict(e, tool=tool_name),
                            "summary": f"{name} {v.strip()[:_SHOWN_VALUE_MAX]}"}
    if stack and writes and (
            any(k in _MCP_PATH_KEYS or k in _COMMAND_KEYS for _v, k, _d in stack)
            or _MCP_CHECKED_KEY_RE.search(json.dumps([v for v, _k, _d in stack], default=str))):
        # More arguments than the guard reads, from a tool that writes files,
        # and a path or a command line among those left: it could be one of
        # the guard's own files.
        return {"decision": "ask", "action_type": "protected_write", "door": None,
                "reason": (f"nable guard: {tool_name} writes files, and its arguments hold more "
                           f"than the {_MCP_WALK_MAX} values the guard checks for the guard's "
                           "own files. A human should confirm."),
                "summary": f"{name} ({_MCP_WALK_MAX}+ values)"}
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


# Ledger writes held back while a hook answers (see answer_first).
_PENDING: list[dict[str, Any]] | None = None
# The tool call a Claude Code hook is judging (its payload's tool_use_id),
# recorded with the verdict so the post hook's outcome can name its ask.
_TOOL_USE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "nable_tool_use_id", default=None)


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
            # The installed pack rules that matched (pack:rule), whether or
            # not they tightened the verdict; the pack whose price book priced
            # the change; the guard's own file a write would have changed.
            **({"pack_rules": v["pack_rules"]} if v.get("pack_rules") else {}),
            **({"price_book": est["price_book"]} if est.get("price_book") else {}),
            **({"protected_path": guard_ledger.redact(v["protected_path"], limit=300)}
               if v.get("protected_path") else {}),
            # Who owns what the call touched, when the org model said (cited
            # in the reason too); confirmed False marks a proposal.
            **({"owner": {k: (guard_ledger.redact(str(x), limit=100)
                              if isinstance(x, str) else x)
                          for k, x in v["owner"].items()}} if v.get("owner") else {}),
            # The org model thresholds the verdict was judged with: figures,
            # the scope that set each, and the file each came from.
            **({"org_thresholds": {k: (guard_ledger.redact(str(x), limit=300)
                                       if isinstance(x, str) else x)
                                   for k, x in v["org_thresholds"].items()}}
               if v.get("org_thresholds") else {}),
            # The change freeze that made this an ask or a deny: its key,
            # scope, end, mode and whether it was confirmed.
            **({"freeze": {k: (guard_ledger.redact(str(x), limit=300)
                               if isinstance(x, str) else x)
                           for k, x in v["freeze"].items()}} if v.get("freeze") else {}),
            # A person approved this exact call from their own terminal
            # (`nable guard approve`): which approval, and who.
            **({"approved_out_of_band": {k: (guard_ledger.redact(str(x), limit=200)
                                             if isinstance(x, str) else x)
                                         for k, x in v["approved_out_of_band"].items()}}
               if v.get("approved_out_of_band") else {}),
            # The one-time approval a deny-only harness's deny carried.
            **({"approval_id": v["approval_id"]} if v.get("approval_id") else {}),
            # What the learning loop reads: the gate rule behind an ask, the
            # scope it was judged in, and the harness's id for the tool call,
            # which the post hook's outcome links back to (guard_outcome).
            **({"rule": v["rule"]} if v.get("rule") else {}),
            **({"scope": {"team": guard_ledger.redact(v["scope"]["team"], limit=100)
                          if v["scope"].get("team") else None,
                          "envs": [guard_ledger.redact(e, limit=40)
                                   for e in v["scope"].get("envs") or []]}}
               if isinstance(v.get("scope"), dict) else {}),
            **({"tool_use_id": _TOOL_USE_ID.get()} if _TOOL_USE_ID.get() else {}),
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

    Handles the Bash tool, MCP tools (`mcp__*`) and the file tools (Write,
    Edit, MultiEdit, NotebookEdit: an edit to one of the guard's own files
    asks); everything else exits 0 with no output. Fails open by design: any error or unknown payload exits 0
    with no output so the guard can never break the user's agent.
    """
    with answer_first():
        return _run_hook(stdin or sys.stdin, stdout or sys.stdout)


def _run_hook(stdin: Any, stdout: Any) -> int:
    tool: Any = None
    token = None
    try:
        payload = json.load(stdin)
        from .guard_outcome import tool_use_id
        token = _TOOL_USE_ID.set(tool_use_id(payload.get("tool_use_id")))
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
        elif tool in _EDITOR_TOOLS:
            verdict = gate_editor(tool, tool_input, cwd=payload.get("cwd"),
                                  harness="claude-code", session_id=session_id)
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
    finally:
        if token is not None:
            _TOOL_USE_ID.reset(token)


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
# the Bash tool, every MCP tool (`mcp__<server>__<tool>`) and the file tools
# Claude Code edits with; run_hook then returns at once for any MCP tool
# guard_mcp does not recognise, and guard_plugin answers a file edit before
# the guard is imported unless the file is one of the guard's own
# (guard_paths): an agent could otherwise Edit nable.org/policy.yaml or the
# settings file that carries this hook, with no shell command to judge.
_HOOK_MATCHER = "^(Bash|mcp__.*|Write|Edit|MultiEdit|NotebookEdit)$"
_EDITOR_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")   # guard_plugin.EDITOR_TOOLS
_LEGACY_MATCHER = "Bash"          # what every release before MCP coverage wrote
# What releases before file-tool coverage wrote; install widens both.
_OLD_MATCHERS = (_LEGACY_MATCHER, "^(Bash|mcp__.*)$")


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
    """Which tool surfaces our installed hook actually sees in this file.
    "editor" is every one of Claude Code's file tools."""
    covered = {"bash": False, "mcp": False, "editor": False}
    for entry, _h in _read_our_hooks(path):
        m = entry.get("matcher")
        covered["bash"] |= matcher_covers(m, "Bash")
        covered["mcp"] |= matcher_covers(m, "mcp__server__call_aws")
        covered["editor"] |= all(matcher_covers(m, t) for t in _EDITOR_TOOLS)
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

# Claude Code blocks the tool call when a PreToolUse hook exits 2, and uvx
# exits 2 when it cannot reach the package index (the first call after an
# install moves the pin to a new release, offline or in a sandbox without
# network, or after `uv cache clean`), before nable runs at all. A bare command
# in settings.json then stops every Bash and MCP call. So the settings hook
# ends in `; exit 0`, which means the same in sh, Git Bash and PowerShell, the
# shells Claude Code runs hooks in. `finops guard hook` always exits 0 itself,
# and its verdict is on stdout, which the suffix leaves alone. Releases before
# this wrote the bare command; install wraps it in place.
_FAIL_SAFE_SUFFIX = "; exit 0"
# Also `|| exit 0` (the Codex spelling) and a trailing `;`: a command that
# already ends in one of these exits 0 whatever the launcher does.
_FAIL_SAFE_RE = re.compile(r"\s*(?:;|\|\|)\s*exit\s+0\s*;?\s*$")


def is_fail_safe(cmd: Any) -> bool:
    """Does this hook command exit 0 however its launcher fails?"""
    return isinstance(cmd, str) and _FAIL_SAFE_RE.search(cmd) is not None


def _bare(cmd: str) -> str:
    """A hook command without the fail-safe wrapper, however it was spelled."""
    return _FAIL_SAFE_RE.sub("", cmd)


def _fail_safe(cmd: str) -> str:
    """The Claude Code settings hook for `cmd`: wrapped once, never twice."""
    return f"{_bare(cmd)}{_FAIL_SAFE_SUFFIX}"


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
    release, see _UVX_HOOK_CMD, not to whatever PyPI has that day).

    This is the bare command. Each harness wraps it the way its shell and its
    exit-code rules need: _claude_hook_command for Claude Code's settings,
    guard_adapters for the others."""
    import shutil
    found = shutil.which("finops")
    if found and not _is_ephemeral(found):
        # Quote in case the path has spaces (framework installs on macOS do not,
        # but user venvs can).
        return f'"{found}" guard hook' if " " in found else f"{found} guard hook"
    return _UVX_HOOK_CMD


def _claude_hook_command() -> str:
    """What install writes into Claude Code's settings: _hook_command, fail-safe
    (see _FAIL_SAFE_SUFFIX). The binary form gets the suffix too: a missing
    binary exits 127, which Claude Code only reports, but one form is simpler
    to recognise than two, and a wrapper script there could exit 2."""
    return _fail_safe(_hook_command())


def hook_form() -> str:
    """"uvx" or "binary": which form install writes on this machine."""
    return "uvx" if _bare(_hook_command()) == _UVX_HOOK_CMD else "binary"


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


def blocking_hook_command(path: Path) -> str | None:
    """Our installed hook command, when it is the bare form releases before
    the fail-safe wrapper wrote: a uvx that cannot reach the package index
    exits 2 and Claude Code blocks the tool call (see _FAIL_SAFE_SUFFIX).
    None when the hook is absent or already fail-safe."""
    for _entry, h in _read_our_hooks(path):
        if not is_fail_safe(h["command"]):
            return h["command"]
    return None


def _stale(cmd: str) -> bool:
    """Should install() point this existing hook command at a new one?

    Dead, unpinned, or pinned to a release other than the one running the
    install. Re-running install is an explicit choice of release, so the pin
    follows it; a healthy binary-path hook keeps its program (see _upgraded)."""
    return not _command_runs(cmd) or hook_pin(cmd) in ("unpinned", "other")


def _upgraded(cmd: str) -> str | None:
    """The command install() writes over this existing one of ours, or None
    to leave it as found. A stale one is re-resolved; a healthy one in the
    bare form keeps its program and gains the fail-safe wrapper."""
    if _stale(cmd):
        return _claude_hook_command()
    if not is_fail_safe(cmd):
        return _fail_safe(cmd)
    return None


def _widen_matcher(pre: list, entry: dict, hook: dict) -> bool:
    """Move our hook from a matcher earlier releases wrote ("Bash", then
    "^(Bash|mcp__.*)$") to the one that also covers MCP and the file tools.
    Returns True when something changed.

    Only a matcher we wrote is upgraded: any other is a choice someone made
    by hand, and it stays theirs. When our hook shares that entry with
    someone else's, widening the entry would start running THEIR hook on
    every MCP call and file edit, so ours moves to an entry of its own
    instead."""
    if entry.get("matcher") not in _OLD_MATCHERS:
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
    is now rewritten in place when it is stale, when it predates the fail-safe
    wrapper, or when its matcher predates MCP coverage, keeping its position
    and every other hook in the file exactly as found."""
    path = _settings_path(global_scope)
    settings = _load_settings(path)
    pre = _hook_list(settings, path, create=True)
    ours = list(_our_hooks(pre))
    if ours:
        changed = False
        for entry, h in ours:
            changed = _widen_matcher(pre, entry, h) or changed
            cmd = _upgraded(h["command"])
            if cmd is None:
                continue
            h["command"] = cmd
            old = h.get("timeout")
            h["timeout"] = max(old, _timeout_for(cmd)) if isinstance(old, int) else _timeout_for(cmd)
            changed = True
        pre_cmd = ours[0][1]["command"]
    else:
        pre_cmd = _claude_hook_command()
        pre.append({
            "matcher": _HOOK_MATCHER,
            "hooks": [{"type": "command", "command": pre_cmd, "timeout": _timeout_for(pre_cmd)}],
        })
        changed = True
    from .guard_adapters import claude_post_install
    changed = claude_post_install(settings, path, pre_cmd) or changed
    if not changed:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    return path


def uninstall(global_scope: bool = False) -> bool:
    """Remove the guard hook. Returns True when something was removed."""
    path = _settings_path(global_scope)
    settings = _load_settings(path)
    pre = _hook_list(settings, path, create=False)
    # The post hook goes with it, whatever state the PreToolUse list is in.
    from .guard_adapters import claude_post_uninstall
    post_removed = claude_post_uninstall(settings, path)
    if not pre:
        if post_removed:
            path.write_text(json.dumps(settings, indent=2) + "\n")
        return post_removed
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
    if removed or post_removed:
        path.write_text(json.dumps(settings, indent=2) + "\n")
    return removed or post_removed


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
    which a clean check moves forward: it inspects settings files and the ledger
    and calls no cloud API. It runs nothing but what the guard itself would
    start: with a Cursor Admin API key and an old or missing Cursor cache, the
    background refresh (background_refresh), which it does not wait for."""
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
                       bash=surf["bash"], mcp=surf["mcp"], editor=surf["editor"],
                       fail_safe=is_fail_safe(cmd))
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
    if any(r.get("editor") for r in live):
        covered.append("Claude Code: Write, Edit, MultiEdit and NotebookEdit on the guard's "
                       "own files")
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
        if not r.get("editor") and not any(x.get("editor") for x in live):
            gaps.append(f"Claude Code ({r['scope']}): file edits to the guard's own files "
                        "(the hook does not see Write, Edit, MultiEdit or NotebookEdit)")
            if r.get("mcp") or any(x.get("mcp") for x in live):
                # Otherwise the MCP repair above is the same install, and widens both.
                fix(f"nable guard install{flag}", "widens the hook to Claude Code's file tools")
        if r.get("pin") == "unpinned":
            fix(f"nable guard install{flag}", "pins the hook to this release instead of "
                "the newest PyPI release on every call")
        elif r.get("pin") == "other":
            fix(f"nable guard install{flag}", "pins the hook to this release instead of "
                f"another release ({hook_release(r['command']) or 'unknown'})")
        if r.get("fail_safe") is False:
            fix(f"nable guard install{flag}", "lets tool calls through when the hook cannot "
                "start (uvx offline exits 2, which blocks every Bash and MCP call)")

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
                "tool calls only; the file tools are checked for the guard's own files "
                "and nothing else")
    gaps.append("writes to the guard's own files that the guard cannot see coming: a path "
                "built at run time ($(...), a loop variable), a script the agent runs, "
                "git reset or stash, an archive unpacked over them")
    for name, (label, _what) in _ADAPTER_SURFACES.items():
        if any(r["harness"] == name and r.get("runs") for r in adapter_rows):
            gaps.append(f"{label}: {_EDITOR_GAPS[name]}")

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
                    f"{lost['last']}: the ledger file was locked by another process, "
                    "replaced by something that is not a file (a symlink, a FIFO), or "
                    "could not be written (permissions, a full disk)")
        fix(f"check what holds {ledger['path']} (lsof), then remove {lost['path']}",
            "records the guard could not write")
    org = org_status()
    org_team = org.get("team") if org.get("team_source") != "FINOPS_GUARD_TEAM" else None
    if org_team:
        covered.append(f"priced changes here as team {org_team} (the org model's confirmed "
                       f"owner of this repo path): its budgets and thresholds")
    if org.get("error"):
        gaps.append(f"the org model: it could not be read ({org['error']}), so the guard "
                    "judges as if there were none")
        fix("nable org status", "shows which org file is at fault")
    budgets = budget_status(org_team)
    if budgets["state"] == "fresh" and budgets["enforced"]:
        n = len(budgets["enforced"])
        covered.append(f"priced changes against {n} cloud budget{'s' if n != 1 else ''} "
                       f"(spend figure from {_summary_age(budgets)} ago)")
    if budgets["state"] == "absent":
        gaps.append("the cloud budget on priced changes: there is no spend figure on this "
                    "machine yet, so only the per-action threshold and velocity cap apply")
        fix("nable budget refresh", "computes the spend figure the guard checks budgets "
            "against; run it daily")
    elif budgets["state"] == "stale":
        old = ("from last month" if budgets["previous_month"]
               else f"{_summary_age(budgets)} old")
        gaps.append(f"the cloud budget on priced changes: the spend figure is {old}, past "
                    f"the {budgets['max_age_hours']:g} hours the guard trusts, so budgets "
                    "are not checked")
        fix("nable budget refresh", "updates the spend figure; run it daily")
    elif not budgets["enforced"] and not budgets["not_enforced"]:
        gaps.append("the cloud budget on priced changes: no cloud budget is set (ask your AI "
                    "to \"set a monthly budget of $X\", or sync a budget.yml)")
    for row in budgets["not_enforced"]:
        gaps.append(f"the '{row['name']}' budget ({row['scope']}): the guard cannot tell "
                    f"which changes are in it; set {row['needs']} where the agent runs")
    from . import background_refresh
    refresh = background_refresh.status(start=True)
    _doctor_cursor(refresh, covered, gaps, fix)
    from .policy import policy_problems
    problems = policy_problems()
    for problem in problems:
        fix("correct the policy setting", problem)
    packs = _doctor_packs(covered, gaps, fix)
    try:
        from . import guard_paths
        protected = [e.as_dict() for e in guard_paths.protected()]
    except Exception as exc:
        protected = []
        gaps.append(f"the guard's own files: they could not be listed ({type(exc).__name__}), "
                    "so writes to them may not ask")
    editor_tools = _editor_coverage(rows)
    fixes = [f"{cmd}  ({'; '.join(why)})" if why else cmd for cmd, why in todo.items()]
    fixes.append("give agents read-only cloud credentials; keep write access behind a human")

    return {
        "ok": bool(live) and ledger["clean"],
        "surfaces": rows,
        "covered": covered,
        "not_covered": gaps,
        "mcp_tools": families,
        "ledger": ledger,
        "budgets": budgets,
        "org": org,
        "background_refresh": refresh,
        "policy_problems": problems,
        "protected_paths": protected,
        "editor_tools": editor_tools,
        "packs": packs,
        "recommendations": fixes,
        "seatbelt": SEATBELT,
        "version": __version__,
    }


# Why each other harness's file edits are not checked, from what the adapter
# (guard_adapters.py) knows of its hook protocol. Their shell tools are, so
# `echo x > nable.org/policy.yaml` asks everywhere the guard is installed.
_EDITOR_GAPS = {
    "cursor": ("file edits to the guard's own files (Cursor's documented hooks run after a "
               "file edit, afterFileEdit, or before a read, beforeReadFile; nothing can stop "
               "an edit before it happens)"),
    "codex": ("file edits to the guard's own files (the guard's Codex hook is on Bash and "
              "MCP calls; Codex's file edits are not among the tool names nable knows it "
              "sends to a hook)"),
    "copilot": ("file edits to the guard's own files (the guard's Copilot hook reads the "
                "bash and powershell tools only)"),
    "gemini": ("file edits to the guard's own files (the guard's Gemini CLI hook matches "
               "run_shell_command only)"),
    "cline": ("file edits to the guard's own files (the guard's Cline hook reads "
              "run_commands and execute_command only)"),
}


def _editor_coverage(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Per harness: whether an edit to one of the guard's own files through
    the harness's file tools asks ("covered"), or why not."""
    out: dict[str, str] = {}
    claude = [r for r in rows if r["harness"] == "claude-code" and r.get("runs")]
    if any(r.get("editor") for r in claude):
        out["claude-code"] = "covered"
    elif claude:
        out["claude-code"] = ("not covered: the installed hook's matcher predates the file "
                              "tools (nable guard install widens it)")
    else:
        out["claude-code"] = "not covered: no working guard hook"
    for name in _ADAPTER_SURFACES:
        out[name] = "not covered: " + _EDITOR_GAPS[name]
    return out


def _doctor_packs(covered: list[str], gaps: list[str], fix: Any) -> dict[str, Any]:
    """The doctor's word on installed packs in the guard: the guard rules
    and price books in effect, and any pack it is judging without."""
    from . import guard_packs
    st = guard_packs.status()
    rules, books = st["guard_rules"], st["price_books"]
    if rules:
        n = len(rules)
        covered.append(f"{n} guard rule{'s' if n != 1 else ''} from installed packs "
                       f"({', '.join(sorted({r['pack'] for r in rules}))}); they only tighten")
    if books:
        covered.append(f"prices from {len(books)} price book rate"
                       f"{'s' if len(books) != 1 else ''} in installed packs "
                       f"({', '.join(sorted({b['pack'] for b in books}))}), where one covers "
                       "the SKU; verdicts judge at the higher of list and book rate")
    for problem in st.get("guard_problems") or []:
        gaps.append(f"an installed pack the guard judges without: {problem}")
        fix("nable pack audit", "says what changed in the pack since it was approved")
    return st


def _doctor_cursor(refresh: dict[str, Any], covered: list[str], gaps: list[str],
                   fix: Any) -> None:
    """The doctor's word on Cursor usage in the AI budget: how old the Admin
    API read the guard counts is, and what is refreshing it. Nothing without
    a key: then Cursor usage is not read at all (`nable ai-budget` says so)."""
    from .budget.summary import age_words
    cursor = refresh["cursor"]
    if not cursor.get("enabled"):
        return
    what = "Cursor usage in the AI budget"
    busy = "; a refresh is running in the background" if cursor.get("refreshing") else ""
    age = cursor.get("age_hours")
    if cursor.get("error"):
        counted = (f"counts the read from {age_words(age)} ago" if age is not None
                   else "counts no Cursor usage")
        gaps.append(f"{what}: the last Admin API read failed ({cursor['error']}), so the "
                    f"guard {counted} until one succeeds")
        fix("nable ai-budget", "reads Cursor usage now, once the key or the network is fixed")
    elif age is None:
        gaps.append(f"{what}: no Admin API read yet, so the guard counts none{busy}")
    elif cursor.get("stale"):
        gaps.append(f"{what}: the guard is counting an Admin API read {age_words(age)} "
                    f"old, past the {cursor['ttl_hours']:g} hour it is good for{busy}")
    else:
        covered.append(f"{what} (Admin API read {age_words(age)} ago)")
    if not refresh.get("enabled") and cursor.get("stale"):
        fix("nable ai-budget", "reads Cursor usage now; background refresh is off "
            "(FINOPS_GUARD_BACKGROUND_REFRESH=0), so the guard's Cursor figure is only "
            "as fresh as the last read outside the hook")


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
