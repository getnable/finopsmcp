# SPDX-License-Identifier: Apache-2.0
"""AWS Organizations: what the org already calls its accounts.

Reads the org_accounts table (connectors/aws_org.py syncs it; this adapter
never calls AWS) and the latest month of cost_snapshots, and proposes:

  account      the account's name, its business unit and cost center.
               Name alone 0.9 (AWS's own record); a business unit read from
               the OU path 0.6 (an OU is often a BU, not always); from an
               account tag (BusinessUnit, CostCenter) 0.8. The lowest part
               sets the fact's confidence.
  environment  from an Environment tag on the account 0.85; from the OU
               path 0.7 (an OU called Prod is a deliberate structure); from
               the account name 0.65 for prod, dr and shared, 0.6 for
               nonprod and sandbox. Names cover prod, production, staging,
               dev, sandbox, dr, and the foundational accounts (security,
               log-archive, audit, shared-services), which read as shared.
               Tag, OU and name that disagree propose nothing.
  owner        from a Team or Owner tag on the account 0.8; from an account
               name whose words, less its environment words, are exactly a
               team the org already names (CODEOWNERS, Terraform, the tagged
               teams in attributed_costs) 0.5, and only when nothing has
               proposed an owner for it yet.

The OU path is taken from parent_id when it reads as a path or a name
("Root/Payments/Prod", "Payments"), or from an ou_path tag; an opaque id
(ou-ab12-...) says nothing. An OU part is a business unit unless it is an
environment word or one of AWS's structural OU names (Workloads, Security,
Infrastructure, Sandbox, Suspended, Policy Staging, ...).

Dollars: the account's spend in the latest month, when there is any.
"""
from __future__ import annotations

import re
from typing import Any

from ..model import Fact, Subject
from ._common import dedupe, env_of_name, env_words, fact, key_meaning, team_norm
from .data import PROVIDER_KIND, UNATTRIBUTED

_OPAQUE = re.compile(r"^(?:ou-[a-z0-9]+-[a-z0-9]+|r-[a-z0-9]+|folders/\d+|\d+|"
                     r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.IGNORECASE)
# The OU names AWS's multi-account guidance uses for structure, not for a BU.
_STRUCTURAL = {"root", "workloads", "workload", "security", "infrastructure", "infra",
               "sandbox", "sandboxes", "suspended", "policy staging", "policystaging",
               "exceptions", "deployments", "transitional", "individual business users",
               "business continuity", "accounts", "members", "core", "shared",
               "shared services", "foundational", "platform", "log archive", "nonprod",
               "non-prod", "prod", "production", "test", "dev", "staging"}
_OU_TAGS = ("ou_path", "oupath", "ou", "organizational_unit", "organizationalunit")
_BU_TAGS = ("business_unit", "businessunit", "bu", "division", "department")


def ou_path(row: dict[str, Any]) -> list[str]:
    """The OU names above an account, root first, when they can be read."""
    tags = {k.lower().replace("-", "_"): v for k, v in row.get("tags", {}).items()}
    raw = next((tags[k] for k in _OU_TAGS if tags.get(k)), "") or row.get("parent_id") or ""
    raw = raw.strip().strip("/")
    if not raw or _OPAQUE.match(raw):
        return []
    parts = [p.strip() for p in re.split(r"\s*[/>]\s*", raw) if p.strip()]
    if parts and parts[0].lower() == "root":
        parts = parts[1:]
    return [p for p in parts if not _OPAQUE.match(p)]


def business_unit(parts: list[str]) -> str | None:
    for p in parts:
        low = p.lower()
        if low in _STRUCTURAL or env_words(low):
            continue
        return p
    return None


def _account_fact(row: dict[str, Any], subject: Subject, usd: float | None,
                  src: str) -> Fact | None:
    tags = row.get("tags", {})
    low = {k.lower().replace("-", "_"): v for k, v in tags.items()}
    value: dict[str, Any] = {}
    conf = 0.9
    if row.get("name"):
        value["name"] = row["name"]
    bu = next((low[k] for k in _BU_TAGS if low.get(k)), None)
    if bu:
        value["business_unit"] = bu
        conf = min(conf, 0.8)
    else:
        bu = business_unit(ou_path(row))
        if bu:
            value["business_unit"] = bu
            conf = min(conf, 0.6)
    cc = next((v for k, v in tags.items() if key_meaning(k) == "cost_center"), None)
    if cc:
        value["cost_center"] = cc
        conf = min(conf, 0.8)
    if not value:
        return None
    return fact("account", subject, value, src, conf, usd)


def _env_fact(row: dict[str, Any], subject: Subject, usd: float | None,
              src: str) -> Fact | None:
    votes: list[tuple[str, float]] = []
    for k, v in row.get("tags", {}).items():
        if key_meaning(k) == "environment":
            got = env_of_name(v)
            if got is None:
                return None               # a tag that says something else
            votes.append((got[0], 0.85))
    ou = ou_path(row)
    if ou:
        got = env_of_name(" ".join(ou))
        if got:
            votes.append((got[0], 0.7))
    if row.get("name"):
        got = env_of_name(row["name"])
        if got:
            votes.append((got[0], 0.6 if got[0] in ("nonprod", "sandbox") else 0.65))
    if not votes or len({e for e, _ in votes}) != 1:
        return None
    return fact("environment", subject, {"env": votes[0][0]}, src, max(c for _, c in votes), usd)


def _known_teams(ctx: Any) -> dict[str, str]:
    """normal form -> team name, for the teams the org already names."""
    out: dict[str, str] = {}
    for f in ctx.view().facts:
        if f.live and f.fact == "owner":
            out.setdefault(team_norm(f.value["team"]), f.value["team"])
        elif f.live and f.fact == "team":
            out.setdefault(team_norm(f.subject.id), f.subject.id)
    for parts in ctx.cost.teams.values():
        for t in parts:
            if t.strip().lower() not in UNATTRIBUTED and not t.startswith("@"):
                out.setdefault(team_norm(t), t)
    out.pop("", None)
    return out


def _owner_fact(ctx: Any, row: dict[str, Any], subject: Subject, usd: float | None,
                src: str, teams: dict[str, str]) -> Fact | None:
    for k, v in row.get("tags", {}).items():
        if key_meaning(k) == "team":
            return fact("owner", subject, {"team": v}, src, 0.8, usd)
    for k, v in row.get("tags", {}).items():
        if key_meaning(k) == "owner":
            value = {"team": v, "people": [v]} if "@" in v else {"team": v}
            return fact("owner", subject, value, src, 0.7 if "@" not in v else 0.5, usd)
    if not row.get("name") or ctx.live("owner", subject):
        return None
    envs = {w for _, w in env_words(row["name"])}
    words = [w for w in re.split(r"[^a-z0-9]+", row["name"].lower()) if w and w not in envs
             and w not in ("aws", "account", "acct", "org")]
    if not words:
        return None
    hits = {teams[n] for n in {team_norm(w) for w in words} | {team_norm("-".join(words))}
            if n in teams}
    if len(hits) != 1:
        return None
    return fact("owner", subject, {"team": next(iter(hits))}, src, 0.5, usd)


def propose(ctx: Any) -> list[Fact]:
    rows = [r for r in ctx.cost.org_accounts
            if r.get("status", "ACTIVE") not in ("SUSPENDED", "CLOSED", "PENDING_CLOSURE")]
    if not rows:
        return []
    teams = _known_teams(ctx)
    out: list[Fact | None] = []
    for row in sorted(rows, key=lambda r: (r["provider"], r["account_id"])):
        kind = PROVIDER_KIND.get(row["provider"], "aws_account")
        if not row["account_id"]:
            continue
        subject = Subject(kind, row["account_id"])
        usd = ctx.cost.account_usd(kind, row["account_id"])
        src = f"aws_org:org_accounts/{row['account_id']}"
        acct = _account_fact(row, subject, usd, src)
        if acct is not None and any(f.confirmed and all(f.value.get(k) == v for k, v in
                                                        acct.value.items())
                                    for f in ctx.live("account", subject)):
            acct = None                   # a person already said as much
        out.append(acct)
        out.append(_env_fact(row, subject, usd, src))
        out.append(_owner_fact(ctx, row, subject, usd, src, teams))
    return dedupe(out)
