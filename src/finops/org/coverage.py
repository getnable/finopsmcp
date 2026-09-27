# SPDX-License-Identifier: Apache-2.0
"""How much of the spend has an owner, and how sure that is.

The Phase 1 exit is "80% of spend mapped to a confirmed owner", so this is the
number the org model is judged by, and it must never be a false 100%. It reads
the 30 days that end on the last day every provider in the history has
reported (so the first of a month is not "one day of AWS, 100% owned" while
GCP's export lags a day), and names a provider that reported the 30 days
before and nothing since under `not_read`:

  cost_snapshots     spend per account; an account's owner fact covers all of it
  attributed_costs   spend per account and team (from tags); covers the part
                     of an account its owner fact does not, team by team

An account with a confirmed owner counts as confirmed. Otherwise its tagged
spend counts under the tagged team (confirmed when a confirmed team fact
describes that team), and the rest goes to the account's proposed owner, or
to nobody. With no cost history at all the answer is counts of subjects, and
it says the spend was not read: not 0%, not 100%.

SQLAlchemy is imported inside read_spend(), never at module import: the guard
hook imports finops.org.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .model import ACCOUNT_KINDS, OrgModel, Subject

_PROVIDER_KIND = {"aws": "aws_account", "gcp": "gcp_project", "azure": "azure_subscription"}
_UNATTRIBUTED = ("", "unattributed", "untagged", "unknown", "none")


WINDOW_DAYS = 30
# A provider whose latest day is further behind the others than this has
# stopped reporting: it no longer holds the window back, and is named.
_STALE_DAYS = 10


@dataclass
class Spend:
    month: str | None = None       # the month the window ends in
    through: str | None = None     # the window's last day
    start: str | None = None       # and its first
    # (provider, account_id) -> dollars in the month
    accounts: dict[tuple[str, str], float] = field(default_factory=dict)
    # account_id -> team -> dollars in the month (from attributed_costs)
    teams: dict[str, dict[str, float]] = field(default_factory=dict)
    read: list[str] = field(default_factory=list)
    not_read: list[str] = field(default_factory=list)


def _db_path() -> Path | None:
    """The SQLite file get_engine() would open, or None for Postgres."""
    if os.environ.get("DATABASE_URL", "").startswith(("postgres", "postgresql")):
        return None
    raw = os.environ.get("FINOPS_DB_PATH", "")
    if raw:
        return Path(raw).expanduser()
    from ..storage.db import data_dir
    return data_dir() / "finops.db"


def _day(s: Any) -> date:
    return date.fromisoformat(str(s)[:10])


def window(latest: dict[str, str]) -> tuple[str, str, list[str]] | None:
    """(first day, last day, providers that stopped reporting) for providers'
    latest days. The window ends on the earliest latest day among providers
    still reporting (within _STALE_DAYS of the newest), so every one of them
    is in it for all 30 days."""
    if not latest:
        return None
    days = {p: _day(d) for p, d in latest.items()}
    newest = max(days.values())
    live = {p: d for p, d in days.items() if (newest - d).days <= _STALE_DAYS}
    end = min(live.values())
    start = end - timedelta(days=WINDOW_DAYS - 1)
    gone = sorted(p for p, d in days.items() if p not in live)
    return start.isoformat(), end.isoformat(), gone


def read_spend() -> Spend:
    """The 30 days of spend in the local history that every reporting
    provider covers (window()). Never raises; what it could not read is
    listed in `not_read`, a provider that stopped reporting included."""
    out = Spend()
    try:
        path = _db_path()
        if path is not None and not path.exists():
            out.not_read.append(f"cost history: {path} does not exist yet (run `nable scan`)")
            return out
        from sqlalchemy import func, select

        from ..storage.db import attributed_costs, cost_snapshots, get_engine
        snaps, attr = cost_snapshots, attributed_costs
        with get_engine().connect() as conn:
            latest = {str(p): str(d) for p, d in conn.execute(
                select(snaps.c.provider, func.max(snaps.c.snapshot_date))
                .group_by(snaps.c.provider)).all() if d}
            latest_attr = {str(p): str(d) for p, d in conn.execute(
                select(attr.c.provider, func.max(attr.c.snapshot_date))
                .group_by(attr.c.provider)).all() if d}
            got = window(latest or latest_attr)
            if got is None:
                out.not_read.append("cost history: no rows in cost_snapshots or attributed_costs")
                return out
            out.start, out.through, gone = got
            out.month = out.through[:7]
            span = f"{out.start} to {out.through}"
            before = (_day(out.start) - timedelta(days=WINDOW_DAYS)).isoformat()
            for p in gone:
                d = (latest or latest_attr)[p]
                if d >= before:
                    out.not_read.append(f"{p}: no cost rows since {d}, so none in {span} "
                                        "(its export stopped or lags); its spend is not counted")
            if latest:
                rows = conn.execute(
                    select(snaps.c.provider, snaps.c.account_id, func.sum(snaps.c.amount_usd))
                    .where(snaps.c.snapshot_date >= out.start,
                           snaps.c.snapshot_date <= out.through)
                    .group_by(snaps.c.provider, snaps.c.account_id)).all()
                for provider, account, usd in rows:
                    out.accounts[(str(provider), str(account))] = float(usd or 0.0)
                out.read.append(f"cost_snapshots {span}: {len(rows)} accounts")
            else:
                out.not_read.append("cost_snapshots: no rows")
            rows = conn.execute(
                select(attr.c.account_id, attr.c.team, func.sum(attr.c.amount_usd))
                .where(attr.c.snapshot_date >= out.start, attr.c.snapshot_date <= out.through)
                .group_by(attr.c.account_id, attr.c.team)).all()
            for account, team, usd in rows:
                out.teams.setdefault(str(account), {})[str(team or "")] = float(usd or 0.0)
            if rows:
                out.read.append(f"attributed_costs {span}: {len(rows)} account-team rows")
            else:
                out.not_read.append(f"attributed_costs: no rows for {span} "
                                    "(team-level attribution not run)")
    except Exception as e:  # noqa: BLE001 - reported, never raised
        out.not_read.append(f"cost history: could not be read ({type(e).__name__}: {e})")
    return out


def _owner(model: OrgModel, provider: str, account: str):
    kinds = [_PROVIDER_KIND[provider]] if provider in _PROVIDER_KIND else list(ACCOUNT_KINDS)
    for kind in kinds:
        try:
            r = model.owner_of(Subject(kind, account))
        except Exception:  # noqa: BLE001 - an odd id is just not owned
            r = None
        if r is not None and r.team:
            return kind, r
    return kinds[0], None


def _team_status(model: OrgModel, team: str) -> tuple[str, bool]:
    name, via = model.canonical_team(team)
    return name, bool(via is not None and via.confirmed)


def _pct(part: float, whole: float) -> float | None:
    return round(100.0 * part / whole, 1) if whole > 0 else None


def coverage(model: OrgModel | None = None, *, spend: Spend | None = None) -> dict[str, Any]:
    """{spend_total, spend_confirmed_owner, spend_proposed_owner, pct_confirmed,
    by_team, ...}. The spend fields are None, and `summary` says "not read",
    when there is no cost history to read."""
    if model is None:
        from .store import load
        model = load()
    sp = spend if spend is not None else read_spend()
    by_team: dict[str, dict[str, float]] = {}
    unowned: list[dict[str, Any]] = []
    confirmed = proposed = total = 0.0

    def add(team: str, usd: float, is_confirmed: bool) -> None:
        nonlocal confirmed, proposed
        slot = by_team.setdefault(team, {"confirmed": 0.0, "proposed": 0.0})
        slot["confirmed" if is_confirmed else "proposed"] += usd
        if is_confirmed:
            confirmed += usd
        else:
            proposed += usd

    def tagged(account: str, cap: float | None) -> float:
        """Add the account's tagged spend by team; returns the dollars covered."""
        parts = {t: usd for t, usd in sp.teams.get(account, {}).items()
                 if t.strip().lower() not in _UNATTRIBUTED and usd > 0}
        got = sum(parts.values())
        scale = min(1.0, cap / got) if cap is not None and got > 0 else 1.0
        for team, usd in parts.items():
            name, ok = _team_status(model, team)
            add(name, usd * scale, ok)
        return got * scale

    if sp.accounts:
        for (provider, account), usd in sorted(sp.accounts.items()):
            if usd <= 0:
                continue
            total += usd
            kind, r = _owner(model, provider, account)
            if r is not None and r.confirmed:
                add(r.team or "", usd, True)
                continue
            rest = usd - tagged(account, usd)
            if rest <= 0.005:
                continue
            if r is not None:
                add(r.team or "", rest, False)
            else:
                unowned.append({"subject": f"{kind}:{account}", "provider": provider,
                                "dollars_monthly": round(rest, 2)})
    elif sp.teams:
        for account, parts in sorted(sp.teams.items()):
            for team, usd in parts.items():
                if usd <= 0:
                    continue
                total += usd
                if team.strip().lower() in _UNATTRIBUTED:
                    unowned.append({"subject": f"aws_account:{account}", "provider": "",
                                    "dollars_monthly": round(usd, 2)})
                else:
                    name, ok = _team_status(model, team)
                    add(name, usd, ok)

    unowned.sort(key=lambda u: -u["dollars_monthly"])
    out: dict[str, Any] = {"read": sp.read, "not_read": sp.not_read,
                           "month": sp.month, "through": sp.through,
                           "start": getattr(sp, "start", None)}
    if total > 0:
        out.update({
            "basis": "spend",
            "spend_total": round(total, 2),
            "spend_confirmed_owner": round(confirmed, 2),
            "spend_proposed_owner": round(proposed, 2),
            "spend_unowned": round(max(total - confirmed - proposed, 0.0), 2),
            "pct_confirmed": _pct(confirmed, total),
            "pct_proposed": _pct(proposed, total),
            "by_team": {t: {k: round(v, 2) for k, v in d.items()}
                        for t, d in sorted(by_team.items(), key=lambda kv: -sum(kv[1].values()))},
            "unowned": unowned[:20],
        })
        span = (f"the 30 days to {sp.through}" if getattr(sp, "start", None)
                else f"{sp.month}")
        out["summary"] = (f"{out['pct_confirmed']}% of {span} spend "
                          f"(${confirmed:,.0f} of ${total:,.0f}) has a confirmed owner; "
                          f"{out['pct_proposed']}% a proposed one"
                          + (f" (not counted: {len(sp.not_read)} source(s), see not_read)"
                             if any(": no cost rows since" in n for n in sp.not_read) else ""))
    else:
        why = "; ".join(sp.not_read) or "no spend in the latest month"
        out.update({"basis": "subjects", "spend_total": None, "spend_confirmed_owner": None,
                    "spend_proposed_owner": None, "spend_unowned": None,
                    "pct_confirmed": None, "pct_proposed": None, "by_team": {},
                    "unowned": []})
        out["summary"] = f"spend coverage: not read ({why})"
    out["subjects"] = _subject_counts(model)
    return out


def _subject_counts(model: OrgModel) -> dict[str, int]:
    """Owned subjects among every subject the model mentions that can have an
    owner. The fallback when there is no spend, and context when there is."""
    subjects: set[Subject] = set()
    for f in model.facts:
        if f.live and f.subject.kind not in ("org", "team", "tag_value", "environment"):
            subjects.add(f.subject)
    conf = prop = 0
    for s in subjects:
        r = model.owner_of(s)
        if r is None:
            continue
        if r.confirmed:
            conf += 1
        else:
            prop += 1
    return {"total": len(subjects), "confirmed_owner": conf, "proposed_owner": prop,
            "unowned": len(subjects) - conf - prop}
