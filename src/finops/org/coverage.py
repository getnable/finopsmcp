# SPDX-License-Identifier: Apache-2.0
"""How much of the spend has an owner, and how sure that is.

The Phase 1 exit is "80% of spend mapped to a confirmed owner", so this is the
number the org model is judged by, and it must never be a false 100%. It reads
the latest month in the local cost history:

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
from pathlib import Path
from typing import Any

from .model import ACCOUNT_KINDS, OrgModel, Subject

_PROVIDER_KIND = {"aws": "aws_account", "gcp": "gcp_project", "azure": "azure_subscription"}
_UNATTRIBUTED = ("", "unattributed", "untagged", "unknown", "none")


@dataclass
class Spend:
    month: str | None = None
    through: str | None = None
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


def read_spend() -> Spend:
    """The latest month of spend in the local history. Never raises; what it
    could not read is listed in `not_read`."""
    out = Spend()
    try:
        path = _db_path()
        if path is not None and not path.exists():
            out.not_read.append(f"cost history: {path} does not exist yet (run `nable scan`)")
            return out
        from sqlalchemy import func, select

        from ..storage.db import attributed_costs, cost_snapshots, get_engine
        with get_engine().connect() as conn:
            latest = conn.execute(select(func.max(cost_snapshots.c.snapshot_date))).scalar()
            latest_attr = conn.execute(
                select(func.max(attributed_costs.c.snapshot_date))).scalar()
            if not latest and not latest_attr:
                out.not_read.append("cost history: no rows in cost_snapshots or attributed_costs")
                return out
            out.through = str(latest or latest_attr)
            out.month = out.through[:7]
            like = f"{out.month}-%"
            if latest:
                rows = conn.execute(
                    select(cost_snapshots.c.provider, cost_snapshots.c.account_id,
                           func.sum(cost_snapshots.c.amount_usd))
                    .where(cost_snapshots.c.snapshot_date.like(like))
                    .group_by(cost_snapshots.c.provider, cost_snapshots.c.account_id)).all()
                for provider, account, usd in rows:
                    out.accounts[(str(provider), str(account))] = float(usd or 0.0)
                out.read.append(f"cost_snapshots {out.month}: {len(rows)} accounts")
            else:
                out.not_read.append("cost_snapshots: no rows")
            rows = conn.execute(
                select(attributed_costs.c.account_id, attributed_costs.c.team,
                       func.sum(attributed_costs.c.amount_usd))
                .where(attributed_costs.c.snapshot_date.like(like))
                .group_by(attributed_costs.c.account_id, attributed_costs.c.team)).all()
            for account, team, usd in rows:
                out.teams.setdefault(str(account), {})[str(team or "")] = float(usd or 0.0)
            if rows:
                out.read.append(f"attributed_costs {out.month}: {len(rows)} account-team rows")
            else:
                out.not_read.append(f"attributed_costs: no rows for {out.month} "
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
                           "month": sp.month, "through": sp.through}
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
        out["summary"] = (f"{out['pct_confirmed']}% of {sp.month} spend "
                          f"(${confirmed:,.0f} of ${total:,.0f}) has a confirmed owner; "
                          f"{out['pct_proposed']}% a proposed one")
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
