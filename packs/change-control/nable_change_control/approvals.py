# SPDX-License-Identifier: Apache-2.0
"""adapter approval-chains: who must approve a change, proposed from GitHub.

Reads, all from local files and none of it over the network:

  CODEOWNERS        in the repository (.github/CODEOWNERS, CODEOWNERS, then
                    docs/CODEOWNERS, the first found, as GitHub reads them).
                    Each rule that names a team proposes an approval chain for
                    that team: the rule's owners approve its changes.
  branch_protection optional: the JSON `gh api
                    repos/OWNER/REPO/branches/BRANCH/protection` prints. Its
                    required_approving_review_count becomes each chain's
                    `min`; whether it requires code owner reviews sets how
                    sure the proposal is.
  environments      optional: the JSON `gh api repos/OWNER/REPO/environments`
                    prints. A deployment environment whose protection rules
                    name required reviewers proposes an approval chain for the
                    nable environment it maps to (production -> prod, staging
                    -> nonprod, ...). GitHub needs one of them to approve.

The context says where (`nable pack run io.github.getnable/change-control
approval-chains --context repo=. --context branch_protection=bp.json`):
relative paths are read from `cwd`, the directory the command ran in.

Whatever this returns, nable writes it as a proposal (status proposed, source
starting with the pack id) for a person to confirm. It never confirms, never
writes a file and never calls GitHub: making those exports with the person's
own `gh` keeps credentials and network out of the pack.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
MAX_BYTES = 1024 * 1024
_TEAM = re.compile(r"^@([A-Za-z0-9][\w.-]*)/([A-Za-z0-9][\w.-]*)$")
_USER = re.compile(r"^@([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# GitHub deployment environment names, read as nable environments. A name
# that is none of these is left out: guessing prod would name approvers for
# changes they never reviewed.
ENVIRONMENTS = {
    "prod": ("prod", "production", "prd", "live"),
    "nonprod": ("staging", "stage", "dev", "development", "test", "testing", "qa", "uat",
                "preprod", "pre-prod", "nonprod", "non-prod"),
    "sandbox": ("sandbox", "sbx"),
    "dr": ("dr", "disaster-recovery"),
}


def _tokens(line: str) -> list[str]:
    """Split on unescaped whitespace, stop at an unescaped #."""
    out: list[str] = []
    cur: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            cur.append(line[i + 1] if line[i + 1] in " \t#" else ch + line[i + 1])
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


def parse(text: str) -> list[tuple[int, str, list[str], list[str]]]:
    """(line, pattern, team slugs, people as kind:id) for each valid rule.
    A line GitHub would skip (an unknown owner form, a negation, a range) is
    skipped here too; a rule with no owners unowns its paths and is kept."""
    rules = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or re.match(r"^\^?\[[^\]]*\]", line):
            continue
        toks = _tokens(line)
        if not toks or toks[0].startswith("!") or "[" in toks[0]:
            continue
        pattern, owners = toks[0], toks[1:]
        teams: list[str] = []
        people: list[str] = []
        ok = True
        for o in owners:
            if (m := _TEAM.match(o)):
                teams.append(m.group(2).lower())
            elif (m := _USER.match(o)):
                people.append(f"github:{m.group(1)}")
            elif _EMAIL.match(o):
                people.append(f"email:{o}")
            else:
                ok = False
        if ok:
            rules.append((n, pattern, teams, people))
    return rules


def _read(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _json(value: Any, base: Path, what: str, notes: list[str]) -> Any:
    if not value:
        return None
    p = Path(str(value)).expanduser()
    p = p if p.is_absolute() else base / p
    text = _read(p)
    if text is None:
        notes.append(f"{what}: {p} could not be read")
        return None
    try:
        return json.loads(text)
    except ValueError:
        notes.append(f"{what}: {p} is not JSON")
        return None


def _env_of(name: str) -> str | None:
    low = name.strip().lower()
    for env, words in ENVIRONMENTS.items():
        if low in words:
            return env
    return None


def _environment_facts(doc: Any, notes: list[str]) -> list[dict[str, Any]]:
    envs = doc.get("environments") if isinstance(doc, dict) else doc
    if not isinstance(envs, list):
        notes.append("environments: expected the object `gh api .../environments` prints")
        return []
    facts: list[dict[str, Any]] = []
    for e in envs:
        if not isinstance(e, dict) or not isinstance(e.get("name"), str):
            continue
        approvers: list[str] = []
        for rule in e.get("protection_rules") or []:
            if not isinstance(rule, dict) or rule.get("type") != "required_reviewers":
                continue
            for r in rule.get("reviewers") or []:
                who = r.get("reviewer") if isinstance(r, dict) else None
                if not isinstance(who, dict):
                    continue
                if r.get("type") == "Team" and who.get("slug"):
                    approvers.append(f"team:{str(who['slug']).lower()}")
                elif r.get("type") == "User" and who.get("login"):
                    approvers.append(f"github:{who['login']}")
        if not approvers:
            continue
        env = _env_of(e["name"])
        if env is None:
            notes.append(f"environment {e['name']!r} names reviewers, but it is not a name "
                         "nable reads as prod, nonprod, sandbox or dr, so it is left out")
            continue
        facts.append({"fact": "approval", "subject": {"kind": "environment", "id": env},
                      "value": {"action_classes": ["*"],
                                "approvers": list(dict.fromkeys(approvers)), "min": 1,
                                "change_ticket": False},
                      "source": f"github-environment:{e['name']}", "confidence": 0.7})
    return facts


def propose(ctx: Any, context: dict[str, Any]) -> list[dict[str, Any]]:
    """Approval facts (proposals) from CODEOWNERS and the GitHub exports the
    context names."""
    base = Path(str(context.get("cwd") or ".")).expanduser()
    repo = Path(str(context.get("repo") or base)).expanduser()
    repo = repo if repo.is_absolute() else base / repo
    notes: list[str] = []
    facts: list[dict[str, Any]] = []

    protection = _json(context.get("branch_protection"), base, "branch_protection", notes)
    reviews = (protection or {}).get("required_pull_request_reviews") \
        if isinstance(protection, dict) else None
    reviews = reviews if isinstance(reviews, dict) else {}
    need = reviews.get("required_approving_review_count")
    need = need if isinstance(need, int) and not isinstance(need, bool) and need >= 1 else 1
    owners_required = bool(reviews.get("require_code_owner_reviews"))
    if protection is not None and not reviews:
        notes.append("branch_protection requires no pull request reviews, so CODEOWNERS "
                     "names reviewers by convention only")

    found = next((repo / loc for loc in LOCATIONS if (repo / loc).is_file()), None)
    text = _read(found) if found is not None else None
    if found is None:
        notes.append(f"no CODEOWNERS in {repo} ({', '.join(LOCATIONS)})")
    elif text is None:
        notes.append(f"{found} could not be read")
    chains: dict[str, dict[str, Any]] = {}
    rel = found.relative_to(repo).as_posix() if found is not None else ""
    for n, pattern, teams, people in parse(text or ""):
        if not teams:
            if people:
                notes.append(f"{rel}:{n} ({pattern}) names only people, so no team's "
                             "approval chain is proposed from it")
            continue
        chain = chains.setdefault(teams[0], {"approvers": [], "lines": []})
        chain["approvers"] += [f"team:{t}" for t in teams] + people
        chain["lines"].append(str(n))
    if protection is not None and owners_required:
        confidence, how = 0.65, "code owner reviews required"
    elif protection is not None:
        confidence, how = 0.4, "code owner reviews not required"
    else:
        confidence, how = 0.5, "no branch protection export"
    for team, chain in sorted(chains.items()):
        approvers = list(dict.fromkeys(chain["approvers"]))
        facts.append({
            "fact": "approval", "subject": {"kind": "team", "id": team},
            "value": {"action_classes": ["*"], "approvers": approvers,
                      "min": min(need, len(approvers)), "change_ticket": False},
            "source": f"codeowners:{rel}:{','.join(chain['lines'][:10])} ({how})",
            "confidence": confidence})

    envs = _json(context.get("environments"), base, "environments", notes)
    if envs is not None:
        facts += _environment_facts(envs, notes)
    for note in notes:
        ctx.log(f"approval-chains: {note}")
    return facts
