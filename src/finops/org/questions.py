# SPDX-License-Identifier: Apache-2.0
"""The few questions worth a human's time, most dollars first.

Ten questions in week one is the budget, so each one has to change a dollar
decision and come with a default answer ready. Candidates:

  bulk      every uncontested proposal that says one thing, as one question:
            "payments owns these 14 subjects, $8,210/mo", "these 6 accounts
            are nonprod", "these 12 account records are right"; default yes
  confirm   a proposed fact on its own ("is aws_account 123 owned by
            payments?"), default yes
  conflict  a proposal that disagrees with a confirmed fact, default no (keep it);
            or two confirmed facts that disagree (two branches merged), default
            yes (keep the one that answers now)
  recheck   a confirmed fact past its review_after, default yes (still true)
  unowned   an account with spend and no owner at all, no default team

Grouping. A proposal joins a bulk group by what it says: owner facts, team
facts and team tag aliases by their (canonical) team; environment facts and
environment tag aliases by their environment; everything else by kind. A
proposal is asked on its own when a group would hold only it, when another
live proposal about the same subject says something else (contested: a
person should see both), when a confirmed fact already answers (a
conflict), when it is a threshold or an approval chain (default no: each
is a person's call), when it is a freeze (default yes: it only restricts),
or when an agent proposed a team or a tag alias (it would route every
fact of that team, so it is never waved through with others).

A bulk answer is bound to the facts it showed: its command carries a digest
of their keys (`--owner-bulk payments@1a2b3c4d`), and the CLI refuses it,
printing the set as it is now, when a proposal joined or left the group
since.

Ranking. Ownership first (owner, team and unowned questions), then
environments, then descriptive facts (account records, tag keys): the Phase 1
exit is spend with a confirmed owner, and an account's name is not worth a
question the owner question needs. Within a tier, dollars_monthly (the
fact's own, else its subject's spend in the latest month) descending, then
confidence ascending: of two equal questions, the one the proposer was
least sure of is asked first.

A bulk question's dollars count each account once, add the tagged spend
its team aliases carry in other accounts, and count namespaces and
resources only when no account is in the group (they sit inside accounts).
"""
from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from .model import ACCOUNT_KINDS, Fact, OrgModel


@dataclass
class Question:
    kind: str                      # bulk | confirm | conflict | recheck | unowned
    text: str
    default: str                   # "y", "n", or "" when there is no default
    command: str                   # the CLI command a human runs to say yes
    dollars_monthly: float | None = None
    confidence: float = 0.0
    key: str | None = None
    subject: str | None = None
    fact: dict[str, Any] | None = field(default=None)
    no_command: str | None = None  # ... and to say no, where no is an answer
    group: str | None = None       # bulk: "owner:payments", "env:nonprod", "kind:account"
    keys: list[str] = field(default_factory=list)       # bulk: every fact it decides
    subjects: list[str] = field(default_factory=list)   # bulk: what those facts are about
    items: list[str] = field(default_factory=list)      # bulk: each fact, in full
    digest: str | None = None                           # bulk: bulk_digest(keys)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "default": self.default,
                "command": self.command, "dollars_monthly": self.dollars_monthly,
                "confidence": self.confidence, "key": self.key, "subject": self.subject,
                "no_command": self.no_command, "fact": self.fact, "group": self.group,
                "keys": list(self.keys), "subjects": list(self.subjects),
                "items": list(self.items), "digest": self.digest}


def describe(f: Fact) -> str:
    """One line a person can say yes or no to."""
    v, s = f.value, str(f.subject)
    if f.fact == "owner":
        return f"{s} is owned by team {v['team']}{_contact(v)}"
    if f.fact == "team":
        aliases = ", ".join(v.get("aliases") or [])
        return (f"team {f.subject.id} exists" + (f" (also called {aliases})" if aliases else "")
                + _contact(v))
    if f.fact == "environment":
        return f"{s} is {v['env']}"
    if f.fact == "tag_key":
        return f"tag key(s) {', '.join(v['keys'])} mean {v['canonical']}"
    if f.fact == "tag_alias":
        return f"tag value '{f.subject.id}' means {v['canonical_key']} {v['canonical_value']}"
    if f.fact == "account":
        name = v.get("name")
        bits = [f"business unit {v['business_unit']}" if v.get("business_unit") else "",
                f"cost center {v['cost_center']}" if v.get("cost_center") else ""]
        extra = ", ".join(b for b in bits if b)
        base = f"{s} is the account {name!r}" if name else f"{s} is one of our accounts"
        return base + (f" ({extra})" if extra else "")
    if f.fact == "threshold":
        parts = [f"{k}={v[k]}" for k in ("max_auto_monthly_usd", "velocity_cap_usd")
                 if v.get(k) is not None]
        return f"{s} thresholds: {', '.join(parts)}"
    if f.fact == "freeze":
        return (f"{s} is frozen from {v['start']} to {v['end']} ({v['reason']}), "
                f"changes {'denied' if v.get('mode') == 'deny' else 'asked about'}")
    if f.fact == "approval":
        least = v.get("min", 1)
        return (f"{s} changes of class {', '.join(v['action_classes'])} need {least} of "
                f"{', '.join(v['approvers'])} to approve"
                + (", with a change ticket" if v.get("change_ticket") else ""))
    return f"{f.fact} {s}: {v}"


def _contact(v: dict[str, Any]) -> str:
    """", channel #x, people a, b": where an owner or team is reached, which
    is what a yes routes tickets and asks to."""
    bits = []
    if v.get("channel"):
        bits.append(f"channel {v['channel']}")
    if v.get("people"):
        bits.append("people " + ", ".join(v["people"]))
    return "".join(f", {b}" for b in bits)


def bulk_digest(keys: Any) -> str:
    """Eight hex characters over the sorted keys a bulk question decides:
    what binds a bulk answer to the facts the question showed."""
    blob = ",".join(sorted({str(k) for k in keys}))
    return hashlib.sha1(blob.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]


def split_bulk(arg: str) -> tuple[str, str | None]:
    """("payments", "1a2b3c4d") for "payments@1a2b3c4d"; no digest, None."""
    what, sep, digest = (arg or "").strip().rpartition("@")
    if not sep or not re.fullmatch(r"[0-9a-f]{8}", digest.lower()):
        return (arg or "").strip(), None
    return what, digest.lower()


def _money(x: float | None) -> str:
    return f" (${x:,.0f}/mo)" if x else ""


# ── groups ────────────────────────────────────────────────────────────────────

def _says(model: OrgModel, f: Fact) -> str:
    """The group a proposal belongs to: what it says, not how."""
    v = f.value
    if f.fact == "owner":
        return "owner:" + model.canonical_team(str(v["team"]))[0]
    if f.fact == "team":
        return "owner:" + f.subject.id
    if f.fact == "tag_alias":
        if v["canonical_key"] == "team":
            return "owner:" + model.canonical_team(str(v["canonical_value"]))[0]
        if v["canonical_key"] == "environment":
            return "env:" + str(v["canonical_value"])
        return "kind:tag_alias"
    if f.fact == "environment":
        return "env:" + str(v["env"])
    return "kind:" + f.fact


def _tier(group_or_fact: str) -> int:
    kind = group_or_fact.split(":", 1)[0]
    return {"owner": 0, "team": 0, "unowned": 0, "env": 1, "environment": 1}.get(kind, 2)


def _fact_tier(f: Fact) -> int:
    if f.fact == "tag_alias":
        return {"team": 0, "environment": 1}.get(str(f.value.get("canonical_key")), 2)
    return _tier(f.fact)


def bulk_ok(f: Fact) -> bool:
    """Whether a proposal may be decided with others: never a threshold,
    a freeze or an approval chain, never a team or tag alias an agent
    proposed."""
    if f.fact in ("threshold", "freeze", "approval"):
        return False
    return not (f.fact in ("team", "tag_alias") and f.source.startswith("agent:"))


def groups(model: OrgModel) -> tuple[dict[str, list[Fact]], list[Fact]]:
    """({group: uncontested proposals}, proposals asked on their own:
    contested ones and those bulk_ok() keeps out). Proposals that a confirmed
    fact already answers are in neither: they are conflicts."""
    conflicted = {p.key for _, p in model.conflicts()}
    props = [f for f in model.proposals() if f.key not in conflicted]
    alone = [f for f in props if not bulk_ok(f)]
    props = [f for f in props if bulk_ok(f)]
    says = {f.key: _says(model, f) for f in props}
    by_slot: dict[tuple, set[str]] = {}
    for f in props:
        # Descriptive facts disagree on any difference; the rest on what they say.
        by_slot.setdefault(f.slot, set()).add(
            f.key if says[f.key].startswith("kind:") else says[f.key])
    for slot, answers in by_slot.items():
        if slot[0] == "tag_key" and len(answers) > 1:
            # Tag key proposals that only add keys to each other (a chain of
            # supersets) agree: confirming them smallest first leaves the
            # largest confirmed and the rest expired.
            sets = sorted((frozenset(k.lower() for k in f.value["keys"])
                           for f in props if f.slot == slot), key=len)
            if all(a <= b for a, b in pairwise(sets)):
                by_slot[slot] = {"chain"}
    out: dict[str, list[Fact]] = {}
    contested: list[Fact] = list(alone)
    for f in props:
        if len(by_slot[f.slot]) > 1:
            contested.append(f)
        else:
            out.setdefault(says[f.key], []).append(f)
    if "kind:tag_key" in out:
        out["kind:tag_key"].sort(key=lambda f: (f.value["canonical"], len(f.value["keys"])))
    return out, contested


def bulk_facts(model: OrgModel | None = None, *, owner: str | None = None,
               env: str | None = None, kind: str | None = None) -> list[Fact]:
    """The proposals one bulk question decides: those for team `owner`
    (through its aliases), environment `env`, or fact kind `kind`. The CLI's
    --owner-bulk/--env-bulk/--kind-bulk use it, so a bulk answer decides
    what the question listed; the CLI checks bulk_digest() of the keys
    against the one the question printed, so a set that changed since is
    refused, not decided."""
    if model is None:
        from .store import load
        model = load()
    grouped, _ = groups(model)
    if owner is not None:
        return list(grouped.get("owner:" + model.canonical_team(owner)[0], []))
    if env is not None:
        return list(grouped.get("env:" + env.strip().lower(), []))
    if kind is not None:
        return list(grouped.get("kind:" + kind.strip(), []))
    return []


def _label(model: OrgModel, f: Fact) -> str:
    s = f.subject
    if s.kind in ACCOUNT_KINDS:
        acct = model.resolve("account", s)
        name = acct.value.get("name") if acct is not None else None
        base = f"{s} ({name})" if name else str(s)
    elif f.fact == "team":
        aliases = ", ".join(f.value.get("aliases") or [])
        base = f"team {s.id}" + (f" (also called {aliases})" if aliases else "")
    elif s.kind == "tag_value":
        base = f"tag value {s.id}"
    else:
        base = str(s)
    # Where a yes sends tickets and asks: shown for every fact in the group.
    if f.fact in ("owner", "team"):
        base += _contact(f.value)
    return base


def _group_usd(group: str, facts: list[Fact], usd, spend_teams: dict[str, dict[str, float]]
               ) -> float | None:
    per: dict[str, float] = {}
    for f in facts:
        d = usd(f)
        if d:
            per[str(f.subject)] = max(per.get(str(f.subject), 0.0), d)
    if group == "kind:tag_key":
        return max(per.values(), default=0.0) or None
    accts = {f.subject.id for f in facts if f.subject.kind in ACCOUNT_KINDS}
    total = sum(v for s, v in per.items() if s.split(":", 1)[0] in ACCOUNT_KINDS)
    tagged = ("team", "tag_value")
    if group.startswith("owner:") and spend_teams:
        names = {group.split(":", 1)[1].lower()} | {
            f.subject.id.lower() for f in facts if f.fact == "tag_alias"}
        total += sum(v for acct, parts in spend_teams.items() if acct not in accts
                     for t, v in parts.items() if t.strip().lower() in names)
        others = [(s, v) for s, v in per.items()
                  if s.split(":", 1)[0] not in ACCOUNT_KINDS + tagged]
    else:
        others = [(s, v) for s, v in per.items() if s.split(":", 1)[0] not in ACCOUNT_KINDS]
    if not accts:
        total += sum(v for _, v in others)
    return round(total, 2) or None


def _sources(facts: list[Fact]) -> str:
    names = sorted({f.source.split(":", 1)[0] for f in facts})
    return ", ".join(names)


def _bulk_question(model: OrgModel, group: str, facts: list[Fact], usd,
                   spend_teams: dict[str, dict[str, float]]) -> Question:
    kind, _, what = group.partition(":")
    keys = [f.key for f in facts]         # the order they are decided in
    facts = sorted(facts, key=lambda f: (-(usd(f) or 0.0), str(f.subject), f.key))
    d = _group_usd(group, facts, usd, spend_teams)
    subjects = list(dict.fromkeys(str(f.subject) for f in facts))
    money = f", ${d:,.0f}/mo" if d else ""
    if kind == "owner":
        shown = list(dict.fromkeys(_label(model, f) for f in facts))
        preview = ", ".join(shown[:5]) + (f" and {len(shown) - 5} more" if len(shown) > 5
                                          else "")
        text = (f"{what} owns these {len(shown)} subjects{money}: {preview}. "
                f"From {_sources(facts)}.")
        flag = "--owner-bulk"
    elif kind == "env":
        shown = list(dict.fromkeys(_label(model, f) for f in facts))
        preview = ", ".join(shown[:5]) + (f" and {len(shown) - 5} more" if len(shown) > 5
                                          else "")
        note = (" Only a confirmed nonprod or sandbox makes anything eligible for an action."
                if what in ("nonprod", "sandbox") else "")
        text = (f"These {len(shown)} subjects are {what}{money}: {preview}. "
                f"From {_sources(facts)}.{note}")
        flag = "--env-bulk"
    else:
        preview = "; ".join(describe(f) for f in facts[:3]) + (
            f"; and {len(facts) - 3} more" if len(facts) > 3 else "")
        text = f"Are these {len(facts)} {what} facts right{money}? {preview}. " \
               f"From {_sources(facts)}."
        flag = "--kind-bulk"
    digest = bulk_digest(keys)
    arg = shlex.quote(f"{what}@{digest}")
    return Question(kind="bulk", text=text, default="y",
                    command=f"nable org confirm {flag} {arg}",
                    no_command=f"nable org reject {flag} {arg}", dollars_monthly=d,
                    confidence=min(f.confidence for f in facts), group=group,
                    keys=keys, subjects=subjects, digest=digest,
                    items=[f"{f.key}  {describe(f)}" for f in facts])


# ── questions ─────────────────────────────────────────────────────────────────

def questions(limit: int = 10, *, model: OrgModel | None = None,
              spend: Any = None, include_spend: bool = True, bulk: bool = True) -> list[Question]:
    """The top `limit` questions. `spend` is a coverage.Spend to reuse; with
    include_spend the local cost history is read for unowned accounts and to
    price facts that carry no dollars of their own. bulk=False asks every
    proposal on its own."""
    if model is None:
        from .store import load
        model = load()
    subject_usd: dict[str, float] = {}
    unowned: list[dict[str, Any]] = []
    spend_teams: dict[str, dict[str, float]] = {}
    if include_spend:
        from .coverage import _PROVIDER_KIND, coverage, read_spend
        sp = spend if spend is not None else read_spend()
        for (provider, account), usd_ in sp.accounts.items():
            kind = _PROVIDER_KIND.get(provider, "aws_account")
            subject_usd[f"{kind}:{account}"] = subject_usd.get(f"{kind}:{account}", 0.0) + usd_
        spend_teams = sp.teams
        if sp.accounts or sp.teams:
            unowned = coverage(model, spend=sp).get("unowned") or []

    def usd(f: Fact) -> float | None:
        if f.dollars_monthly is not None:
            return f.dollars_monthly
        return subject_usd.get(str(f.subject))

    out: list[tuple[int, Question]] = []
    conflicted: dict[str, Fact] = {p.key: w for w, p in model.conflicts()}
    grouped, contested = groups(model)
    singles: list[Fact] = list(contested)
    for group, facts in sorted(grouped.items()):
        if bulk and len(facts) > 1:
            out.append((_tier(group), _bulk_question(model, group, facts, usd, spend_teams)))
        else:
            singles.extend(facts)
    singles.extend(p for p in model.proposals() if p.key in conflicted)
    for w, other in model.conflicts():
        if not other.confirmed:
            continue
        d = usd(w) if usd(w) is not None else usd(other)
        out.append((_fact_tier(w), Question(
            kind="conflict", default="y", key=w.key, subject=str(w.subject),
            text=(f"Two confirmed facts disagree: '{describe(w)}' ({w.key}, answers now, "
                  f"confirmed by {w.confirmed_by or 'a person'}) and '{describe(other)}' "
                  f"({other.key}, confirmed by {other.confirmed_by or 'a person'}). "
                  f"Keep the first?{_money(d)}"),
            command=f"nable org confirm {w.key}", dollars_monthly=d,
            confidence=w.confidence, fact=w.summary(),
            no_command=f"nable org confirm {other.key}")))
    for f in singles:
        d = usd(f)
        if f.key in conflicted:
            w = conflicted[f.key]
            d = d if d is not None else usd(w)
            out.append((_fact_tier(f), Question(
                kind="conflict", default="n", key=f.key, subject=str(f.subject),
                text=(f"Replace the confirmed '{describe(w)}' with '{describe(f)}'?"
                      f"{_money(d)} Proposed by {f.source}, confidence {f.confidence:.2f}."),
                command=f"nable org confirm {f.key}", dollars_monthly=d,
                confidence=f.confidence, fact=f.summary(),
                no_command=f"nable org reject {f.key}")))
        else:
            out.append((_fact_tier(f), Question(
                # A threshold decides what runs unasked, an approval chain whom
                # nable asks for reviews: a person says yes to either on
                # purpose, never by pressing enter. A freeze only restricts.
                kind="confirm", default="n" if f.fact in ("threshold", "approval") else "y",
                key=f.key, subject=str(f.subject),
                text=(f"Is it right that {describe(f)}?{_money(d)} "
                      f"From {f.source}, confidence {f.confidence:.2f}."),
                command=f"nable org confirm {f.key}", dollars_monthly=d,
                confidence=f.confidence, fact=f.summary(),
                no_command=f"nable org reject {f.key}")))
    for f in model.stale():
        d = usd(f)
        out.append((_fact_tier(f), Question(
            kind="recheck", default="y", key=f.key, subject=str(f.subject),
            text=(f"Still true that {describe(f)}?{_money(d)} Confirmed by "
                  f"{f.confirmed_by or 'a person'} on {f.confirmed_at}, due for review "
                  f"{f.review_after}."),
            command=f"nable org confirm {f.key}", dollars_monthly=d,
            confidence=f.confidence, fact=f.summary(),
            no_command=f"nable org reject {f.key}")))
    for u in unowned:
        out.append((0, Question(
            kind="unowned", default="", subject=u["subject"],
            text=f"Which team owns {u['subject']}?{_money(u['dollars_monthly'])} Nothing says yet.",
            command=f"nable org set owner --subject {u['subject']} --team TEAM",
            dollars_monthly=u["dollars_monthly"], confidence=0.0)))
    out.sort(key=lambda tq: (tq[0], -(tq[1].dollars_monthly or 0.0), tq[1].confidence,
                             tq[1].key or tq[1].group or tq[1].subject or ""))
    return [q for _, q in out[:max(int(limit), 0)]]
