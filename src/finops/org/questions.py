# SPDX-License-Identifier: Apache-2.0
"""The few questions worth a human's time, most dollars first.

Ten questions in week one is the budget, so each one has to change a dollar
decision and come with a default answer ready. Candidates:

  confirm   a proposed fact ("is aws_account 123 owned by payments?"), default yes
  conflict  a proposal that disagrees with a confirmed fact, default no (keep it)
  recheck   a confirmed fact past its review_after, default yes (still true)
  unowned   an account with spend and no owner at all, no default team

Ranked by dollars_monthly (the fact's own, else the subject's spend in the
latest month) descending, then confidence ascending: of two equal questions,
the one the proposer was least sure of is asked first.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import Fact, OrgModel


@dataclass
class Question:
    kind: str                      # confirm | conflict | recheck | unowned
    text: str
    default: str                   # "y", "n", or "" when there is no default
    command: str                   # the CLI command a human runs to say yes
    dollars_monthly: float | None = None
    confidence: float = 0.0
    key: str | None = None
    subject: str | None = None
    fact: dict[str, Any] | None = field(default=None)
    no_command: str | None = None  # ... and to say no, where no is an answer

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "default": self.default,
                "command": self.command, "dollars_monthly": self.dollars_monthly,
                "confidence": self.confidence, "key": self.key, "subject": self.subject,
                "no_command": self.no_command, "fact": self.fact}


def describe(f: Fact) -> str:
    """One line a person can say yes or no to."""
    v, s = f.value, str(f.subject)
    if f.fact == "owner":
        extra = f", channel {v['channel']}" if v.get("channel") else ""
        return f"{s} is owned by team {v['team']}{extra}"
    if f.fact == "team":
        aliases = ", ".join(v.get("aliases") or [])
        return f"team {f.subject.id} exists" + (f" (also called {aliases})" if aliases else "")
    if f.fact == "environment":
        return f"{s} is {v['env']}"
    if f.fact == "tag_key":
        return f"tag key(s) {', '.join(v['keys'])} mean {v['canonical']}"
    if f.fact == "tag_alias":
        return f"tag value '{f.subject.id}' means {v['canonical_key']} {v['canonical_value']}"
    if f.fact == "account":
        name = v.get("name")
        return f"{s} is the account {name!r}" if name else f"{s} is one of our accounts"
    if f.fact == "threshold":
        parts = [f"{k}={v[k]}" for k in ("max_auto_monthly_usd", "velocity_cap_usd")
                 if v.get(k) is not None]
        return f"{s} thresholds: {', '.join(parts)}"
    return f"{f.fact} {s}: {v}"


def _money(x: float | None) -> str:
    return f" (${x:,.0f}/mo)" if x else ""


def questions(limit: int = 10, *, model: OrgModel | None = None,
              spend: Any = None, include_spend: bool = True) -> list[Question]:
    """The top `limit` questions. `spend` is a coverage.Spend to reuse; with
    include_spend the local cost history is read for unowned accounts and to
    price facts that carry no dollars of their own."""
    if model is None:
        from .store import load
        model = load()
    subject_usd: dict[str, float] = {}
    unowned: list[dict[str, Any]] = []
    if include_spend:
        from .coverage import _PROVIDER_KIND, coverage, read_spend
        sp = spend if spend is not None else read_spend()
        for (provider, account), usd in sp.accounts.items():
            kind = _PROVIDER_KIND.get(provider, "aws_account")
            subject_usd[f"{kind}:{account}"] = subject_usd.get(f"{kind}:{account}", 0.0) + usd
        if sp.accounts or sp.teams:
            unowned = coverage(model, spend=sp).get("unowned") or []

    def usd(f: Fact) -> float | None:
        if f.dollars_monthly is not None:
            return f.dollars_monthly
        return subject_usd.get(str(f.subject))

    out: list[Question] = []
    conflicted: dict[str, Fact] = {p.key: w for w, p in model.conflicts()}
    for f in model.proposals():
        d = usd(f)
        if f.key in conflicted:
            w = conflicted[f.key]
            d = d if d is not None else usd(w)
            out.append(Question(
                kind="conflict", default="n", key=f.key, subject=str(f.subject),
                text=(f"Replace the confirmed '{describe(w)}' with '{describe(f)}'?"
                      f"{_money(d)} Proposed by {f.source}, confidence {f.confidence:.2f}."),
                command=f"nable org confirm {f.key}", dollars_monthly=d,
                confidence=f.confidence, fact=f.summary(),
                no_command=f"nable org reject {f.key}"))
        else:
            out.append(Question(
                kind="confirm", default="y", key=f.key, subject=str(f.subject),
                text=(f"Is it right that {describe(f)}?{_money(d)} "
                      f"From {f.source}, confidence {f.confidence:.2f}."),
                command=f"nable org confirm {f.key}", dollars_monthly=d,
                confidence=f.confidence, fact=f.summary(),
                no_command=f"nable org reject {f.key}"))
    for f in model.stale():
        d = usd(f)
        out.append(Question(
            kind="recheck", default="y", key=f.key, subject=str(f.subject),
            text=(f"Still true that {describe(f)}?{_money(d)} Confirmed by "
                  f"{f.confirmed_by or 'a person'} on {f.confirmed_at}, due for review "
                  f"{f.review_after}."),
            command=f"nable org confirm {f.key}", dollars_monthly=d,
            confidence=f.confidence, fact=f.summary(),
                no_command=f"nable org reject {f.key}"))
    for u in unowned:
        out.append(Question(
            kind="unowned", default="", subject=u["subject"],
            text=f"Which team owns {u['subject']}?{_money(u['dollars_monthly'])} Nothing says yet.",
            command=f"nable org set owner --subject {u['subject']} --team TEAM",
            dollars_monthly=u["dollars_monthly"], confidence=0.0))
    out.sort(key=lambda q: (-(q.dollars_monthly or 0.0), q.confidence, q.key or q.subject or ""))
    return out[:max(int(limit), 0)]
