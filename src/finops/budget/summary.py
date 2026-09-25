"""Month-to-date spend against each cloud budget, cached as a small JSON file.

The guard hook runs before every agent tool call, so it cannot afford the
SQLAlchemy import (~240ms) that reading the budgets table costs. Instead,
whatever already computes budget status (enforcer.check_all_budgets: the
check_budget_status tool, `nable budget refresh`, `nable budget ci-gate`,
the dashboard and the scheduled reports, plus every budget create, delete
and budget.yml sync) writes the figures here, with the time they were
computed, and the guard reads this file: stdlib only, one small read.

The file sits beside the decision ledger in nable's data directory:

    {"version": 1,
     "as_of": "2026-09-25T08:00:00+00:00",   when the figures were computed
     "spend_through": "2026-09-24",          newest cost snapshot in the period
     "budgets": [{"name", "scope_type", "scope_value", "period",
                  "period_start", "period_end", "spent", "limit",
                  "pct_used", "status"}, ...]}

A summary older than max_age_hours() is stale and the guard does not use it:
a budget lens that trusts last week's spend would wave through what it should
stop, and one that trusts last month's would stop what it should not.
"""
from __future__ import annotations

import contextlib
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

SUMMARY_NAME = "budget-summary.json"
SUMMARY_VERSION = 1
# How old the summary may be before the guard stops trusting it. Cost data
# itself lands about a day late, so a summary refreshed daily stays inside
# this with a day to spare.
DEFAULT_MAX_AGE_HOURS = 48.0
_FIELDS = ("name", "scope_type", "scope_value", "period", "period_start", "period_end",
           "spent", "limit", "pct_used", "status")


def summary_path() -> Path:
    """Beside the decision ledger, in nable's data directory (no SQLAlchemy)."""
    from .. import guard_ledger
    return guard_ledger.ledger_path().with_name(SUMMARY_NAME)


def max_age_hours() -> float:
    """FINOPS_GUARD_BUDGET_MAX_AGE_HOURS, else DEFAULT_MAX_AGE_HOURS."""
    raw = os.getenv("FINOPS_GUARD_BUDGET_MAX_AGE_HOURS", "").strip()
    if raw:
        with contextlib.suppress(ValueError):
            return max(float(raw), 0.0)
    return DEFAULT_MAX_AGE_HOURS


def write_summary(results: list[dict[str, Any]], *, spend_through: str | None = None,
                  now: datetime | None = None) -> Path | None:
    """Write check_all_budgets' results for the guard. Never raises: a summary
    that cannot be written is a guard that skips the budget lens and says so,
    never a failed budget check."""
    try:
        path = summary_path()
        doc = {
            "version": SUMMARY_VERSION,
            "as_of": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
            "spend_through": spend_through,
            "budgets": [{k: r.get(k) for k in _FIELDS} for r in results],
        }
        tmp = path.with_name(f".{SUMMARY_NAME}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, default=str)
        os.replace(tmp, path)
        return path
    except Exception:  # noqa: BLE001 - never raises, see the docstring
        return None


def read_summary() -> dict[str, Any] | None:
    """The cached summary, or None when there is none or it cannot be read."""
    try:
        doc = json.loads(summary_path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("budgets"), list):
        return None
    return doc


def _parse_ts(raw: Any) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def freshness(doc: dict[str, Any] | None, *, now: datetime | None = None) -> dict[str, Any]:
    """{"state": "absent" | "stale" | "fresh", "as_of", "age_hours",
    "spend_through", "max_age_hours", "previous_month"} for a summary read by
    read_summary."""
    limit = max_age_hours()
    if doc is None:
        return {"state": "absent", "as_of": None, "age_hours": None,
                "spend_through": None, "max_age_hours": limit, "previous_month": False}
    now = now or datetime.now(UTC)
    ts = _parse_ts(doc.get("as_of"))
    age = round(max((now - ts).total_seconds(), 0.0) / 3600, 1) if ts else None
    # Figures from an earlier month are no figures for this one, whatever
    # their age: month-to-date spend starts again on the 1st. Local dates, as
    # enforcer._period_dates draws the period.
    previous = False
    if ts is not None:
        then, today = ts.astimezone().date(), now.astimezone().date()
        previous = (then.year, then.month) != (today.year, today.month)
    stale = age is None or age > limit or previous
    return {"state": "stale" if stale else "fresh", "as_of": doc.get("as_of"),
            "age_hours": age, "spend_through": doc.get("spend_through"),
            "max_age_hours": limit, "previous_month": previous}


def age_words(hours: float | None) -> str:
    """'40 minutes', '6 hours', '3 days': how old a figure is, for a human."""
    if hours is None:
        return "of unknown age"
    if hours < 1:
        m = max(round(hours * 60), 1)
        return f"{m} minute{'s' if m != 1 else ''}"
    if hours < 48:
        h = round(hours)
        return f"{h} hour{'s' if h != 1 else ''}"
    d = round(hours / 24)
    return f"{d} days"


def current_budgets(doc: dict[str, Any], *, today: date | None = None) -> list[dict[str, Any]]:
    """The summary's budgets whose period includes today (the local date, as
    enforcer._period_dates draws the period)."""
    today = today or datetime.now().astimezone().date()
    out = []
    for b in doc.get("budgets") or []:
        if not isinstance(b, dict):
            continue
        try:
            start = date.fromisoformat(str(b.get("period_start")))
            end = date.fromisoformat(str(b.get("period_end")))
            float(b.get("spent"))
            float(b.get("limit"))
        except (TypeError, ValueError):
            continue
        if start <= today <= end:
            out.append(b)
    return out
