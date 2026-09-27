# SPDX-License-Identifier: Apache-2.0
"""Tags: which keys mean what, and which team values are one team.

Reads the latest month of attributed_costs (team and environment values
with dollars), resource_inventory (every tag key on active resources, with
their monthly cost) and the tag_rules table, and proposes:

  tag_key    which keys carry team, owner, cost_center, environment and
             service. By name first (Team, squad, owner, CostCenter, Env,
             ...), then checked against the values: a team key whose values
             are nearly one per resource reads as an id (0.4, dropped); an
             owner key whose values are mostly people is owner (0.8), mostly
             known team names is team (0.6); an environment key whose values
             are mostly environment words 0.85, else 0.5; a key with no
             telling name whose values are mostly known team names is team
             at 0.5. From the tag_rules table (a person wrote the rule) 0.9.
             A proposal only adds keys to what is known for that meaning.
  tag_alias  near-duplicate team values, one team: the same after lowering
             case and dropping separators (0.85), or after dropping a
             -svc/-service/-team/-squad suffix or a plural s too (0.75).
             Nothing looser: "pay" is not "payments", "search" is not
             "research". The canonical value is a team the org already names
             (an owner or team fact), else the plain lowercase spelling with
             the most spend. When the alias and the canonical value both
             carry large spend of their own (each at least $1,000/mo and a
             quarter of the cluster), they may be two teams: 0.45. Two values
             that are both named teams already are never merged. Environment
             values that are environment words ("production", "staging")
             get an environment alias, 0.75 for prod and dr, 0.7 otherwise.
  team       a team for each cluster of tagged spend that no team fact
             describes yet, 0.6, so confirming it lets that spend count as
             owned.

Dollars: the spend each value, key or cluster carries in the latest month.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from ..model import ENVIRONMENTS, Fact, Subject
from ._common import (
    ENV_COMPOUNDS,
    ENV_TOKENS,
    TEAM_SUFFIXES,
    bend,
    dedupe,
    env_of_name,
    fact,
    key_meaning,
    tag_key_fact,
    team_norm,
)
from .data import UNATTRIBUTED

_PERSON = re.compile(r"^(?:@[\w-]+|[^@\s]+@[^@\s]+\.[^@\s]+)$")
_FIELD = {"team": "team", "service": "service", "env": "environment",
          "environment": "environment", "owner": "owner", "cost_center": "cost_center"}
LARGE_USD = 1000.0


def _plain(v: str) -> bool:
    """A value already in the form a team name is written in."""
    parts = [p for p in re.split(r"[\s._/:-]+", v) if p]
    return v == v.lower() and not (len(parts) > 1 and parts[-1] in TEAM_SUFFIXES)


def _anchors(ctx: Any) -> dict[str, tuple[int, str]]:
    """lowercase team name -> (rank, name) for the teams already named;
    confirmed facts rank first."""
    out: dict[str, tuple[int, str]] = {}
    for f in ctx.view().facts:
        if not f.live:
            continue
        name = f.subject.id if f.fact == "team" else \
            f.value.get("team") if f.fact == "owner" else None
        if not name or _PERSON.match(name):
            continue
        rank = 0 if f.confirmed else 1
        prev = out.get(name.lower())
        if prev is None or rank < prev[0]:
            out[name.lower()] = (rank, name)
    return out


# ── tag keys ──────────────────────────────────────────────────────────────────

def _key_stats(inventory: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for r in inventory:
        for k, v in r["tags"].items():
            s = stats.setdefault(k, {"usd": 0.0, "n": 0, "values": defaultdict(float)})
            s["usd"] += r["usd"]
            s["n"] += 1
            s["values"][v] += r["usd"]
    return stats


def _share(values: dict[str, float], test) -> float:
    total = sum(values.values())
    if total <= 0:
        n = len(values)
        return sum(1 for v in values if test(v)) / n if n else 0.0
    return sum(usd for v, usd in values.items() if test(v)) / total


def _read_key(key: str, s: dict[str, Any], teams: set[str]) -> tuple[str, float] | None:
    values = s["values"]

    def known(v: str) -> bool:
        return team_norm(v) in teams

    def is_env(v: str) -> bool:
        return env_of_name(v, accounts=False) is not None

    meaning = key_meaning(key)
    distinct, n = len(values), s["n"]
    if meaning == "team":
        if n >= 10 and distinct > 0.5 * n:
            return None                   # one value per resource: an id, not a team
        return "team", 0.8
    if meaning == "owner":
        if _share(values, lambda v: bool(_PERSON.match(v))) >= 0.6:
            return "owner", 0.8
        if _share(values, known) >= 0.6:
            return "team", 0.6
        return "owner", 0.5
    if meaning == "environment":
        return "environment", 0.85 if _share(values, is_env) >= 0.6 else 0.5
    if meaning == "cost_center":
        return "cost_center", 0.8
    if meaning == "service":
        return "service", 0.7
    if distinct >= 2 and teams and _share(values, known) >= 0.7:
        return "team", 0.5
    if _share(values, is_env) >= 0.8:
        return "environment", 0.55
    return None


def _tag_keys(ctx: Any, teams: set[str]) -> tuple[list[Fact | None], dict[str, list[str]]]:
    inv = ctx.cost.inventory
    by_meaning: dict[str, list[tuple[str, float]]] = {}
    for key, s in sorted(_key_stats(inv).items()):
        got = _read_key(key, s, teams)
        if got is not None and got[1] >= 0.5:
            by_meaning.setdefault(got[0], []).append((key, got[1]))
    out: list[Fact | None] = []
    keys_for: dict[str, list[str]] = {}
    for meaning, rows in sorted(by_meaning.items()):
        keys = [k for k, _ in rows]
        keys_for[meaning] = keys
        low = {k.lower() for k in keys}
        usd = sum(r["usd"] for r in inv if low & {k.lower() for k in r["tags"]})
        out.append(tag_key_fact(ctx, meaning, keys, "tags:resource_inventory",
                                min(c for _, c in rows), usd))
    return out, keys_for


def _from_tag_rules(ctx: Any) -> list[Fact | None]:
    keys: dict[str, list[str]] = {}
    out: list[Fact | None] = []
    for r in sorted(ctx.cost.tag_rules, key=lambda r: (r.get("priority") or 100,
                                                       str(r.get("tag_key")))):
        field = _FIELD.get(str(r.get("maps_to_field") or "").lower())
        key = str(r.get("tag_key") or "").strip()
        pattern = str(r.get("tag_value_pattern") or "*").strip()
        to = str(r.get("maps_to_value") or "").strip()
        if not field or not key:
            continue
        if pattern == "*":
            if key.lower() not in {k.lower() for k in keys.get(field, [])}:
                keys.setdefault(field, []).append(key)
        elif to and not set("*?[") & set(pattern):
            if field == "environment":
                got = env_of_name(to, accounts=False)
                if to.lower() not in ENVIRONMENTS and got is None:
                    continue
                to = to.lower() if to.lower() in ENVIRONMENTS else got[0]
            out.append(fact("tag_alias", Subject("tag_value", pattern),
                            {"canonical_key": field, "canonical_value": to},
                            "tags:tag_rules", 0.9))
    for field, ks in sorted(keys.items()):
        out.append(tag_key_fact(ctx, field, ks, "tags:tag_rules", 0.9))
    return out


# ── team values ───────────────────────────────────────────────────────────────

def _team_values(ctx: Any, team_keys: list[str]) -> dict[str, tuple[str, float]]:
    """lowercase value -> (spelling with the most spend, dollars)."""
    spend: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for parts in ctx.cost.teams.values():
        for t, usd in parts.items():
            if t.strip().lower() not in UNATTRIBUTED and not _PERSON.match(t.strip()):
                spend[t.strip().lower()][t.strip()] += usd
    seen = set(spend)
    low_keys = {k.lower() for k in team_keys}
    for r in ctx.cost.inventory:
        for k, v in r["tags"].items():
            v = v.strip()
            if k.lower() in low_keys and v.lower() not in seen and \
                    v.lower() not in UNATTRIBUTED and not _PERSON.match(v):
                spend[v.lower()][v] += r["usd"]
    out: dict[str, tuple[str, float]] = {}
    for low, spellings in spend.items():
        best = max(spellings.items(), key=lambda kv: (kv[1], kv[0] == low, kv[0]))[0]
        out[low] = (best, sum(spellings.values()))
    return out


def _team_facts(ctx: Any, team_keys: list[str]) -> list[Fact | None]:
    view = ctx.view()
    anchors = _anchors(ctx)
    values = _team_values(ctx, team_keys)
    clusters: dict[str, list[str]] = defaultdict(list)
    for low in values:
        clusters[team_norm(low)].append(low)
    anchor_norm: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for low, (rank, name) in anchors.items():
        anchor_norm[team_norm(low)].append((rank, name))
    out: list[Fact | None] = []
    for norm, members in sorted(clusters.items()):
        if not norm:
            continue
        total = sum(values[m][1] for m in members)
        named = sorted(anchor_norm.get(norm, []))
        if named:
            canonical = named[0][1]
        else:
            canonical = max((values[m] for m in members),
                            key=lambda v: (_plain(v[0]), v[1], v[0]))[0]
        canon_usd = values.get(canonical.lower(), ("", 0.0))[1]
        for m in sorted(members):
            spelling, usd = values[m]
            if m == canonical.lower() or m in anchors:
                continue                  # the team itself, or a team of its own
            _, via = view.canonical_team(spelling)
            if via is not None:
                continue                  # something already says what it is
            how = bend(spelling, canonical)
            if how not in ("separator", "suffix"):
                continue
            conf = 0.85 if how == "separator" else 0.75
            floor = max(LARGE_USD, 0.25 * total)
            if usd >= floor and canon_usd >= floor:
                conf = 0.45               # both spend a lot on their own: maybe two teams
            out.append(fact("tag_alias", Subject("tag_value", spelling),
                            {"canonical_key": "team", "canonical_value": canonical},
                            "tags:attributed_costs", conf, usd))
        if total > 0 and not _PERSON.match(canonical):
            _, via = view.canonical_team(canonical)
            if via is None:
                out.append(fact("team", Subject("team", canonical), {"name": canonical},
                                "tags:attributed_costs", 0.6, total))
    return out


def _env_aliases(ctx: Any, env_keys: list[str]) -> list[Fact | None]:
    spend: dict[str, tuple[str, float]] = {}

    def add(v: str, usd: float) -> None:
        v = v.strip()
        low = v.lower()
        if not low or low in ENVIRONMENTS or low in UNATTRIBUTED:
            return
        if low not in ENV_TOKENS and low not in ENV_COMPOUNDS:
            return
        prev = spend.get(low)
        spend[low] = (v if prev is None or usd > prev[1] else prev[0],
                      (prev[1] if prev else 0.0) + usd)

    for parts in ctx.cost.envs.values():
        for v, usd in parts.items():
            add(v, usd)
    low_keys = {k.lower() for k in env_keys}
    for r in ctx.cost.inventory:
        for k, v in r["tags"].items():
            if k.lower() in low_keys:
                add(v, r["usd"])
    view = ctx.view()
    out: list[Fact | None] = []
    for low, (spelling, usd) in sorted(spend.items()):
        got = env_of_name(low, accounts=False)
        if got is None or view._alias(spelling, ("environment",)) is not None:
            continue
        conf = 0.75 if got[0] in ("prod", "dr") else 0.7
        out.append(fact("tag_alias", Subject("tag_value", spelling),
                        {"canonical_key": "environment", "canonical_value": got[0]},
                        "tags:attributed_costs", conf, usd))
    return out


def propose(ctx: Any) -> list[Fact]:
    anchors = _anchors(ctx)
    teams = {team_norm(n) for _, n in anchors.values()}
    teams |= {team_norm(t) for parts in ctx.cost.teams.values() for t in parts
              if t.strip().lower() not in UNATTRIBUTED}
    teams.discard("")
    out: list[Fact | None] = list(_from_tag_rules(ctx))
    key_facts, keys_for = _tag_keys(ctx, teams)
    out.extend(key_facts)
    team_keys = keys_for.get("team", []) or [k for k, _ in ctx.view().tag_keys("team")]
    env_keys = keys_for.get("environment", []) or \
        [k for k, _ in ctx.view().tag_keys("environment")]
    out.extend(_team_facts(ctx, team_keys))
    out.extend(_env_aliases(ctx, env_keys))
    return dedupe(out)
