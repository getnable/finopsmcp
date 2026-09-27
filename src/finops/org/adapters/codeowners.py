# SPDX-License-Identifier: Apache-2.0
"""CODEOWNERS: who reviews a path is the best first guess at who owns it.

Reads the CODEOWNERS file GitHub would use (.github/CODEOWNERS, then
CODEOWNERS, then docs/CODEOWNERS, first found) in each repo in ctx.repos, and
proposes an owner fact for each repo path that holds infrastructure code
(Terraform, Helm charts, CloudFormation, CDK, Pulumi, Kubernetes manifests).
Not for every file: a directory rule is proposed once, at the directory it
names; a deeper path only when its owner differs.

GitHub's syntax, as GitHub reads it:
  - the last matching rule wins, and a rule with no owners unowns a path;
  - `#` starts a comment unless escaped (`\\#`); `\\ ` is a space in a path;
  - a pattern with a slash anywhere but the end is anchored at the root, one
    without matches at any depth; a trailing slash matches a directory's
    contents; `*` stays within one path segment, `**` crosses them, and
    `docs/*` matches direct children only;
  - owners are @org/team, @user or an email; a line with any other owner, a
    `!` negation or a `[ ]` range is invalid and GitHub skips it, so do we.

@org/team becomes team <team>; @users and emails become people. A rule
naming only people has no team, so its first person stands in as the team,
at lower confidence, for a human to correct.

Confidence (before the adjustments below):
  0.85  an anchored directory rule with no wildcards (/infra/payments/)
  0.75  an unanchored directory, or one with wildcards (apps/, infra/*/prod/)
  0.60  a file glob (*.tf)
  0.50  the catch-all (*): a default reviewer, weak evidence of ownership
  -0.10 when the rule names more than one team (the rest go in co_owners)
  x0.6  when it names only people
  x share when the IaC files of one directory have different owners (the
        majority is proposed, scaled by its share)
  <=0.4 when two repos give one repo path different owners
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..model import Fact
from ._common import dedupe, fact, is_under, scan_iac

LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
_TEAM = re.compile(r"^@([A-Za-z0-9][\w.-]*)/([A-Za-z0-9][\w.-]*)$")
_USER = re.compile(r"^@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_GLOB = set("*?")


@dataclass
class Rule:
    pattern: str
    owners: list[str]
    line: int
    regex: re.Pattern[str]
    strength: float
    root: str | None = None                   # the directory an anchored literal rule names
    teams: list[str] = field(default_factory=list)
    people: list[str] = field(default_factory=list)


def _tokens(line: str) -> list[str]:
    """Split on unescaped whitespace, stop at an unescaped #, unescape."""
    out: list[str] = []
    cur: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            nxt = line[i + 1]
            # "\ " and "\#" become the character; any other escape is kept for
            # the glob translation ("\*" is a literal star).
            cur.append(nxt if nxt in " \t#" else ch + nxt)
            i += 2
            continue
        if ch == "#":
            break
        if ch in " \t":
            if cur:
                out.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
        i += 1
    if cur:
        out.append("".join(cur))
    return out


def _translate(p: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(p):
        if p.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif p.startswith("**", i):
            out.append(".*")
            i += 2
        elif p[i] == "*":
            out.append("[^/]*")
            i += 1
        elif p[i] == "?":
            out.append("[^/]")
            i += 1
        elif p[i] == "\\" and i + 1 < len(p):
            out.append(re.escape(p[i + 1]))
            i += 2
        else:
            out.append(re.escape(p[i]))
            i += 1
    return "".join(out)


def compile_pattern(pattern: str) -> tuple[re.Pattern[str], float, str | None] | None:
    """(regex over repo-relative file paths, strength, root dir) or None for
    a pattern GitHub does not support."""
    if not pattern or pattern.startswith("!") or "[" in pattern or "]" in pattern:
        return None
    anchored = pattern.startswith("/")
    p = pattern.lstrip("/")
    dir_only = p.endswith("/")
    p = p.rstrip("/")
    if not p:
        p, dir_only = "**", False
    if "/" in p:
        anchored = True
    body = _translate(p)
    prefix = "" if anchored else "(?:.*/)?"
    if p.endswith("/*") and not p.endswith("/**"):
        suffix = ""                       # docs/*: direct children only
    elif dir_only:
        suffix = "/.+"
    else:
        suffix = "(?:/.*)?"
    regex = re.compile("^" + prefix + body + suffix + "$")
    has_glob = bool(_GLOB & set(p.replace("\\*", "").replace("\\?", "")))
    if p in ("*", "**", "**/*"):
        return regex, 0.5, None
    last = p.rsplit("/", 1)[-1]
    if has_glob and "*" in last and not dir_only and "." in last:
        return regex, 0.6, None           # a file glob: *.tf, infra/**/*.yaml
    if anchored and not has_glob:
        return regex, 0.85, p.replace("\\", "")
    return regex, 0.75, None


def parse(text: str) -> list[Rule]:
    """Rules in file order. Invalid lines are skipped, as GitHub skips them."""
    rules: list[Rule] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^\^?\[[^\]]*\]", line):
            continue                      # a GitLab section header
        toks = _tokens(line)
        if not toks:
            continue
        pattern, owners = toks[0], toks[1:]
        compiled = compile_pattern(pattern)
        if compiled is None:
            continue
        if any(not (_TEAM.match(o) or _USER.match(o) or _EMAIL.match(o)) for o in owners):
            continue
        regex, strength, root = compiled
        teams = [m.group(2) for o in owners if (m := _TEAM.match(o))]
        people = [o for o in owners if not _TEAM.match(o)]
        rules.append(Rule(pattern, owners, n, regex, strength, root, teams, people))
    return rules


def owning_rule(rules: list[Rule], path: str) -> Rule | None:
    """The last rule that matches `path` (a repo-relative file path)."""
    for r in reversed(rules):
        if r.regex.match(path):
            return r
    return None


def find(repo: Path) -> Path | None:
    for loc in LOCATIONS:
        p = repo / loc
        if p.is_file():
            return p
    return None


def _value(rule: Rule) -> tuple[dict[str, Any], float]:
    conf = rule.strength
    if rule.teams:
        value: dict[str, Any] = {"team": rule.teams[0]}
        if len(rule.teams) > 1:
            value["co_owners"] = rule.teams[1:]
            conf -= 0.1
    else:
        value = {"team": rule.people[0]}
        conf *= 0.6
    if rule.people:
        value["people"] = list(rule.people)
    return value, conf


def repo_proposals(repo: Path, label: str = "") -> list[tuple[str, dict[str, Any], float, str]]:
    """(repo path, owner value, confidence, source) for one repo."""
    path = find(repo)
    if path is None:
        return []
    try:
        rules = parse(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []
    if not rules:
        return []
    co_rel = path.relative_to(repo).as_posix()
    decided: list[tuple[str, Rule, float]] = []
    unowned: list[str] = []
    for d, info in sorted(scan_iac(repo).items()):
        by_rule: dict[int, list[Rule]] = {}
        for f in info["files"]:
            r = owning_rule(rules, f)
            if r is not None:
                by_rule.setdefault(id(r), []).append(r)
        if not by_rule:
            continue
        hits = max(by_rule.values(), key=len)
        if not hits[0].owners:
            unowned.append(d)             # a rule with no owners: explicitly unowned
            continue
        decided.append((d, hits[0], len(hits) / len(info["files"])))
    picked: dict[str, tuple[dict[str, Any], float, str]] = {}
    for d, rule, share in decided:
        value, conf = _value(rule)
        conf *= share
        # Proposed once at the directory the rule names, unless a path under
        # it is explicitly unowned: a prefix fact would own that path too.
        lift = rule.root is not None and is_under(d, rule.root) and \
            not any(is_under(u, rule.root) for u in unowned)
        subject = rule.root if lift else d
        source = f"codeowners:{label}{co_rel}:{rule.line}"
        prev = picked.get(subject)
        if prev is None or (prev[0] == value and conf < prev[1]):
            picked[subject] = (value, conf, source)
    # A path whose nearest proposed ancestor already says the same adds nothing.
    out: list[tuple[str, dict[str, Any], float, str]] = []
    for subject in sorted(picked, key=lambda s: (s.count("/"), s)):
        value, conf, source = picked[subject]
        parents = [p for p in picked if p != subject and is_under(subject, p)]
        if parents:
            nearest = max(parents, key=lambda p: (p != ".", p.count("/"), len(p)))
            if picked[nearest][0] == value:
                continue
        out.append((subject, value, conf, source))
    return out


def propose(ctx: Any) -> list[Fact]:
    """Owner facts for the IaC paths of every repo in ctx.repos."""
    rows: list[tuple[str, dict[str, Any], float, str]] = []
    for repo in ctx.repos:
        rows.extend(repo_proposals(repo, ctx.repo_label(repo)))
    teams_for: dict[str, set[str]] = {}
    for subject, value, _, _ in rows:
        teams_for.setdefault(subject, set()).add(value["team"])
    out: list[Fact | None] = []
    for subject, value, conf, source in rows:
        if len(teams_for[subject]) > 1:
            conf = min(conf, 0.4)         # one path, two repos, two owners
        out.append(fact("owner", f"repo_path:{subject}", value, source, conf))
    return dedupe(out)
