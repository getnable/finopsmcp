# SPDX-License-Identifier: Apache-2.0
"""The authority ladder in the guard: classes that move no money (D172 to D178).

An agent tags resources, opens tickets and stops what nobody uses at night.
None of it buys anything, so a team can let an agent do more of it unasked,
a rung at a time, within a bound a person wrote in authority.yaml
(finops.org.model, OrgModel.ladder):

  tag                aws ec2 create-tags, resourcegroupstaggingapi
                     tag-resources, rds and elasticache add-tags-to-resource,
                     any aws SERVICE tag-resource, s3api put-bucket-tagging
                     (it replaces the whole set, so it is never within a
                     bound) and kubectl label. Their removals (ec2
                     delete-tags, untag-resources, remove-tags-from-resource,
                     untag-resource, kubectl label KEY-) are in the class and
                     never within a bound. IAM, Organizations, SSO and KMS
                     tagging stay out: a tag there can grant access.
  ticket             gh, glab and jira `issue create`, and an MCP tool whose
                     name says create with issue or ticket (create_issue,
                     createJiraIssue, issue_write with method create).
  offhours_schedule  ec2 stop-instances and start-instances; rds
                     stop/start-db-instance and stop/start-db-cluster;
                     autoscaling put-scheduled-update-group-action and its
                     batch form; scheduler create-schedule and update-schedule
                     whose target is an EC2 or RDS stop or start. Their
                     removals (delete-scheduled-action, its batch form,
                     scheduler delete-schedule) and a schedule set to
                     DISABLED are in the class and never within a bound.

A command any money-moving, one-way or priced rule of the guard matches is
never in a class (ladder_verdict answers None and the guard judges it as
before), whatever else it holds; the class's own removals are the one
exception, since the guard's generic delete rule also matches them, and
they are never within a bound, so they always ask or are denied.

The rungs: a class the file does not name answers rung "none" and adds no
ask, so with no file nothing changes for anyone. off and recommend deny
with a reason a person can act on; approve_each asks, naming the approvers;
bounded adds no ask within its bound and asks outside it. The guard merges
this answer with its own, the more severe answering, so bounded never
removes an ask the guard raises without the file.

On a machine connected to a paid workspace the stricter of the file and the
box's standing applies (authority_standing.for_guard): the box can only
narrow, a demotion after a reversal holds the class at approve_each, and a
standing that cannot be read holds every class the file has at bounded at
approve_each.

Cheap for everything else: a call in no class is turned away by one
precompiled search, and the org model, the standing and the ledger are read
only for a call in a class, every file through guard_paths.read_regular.
Nothing here raises: a call in a class that cannot be judged asks.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

# finops.org.model.NO_MONEY_CLASSES, copied: the org package is read only for
# a call in a class.
CLASSES = ("tag", "ticket", "offhours_schedule")
ACTION_TYPE = "authority_class"
_SEVERITY = {"allow": 0, "ask": 2, "deny": 3}
# How much freedom each rung leaves an agent: "none" (no file) the most.
_FREEDOM = {"off": 0, "recommend": 1, "approve_each": 2, "bounded": 3, "none": 4}
_LABEL = {"tag": "tagging (tag)", "ticket": "opening a ticket (ticket)",
          "offhours_schedule": "an off-hours stop, start or schedule (offhours_schedule)"}
_REMOVALS = frozenset({"remove", "disable"})
# The most of a command, and of authority.yaml, this reads.
_COMMAND_MAX = 16 * 1024
_TOKENS_MAX = 2048
_FILE_MAX = 1024 * 1024
_SHOWN_MAX = 20
_DEPTH_MAX = 3

# One search turns away every call in no class: the verbs below, as words.
_HINT = re.compile(
    r"tags?-(?:resources?|to-resource|from-resource)|create-tags|delete-tags|bucket-tagging"
    r"|(?<![\w-])label(?![\w-])|(?<![\w-])issues?(?![\w-])"
    r"|(?:stop|start)-(?:instances|db-instance|db-cluster)|scheduled-|-schedule(?![\w-])",
    re.IGNORECASE)
# The same verbs, for a command too odd to split into words: any of these is
# a call in a class whose bound cannot be read.
_RAW = (
    ("tag", re.compile(r"\baws\b.*\b(?:create-tags|delete-tags|(?:un)?tag-resources?|"
                       r"(?:add|remove)-tags-(?:to|from)-resource|put-bucket-tagging)\b"
                       r"|\bkubectl\b.*\blabel\b", re.DOTALL)),
    ("ticket", re.compile(r"\b(?:gh|glab|jira)\s+issues?\s+create\b")),
    ("offhours_schedule", re.compile(
        r"\baws\b.*\b(?:(?:stop|start)-(?:instances|db-instance|db-cluster)|"
        r"(?:batch-)?(?:put|delete)-scheduled-(?:update-group-)?actions?|"
        r"scheduler\s+(?:create|update|delete)-schedule)\b", re.DOTALL)),
)
# Tagging these can grant access (an IAM or KMS condition on a tag), so it is
# judged as before, never in the class.
_ACCESS_SERVICES = frozenset({"iam", "kms", "organizations", "sso", "sso-admin", "sso-oidc",
                              "identitystore", "sts"})
_AWS_GLOBAL_VALUE = frozenset({"--profile", "--region", "--output", "--endpoint-url",
                               "--query", "--color", "--ca-bundle", "--cli-read-timeout",
                               "--cli-connect-timeout", "--cli-binary-format"})
_AWS_GLOBAL_FLAG = frozenset({"--debug", "--no-verify-ssl", "--no-paginate",
                              "--no-sign-request", "--no-cli-pager", "--cli-auto-prompt",
                              "--no-cli-auto-prompt"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_WRAPPERS = frozenset({"sudo", "doas", "env", "command", "exec", "nohup", "time", "nice",
                       "timeout", "stdbuf", "ionice", "chronic"})
_WRAPPER_VALUE = frozenset({"-u", "-g", "-C", "-h", "-p", "-n", "-o", "-e", "-i", "-c",
                            "-s", "-k", "--signal", "--kill-after", "--user", "--group"})
_FEEDERS = frozenset({"xargs", "parallel"})
_FEEDER_VALUE = frozenset({"-a", "-d", "-E", "-I", "-J", "-L", "-n", "-P", "-R", "-S", "-s",
                           "--arg-file", "--delimiter", "--max-args", "--max-procs",
                           "--replace"})
_KUBECTL_VALUE = frozenset({"-n", "--namespace", "--context", "--kubeconfig", "--cluster",
                            "--user", "-s", "--server", "--token", "--as", "--as-group",
                            "--request-timeout", "-o", "--output", "--field-selector",
                            "--resource-version", "--field-manager", "--template",
                            "-l", "--selector", "-f", "--filename", "-k", "--kustomize"})
_KUBECTL_MANY = frozenset({"-l", "--selector", "--all", "-A", "--all-namespaces", "-f",
                           "--filename", "-k", "--kustomize", "-R", "--recursive",
                           "--field-selector"})
_RESOURCE_FLAGS = ("--resource-arn", "--resource", "--resource-id", "--resource-name",
                   "--arn", "--resource-arns", "--resource-arn-list", "--resource-ids")
# The EC2 and RDS stops and starts an EventBridge Scheduler universal target
# names (arn:aws:scheduler:::aws-sdk:SERVICE:ACTION).
_SCHED_TARGET = re.compile(r"arn:aws[a-z-]*:scheduler:::aws-sdk:(ec2|rds):"
                           r"(stopInstances|startInstances|stopDBInstance|startDBInstance|"
                           r"stopDBCluster|startDBCluster)\b")
_INSTANCE_ID = re.compile(r"(?<![\w-])i-[0-9a-f]{1,17}(?![\w-])")
_RDS_ID = re.compile(r"Db(?:Instance|Cluster)Identifier\W{1,6}([A-Za-z][A-Za-z0-9-]{0,62})")
_ACTION_NAME = re.compile(r"ScheduledActionName\W{1,6}([\w.-]{1,255})")
_DYNAMIC = re.compile(r"\$|`|^(?:file|fileb|https?)://|<\(")
_MCP_WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
_REMOTE = re.compile(r"^\s*url\s*=\s*(\S+)\s*$", re.MULTILINE)
_SLUG = re.compile(r"(?:^[a-z][a-z0-9+.-]*://[^/]+/|^[^@/:\s]+@[^:/\s]+:|^[^:/\s]+:)"
                   r"([^\s]+?)(?:\.git)?/?$", re.IGNORECASE)


def _now() -> datetime:
    """The time the ladder is judged at (tests fix it)."""
    return datetime.now(UTC)


# ── what a call is ───────────────────────────────────────────────────────────

@dataclass
class _Call:
    """One call in a class, as far as the command says."""

    cls: str
    op: str                                 # add, remove, replace, create, stop, start,
    tool: str                               # schedule, disable
    resources: list[str] | None = None      # None: they cannot be counted
    keys: list[str] | None = None           # tag keys; None: they cannot be read
    repo: str | None = None
    schedule: list[str] = field(default_factory=list)
    group: str | None = None
    counted: bool = True                    # False: a substitution or a feeder in it

    def view(self) -> dict[str, Any]:
        out: dict[str, Any] = {"op": self.op, "tool": self.tool}
        if self.resources is not None:
            out["resources"] = self.resources[:_SHOWN_MAX]
            out["resources_n"] = len(self.resources)
        if self.cls == "tag":
            out["keys"] = self.keys[:_SHOWN_MAX] if self.keys is not None else None
        if self.repo:
            out["repo"] = self.repo
        if self.schedule:
            out["schedule"] = self.schedule[:_SHOWN_MAX]
        if self.group:
            out["group"] = self.group
        return out


def _dynamic(word: str) -> bool:
    return bool(_DYNAMIC.search(word))


def _flags(words: list[str]) -> dict[str, list[str]]:
    """--name VALUE... and --name=VALUE, each flag's values in order."""
    out: dict[str, list[str]] = {}
    i = 0
    while i < len(words):
        w = words[i]
        if w.startswith("--") and len(w) > 2:
            name, eq, val = w.partition("=")
            vals = out.setdefault(name, [])
            if eq:
                vals.append(val)
                i += 1
                continue
            i += 1
            while i < len(words) and not words[i].startswith("--"):
                vals.append(words[i])
                i += 1
            continue
        i += 1
    return out


def _many(vals: list[str] | None) -> list[str] | None:
    """The items a list flag names (space-separated, or a JSON list), or
    None when they cannot be read or counted."""
    if not vals:
        return None
    out: list[str] = []
    for v in vals:
        if _dynamic(v):
            return None
        s = v.strip()
        if s.startswith("["):
            try:
                data = json.loads(s)
            except ValueError:
                return None
            if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
                return None
            out += data
        else:
            out += [x for x in s.split(",") if x] if "," in s and " " not in s else [s]
    return out or None


def _pairs(text: str) -> list[tuple[str, str]] | None:
    """KEY=VALUE,KEY=VALUE as the aws CLI's shorthand writes a map or a
    structure; None when it is not that."""
    out = []
    for part in text.split(","):
        name, eq, val = part.partition("=")
        if not eq or not name.strip():
            return None
        out.append((name.strip(), val))
    return out


def _tag_keys(vals: list[str] | None, form: str) -> list[str] | None:
    """The tag keys a --tags (or --tag-keys) flag sets: `form` is "struct"
    (Key=k,Value=v items, or a JSON list of {"Key"}), "map" (k=v,k2=v2, or a
    JSON object), "any" (either, read as the aws CLI would) or "list" (keys
    as words). None when they cannot be read."""
    if not vals:
        return None
    out: list[str] = []
    for v in vals:
        if _dynamic(v):
            return None
        s = v.strip()
        if form == "list":
            got = _many([s])
            if got is None:
                return None
            out += got
            continue
        if s.startswith(("[", "{")):
            try:
                data = json.loads(s)
            except ValueError:
                return None
            if isinstance(data, dict) and form in ("map", "any"):
                out += [str(k) for k in data]
            elif isinstance(data, list) and form in ("struct", "any") and all(
                    isinstance(x, dict) and isinstance(x.get("Key"), str) for x in data):
                out += [x["Key"] for x in data]
            else:
                return None
            continue
        pairs = _pairs(s)
        if pairs is None:
            return None
        names = [k for k, _ in pairs]
        if "Key" in names and form in ("struct", "any"):
            out += [val for k, val in pairs if k == "Key"]
        elif form in ("map", "any"):
            out += names
        else:
            return None
    return out or None


def _tagset_keys(vals: list[str] | None) -> list[str] | None:
    text = " ".join(vals or [])
    if not text or _dynamic(text):
        return None
    return re.findall(r"\bKey\W{1,4}([^,}\]\"'\s]+)", text) or None


def _aws(args: list[str], counted: bool) -> list[_Call]:
    """The class calls one aws command makes (global options anywhere)."""
    words: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in _AWS_GLOBAL_VALUE:
            i += 2
            continue
        if a.partition("=")[0] in _AWS_GLOBAL_VALUE and "=" in a or a in _AWS_GLOBAL_FLAG:
            i += 1
            continue
        words.append(a)
        i += 1
    if len(words) < 2:
        return []
    svc, op = words[0].lower(), words[1].lower()
    f = _flags(words[2:])

    def call(cls: str, kind: str, resources: list[str] | None, **kw: Any) -> list[_Call]:
        return [_Call(cls, kind, f"aws {svc} {op}", resources if counted else None,
                      counted=counted, **kw)]
    if (svc, op) == ("ec2", "create-tags"):
        return call("tag", "add", _many(f.get("--resources")),
                    keys=_tag_keys(f.get("--tags"), "struct"))
    if (svc, op) == ("ec2", "delete-tags"):
        return call("tag", "remove", _many(f.get("--resources")),
                    keys=_tag_keys(f.get("--tags"), "struct"))
    if svc == "resourcegroupstaggingapi" and op in ("tag-resources", "untag-resources"):
        if op == "tag-resources":
            return call("tag", "add", _many(f.get("--resource-arn-list")),
                        keys=_tag_keys(f.get("--tags"), "map"))
        return call("tag", "remove", _many(f.get("--resource-arn-list")),
                    keys=_tag_keys(f.get("--tag-keys"), "list"))
    if svc in ("rds", "elasticache") and op == "add-tags-to-resource":
        return call("tag", "add", _many(f.get("--resource-name")),
                    keys=_tag_keys(f.get("--tags"), "struct"))
    if svc in ("rds", "elasticache") and op == "remove-tags-from-resource":
        return call("tag", "remove", _many(f.get("--resource-name")),
                    keys=_tag_keys(f.get("--tag-keys"), "list"))
    if (svc, op) == ("s3api", "put-bucket-tagging"):
        return call("tag", "replace", _many(f.get("--bucket")),
                    keys=_tagset_keys(f.get("--tagging")))
    if op in ("tag-resource", "untag-resource") and svc not in _ACCESS_SERVICES:
        named = next((f[k] for k in _RESOURCE_FLAGS if f.get(k)), None)
        resources = _many(named[:1]) if named else None
        if op == "tag-resource":
            return call("tag", "add", resources, keys=_tag_keys(f.get("--tags"), "any"))
        return call("tag", "remove", resources,
                    keys=_tag_keys(f.get("--tag-keys") or f.get("--keys"), "list"))
    if svc == "ec2" and op in ("stop-instances", "start-instances"):
        return call("offhours_schedule", op.split("-")[0], _many(f.get("--instance-ids")))
    if svc == "rds" and op in ("stop-db-instance", "start-db-instance"):
        return call("offhours_schedule", op.split("-")[0],
                    _many(f.get("--db-instance-identifier")))
    if svc == "rds" and op in ("stop-db-cluster", "start-db-cluster"):
        return call("offhours_schedule", op.split("-")[0],
                    _many(f.get("--db-cluster-identifier")))
    if svc == "autoscaling" and op in ("put-scheduled-update-group-action",
                                       "batch-put-scheduled-update-group-action",
                                       "delete-scheduled-action",
                                       "batch-delete-scheduled-action"):
        names = (f.get("--scheduled-action-name") or f.get("--scheduled-action-names")
                 or _ACTION_NAME.findall(" ".join(f.get("--scheduled-update-group-actions")
                                                  or [])))
        return call("offhours_schedule", "remove" if "delete" in op else "schedule",
                    _many(f.get("--auto-scaling-group-name")), schedule=list(names or []))
    if svc == "scheduler" and op in ("create-schedule", "update-schedule"):
        target = " ".join(f.get("--target") or [])
        m = _SCHED_TARGET.search(target)
        if m is None:
            return []                   # not a stop or a start: in no class
        state = " ".join(f.get("--state") or []).strip().upper()
        if m.group(1) == "ec2":
            ids = _INSTANCE_ID.findall(target)
        else:
            ids = _RDS_ID.findall(target)
        resources = ids if ids and not _dynamic(target) else None
        return call("offhours_schedule", "disable" if state == "DISABLED" else "schedule",
                    resources, schedule=list(f.get("--name") or []),
                    group=" ".join(f.get("--group-name") or []) or "default")
    if svc == "scheduler" and op == "delete-schedule":
        return call("offhours_schedule", "remove", None, schedule=list(f.get("--name") or []),
                    group=" ".join(f.get("--group-name") or []) or "default")
    return []


def _kubectl(args: list[str], counted: bool) -> list[_Call]:
    pos: list[str] = []
    many = not counted
    i = 0
    while i < len(args):
        a = args[i]
        name = a.partition("=")[0]
        if name in _KUBECTL_MANY:
            many = True
        if a.startswith("-") and a != "-":
            i += 2 if (a in _KUBECTL_VALUE and "=" not in a) else 1
            continue
        pos.append(a)
        i += 1
    if not pos or pos[0] != "label":
        return []
    rest = pos[1:]
    labels = [a for a in rest if "=" in a or (a.endswith("-") and len(a) > 1)]
    others = [a for a in rest if a not in labels]
    removed = [a[:-1] for a in labels if a.endswith("-") and "=" not in a]
    keys = [a.split("=", 1)[0] for a in labels if "=" in a] + removed
    names = others if others and "/" in others[0] else others[1:]
    resources = None if many or not names or any(_dynamic(n) for n in names) else names
    if any(_dynamic(k) for k in keys):
        keys = []
    return [_Call("tag", "remove" if removed else "add", "kubectl label", resources,
                  keys=keys or None, counted=counted and not many)]


def _issue(prog: str, args: list[str]) -> list[_Call]:
    """gh, glab or jira `issue create`, and the repository it names."""
    pos = [a for a in args if not a.startswith("-")]
    if len(pos) < 2 or pos[0] not in ("issue", "issues") or pos[1] != "create":
        return []
    repo: str | None = None
    if prog in ("gh", "glab"):
        for i, a in enumerate(args):
            if a in ("-R", "--repo") and i + 1 < len(args):
                repo = args[i + 1]
            elif a.startswith("--repo="):
                repo = a.split("=", 1)[1]
            elif a.startswith("-R") and len(a) > 2:
                repo = a[2:]
        if repo is not None and _dynamic(repo):
            repo = None
    return [_Call("ticket", "create", f"{prog} issue create", ["1"],
                  repo=repo.strip().lower() if repo else None)]


def _unwrap(words: list[str]) -> tuple[list[str], bool]:
    """The words after leading assignments and wrappers (sudo, env, nice,
    timeout, ...), and whether a feeder (xargs, parallel) hands it more
    arguments than the command shows."""
    counted = True
    for _ in range(8):
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words = words[1:]
        if not words:
            return words, counted
        prog = os.path.basename(words[0]).lower()
        if prog in _WRAPPERS or prog in _FEEDERS:
            if prog in _FEEDERS:
                counted = False
            i = 1
            takes = _FEEDER_VALUE if prog in _FEEDERS else _WRAPPER_VALUE
            while i < len(words) and words[i].startswith("-"):
                i += 2 if (words[i] in takes and "=" not in words[i]) else 1
            if prog == "timeout" and i < len(words):
                i += 1
            words = words[i:]
            continue
        break
    return words, counted


def _segment_calls(words: list[str], depth: int) -> list[_Call]:
    words, counted = _unwrap(words)
    if not words:
        return []
    prog = os.path.basename(words[0]).lower()
    if prog in _SHELLS and depth < _DEPTH_MAX:
        for i, w in enumerate(words[1:], start=1):
            if w.startswith("-") and not w.startswith("--") and "c" in w[1:] \
                    and i + 1 < len(words):
                return _calls_in(words[i + 1], depth + 1)
        return []
    if re.match(r"^python[\d.]*$", prog) and words[1:3] == ["-m", "awscli"]:
        words, prog = ["aws", *words[3:]], "aws"
    if prog == "aws":
        return _aws(words[1:], counted)
    if prog in ("kubectl", "oc"):
        return _kubectl(words[1:], counted)
    if prog in ("gh", "glab", "jira"):
        return _issue(prog, words[1:])
    return []


def _segments(command: str) -> list[list[str]]:
    """The command's simple commands, split at ; & | && || and newlines.
    Raises ValueError for one that cannot be split (a quote left open)."""
    lex = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=";&|")
    lex.whitespace_split = True
    out: list[list[str]] = []
    cur: list[str] = []
    for n, tok in enumerate(lex, start=1):
        if n > _TOKENS_MAX:
            raise ValueError("too many words")
        if tok and not tok.strip(";&|"):
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        out.append(cur)
    return out


def _raw_calls(command: str) -> list[_Call]:
    """A command that cannot be split into words: each class whose verb it
    names, none of its bound readable."""
    return [_Call(cls, "unread", "unread", None, counted=False)
            for cls, rule in _RAW if rule.search(command)]


def _calls_in(command: str, depth: int = 0) -> list[_Call]:
    try:
        segs = _segments(command)
    except ValueError:
        return _raw_calls(command)
    out: list[_Call] = []
    for seg in segs:
        out += _segment_calls(seg, depth)
    if not out and any(s in command for s in ("$(", "`", "<(")):
        out = _raw_calls(command)
    return out


def classify(command: str) -> list[_Call]:
    """The calls in a class `command` makes, [] for none. One search turns
    away a command in no class."""
    if not isinstance(command, str) or not _HINT.search(command):
        return []
    if len(command) > _COMMAND_MAX:
        return _raw_calls(command)
    return _calls_in(command)


def _mcp_call(tool: str, args: Any) -> list[_Call]:
    """An MCP tool that creates an issue or a ticket (D172)."""
    name = str(tool).rsplit("__", 1)[-1]
    words = {w.lower() for w in _MCP_WORD.findall(name)}
    args = args if isinstance(args, dict) else {}
    creates = "create" in words and ("issue" in words or "ticket" in words)
    if not creates and {"issue", "write"} <= words:
        creates = str(args.get("method") or "").lower() == "create"
    if not creates:
        return []
    repo = None
    owner, name_ = args.get("owner"), args.get("repo")
    if isinstance(owner, str) and isinstance(name_, str) and owner and name_:
        repo = f"{owner}/{name_}".lower()
    elif isinstance(args.get("repository"), str) and "/" in args["repository"]:
        repo = args["repository"].lower()
    return [_Call("ticket", "create", f"mcp {name}"[:80], ["1"], repo=repo)]


# ── never money ──────────────────────────────────────────────────────────────

def _money(hit: tuple[str, str] | None) -> bool:
    """A guard classification that moves money or cannot be undone: every
    rule but the one that stops an idle instance."""
    return hit is not None and hit != ("two_way", "stop_idle")


def _excluded(command: str, calls: list[_Call], cwd: str | None) -> bool | None:
    """True when any money-moving, one-way or priced rule of the guard
    matches the command, the whole of it or any one of its commands (it is
    then in no class); None when that cannot be told (the call is judged
    outside any bound). The class's own removals match the guard's generic
    delete rule too: a command whose only such match is one of them is in
    its class, and never within a bound."""
    try:
        from . import guard
        try:
            segs = _segments(command)
        except ValueError:
            segs = []
        rest: list[str] = []
        for seg in segs:
            mine = _segment_calls(seg, 0)
            if mine and all(c.op == "remove" for c in mine):
                continue
            rest.append(shlex.join(seg))
        if any(_money(guard.classify_command(one)) for one in rest):
            return True
        whole = guard.classify_command(command)
        if _money(whole):
            if whole != ("one_way", "delete_resource") or not segs or len(rest) == len(segs):
                return True
            if rest and _money(guard.classify_command(" ; ".join(rest))):
                return True
        buys = getattr(guard, "_buys_commitment", None)
        if buys is not None and buys(command):
            return True
        return guard.estimate_command_monthly_cost(command, cwd=cwd) is not None
    except Exception:  # noqa: BLE001 - not told is outside any bound, never a crash
        return None


# ── the ladder ───────────────────────────────────────────────────────────────

def _authority_model(cwd: str | None) -> Any:
    """The org model's authority.yaml, in every layer the guard reads (a
    repo's nable.org/ over the data dir's), each read through
    guard_paths.read_regular: a FIFO in its place is a file that cannot be
    read, never one waited on. Refusals as the store makes them."""
    from . import guard_paths
    from .org import model as m
    from .org import store
    layers = store.layers(None, cwd=cwd)
    facts: list[Any] = []
    refused: list[dict[str, Any]] = []
    seen: set[str] = set()
    for layer in layers:
        p = layer.dir / m.AUTHORITY_FILE
        try:
            raw = guard_paths.read_regular(p, _FILE_MAX + 1)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            refused.append(m.refusal(str(p), None, None, f"{m.AUTHORITY_FILE} could not be "
                                     f"read ({exc.strerror or type(exc).__name__})"))
            continue
        if len(raw) > _FILE_MAX:
            refused.append(m.refusal(str(p), None, None, f"{m.AUTHORITY_FILE} is over the "
                                                         f"{_FILE_MAX:,} bytes the guard reads"))
            continue
        try:
            data = store.safe_yaml(raw.decode("utf-8").removeprefix("﻿"),
                                   unique_keys=True)
        except Exception as exc:  # noqa: BLE001 - an unreadable file holds every class
            refused.append(m.refusal(str(p), None, None, f"{m.AUTHORITY_FILE} is not "
                                     f"readable as YAML ({type(exc).__name__})"))
            continue
        if data is None:
            continue
        if not isinstance(data, list):
            refused.append(m.refusal(str(p), None, None, f"{m.AUTHORITY_FILE} is not a list"))
            continue
        for i, entry in enumerate(data):
            try:
                f = m.Fact.from_dict(entry, origin=m.AUTHORITY_FILE)
            except Exception as exc:  # noqa: BLE001 - refused, never skipped in silence
                refused.append(m.refusal(str(p), i, entry, exc))
                continue
            if f.key in seen:
                continue
            seen.add(f.key)
            f.src_dir, f.trusted, f.anchor, f.layer = (str(layer.dir), layer.trusted,
                                                       layer.anchor, layer.layer)
            facts.append(f)
    return m.OrgModel(facts, layers=layers, refused=refused)


def _ladder(cwd: str | None, today: date) -> dict[str, Any]:
    return _authority_model(cwd).ladder(today=today)


def _apply_standing(cls: str, rung: str, standing: dict[str, Any] | None
                    ) -> tuple[str, str | None, dict[str, Any] | None]:
    """(the rung after the box's standing, a sentence when it narrowed it,
    what the line records of the standing). The box can only narrow."""
    if standing is None:
        return rung, None, None
    if not standing.get("read"):
        note = {"read": False}
        if rung == "bounded":
            why = (f"Your workspace's standing on this machine was not read "
                   f"({standing.get('why')}), so bounded is held at approve_each.")
            return "approve_each", why, note
        return rung, None, note
    c = (standing.get("classes") or {}).get(cls) or {}
    note = {"read": True, "demoted": bool(c.get("demoted")),
            "rung_in_force": c.get("rung_in_force"), "since": c.get("since")}
    out, why = rung, None
    if c.get("demoted") and _FREEDOM[out] > _FREEDOM["approve_each"]:
        out = "approve_each"
        why = (f"Your workspace demoted {cls} on {c.get('since')} after a reversal, so it is "
               "at approve_each here until an admin of the workspace lifts it.")
    box = c.get("rung_in_force")
    if box in _FREEDOM and _FREEDOM[box] < _FREEDOM[out]:
        out = box
        why = f"Your workspace holds {cls} at {box}, stricter than authority.yaml here."
    return out, why, note


def _cwd_repo(cwd: str | None) -> str | None:
    """OWNER/NAME of the origin remote of the repo holding `cwd` (else its
    first remote), as gh and glab pick it, from .git/config read through
    guard_paths.read_regular; None when it cannot be told."""
    from . import guard_paths
    root = guard_paths.git_root(cwd)
    if root is None:
        return None
    for cfg in guard_paths.git_config_files(root)[::-1]:
        if cfg.name != "config" or cfg.parent.name in ("git",):
            continue
        try:
            text = guard_paths.read_regular(cfg, 256 * 1024).decode("utf-8", "replace")
        except OSError:
            continue
        sections = re.split(r"^\s*\[", text, flags=re.MULTILINE)
        remotes = [sec for sec in sections if sec.startswith("remote ")]
        remotes.sort(key=lambda sec: not sec.startswith('remote "origin"'))
        for sec in remotes:
            m = _REMOTE.search(sec)
            slug = _SLUG.match(m.group(1)) if m else None
            if slug:
                return slug.group(1).strip("/").lower()
    return None


def _outside(cls: str, calls: list[_Call], bound: dict[str, Any], *, multi: bool,
             cwd: str | None,
             scope: Callable[[], tuple[tuple[str, ...] | None, str | None]],
             tickets_today: Callable[[], int | None], extra: dict[str, Any]) -> list[str]:
    """Why this call is outside its class's bound; [] when it is within."""
    why: list[str] = []
    if multi:
        why.append("the command holds calls of more than one class")
    for c in calls:
        if c.op in _REMOVALS:
            why.append(f"it removes or disables ({c.tool}), and a removal is never within a "
                       "bound")
        elif c.op == "replace":
            why.append(f"{c.tool} replaces the whole tag set, which is never within a bound")
        elif c.op == "unread":
            why.append("the command could not be read word by word")
    if cls == "tag":
        allowed = set(bound.get("tag_keys") or [])
        most = int(bound.get("max_resources_per_call") or 0)
        n = 0
        for c in calls:
            if c.keys is None:
                why.append(f"the tag keys {c.tool} sets could not be read")
            else:
                over = [k for k in c.keys if k not in allowed]
                if over:
                    why.append(f"it sets tag key {', '.join(sorted(set(over))[:5])}, which the "
                               f"bound does not name (it names {', '.join(sorted(allowed))})")
            if c.resources is None:
                why.append(f"the resources {c.tool} tags could not be counted")
            else:
                n += len(c.resources)
        if n > most:
            why.append(f"it tags {n} resources, over the bound's {most} a call")
    elif cls == "ticket":
        repos = bound.get("repos")
        if repos is not None:
            for c in calls:
                if not c.repo and c.tool in ("gh issue create", "glab issue create"):
                    c.repo = _cwd_repo(cwd)
                if not c.repo:
                    why.append("its repository could not be read, and the bound names "
                               f"{', '.join(repos)}")
                elif c.repo not in repos:
                    why.append(f"its repository {c.repo} is not one the bound names")
        most = int(bound.get("max_per_day") or 0)
        today = tickets_today()
        extra["tickets_today"] = today
        if today is None:
            why.append("this machine's tickets today could not be counted (its ledger was not "
                       "read)")
        elif today + len(calls) > most:
            why.append(f"it would be ticket {today + len(calls)} of today (UTC) on this "
                       f"machine, over the bound's {most} a day")
    elif cls == "offhours_schedule":
        most = int(bound.get("max_resources_per_call") or 0)
        n = 0
        for c in calls:
            if c.resources is None:
                if c.op not in _REMOVALS:
                    why.append(f"the resources {c.tool} touches could not be counted")
            else:
                n += len(c.resources)
        if n > most:
            why.append(f"it touches {n} resources, over the bound's {most} a call")
        try:
            envs, prod = scope()
        except Exception as exc:  # noqa: BLE001 - not resolved is outside the bound
            envs, prod = None, f"the scope could not be read ({type(exc).__name__})"
        allowed = set(bound.get("environments") or [])
        extra["envs"] = list(envs) if envs is not None else None
        if not envs:
            why.append("the environment it touches could not be resolved from the org model")
        else:
            out = sorted({str(e) for e in envs} - allowed)
            if out:
                why.append(f"it touches {', '.join(out)}, outside the bound's "
                           f"{', '.join(sorted(allowed))}")
        if prod:
            why.append(f"a production context is in it ({str(prod)[:80]})")
    return list(dict.fromkeys(why))


def _tickets_today(now: datetime) -> int | None:
    """Tickets this machine let through unasked today (UTC): ticket lines of
    the ledger that were allowed. None when the ledger cannot be read."""
    try:
        from . import guard_ledger
        midnight = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        minutes = (now - midnight).total_seconds() / 60 + 1
        recs = guard_ledger.recent(minutes, now=now)
        day = midnight.date().isoformat()
        return sum(1 for r in recs if r.get("authority_class") == "ticket"
                   and r.get("decision") in ("allow", "warn")
                   and str(r.get("ts", ""))[:10] == day)
    except Exception:  # noqa: BLE001 - not counted, never zero
        return None


def _approvers_words(c: dict[str, Any]) -> str:
    if c.get("nobody_may_approve"):
        return "; its entries name no approver in common"
    if c.get("approvers"):
        return ": approvers " + ", ".join(c["approvers"])
    return "; it names no approver"


def _judge(cls: str, calls: list[_Call], *, multi: bool, cwd: str | None,
           scope: Callable[[], tuple[tuple[str, ...] | None, str | None]],
           standing: dict[str, Any] | None, ladder: dict[str, Any], now: datetime
           ) -> dict[str, Any]:
    c = ladder["classes"][cls]
    file_rung = c["rung"] if c["named"] else None
    rung, narrowed, st_note = _apply_standing(cls, file_rung or "none", standing)
    record: dict[str, Any] = {"file_rung": file_rung, "entries": c["keys"][:10]}
    if len(calls) == 1:
        record.update(calls[0].view())
    else:
        record["calls"] = [x.view() for x in calls[:5]]
    if st_note is not None:
        record["standing"] = st_note
    out: dict[str, Any] = {"decision": "allow", "action_type": ACTION_TYPE,
                           "authority_class": cls, "rung": rung, "within_bound": None}
    held = f" ({c['held'][0]})" if c.get("held") else ""
    tail = f" {narrowed}" if narrowed else ""
    label = _LABEL[cls]
    if rung in ("off", "recommend"):
        what = ("so an agent does not do it here" if rung == "off"
                else "so an agent proposes it and a person runs it")
        out["decision"] = "deny"
        out["reason"] = (f"nable guard: authority.yaml holds {label} at {rung}{held}, {what}. "
                         "Run it yourself if it should happen, or a person changes the entry "
                         f"(nable org set authority --class {cls} --rung approve_each).{tail}")
    elif rung == "approve_each":
        record["approvers"] = c.get("approvers")
        out["decision"] = "ask"
        out["reason"] = (f"nable guard: authority.yaml holds {label} at approve_each{held}, so "
                         f"a person approves each one{_approvers_words(c)}.{tail}")
    elif rung == "bounded":
        bound = dict(c.get("bound") or {})
        record["bound"] = bound
        extra: dict[str, Any] = {}
        why = _outside(cls, calls, bound, multi=multi, cwd=cwd, scope=scope,
                       tickets_today=lambda: _tickets_today(now), extra=extra)
        record.update({k: v for k, v in extra.items()})
        out["within_bound"] = not why
        if why:
            record["outside"] = why[:5]
            out["decision"] = "ask"
            out["reason"] = (f"nable guard: {label} is bounded in authority.yaml, and this call "
                             f"is outside its bound: {'; '.join(why[:3])}. A person approves "
                             f"it{_approvers_words(c)}.")
    out["authority"] = record
    return out


def _cannot_judge(cls: str, exc: BaseException) -> dict[str, Any]:
    return {"decision": "ask", "action_type": ACTION_TYPE, "authority_class": cls,
            "rung": "approve_each", "within_bound": None,
            "reason": (f"nable guard: {_LABEL[cls]} could not be judged against authority.yaml "
                       f"({type(exc).__name__}), so it asks."),
            "authority": {"error": type(exc).__name__}}


def ladder_verdict(command: str, *, cwd: str | None, harness: str,
                   scope: Callable[[], tuple[tuple[str, ...] | None, str | None]],
                   mcp: tuple[str, dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Seam S1: the ladder's answer for one call, or None for a call in no
    class (and for any call a money-moving, one-way or priced rule of the
    guard matches). Otherwise {"decision": "allow" | "ask" | "deny",
    "action_type": "authority_class", "authority_class", "rung": "none" |
    "off" | "recommend" | "approve_each" | "bounded", "within_bound": bool
    or None, "reason" (on ask and deny only), "authority": what the ledger
    line records}. `scope()` gives (the environments the org model resolves
    for the call, a production context or None) and is called only for an
    offhours_schedule call whose class is at bounded. `harness` is the
    caller's, as the guard records it. The guard merges this with its own
    verdict, the more severe answering. Never raises."""
    del harness                         # every harness is judged alike here
    try:
        if mcp is not None:
            calls = _mcp_call(*mcp)
        else:
            calls = classify(command)
    except Exception:  # noqa: BLE001 - not classified is judged as before
        return None
    if not calls:
        return None
    by_class: dict[str, list[_Call]] = {}
    for c in calls:
        by_class.setdefault(c.cls, []).append(c)
    first = next(iter(by_class))
    try:
        if mcp is None:
            excluded = _excluded(command, calls, cwd)
            if excluded:
                return None
            if excluded is None:
                for c in calls:
                    c.op = "unread" if c.op not in _REMOVALS else c.op
        now = _now()
        ladder = _ladder(cwd, now.astimezone(UTC).date())
        from . import authority_standing
        standing = authority_standing.for_guard()
        multi = len(by_class) > 1
        answers = [_judge(cls, cs, multi=multi, cwd=cwd, scope=scope, standing=standing,
                          ladder=ladder, now=now) for cls, cs in by_class.items()]
    except Exception as exc:  # noqa: BLE001 - a call in a class that cannot be judged asks
        return _cannot_judge(first, exc)
    worst = answers[0]
    for a in answers[1:]:
        if _SEVERITY[a["decision"]] > _SEVERITY[worst["decision"]]:
            worst = a
    return worst


# ── doctor ───────────────────────────────────────────────────────────────────

def doctor(covered: list[str], gaps: list[str], fix: Callable[[str, str], None]
           ) -> dict[str, Any]:
    """The doctor's word on the classes that move no money: each one
    authority.yaml names and the rung the guard applies (after the
    workspace's standing), each refused entry, and a demotion or a standing
    that was not read. {"present": False} when no authority.yaml names one
    and no standing is kept. Never raises."""
    try:
        now = _now()
        lad = _ladder(None, now.astimezone(UTC).date())
        from . import authority_standing
        standing = authority_standing.for_guard()
    except Exception as exc:  # noqa: BLE001 - said, never raised
        gaps.append(f"authority.yaml's classes that move no money could not be read "
                    f"({type(exc).__name__})")
        return {"present": None, "error": type(exc).__name__}
    named = {c: v for c, v in lad["classes"].items() if v["named"]}
    if not named and standing is None:
        return {"present": False}
    classes: dict[str, Any] = {}
    for cls in CLASSES:
        v = lad["classes"][cls]
        file_rung = v["rung"] if v["named"] else None
        rung, narrowed, _note = _apply_standing(cls, file_rung or "none", standing)
        classes[cls] = {"file_rung": file_rung, "rung": rung, "bound": v.get("bound")
                        if file_rung == "bounded" else None}
        if file_rung is not None:
            covered.append(f"authority.yaml: {cls} at {rung}"
                           + (f" (the file says {file_rung})" if rung != file_rung else ""))
        if narrowed:
            gaps.append(f"{cls}: {narrowed}")
    if lad["refused"]:
        gaps.append(f"authority.yaml: {len(lad['refused'])} refused entr"
                    f"{'y holds its class' if len(lad['refused']) == 1 else 'ies hold their class'}"
                    " that moves no money at approve_each")
        fix("nable org authority", "shows each refused entry with its file, entry and field")
    if standing is not None and not standing.get("read"):
        gaps.append(f"the workspace's standing was not read ({standing.get('why')}): every "
                    "class authority.yaml has at bounded is held at approve_each")
        fix("nable cloud status", "reads the workspace's standing again")
    if any(classes[c]["rung"] != classes[c]["file_rung"] and classes[c]["file_rung"]
           for c in CLASSES) and standing is not None and standing.get("read"):
        fix("open /rds-ri on your workspace", "an admin named in the class's entry lifts a "
            "demotion there, after a fresh sign-in")
    return {"present": True, "classes": classes,
            "standing": None if standing is None else {
                "read": standing.get("read"), "why": standing.get("why"),
                "as_of": standing.get("as_of")}}
