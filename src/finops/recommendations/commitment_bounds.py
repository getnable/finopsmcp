# SPDX-License-Identifier: Apache-2.0
"""Commitment bounds from installed packs, applied to commitment advice.

A policy pack may declare `commitment_bounds` (finops.packs.content): a
coverage target, a longest term, the payment options allowed, and migration
blackouts. This module is the post-filter those declarations name. The
commitment purchases nable recommends itself (the Compute Savings Plan
advice and its projection, the Database Savings Plan advice) pass through it
before anyone sees them, and it only ever restricts:

  coverage target   a recommendation that would take coverage past the target
                    is cut down to the amount that reaches it; one that starts
                    at or above the target is dropped
  max term          a recommendation for a longer term is dropped (a shorter
                    offering has other rates, so it is not re-priced here)
  payment options   a recommendation with another payment option is dropped
  blackouts         a recommendation whose term would run into a migration
                    blackout over its scope is dropped. A scope nable cannot
                    pin down (a Compute Savings Plan covers every region) is
                    read as overlapping: a guess never loosens a bound

Bounds from several packs combine to the strictest: the lowest coverage
target, the shortest term, the payment options every pack allows, every
blackout. When an installed pack that provides policies cannot be loaded,
its bounds are unknown, so purchase advice is withheld rather than given
without them.

Not bounded yet: purchase advice a provider computes and nable passes on as
given (Google Cloud Recommender committed-use findings, Azure Advisor
reservation recommendations).

nable never buys a commitment. The bounds shape advice, and the guard asks
before any purchase an agent tries (the pack's guard rules name the bound).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

# Recommendation types that are purchases, and nothing else.
PURCHASE_TYPES = ("savings_plan", "database_savings_plan", "reserved_instance",
                  "committed_use_discount", "reservation")
_MONTH = timedelta(days=30.4375)


@dataclass
class Bounds:
    """The strictest of every installed pack's commitment bounds."""

    coverage_target_pct: float | None = None
    max_term_months: int | None = None
    payment_options: tuple[str, ...] | None = None
    blackouts: list[tuple[Any, str]] = field(default_factory=list)
    # "pack:bound-id" that set each figure, for the reasons.
    by: dict[str, str] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    # Installed packs with policies that could not be loaded.
    unreadable: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"coverage_target_pct": self.coverage_target_pct,
                "max_term_months": self.max_term_months,
                "payment_options": (list(self.payment_options)
                                    if self.payment_options is not None else None),
                "blackouts": [{**b.to_dict(), "by": src} for b, src in self.blackouts],
                "set_by": dict(self.by), "sources": list(self.sources),
                **({"unreadable": list(self.unreadable)} if self.unreadable else {})}


def _ref(item: Any) -> str:
    return f"{item.pack or '(no pack)'}:{item.id}"


def merge(items: list[Any]) -> Bounds | None:
    """The strictest combination of CommitmentBounds items, or None."""
    items = [i for i in items if type(i).__name__ == "CommitmentBounds"]
    if not items:
        return None
    b = Bounds()
    for it in items:
        ref = _ref(it)
        b.sources.append(ref)
        if it.coverage_target_pct is not None and (
                b.coverage_target_pct is None or it.coverage_target_pct < b.coverage_target_pct):
            b.coverage_target_pct, b.by["coverage_target_pct"] = it.coverage_target_pct, ref
        if it.max_term_months is not None and (
                b.max_term_months is None or it.max_term_months < b.max_term_months):
            b.max_term_months, b.by["max_term_months"] = it.max_term_months, ref
        if it.payment_options is not None:
            if b.payment_options is None:
                b.payment_options = tuple(it.payment_options)
                b.by["payment_options"] = ref
            else:
                b.payment_options = tuple(p for p in b.payment_options
                                          if p in it.payment_options)
                b.by["payment_options"] = f"{b.by['payment_options']}, {ref}"
        b.blackouts += [(bo, ref) for bo in it.blackouts]
    return b


def in_force() -> Bounds | None:
    """Bounds from the installed packs now, or None when no pack sets any.
    An installed pack that provides policies and is not loaded makes the
    result withhold purchase advice (Bounds.unreadable)."""
    from finops.packs import runtime, store
    items = runtime.active("policies")
    unreadable: list[str] = []
    problems = runtime.load_problems()
    if problems:
        try:
            idx = store.read_index().get("packs") or {}
        except Exception:  # noqa: BLE001 - an unreadable index: every problem counts
            idx = {}
        for p in problems:
            pid = p.split(" is not loaded", 1)[0]
            e = idx.get(pid) if isinstance(idx, dict) else None
            provides = e.get("provides") if isinstance(e, dict) else None
            if not isinstance(provides, dict) or "policies" in provides:
                unreadable.append(p)
    b = merge(items)
    if b is None and not unreadable:
        return None
    b = b or Bounds()
    b.unreadable = unreadable
    return b


@dataclass
class Judgement:
    action: str                      # "keep" | "cut" | "drop"
    factor: float = 1.0              # for "cut": the share of the recommendation kept
    reasons: list[str] = field(default_factory=list)
    by: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "factor": round(self.factor, 4),
                "reasons": list(self.reasons), "by": list(self.by)}


def _overlaps(bo: Any, view: dict[str, Any], now: datetime, term_months: int) -> bool:
    try:
        start = datetime.fromisoformat(bo.start).astimezone(UTC)
        end = datetime.fromisoformat(bo.end).astimezone(UTC)
    except ValueError:
        return True                  # validated at install; unreadable here reads as in force
    until = now + _MONTH * max(1, term_months)
    if not (start < until and now < end):
        return False
    for dim, key in (("providers", "provider"), ("types", "type"), ("services", "services"),
                     ("regions", "region"), ("accounts", "account")):
        wanted = {str(x).lower() for x in getattr(bo, dim)}
        if not wanted:
            continue
        got = view.get(key)
        if got in (None, "", (), []):
            continue                 # a scope nable cannot pin down overlaps
        values = {str(x).lower() for x in (got if isinstance(got, (list, tuple)) else [got])}
        if not values & wanted:
            return False
    return True


def judge(bounds: Bounds, view: dict[str, Any], *, now: datetime | None = None) -> Judgement:
    """What the bounds make of one recommended purchase.

    `view`: type, provider, services, region, account, term_months, payment,
    coverage_pct_now (percent of eligible spend covered today) and
    covers_monthly_usd (the on-demand spend a month the purchase would cover,
    taken as today's uncovered spend). Missing figures never loosen: a
    coverage target with no coverage figure drops the recommendation."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    j = Judgement("keep")
    if bounds.unreadable:
        j.action = "drop"
        j.reasons.append("an installed pack with policies could not be loaded, so its "
                         "commitment bounds are unknown and no purchase is advised until it "
                         "is fixed (`nable pack audit`): " + "; ".join(bounds.unreadable[:2]))
        return j
    term = int(view.get("term_months") or 0)
    if bounds.max_term_months is not None and (not term or term > bounds.max_term_months):
        j.action = "drop"
        j.reasons.append(f"its {term or 'unknown'}-month term is over the "
                         f"{bounds.max_term_months}-month maximum")
        j.by.append(bounds.by["max_term_months"])
    pay = view.get("payment")
    if bounds.payment_options is not None and pay not in bounds.payment_options:
        j.action = "drop"
        allowed = ", ".join(bounds.payment_options) or "none"
        j.reasons.append(f"payment option {pay or 'unknown'} is not one of those allowed "
                         f"({allowed})")
        j.by.append(bounds.by["payment_options"])
    for bo, ref in bounds.blackouts:
        if _overlaps(bo, view, now, term or 36):
            j.action = "drop"
            j.reasons.append(f"its term would run into migration blackout {bo.id} "
                             f"({bo.start} to {bo.end}): {bo.reason.rstrip('.')}")
            j.by.append(ref)
    target = bounds.coverage_target_pct
    if target is not None and j.action != "drop":
        now_pct = view.get("coverage_pct_now")
        covers = view.get("covers_monthly_usd")
        if not isinstance(now_pct, (int, float)) or not isinstance(covers, (int, float)) \
                or covers <= 0:
            j.action = "drop"
            j.reasons.append(f"its effect on coverage is unknown, and coverage is bounded at "
                             f"{target:g}%")
            j.by.append(bounds.by["coverage_target_pct"])
        elif now_pct >= target:
            j.action = "drop"
            j.reasons.append(f"coverage is already {now_pct:g}%, at or over the {target:g}% "
                             "target")
            j.by.append(bounds.by["coverage_target_pct"])
        else:
            c, t = now_pct / 100.0, target / 100.0
            covered = covers * c / (1.0 - c)
            allowed = t * (covered + covers) - covered
            if allowed < covers:
                j.action = "cut"
                j.factor = max(0.0, allowed / covers)
                j.reasons.append(f"covering all of it would take coverage from {now_pct:g}% "
                                 f"to about 100%, over the {target:g}% target, so it is cut "
                                 f"to {j.factor:.0%} of the amount")
                j.by.append(bounds.by["coverage_target_pct"])
    return j


def _words(j: Judgement) -> str:
    by = ", ".join(dict.fromkeys(j.by))
    return "; ".join(j.reasons) + (f" (bound {by})" if by else "")


_TERM = {"1-year": 12, "3-year": 36}


def compute_view(rec: dict[str, Any]) -> dict[str, Any]:
    """A Compute Savings Plan recommendation (recommendations.commitments)
    as judge() reads it. A Compute SP spans regions and linked accounts."""
    return {"type": "savings_plan", "provider": "aws",
            "services": ("ec2", "fargate", "lambda"), "region": None, "account": None,
            "term_months": _TERM.get(str(rec.get("term")), 0), "payment": rec.get("payment"),
            "coverage_pct_now": rec.get("coverage_pct_now"),
            "covers_monthly_usd": rec.get("baseline_monthly_uncovered_usd")}


def apply_compute(recs: list[dict[str, Any]], bounds: Bounds | None = None, *,
                  now: datetime | None = None
                  ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(recommendations within bounds, those cut or dropped with why).

    Only purchases are judged: a warning about unused commitment passes
    through untouched. A cut recommendation keeps its shape with its
    figures scaled and `bounds` saying what was cut and by which bound."""
    if bounds is None:
        return recs, []
    kept: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    for rec in recs:
        if rec.get("type") not in PURCHASE_TYPES:
            kept.append(rec)
            continue
        j = judge(bounds, compute_view(rec), now=now)
        if j.action == "keep":
            kept.append(rec)
            continue
        if j.action == "drop":
            changed.append({"title": rec.get("title"), "type": rec.get("type"),
                            "dropped": True, "why": _words(j), **j.to_dict(),
                            "was": {k: rec.get(k) for k in ("commitment_per_month",
                                                            "monthly_savings", "term",
                                                            "payment")}})
            continue
        f = j.factor
        was = {k: rec.get(k) for k in ("commitment_per_month", "monthly_savings",
                                       "annual_savings", "baseline_monthly_uncovered_usd")}
        cut = dict(rec)
        for k in was:
            if isinstance(rec.get(k), (int, float)):
                cut[k] = round(rec[k] * f, 2)
        cut["description"] = (f"{rec.get('description', '').rstrip()} Cut by the commitment "
                              f"bounds: {_words(j)}. Within them: "
                              f"${cut.get('commitment_per_month', 0):,.0f}/mo of commitment, "
                              f"about ${cut.get('monthly_savings', 0):,.0f}/mo saved.")
        cut["bounds"] = {"cut": True, "why": _words(j), "was": was, **j.to_dict()}
        kept.append(cut)
        changed.append({"title": rec.get("title"), "type": rec.get("type"), "dropped": False,
                        "why": _words(j), **j.to_dict(), "was": was})
    return kept, changed


def apply_database(result: dict[str, Any], bounds: Bounds | None = None, *,
                   now: datetime | None = None) -> dict[str, Any]:
    """recommend_database_savings_plans' result within bounds: a 1-year
    no-upfront Database Savings Plan on RDS and Aurora."""
    if bounds is None or not isinstance(result, dict) or result.get("data_incomplete"):
        return result
    if not result.get("recommended_sp_hourly_commitment"):
        return result
    view = {"type": "database_savings_plan", "provider": "aws", "services": ("rds", "aurora"),
            "region": None, "account": None, "term_months": 12, "payment": "no-upfront",
            "coverage_pct_now": result.get("current_sp_coverage_pct"),
            "covers_monthly_usd": result.get("uncovered_monthly_spend")}
    j = judge(bounds, view, now=now)
    if j.action == "keep":
        return result
    out = dict(result)
    keys = ("recommended_sp_hourly_commitment", "estimated_monthly_savings",
            "estimated_annual_savings")
    was = {k: result.get(k) for k in keys}
    f = 0.0 if j.action == "drop" else j.factor
    for k in keys:
        if isinstance(result.get(k), (int, float)):
            out[k] = round(result[k] * f, 4 if k == "recommended_sp_hourly_commitment" else 2)
    out["bounds"] = {"cut": j.action == "cut", "dropped": j.action == "drop",
                     "why": _words(j), "was": was, **j.to_dict()}
    if j.action == "drop":
        out["finding"] = None
    elif isinstance(out.get("finding"), dict):
        finding = dict(out["finding"])
        finding["bounds"] = out["bounds"]["why"]
        meta = dict(finding.get("metadata") or {})
        if "recommended_sp_hourly_commitment" in meta:
            meta["recommended_sp_hourly_commitment"] = out["recommended_sp_hourly_commitment"]
        finding["metadata"] = meta
        out["finding"] = finding
    return out


def cap_projection(actionable: dict[str, Any], *, coverage_pct: float | None,
                   bounds: Bounds | None = None, now: datetime | None = None
                   ) -> dict[str, Any]:
    """get_commitment_analysis' "if you bought more" projection (a 1-year
    no-upfront Compute Savings Plan covering half the uncovered on-demand)
    within bounds, and the coverage target the bounds set."""
    if bounds is None:
        return actionable
    out = dict(actionable)
    if bounds.coverage_target_pct is not None:
        target = min(bounds.coverage_target_pct, float(out.get("coverage_target_pct") or 100.0))
        out["coverage_target_pct"] = target
        now_pct = float(out.get("combined_coverage_pct") or 0.0)
        out["coverage_gap_pct"] = round(max(0.0, target - now_pct), 1)
    proj = out.get("if_you_bought_more")
    if not isinstance(proj, dict):
        return out
    monthly = float(out.get("monthly_uncovered_on_demand_usd") or 0.0)
    view = {"type": "savings_plan", "provider": "aws", "services": ("ec2", "fargate", "lambda"),
            "region": None, "account": None, "term_months": 12, "payment": "no-upfront",
            "coverage_pct_now": coverage_pct, "covers_monthly_usd": monthly * 0.5}
    j = judge(bounds, view, now=now)
    if j.action == "keep":
        return out
    if j.action == "drop":
        out.pop("if_you_bought_more")
        out["if_you_bought_more_withheld"] = f"Outside the commitment bounds: {_words(j)}."
        return out
    scaled = dict(proj)
    for k in ("additional_monthly_commitment_usd", "projected_monthly_savings_usd",
              "projected_annual_savings_usd"):
        if isinstance(proj.get(k), (int, float)):
            scaled[k] = round(proj[k] * j.factor, 2)
    scaled["description"] = (
        f"Within the commitment bounds ({_words(j)}): a 1-year no-upfront Compute Savings "
        f"Plan at ${scaled.get('additional_monthly_commitment_usd', 0):,.0f}/mo would save "
        f"about ${scaled.get('projected_monthly_savings_usd', 0):,.0f}/mo.")
    scaled["bounds"] = {"cut": True, "why": _words(j), **j.to_dict()}
    out["if_you_bought_more"] = scaled
    return out


def safely(fn: Any, *args: Any, **kw: Any) -> Any:
    """in_force() for callers that must keep going: a failure to read the
    packs at all withholds purchase advice (Bounds with unreadable set)."""
    try:
        return fn(*args, **kw)
    except Exception as e:  # noqa: BLE001 - reported in the advice, never raised
        log.warning("commitment bounds could not be read: %s", e)
        return Bounds(unreadable=[f"the installed packs could not be read ({type(e).__name__})"])
