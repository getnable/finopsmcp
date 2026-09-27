"""
Infer standing cost policies from what a team keeps rejecting.

context_memory learns exceptions a human types in one at a time. This closes the
loop the other way: when a team dismisses the same CLASS of finding for the same
business reason over and over ("spot on prod, no", "idle in dr, that's the standby"),
nable notices the pattern and proposes the rule back. Three rejections become one
durable policy the human confirms with a click, instead of nable nagging forever.

It only proposes. Confirming a candidate calls context_memory.remember(), so the
human is always the one who turns a pattern into a rule (propose-only, never auto).
The axes it groups on are exactly the scopes context_memory can express: source,
bucket, provider, resource_type.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select

from ...storage.db import get_engine, savings_recommendations
from ..context_memory import list_context

# A dismissal only counts toward a policy when it's a BUSINESS choice ("keep it,
# and here's why"), not a quality miss ("your estimate is wrong") or a deferral
# ("we'll do it next sprint"). Mirrors learning.signal._BUSINESS_DISMISS_REASONS.
BUSINESS_REASONS = frozenset({"reserved_for_peak", "sla_sensitive", "not_our_resource"})
ACTED_STATUSES = frozenset({"acted_on", "verified"})

MIN_SUPPORT = 3        # need at least this many business dismissals before proposing
MIN_CONSISTENCY = 0.8  # dismissed-as-intentional / (dismissed + acted) must be this high

# Which rec field each proposable scope groups on (matches context_memory._SCOPE_FIELD).
_SCOPE_FIELD = {
    "source": "source",
    "bucket": "environment_bucket",
    "provider": "provider",
    "resource_type": "resource_type",
}
_REASON_PHRASE = {
    "reserved_for_peak": "reserved for peak or burst capacity",
    "sla_sensitive": "SLA-sensitive, not worth the risk",
    "not_our_resource": "owned by another team",
}


def _load_decided() -> list[dict]:
    """Recs the team has actually decided on (acted or dismissed). Open/expired don't
    carry a signal about intent, so they're left out."""
    sr = savings_recommendations
    with get_engine().connect() as conn:
        rows = conn.execute(
            select(
                sr.c.source, sr.c.environment_bucket, sr.c.provider, sr.c.resource_type,
                sr.c.status, sr.c.dismiss_reason_category, sr.c.dismiss_reason,
                sr.c.resource_id,
            ).where(sr.c.status.in_(list(ACTED_STATUSES) + ["dismissed"]))
        ).fetchall()
    return [{
        "source": r.source, "environment_bucket": r.environment_bucket,
        "provider": r.provider, "resource_type": r.resource_type, "status": r.status,
        "category": getattr(r, "dismiss_reason_category", None),
        "reason": getattr(r, "dismiss_reason", None), "resource_id": r.resource_id,
    } for r in rows]


def infer_policies(
    *, min_support: int = MIN_SUPPORT, min_consistency: float = MIN_CONSISTENCY,
    recs: list[dict] | None = None,
) -> list[dict[str, Any]]:
    """Propose standing cost policies from the dismissal history. Never writes anything.

    Returns a list of candidate rules, strongest first, each with the evidence behind
    it and the exact remember_cost_context() call that would enact it. Candidates
    already covered by an active context_memory rule are skipped.
    """
    decided = recs if recs is not None else _load_decided()
    existing = {(a["scope"], str(a["match_value"])) for a in list_context()}

    candidates: list[dict] = []
    for scope, field in _SCOPE_FIELD.items():
        groups: dict[str, dict] = defaultdict(
            lambda: {"acted": 0, "business": 0, "cats": defaultdict(int),
                     "reasons": [], "resources": [], "sources": set()})
        for rec in decided:
            val = rec.get(field)
            if not val or str(val).startswith("unknown"):
                continue
            g = groups[str(val)]
            if rec["status"] in ACTED_STATUSES:
                g["acted"] += 1
            elif rec["status"] == "dismissed" and rec["category"] in BUSINESS_REASONS:
                g["business"] += 1
                g["cats"][rec["category"]] += 1
                g["sources"].add(rec.get("source"))
                if rec.get("reason") and len(g["reasons"]) < 3:
                    g["reasons"].append(rec["reason"])
                if rec.get("resource_id") and len(g["resources"]) < 3:
                    g["resources"].append(rec["resource_id"])

        for val, g in groups.items():
            support = g["business"]
            denom = support + g["acted"]
            if support < min_support or denom == 0:
                continue
            consistency = support / denom
            if consistency < min_consistency:
                continue
            if (scope, val) in existing:
                continue
            # Over-suppression guard: a broad rule (provider / bucket / resource_type)
            # is only justified when the rejections span MULTIPLE finding types. If they
            # all came from one source, that's a source rule, not "ignore all of aws".
            if scope != "source" and len({s for s in g["sources"] if s}) < 2:
                continue
            dominant = max(g["cats"], key=g["cats"].get)
            phrase = _REASON_PHRASE.get(dominant, "intentional for this environment")
            candidates.append({
                "scope": scope,
                "match_value": val,
                "support": support,
                "acted": g["acted"],
                "consistency": round(consistency, 2),
                "dominant_reason": dominant,
                "suggested_reason": phrase,
                "sample_reasons": g["reasons"],
                "sample_resources": g["resources"],
                "evidence": (
                    f"You marked {support} {scope}={val} finding(s) intentional "
                    f"({phrase}) and acted on {g['acted']}. nable can stop surfacing "
                    f"this whole class instead of flagging each one."
                ),
                "confirm": (
                    f'remember_cost_context(scope="{scope}", '
                    f'match_value="{val}", reason="{phrase}")'
                ),
            })

    # Strongest evidence first: most rejections, then most consistent.
    candidates.sort(key=lambda c: (c["support"], c["consistency"]), reverse=True)
    return candidates


# ── Guard thresholds from the guard's asks ────────────────────────────────────
# The same loop for the guard: when a person keeps approving the same kind of
# change in the same scope, the guard should stop asking about it; when they
# keep declining, it should ask sooner. Either way nable only proposes an org
# model threshold fact (source inference:guard-ledger, the evidence in its
# note) and a person confirms it in `nable org questions`. A guess never
# loosens the guard: a proposed threshold is never read by it
# (OrgModel.threshold_for reads confirmed facts only).
#
# Loosening, one fact per scope, when an inference key (signal.guard_key) has:
#   - at least GUARD_MIN_APPROVALS approved asks the price threshold caused,
#   - spread over at least GUARD_MIN_SPAN_DAYS between the first and the last,
#   - no declined ask and no revert (signal.guard_signal) for that key,
# and nothing in the scope the fact would cover was declined at or below the
# proposed figure or reverted. The figure is the largest approved monthly
# cost rounded UP to a clean number (clean_up). Never for a one-way door:
# destroys, terminations and commitments always ask, whatever was approved.
#
# Tightening, when a key has at least GUARD_MIN_DECLINES declined asks the
# threshold caused: half the threshold now in force for that scope, rounded
# DOWN to a clean number, so the guard asks earlier. Restrictive, so the
# interview offers it with a default of yes.
#
# Scope. A threshold fact names a team, an environment or the org, the
# subjects guard_org.thresholds reads. A key with a team proposes for that
# team; without one, for its one confirmed environment; with neither, for
# the org (a key that touched several environments and no team proposes
# nothing). A fact a person rejected is not proposed again at or above the
# figure they turned down, and a scope with a proposal still waiting for an
# answer gets no second one.

GUARD_SOURCE = "inference:guard-ledger"
GUARD_MIN_APPROVALS = 5
GUARD_MIN_SPAN_DAYS = 7
GUARD_MIN_DECLINES = 3
_GUARD_FLOOR_USD = 10.0


def clean_up(usd: float) -> float:
    """`usd` rounded UP to two significant figures, and to a multiple of $10
    below $100: $840 stays $840, $847 becomes $850, $1,121 becomes $1,200,
    $12,345 becomes $13,000, $47 becomes $50. At most about 10% above the
    figure: a threshold learned from approvals should sit just over them."""
    import math
    if usd <= 0:
        return 0.0
    step = max(10.0, 10.0 ** (math.floor(math.log10(usd)) - 1))
    return float(math.ceil(round(usd / step, 9)) * step)


def clean_down(usd: float) -> float:
    """`usd` rounded DOWN the same way (two significant figures, $10 steps
    below $100): $250 stays $250, $1,234 becomes $1,200, $95 becomes $90."""
    import math
    if usd < _GUARD_FLOOR_USD:
        return 0.0
    step = max(10.0, 10.0 ** (math.floor(math.log10(usd)) - 1))
    return float(math.floor(round(usd / step, 9)) * step)


def _guard_subject(team: str | None, env: str | None) -> str | None:
    if team:
        return f"team:{team}"
    if env == "mixed":
        return None
    return f"environment:{env}" if env else "org:org"


def _covers(subject: str, key: dict[str, Any]) -> bool:
    """Would a threshold on `subject` apply to verdicts of this key?"""
    kind, _, ident = subject.partition(":")
    if kind == "team":
        return key["team"] == ident
    if kind == "environment":
        return key["env"] == ident or key["env"] == "mixed"
    return True                                    # org: everything without a narrower one


def _in_force(model: Any, subject: str, policy: dict[str, Any]) -> float:
    """The max_auto_monthly_usd the guard applies in `subject` today."""
    kind, _, ident = subject.partition(":")
    t = {}
    if model is not None:
        t = model.threshold_for(ident if kind == "team" else None,
                                ident if kind == "environment" else None, strict=True)
    return float(t.get("max_auto_monthly_usd", policy.get("max_auto_monthly_usd", 500.0)))


def _date(ts: str) -> str:
    """"Sep 12" for an ISO timestamp."""
    from datetime import datetime
    try:
        d = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return str(ts)[:10] or "?"
    return f"{d:%b} {d.day}"


def _where(subject: str) -> str:
    kind, _, ident = subject.partition(":")
    return {"team": f"team {ident}", "environment": f"the {ident} environment",
            "org": "the whole org (every scope without its own threshold)"}.get(kind, subject)


def _scope_words(e: dict[str, Any]) -> str:
    bits = [f"team {e['team']}" if e["team"] else "", f"env {e['env']}" if e["env"] else ""]
    return ", ".join(b for b in bits if b) or "unscoped"


def infer_guard_facts(signal: list[dict[str, Any]] | None = None, *, model: Any = None,
                      policy: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
    """What the guard's asks support: {"proposals": [...], "not_yet": [...]}.

    Each proposal: subject, direction ("loosen" | "tighten"), value,
    current_usd (the threshold in force now), confidence, dollars_monthly,
    note (the sentence a person reads: what it would change and the
    evidence), evidence (the counts and dates behind it) and fact (the org
    Fact to propose). not_yet lists every key with asks and why nothing is
    proposed for it, so `nable learn infer --dry-run` explains itself.
    Deterministic: the same ledger and org model give the same answer."""
    from datetime import datetime
    if signal is None:
        from .signal import guard_signal
        signal = guard_signal()
    if policy is None:
        from ...policy import load_policy
        policy = load_policy()
    if model is None:
        from ... import org
        model = org.load()
    proposals: dict[str, dict[str, Any]] = {}
    not_yet: list[dict[str, Any]] = []

    def hold(e: dict[str, Any], why: str) -> None:
        not_yet.append({"action_type": e["action_type"], "door": e["door"], "team": e["team"],
                        "env": e["env"], "approved": e["approved"], "declined": e["declined"],
                        "unknown": e["unknown"], "why": why})

    for e in signal:
        if e["door"] == "one_way":
            hold(e, "a one-way door: destroys, terminations and commitments always ask")
            continue
        subject = _guard_subject(e["team"], e["env"])
        if subject is None:
            hold(e, "touched several environments and no team: no one scope to name")
            continue
        if len(e["declines"]) >= GUARD_MIN_DECLINES:
            now_usd = _in_force(model, subject, policy)
            figure = clean_down(now_usd / 2)
            if figure < _GUARD_FLOOR_USD or figure >= now_usd:
                hold(e, f"declined {len(e['declines'])} times, but ${now_usd:,.0f}/mo is "
                        "already as low as a threshold goes")
                continue
            prev = proposals.get(subject)
            if prev is not None and (prev["direction"] == "tighten"
                                     and prev["value"]["max_auto_monthly_usd"] <= figure):
                continue
            first, last = e["declines"][0][0], e["declines"][-1][0]
            smallest = min(u for _, u in e["declines"])
            proposals[subject] = {
                "subject": subject, "direction": "tighten",
                "value": {"max_auto_monthly_usd": figure}, "current_usd": now_usd,
                "confidence": round(min(0.9, 0.6 + 0.05 * len(e["declines"])), 2),
                "dollars_monthly": smallest,
                "note": (f"Ask before any change over ${figure:,.0f}/mo in {_where(subject)}, "
                         f"not ${now_usd:,.0f}/mo: {e['action_type']} was declined "
                         f"{len(e['declines'])} times since {_date(first)} "
                         f"(smallest ${smallest:,.0f}/mo)."),
                "evidence": {"direction": "tighten", "action_type": e["action_type"],
                             "declined": len(e["declines"]), "first": first, "last": last,
                             "min_declined_usd": smallest, "current_usd": now_usd}}
            continue
        approvals = e["approvals"]
        if len(approvals) < GUARD_MIN_APPROVALS:
            hold(e, f"{len(approvals)} approved of the {GUARD_MIN_APPROVALS} needed")
            continue
        first, last = approvals[0][0], approvals[-1][0]
        span = (datetime.fromisoformat(last) - datetime.fromisoformat(first)).days
        if span < GUARD_MIN_SPAN_DAYS:
            hold(e, f"approved over {span} day(s) of the {GUARD_MIN_SPAN_DAYS} needed")
            continue
        if e["declined"]:
            hold(e, f"declined {e['declined']} time(s): a threshold would stop asking about "
                    "what a person said no to")
            continue
        if e["reverts"]:
            ex = e["revert_example"] or {}
            hold(e, f"reverted {e['reverts']} time(s) (`{ex.get('destroyed', '?')}` "
                    f"{ex.get('hours_later', '?')}h after `{ex.get('created', '?')}`)")
            continue
        top = max(u for _, u in approvals)
        figure = clean_up(top)
        now_usd = _in_force(model, subject, policy)
        if figure <= now_usd:
            hold(e, f"${now_usd:,.0f}/mo in force already covers ${top:,.0f}/mo")
            continue
        others = [o for o in signal if o is not e and o["door"] != "one_way"
                  and _covers(subject, o)]
        blocked = [o for o in others if o["reverts"]
                   or any(u <= figure for _, u in o["declines"])]
        if blocked:
            b = blocked[0]
            hold(e, f"{b['action_type']} ({_scope_words(b)}) was declined or reverted at or "
                    f"below ${figure:,.0f}/mo, which the threshold would also cover")
            continue
        prev = proposals.get(subject)
        if prev is not None and (prev["direction"] == "tighten"
                                 or prev["value"]["max_auto_monthly_usd"] >= figure):
            continue
        n = len(approvals)
        proposals[subject] = {
            "subject": subject, "direction": "loosen",
            "value": {"max_auto_monthly_usd": figure}, "current_usd": now_usd,
            "confidence": round(min(0.9, 0.5 + 0.05 * n), 2),
            "dollars_monthly": top,
            "note": (f"Stop asking before {e['action_type']} changes up to ${figure:,.0f}/mo "
                     f"in {_where(subject)} (asks now above ${now_usd:,.0f}/mo): approved "
                     f"{n} times since {_date(first)}, max ${top:,.0f}/mo, none declined or "
                     "reverted. One-way doors still ask."),
            "evidence": {"direction": "loosen", "action_type": e["action_type"],
                         "approved": n, "first": first, "last": last, "max_approved_usd": top,
                         "current_usd": now_usd}}

    out = []
    for subject, p in sorted(proposals.items()):
        why = _guard_blocked_by_org(model, p)
        if why:
            not_yet.append({"subject": subject, "direction": p["direction"], "why": why})
            continue
        from ...org import Fact
        p["fact"] = Fact.from_dict({
            "fact": "threshold", "subject": subject, "value": p["value"],
            "source": GUARD_SOURCE, "confidence": p["confidence"], "status": "proposed",
            "dollars_monthly": p["dollars_monthly"], "note": p["note"],
            "evidence": p["evidence"]})
        out.append(p)
    return {"proposals": out, "not_yet": not_yet}


def _guard_blocked_by_org(model: Any, p: dict[str, Any]) -> str | None:
    """Why the org model already answers this proposal, or None."""
    figure = p["value"]["max_auto_monthly_usd"]
    for f in model.candidates("threshold", p["subject"]):
        usd = f.value.get("max_auto_monthly_usd")
        if f.status == "proposed" and f.source == GUARD_SOURCE:
            return f"a proposal for {p['subject']} is waiting for an answer ({f.key})"
        if (f.status == "rejected" and isinstance(usd, (int, float))
                and p["direction"] == "loosen" and usd <= figure):
            return (f"a person rejected ${usd:,.0f}/mo for {p['subject']} "
                    f"({f.key}); nothing at or above it is proposed again")
    return None


def propose_guard_facts(dir: Any = None, *, dry_run: bool = False,
                        signal: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """infer_guard_facts() against the org model in `dir`, proposed there
    unless `dry_run`. Proposals only: nothing here confirms anything, and the
    guard reads confirmed thresholds only. Returns the inference with each
    proposal's `result` (org.propose_many's answer: added, duplicate, ...)
    and `key`."""
    from ... import org
    model = org.load(dir)
    got = infer_guard_facts(signal, model=model)
    if not dry_run and got["proposals"]:
        results = org.propose_many([p["fact"] for p in got["proposals"]], dir)
        for p, r in zip(got["proposals"], results, strict=True):
            p["result"] = r
    for p in got["proposals"]:
        p["key"] = p["fact"].key
    return got


def policy_for_rec(rec: dict[str, Any]) -> dict[str, Any] | None:
    """Cheap check used by the dismiss nudge: does the just-dismissed rec now push
    one of its own axes over the threshold? Returns the matching candidate or None."""
    try:
        cands = infer_policies()
    except Exception:
        return None
    for c in cands:
        field = _SCOPE_FIELD[c["scope"]]
        if str(rec.get(field) or "") == str(c["match_value"]):
            return c
    return None
