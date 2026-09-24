"""Attributed cost storage: every dollar lands once, under the right key and date.

Attribution feeds chargeback, so a row that overwrites another is not a display
glitch, it is a team's bill silently shrinking. Real SQLite, and where Cost
Explorer is involved a fake boto3 module stands where the service sits.
"""
from __future__ import annotations

from datetime import date

import pytest

import finops.storage.db as db_mod


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """A brand new SQLite database, with the engine singleton restored after."""
    prev_engine, prev_dir = db_mod._ENGINE, db_mod._DATA_DIR
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "finops.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FINOPS_PROFILE", raising=False)
    db_mod._ENGINE = None
    db_mod._DATA_DIR = None
    yield db_mod
    if db_mod._ENGINE is not None and db_mod._ENGINE is not prev_engine:
        try:
            db_mod._ENGINE.dispose()
        except Exception:
            pass
    db_mod._ENGINE = prev_engine
    db_mod._DATA_DIR = prev_dir


class _FakeCE:
    """Cost Explorer as documented: at most two GroupBy entries per request, and
    a MONTHLY query spanning two months returns one period per month."""

    def __init__(self, monthly: dict[str, dict[str, dict[str, float]]]):
        # monthly[period_start][tag_key][tag_value] = USD on "EC2"
        self.monthly = monthly
        self.requests: list[list[dict]] = []

    def get_cost_and_usage(self, TimePeriod, GroupBy, **kw):
        self.requests.append(GroupBy)
        if len(GroupBy) > 2:
            raise RuntimeError("ValidationException: GroupBy can have at most 2 entries")
        tag_key = GroupBy[1]["Key"] if len(GroupBy) > 1 else None
        periods = []
        for start, by_key in self.monthly.items():
            groups = [
                {"Keys": ["EC2", f"{tag_key}${val}"],
                 "Metrics": {"UnblendedCost": {"Amount": str(amt)}}}
                for val, amt in by_key.get(tag_key, {}).items()
            ]
            periods.append({"TimePeriod": {"Start": start}, "Groups": groups})
        return {"ResultsByTime": periods}


@pytest.fixture
def fake_aws(monkeypatch):
    import sys
    import types

    def install(monthly):
        ce = _FakeCE(monthly)

        class _STS:
            def get_caller_identity(self):
                return {"Account": "111122223333"}

        mod = types.ModuleType("boto3")
        mod.client = lambda name, *a, **kw: ce if name == "ce" else _STS()
        monkeypatch.setitem(sys.modules, "boto3", mod)
        return ce

    return install


_TWO_MONTHS = {
    "2026-08-01": {"team": {"platform": 300.0}, "env": {"prod": 300.0}},
    "2026-09-01": {"team": {"infra": 60.0, "platform-eng": 40.0},
                   "env": {"prod": 100.0}},
}


def test_fetcher_sends_one_tag_key_per_request(fake_aws):
    """SERVICE plus one TAG per rule key was three or more GroupBy entries the
    moment a user had two rule keys, and Cost Explorer rejects that outright."""
    from finops.attribution.fetcher import fetch_aws_tagged_costs

    ce = fake_aws(_TWO_MONTHS)
    rows = fetch_aws_tagged_costs(date(2026, 8, 1), date(2026, 10, 1), ["team", "env"])

    assert ce.requests and all(len(g) <= 2 for g in ce.requests)
    assert {r["tag_key"] for r in rows} == {"team", "env"}
    team_rows = [r for r in rows if r["tag_key"] == "team"]
    assert sorted((r["period_start"], r["tags"]["team"], r["amount_usd"]) for r in team_rows) == [
        ("2026-08-01", "platform", 300.0),
        ("2026-09-01", "infra", 60.0),
        ("2026-09-01", "platform-eng", 40.0),
    ]


def _by_team_env(start: date, end: date) -> dict[tuple[str, str], float]:
    from finops.storage.snapshots import get_costs_by_team

    return {(r["team"], r["environment"]): round(r["total_usd"], 2)
            for r in get_costs_by_team(start, end)}


def test_same_team_two_environments_same_day_are_both_kept(fresh_db):
    """The upsert key left out environment, so teamA/dev replaced teamA/prod."""
    from finops.storage.snapshots import store_attributed_cost

    day = date(2026, 9, 1)
    store_attributed_cost("aws", "EC2", "111", "teamA", "prod", day, 100.0)
    store_attributed_cost("aws", "EC2", "111", "teamA", "dev", day, 40.0)

    assert _by_team_env(day, day) == {("teamA", "prod"): 100.0, ("teamA", "dev"): 40.0}


def test_rewriting_the_same_key_still_replaces_it(fresh_db):
    from finops.storage.snapshots import store_attributed_cost

    day = date(2026, 9, 1)
    store_attributed_cost("aws", "EC2", "111", "teamA", "prod", day, 100.0)
    store_attributed_cost("aws", "EC2", "111", "teamA", "prod", day, 120.0)

    assert _by_team_env(day, day) == {("teamA", "prod"): 120.0}


_RULES = """\
rules:
  - tag_key: "env"
    maps_to_field: "environment"
    priority: 10
  - tag_key: "team"
    maps_to_field: "team"
    priority: 10
team_aliases:
  platform: [infra, platform-eng]
"""


@pytest.fixture
def attribution_env(fresh_db, tmp_path, monkeypatch):
    from finops.attribution import mapper

    rules = tmp_path / "tag_rules.yaml"
    rules.write_text(_RULES)
    monkeypatch.setenv("FINOPS_TAG_RULES", str(rules))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKEFAKEFAKE")
    monkeypatch.delenv("AWS_ROLE_ARNS", raising=False)
    mapper.reload_rules()
    import finops.server as srv
    yield srv
    mapper.reload_rules()


def test_two_rule_keys_attribute_every_dollar_once(attribution_env, fake_aws):
    """With a team rule and an env rule the request carried three GroupBy
    entries and Cost Explorer refused it, so attribution with two rule keys
    never stored anything. Querying each key separately must not then count
    the bill once per key."""
    import asyncio

    fake_aws({"2026-08-01": _TWO_MONTHS["2026-08-01"]})
    out = asyncio.run(attribution_env.run_attribution_now("2026-08-01", "2026-09-01"))

    assert out["errors"] == {}
    assert out["attributed_by_tag_key"] == "team"
    assert _by_team_env(date(2026, 8, 1), date(2026, 8, 31)) == {("platform", ""): 300.0}
