"""Turn a detected anomaly into something a human can act on.

The alert used to carry a percentage and a z-score and no dollars at all:

    Change: +180% vs 28-day avg     Today: $6,540.00     Z-score: 4.21

A percentage is not a decision. "+180%" on a $12/day service is noise; the same
percentage on a $6,500/day service is a budget event, and the reader has to do
the subtraction themselves to tell which one they are looking at. Every claim we
make is supposed to carry a dollar figure, and this one, the one that wakes people
up, did not.

By default this is pure arithmetic over fields the anomaly already has. No
provider API call, no LLM. That matters: this runs inside the scheduler for every
alerted anomaly, so anything with a per-call cost does not belong on that path.

What it does NOT do by default is claim to know WHY. Naming the resource behind
the spike needs a per-service drill-down (billed Cost Explorer requests, plus
CloudTrail), so the scheduler never runs it. Instead the alert says exactly which
question to ask next. A caller that has been asked for the cause passes
drill_down=True, and enrich() adds the answer from anomaly/root_cause.py: the
usage types and resources behind the delta, and the change that likely or
provably started it.
"""
from __future__ import annotations

from typing import Any


def _f(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def impact(anomaly: dict[str, Any]) -> dict[str, Any]:
    """Dollar impact of an anomaly, plus the next question worth asking.

    `delta_usd` is signed: positive means spending more than baseline. The
    run-rate is what this becomes over 30 days IF it persists, which is the
    honest framing. A one-day spike that self-corrects costs the delta once,
    not the run-rate, so the wording has to carry that conditional.
    """
    current = _f(anomaly.get("current_amount"))
    baseline = _f(anomaly.get("baseline_mean"))
    delta = current - baseline
    service = anomaly.get("service") or "this service"
    provider = (anomaly.get("provider") or "").lower()

    out: dict[str, Any] = {
        "delta_usd": round(delta, 2),
        "monthly_run_rate_usd": round(delta * 30, 2),
        "impact_summary": _summary(delta, service),
        "next_step": _next_step(service, provider),
    }
    return out


def _summary(delta: float, service: str) -> str:
    if delta > 0:
        return (
            f"{service} is running ${delta:,.2f}/day above its baseline. "
            f"If it holds, that is ${delta * 30:,.2f} over the next 30 days."
        )
    if delta < 0:
        return (
            f"{service} is running ${abs(delta):,.2f}/day BELOW its baseline "
            f"(${abs(delta) * 30:,.2f}/30d). Worth checking nothing broke."
        )
    return f"{service} matched its baseline in dollar terms."


def _next_step(service: str, provider: str) -> str:
    """One concrete question, not a menu. The complaint about budget alerts is
    that they tell you a threshold moved and leave you to figure out the rest."""
    scope = f'"{service}"' + (f" on {provider.upper()}" if provider else "")
    step = (
        f"Ask nable: what changed in {scope} over the last 7 days, broken down by "
        f"usage type and resource?"
    )
    if provider == "aws":
        step += (" (get_anomalies with root_cause=True, or `nable why`, names the "
                 "resource and the change behind it.)")
    return step


def root_cause(anomaly: dict[str, Any], *, session: Any = None) -> dict[str, Any] | None:
    """The drill-down for one AWS spike: its day against the week before.
    None for anything else. Billed: see anomaly/drilldown.py."""
    if (anomaly.get("provider") or "").lower() != "aws" or anomaly.get("direction") == "drop":
        return None
    from datetime import date

    from .drilldown import windows_for_day
    from .root_cause import explain

    try:
        day = date.fromisoformat(str(anomaly.get("snapshot_date"))[:10])
    except ValueError:
        return None
    current, baseline = windows_for_day(day)
    return explain(str(anomaly.get("service") or ""), current, baseline, session=session,
                   account_id=linked_account(anomaly))


def linked_account(anomaly: dict[str, Any]) -> str | None:
    """The LINKED_ACCOUNT to filter the drill-down on, or None.

    A snapshot-derived anomaly's account_id is the account the connector's
    credentials are in, not a linked account the spend was billed to. On an
    organization's payer that is the payer itself, and filtering on it hides
    every member account's spend, which is where a spike usually is. So only
    an anomaly that names a linked account explicitly (linked_account_id,
    different from the account it was read in) is filtered; otherwise the
    drill-down reads everything these credentials see: a wider answer beats
    one that hides the spike."""
    linked = str(anomaly.get("linked_account_id") or "")
    if not (linked.isdigit() and len(linked) == 12):
        return None
    return None if linked == str(anomaly.get("account_id") or "") else linked


def enrich(anomaly: dict[str, Any], *, drill_down: bool = False,
           session: Any = None) -> dict[str, Any]:
    """Return the anomaly with impact fields merged in. Never raises: an alert
    that fails to enrich must still be delivered.

    drill_down=True also adds `root_cause` (root_cause() above) for an AWS
    spike. Off by default: it makes billed Cost Explorer requests, and the
    scheduler calls this for every alerted anomaly."""
    try:
        out = {**anomaly, **impact(anomaly)}
    except Exception:  # pragma: no cover - defensive
        return anomaly
    if drill_down:
        try:
            found = root_cause(anomaly, session=session)
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - explain() does not raise
            found = {"error": str(exc)}
        if found is not None:
            out["root_cause"] = found
    return out
