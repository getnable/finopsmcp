"""
customer_signal(): the per-customer learning signal.

Reads this install's savings_recommendations ledger and, per recommendation source,
computes how often the customer ACTS on that rec type and how close PREDICTED
savings landed to MEASURED realized savings, then turns that into a verdict
(boost / suppress / neutral) and a confidence multiplier the rescorer uses to
re-rank proposals.

Three things keep it honest on sparse data (we have ~no ledger yet):
  - Bayesian shrinkage: act-rate is a Beta-posterior pulled toward a global prior,
    so a single dismissal can't nuke a rec type.
  - A COLD/WARMING/WARM ladder: a source is only ever SUPPRESSED once it has enough
    resolved recs (>= WARM_FLOOR); below that it keeps blanket behavior.
  - Reason-aware act-rate: dismissals tagged with a business reason (reserved for
    peak, SLA-sensitive, another team's resource) are kept OUT of the denominator.
    They mean "I chose to keep this", not "your rec was bad", so they never train a
    good source down. Quality dismissals (wrong estimate) and uncategorized ones do
    count. See _BUSINESS_DISMISS_REASONS.

This is deterministic math over the ledger (like quality_signal already is). No ML
training, no model files, fully reproducible and explainable. Single-tenant: it only
ever reads this install's own DB; nothing crosses a customer boundary.
"""
from __future__ import annotations

import statistics
from typing import Any

from sqlalchemy import func, select

from ..savings_tracker import get_engine
from ...storage.db import savings_recommendations

# ── Tunables (conservative on purpose; the ledger is near-empty today) ────────
PRIOR_ACT_RATE = 0.4      # global prior: absent evidence, assume ~40% act rate
PRIOR_STRENGTH = 5.0      # pseudo-observations of the prior (shrinkage strength)
WARM_FLOOR = 8            # >= this many RESOLVED recs before learned weights dominate
WARMING_FLOOR = 1         # 1..WARM_FLOOR-1 = blending learned with blanket
SUPPRESS_ACT_RATE = 0.15  # below this shrunk act-rate (and WARM) -> suppress for this customer
BOOST_FLOOR = 3           # >= this many resolved before we'll actively boost
BOOST_ACT_RATE = 0.5      # at/above this shrunk act-rate (and accurate) -> boost
ACCURACY_OK = (0.8, 1.2)  # predicted/realized within this band counts as "accurate"
APPROVAL_MIN_ACTED = 4    # need this many acted recs before a dollar floor is trusted

_RESOLVED = ("acted_on", "verified", "dismissed", "expired")
_ACTED = ("acted_on", "verified")


def _pctl(values: list[float], q: float) -> float | None:
    """Linear-interpolated percentile (q in 0..1), safe for tiny lists. None if empty."""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = int(pos)
    frac = pos - lo
    if lo + 1 < len(xs):
        return xs[lo] + frac * (xs[lo + 1] - xs[lo])
    return xs[lo]

# Dismiss categories that are a deliberate business choice, NOT evidence that the
# recommendation was low quality. We record them and show them in the `why`, but we
# keep them OUT of the act-rate denominator so they can't suppress a good source. A
# customer who keeps capacity for peak / SLA / another team's resource is not telling
# us the rec was bad, only that they chose not to act on it. Quality dismissals
# (wrong_estimate) and uncategorized ones (other / none) still count fully.
_BUSINESS_DISMISS_REASONS = frozenset({"reserved_for_peak", "sla_sensitive", "not_our_resource"})


def _coverage(resolved: int) -> str:
    if resolved <= 0:
        return "COLD"
    if resolved < WARM_FLOOR:
        return "WARMING"
    return "WARM"


def _shrunk_act_rate(acted: int, resolved: int) -> float:
    """Beta-posterior act-rate pulled toward PRIOR_ACT_RATE; stable when resolved is small."""
    return (acted + PRIOR_ACT_RATE * PRIOR_STRENGTH) / (resolved + PRIOR_STRENGTH)


def _verdict(coverage: str, shrunk: float, resolved: int, accuracy: float | None) -> str:
    # Both suppress and boost require WARM (>= WARM_FLOOR resolved). Below that a
    # source stays neutral (blanket behavior), so sparse data never flips a verdict.
    if coverage != "WARM":
        return "neutral"
    if shrunk < SUPPRESS_ACT_RATE:
        return "suppress"
    if shrunk >= BOOST_ACT_RATE and (
        accuracy is None or ACCURACY_OK[0] <= accuracy <= ACCURACY_OK[1]
    ):
        return "boost"
    return "neutral"


def _confidence_multiplier(shrunk: float, accuracy: float | None) -> float:
    """A ranking weight in ~[0,1]. High act-rate + accurate predictions rank higher;
    over-prediction (accuracy < 1) is penalized; under/unknown accuracy is not."""
    acc_factor = 1.0
    if accuracy is not None and accuracy < 1.0:
        acc_factor = max(0.3, accuracy)
    # Floor so a sparse/low-accuracy source is ranked low, never erased to 0.
    return max(0.001, round(shrunk * acc_factor, 3))


def _why(source: str, coverage: str, verdict: str, acted: int, resolved: int,
         accuracy: float | None, business_dismissed: int = 0) -> str:
    if coverage == "COLD":
        return f"No decisions on {source} recs yet, using the standard ranking (global default)."
    acc_txt = ""
    if accuracy is not None:
        if accuracy < ACCURACY_OK[0]:
            acc_txt = f" and past {source} savings landed ~{round(accuracy*100)}% of estimate (we over-predicted)"
        elif accuracy > ACCURACY_OK[1]:
            acc_txt = f" and past {source} savings beat the estimate (~{round(accuracy*100)}%)"
        else:
            acc_txt = f" and past {source} savings landed within ~{round(accuracy*100)}% of estimate"
    base = f"You acted on {acted}/{resolved} {source} recs you decided on{acc_txt}."
    if business_dismissed:
        base += (f" {business_dismissed} more dismissed for a business reason "
                 f"(peak/SLA/another team), not counted against {source}.")
    if verdict == "suppress":
        return base + " So these are suppressed for you (still here if you want them)."
    if verdict == "boost":
        return base + " So these rank higher for you."
    return base


def _new_counts() -> dict:
    # business_dismissed is a SUBSET of dismissed: dismissals tagged with a business
    # reason (peak / SLA / not-ours). Tracked separately so it can be excluded from the
    # act-rate denominator without losing it from the user-facing counts.
    return {"open": 0, "acted_on": 0, "verified": 0, "dismissed": 0,
            "business_dismissed": 0, "expired": 0, "realized": 0.0}


def _entry(source: str, bucket: str | None, c: dict, acc_list: list[float]) -> dict:
    """Build one signal entry (the same math for a per-source or a per-(source,bucket)
    grouping). accuracy is the median per-rec ratio; act-rate is Bayesian-shrunk."""
    acted = c["acted_on"] + c["verified"]
    business_dismissed = c["business_dismissed"]
    # Business-reason dismissals (peak/SLA/not-ours) are a choice, not a quality
    # signal, so they don't count toward resolved. Only quality + uncategorized
    # dismissals do. clamp guards a malformed count where business > total dismissed.
    quality_dismissed = max(0, c["dismissed"] - business_dismissed)
    resolved = acted + quality_dismissed + c["expired"]
    accuracy = round(statistics.median(acc_list), 3) if acc_list else None
    shrunk = round(_shrunk_act_rate(acted, resolved), 3)
    coverage = _coverage(resolved)
    verdict = _verdict(coverage, shrunk, resolved, accuracy)
    e = {
        "source": source,
        "open": c["open"], "acted": acted, "verified": c["verified"],
        "dismissed": c["dismissed"], "business_dismissed": business_dismissed,
        "expired": c["expired"], "resolved": resolved,
        "act_rate": shrunk,
        "act_rate_raw": round(acted / resolved, 3) if resolved else None,
        "accuracy": accuracy, "coverage": coverage, "verdict": verdict,
        "confidence_multiplier": _confidence_multiplier(shrunk, accuracy),
        "why": _why(source, coverage, verdict, acted, resolved, accuracy, business_dismissed),
        "realized_monthly_usd": round(c["realized"], 2),
    }
    if bucket is not None:
        e["bucket"] = bucket
    return e


def customer_signal(apply_learned_overrides: bool = True) -> dict[str, Any]:
    """Per-source AND per-(source, bucket) learning signal for this install. The bucket
    breakdown lets the loop learn e.g. spot is fine for nonprod-batch but not prod-steady.
    See module docstring.

    apply_learned_overrides: rolled-back lessons (ledger.py) pin their keys to
    neutral. On by default so every consumer honors a customer's rollback; the
    ledger's own sync passes False, because it must diff the raw signal."""
    sr = savings_recommendations
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                sr.c.source, sr.c.environment_bucket, sr.c.status,
                sr.c.dismiss_reason_category,
                func.count().label("cnt"),
                func.sum(sr.c.verified_monthly_savings_usd).label("sum_ver"),
            ).group_by(sr.c.source, sr.c.environment_bucket, sr.c.status,
                       sr.c.dismiss_reason_category)
        ).fetchall()
        # Per-rec accuracy among verified recs; median (not a sum-ratio) so one big
        # miss can't dominate; negatives clamp to 0 (a "verified" loss = a failed rec).
        vrows = conn.execute(
            select(sr.c.source, sr.c.environment_bucket,
                   sr.c.estimated_monthly_savings_usd,
                   sr.c.verified_monthly_savings_usd).where(sr.c.status == "verified")
        ).fetchall()

    source_counts: dict[str, dict] = {}
    bucket_counts: dict[tuple, dict] = {}
    for r in rows:
        src = r.source or "unknown"
        bkt = r.environment_bucket or "unknown|other"
        cnt = int(r.cnt or 0)
        ver = max(0.0, float(r.sum_ver or 0.0))
        is_business_dismiss = (
            r.status == "dismissed"
            and getattr(r, "dismiss_reason_category", None) in _BUSINESS_DISMISS_REASONS
        )
        for store, key in ((source_counts, src), (bucket_counts, (src, bkt))):
            d = store.setdefault(key, _new_counts())
            if r.status in d:
                d[r.status] += cnt
            if is_business_dismiss:
                d["business_dismissed"] += cnt
            if r.status == "verified":
                d["realized"] += ver

    source_acc: dict[str, list[float]] = {}
    bucket_acc: dict[tuple, list[float]] = {}
    for r in vrows:
        est = float(r.estimated_monthly_savings_usd or 0.0)
        if est <= 0:
            continue
        ratio = max(0.0, float(r.verified_monthly_savings_usd or 0.0)) / est
        src = r.source or "unknown"
        bkt = r.environment_bucket or "unknown|other"
        source_acc.setdefault(src, []).append(ratio)
        bucket_acc.setdefault((src, bkt), []).append(ratio)

    by_source = [_entry(src, None, c, source_acc.get(src, [])) for src, c in sorted(source_counts.items())]
    by_source.sort(key=lambda s: s["realized_monthly_usd"], reverse=True)
    by_bucket = [_entry(src, bkt, c, bucket_acc.get((src, bkt), []))
                 for (src, bkt), c in sorted(bucket_counts.items())]
    total_realized = sum(c["realized"] for c in source_counts.values())

    result = {
        "by_source": by_source,
        "by_bucket": by_bucket,
        "approval_profile": approval_profile(),
        "verified_monthly_usd": round(total_realized, 2),
        "verified_annual_run_rate_usd": round(total_realized * 12, 2),
        "params": {
            "prior_act_rate": PRIOR_ACT_RATE, "prior_strength": PRIOR_STRENGTH,
            "warm_floor": WARM_FLOOR, "suppress_act_rate": SUPPRESS_ACT_RATE,
            "boost_act_rate": BOOST_ACT_RATE, "accuracy_ok": list(ACCURACY_OK),
        },
        "note": ("Per recommendation source (and per environment bucket): act-rate "
                 "(acted vs all you decided on, Bayesian-shrunk) and accuracy (measured "
                 "vs predicted savings among verified recs). Dismissals for a business "
                 "reason (reserved for peak, SLA-sensitive, another team's resource) are "
                 "recorded but kept out of the act-rate, so a valid 'keep it' never trains "
                 "a good source down. A source/bucket stays on blanket behavior until it "
                 f"has >= {WARM_FLOOR} resolved recs; only then can it be suppressed for you."),
    }
    if apply_learned_overrides:
        # Lazy import: ledger imports this module, and a broken ledger must
        # degrade to the un-overridden signal, never break ranking.
        try:
            from .ledger import apply_overrides
            result = apply_overrides(result)
        except Exception:
            pass
    return result


def approval_profile() -> dict[str, Any]:
    """What this customer actually says yes to: the dollar size and the resource
    types they act on versus dismiss. Lets the loop lead with recs shaped like the
    ones they have already approved, not by raw dollars alone.

    approval_floor_usd is the 25th percentile of what they DID act on, so a rec far
    below it can rank lower for them. It stays None until there are at least
    APPROVAL_MIN_ACTED acted recs, so on a near-empty ledger callers fall back to
    plain dollar ordering. Business-reason dismissals (peak / SLA / not-ours) are a
    choice, not a no, so they are excluded from both the dollar and the per-type math.
    Single-tenant: reads only this install's own ledger.
    """
    sr = savings_recommendations
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                sr.c.status, sr.c.resource_type, sr.c.dismiss_reason_category,
                sr.c.estimated_monthly_savings_usd,
            ).where(sr.c.status.in_(_RESOLVED))
        ).fetchall()

    acted_usd: list[float] = []
    dismissed_usd: list[float] = []
    by_type: dict[str, dict] = {}
    for r in rows:
        is_business = (
            r.status == "dismissed"
            and getattr(r, "dismiss_reason_category", None) in _BUSINESS_DISMISS_REASONS
        )
        if is_business:
            continue
        rt = (r.resource_type or "unknown").strip() or "unknown"
        est = float(r.estimated_monthly_savings_usd or 0.0)
        d = by_type.setdefault(rt, {"acted": 0, "resolved": 0})
        d["resolved"] += 1
        if r.status in _ACTED:
            d["acted"] += 1
            acted_usd.append(est)
        else:
            dismissed_usd.append(est)

    total_acted = len(acted_usd)
    coverage = _coverage(total_acted + len(dismissed_usd))
    floor = round(_pctl(acted_usd, 0.25), 2) if total_acted >= APPROVAL_MIN_ACTED else None

    types = [
        {"resource_type": rt, "acted": c["acted"], "resolved": c["resolved"],
         "act_rate": round(c["acted"] / c["resolved"], 3)}
        for rt, c in sorted(by_type.items()) if c["resolved"] > 0
    ]
    types.sort(key=lambda t: (t["act_rate"], t["resolved"]), reverse=True)

    return {
        "coverage": coverage,
        "approval_floor_usd": floor,
        "acted_median_usd": round(statistics.median(acted_usd), 2) if acted_usd else None,
        "dismissed_median_usd": round(statistics.median(dismissed_usd), 2) if dismissed_usd else None,
        "acted_count": total_acted,
        "by_resource_type": types,
        "note": ("The dollar size and resource types you have acted on before. "
                 "approval_floor_usd is the 25th percentile of what you acted on, set "
                 "only once there are enough decisions; recs below it can rank lower "
                 "for you. Business-reason dismissals are excluded."),
    }


def _has_signal(entry: dict) -> bool:
    """An entry carries real signal if the customer has resolved any recs OR has
    dismissed some for a business reason. The latter has resolved == 0 (business
    dismissals are out of the denominator) but is still a learned 'keep it for this
    source/bucket', so it should win over a fall-through to a possibly-suppressed
    source aggregate or a COLD default."""
    return entry.get("resolved", 0) > 0 or entry.get("business_dismissed", 0) > 0


def signal_for(signal: dict, source: str, bucket: str | None = None) -> dict:
    """Look up the signal for a source (and bucket if given). Prefers a bucket-level
    entry with real signal, falls back to the source aggregate, then a COLD default."""
    if bucket:
        for s in signal.get("by_bucket", []):
            if s["source"] == source and s.get("bucket") == bucket and _has_signal(s):
                return s
    for s in signal.get("by_source", []):
        if s["source"] == source and _has_signal(s):
            return s
    return {
        "source": source, "bucket": bucket, "resolved": 0, "acted": 0,
        "business_dismissed": 0,
        "act_rate": PRIOR_ACT_RATE, "accuracy": None, "coverage": "COLD", "verdict": "neutral",
        "confidence_multiplier": _confidence_multiplier(PRIOR_ACT_RATE, None),
        "why": f"No decisions on {source} recs yet, using the standard ranking (global default).",
    }


# ── The guard's asks ──────────────────────────────────────────────────────────
# The same idea as customer_signal(), fed by the other place a person decides:
# the guard's asks (guard_ledger, with the post hook's outcomes). An ask that
# ran was approved; one that did not was declined (guard_ledger.ask_outcomes).
# Rolled up per inference key, the unit a threshold can be learned for:
#
#   (action_type, door, team, env)
#
# team is the team the guard judged the command under (FINOPS_GUARD_TEAM, or
# the confirmed owner of the working directory's repo path: the team whose
# threshold the guard reads, guard._with_scope), else the confirmed owner of
# what the command touched (the ask's `owner`), else None (unscoped); env is
# the one confirmed environment it touched, "mixed" for several, None for
# none. A proposal names the narrowest of these the evidence came from, so a
# yes never reaches further than what was approved. Plain
# counting over the ledger, like the rest of this module: no model, the same
# ledger always gives the same answer, and every figure comes with the asks
# behind it. policy_inference.infer_guard_facts turns it into proposals.

GUARD_LOOKBACK_DAYS = 90
# A destroy of what an approved creation made, this soon after it, is a revert.
REVERT_WINDOW_HOURS = 24


def guard_key(rec: dict[str, Any]) -> tuple[str, str, str | None, str | None]:
    """(action_type, door, team, env) for one verdict record."""
    scope = rec.get("scope") if isinstance(rec.get("scope"), dict) else {}
    envs = [e for e in (scope.get("envs") or []) if isinstance(e, str)]
    env = envs[0] if len(envs) == 1 else ("mixed" if envs else None)
    team = scope.get("team") if isinstance(scope.get("team"), str) else None
    owner = rec.get("owner") if isinstance(rec.get("owner"), dict) else {}
    if not team and owner.get("confirmed") is True and isinstance(owner.get("team"), str):
        team = owner["team"]
    return (str(rec.get("action_type") or "unknown"), str(rec.get("door") or "two_way"),
            team or None, env)


def _tool_family(command: Any) -> str:
    """What a command acts on, coarsely, so a destroy can be matched to the
    creation it undoes: the IaC tool (terraform, pulumi, cdk, sam), a
    CloudFormation stack by name, an AWS, gcloud or az service, kubectl or a
    Helm release. Coarse on purpose: a false match costs a proposal (the
    safe direction), a missed one would let a revert pass unseen."""
    import re
    words = str(command or "").split()
    for i, w in enumerate(words):
        base = w.rsplit("/", 1)[-1]
        if base in ("terraform", "tofu", "terragrunt"):
            return "terraform"
        if base in ("pulumi", "cdk", "sam", "kubectl", "doctl"):
            return base
        if base == "helm":
            rel = [x for x in words[i + 1:] if not x.startswith("-")]
            return f"helm:{rel[1]}" if len(rel) > 1 else "helm"
        if base in ("aws", "gcloud", "az") and i + 1 < len(words):
            svc = words[i + 1]
            if base == "aws" and svc == "cloudformation":
                m = re.search(r"--stack-name(?:=|\s+)(\S+)", " ".join(words[i:]))
                return f"cloudformation:{m.group(1)}" if m else "cloudformation"
            return f"{base}:{svc}"
    return words[0] if words else ""


def _pos(x: Any) -> float:
    return float(x) if isinstance(x, (int, float)) and x > 0 else 0.0


def _group_answers(verdicts: list[dict[str, Any]], answers: dict[str, str]) -> dict[int, str]:
    """{index of the first ask of each run of repeats: the run's answer}.

    A run is what guard_ledger._repeats folds into one decision (the same
    command from the same session, each within ten minutes of the last).
    Its answer is "declined" when the person said no to any ask in it, else
    "approved" when any ran, else "unknown": a person who said yes once and
    no to the retry, or no and then yes, has not approved it on every ask,
    and only asks approved without a no are evidence for a higher figure."""
    from datetime import datetime, timedelta

    from ... import guard_ledger
    seen: dict[tuple, tuple[datetime, int]] = {}
    runs: dict[int, list[str]] = {}
    for i, r in enumerate(verdicts):
        head = i
        ts = guard_ledger._ts(r)
        if r.get("command") and ts is not None:
            bucket = "stake" if r.get("decision") in ("ask", "deny") else r.get("decision")
            key = (r["command"], r.get("session"), bucket)
            last = seen.get(key)
            if last is not None and timedelta(0) <= ts - last[0] <= guard_ledger._REPEAT_WINDOW:
                head = last[1]
            seen[key] = (ts, head)
        if r.get("decision") == "ask":
            runs.setdefault(head, []).append(answers.get(r.get("_hash") or "", "unknown"))
    return {head: ("declined" if "declined" in got else
                   "approved" if "approved" in got else "unknown")
            for head, got in runs.items()}


def guard_signal(records: list[dict[str, Any]] | None = None, *,
                 days: float = GUARD_LOOKBACK_DAYS, now: Any = None) -> list[dict[str, Any]]:
    """How the guard's asks were answered, per inference key, most asks first.

    `records` is guard_ledger.read(days, outcomes=True) (read when None).
    Each entry: the key's parts; asks, approved, declined, unknown; and for
    the asks the price threshold caused (rule "threshold", the only asks a
    threshold fact can stop, outside a change freeze), `approvals` and `declines` as [ts, monthly_usd]
    pairs, oldest first; `reverts`, approved creations of this key undone by
    a destroy in the same scope within REVERT_WINDOW_HOURS, with one example.
    A repeat of the same command from the same session within ten minutes is
    one decision asked twice (guard_ledger._repeats) and is counted once:
    declined when any ask in it was (_group_answers)."""
    from datetime import UTC, datetime, timedelta

    from ... import guard_ledger
    if records is None:
        records = guard_ledger.read(days, outcomes=True)
    now = now or datetime.now(UTC)
    answers = guard_ledger.ask_outcomes(records, now=now)
    verdicts = [r for r in records if not guard_ledger.is_outcome(r)]
    repeats = guard_ledger._repeats(verdicts)
    answer_of = _group_answers(verdicts, answers)

    # Destroys that went ahead (or may have): what counts against a creation.
    destroys: list[tuple[datetime, tuple, str, str]] = []
    for r in verdicts:
        if r.get("door") != "one_way":
            continue
        d = r.get("decision")
        if d == "deny" or (d == "ask" and answers.get(r.get("_hash") or "") == "declined"):
            continue
        ts = guard_ledger._ts(r)
        if ts is not None:
            k = guard_key(r)
            destroys.append((ts, (k[2], k[3]), _tool_family(r.get("command")),
                             str(r.get("command") or "")))

    out: dict[tuple, dict[str, Any]] = {}
    for i, r in enumerate(verdicts):
        if r.get("decision") != "ask" or i in repeats:
            continue
        key = guard_key(r)
        e = out.setdefault(key, {
            "action_type": key[0], "door": key[1], "team": key[2], "env": key[3],
            "asks": 0, "approved": 0, "declined": 0, "unknown": 0,
            "approvals": [], "declines": [], "reverts": 0, "revert_example": None})
        answer = answer_of.get(i, "unknown")
        e["asks"] += 1
        e[answer] += 1
        ts = guard_ledger._ts(r)
        usd = _pos(r.get("monthly_usd"))
        # An ask during a change freeze answers the freeze as much as the
        # price, so it is no evidence about the threshold either way.
        if ts is None or r.get("rule") != "threshold" or not usd or r.get("freeze"):
            continue
        if answer == "approved":
            e["approvals"].append([ts.isoformat(timespec="seconds"), usd])
            fam = _tool_family(r.get("command"))
            for dts, scope, dfam, dcmd in destroys:
                if (scope == (key[2], key[3]) and dfam == fam
                        and timedelta(0) < dts - ts <= timedelta(hours=REVERT_WINDOW_HOURS)):
                    e["reverts"] += 1
                    e["revert_example"] = e["revert_example"] or {
                        "created": str(r.get("command") or ""), "destroyed": dcmd,
                        "hours_later": round((dts - ts).total_seconds() / 3600, 1)}
                    break
        elif answer == "declined":
            e["declines"].append([ts.isoformat(timespec="seconds"), usd])
    return sorted(out.values(), key=lambda e: (-e["asks"], e["action_type"], e["door"],
                                               e["team"] or "", e["env"] or ""))
