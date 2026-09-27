# SPDX-License-Identifier: Apache-2.0
"""The founder's pricing rule, as tests.

Gross margin is protected at 80% on every paid plan, and pricing is flat: never
a percentage of cloud spend or savings. finops/margin_guard.py holds the model;
these fail when a price, a cap, a cost coefficient or a model price moves a
paid plan under the floor, when the included AI credit grows past its share of
price, when an add-on is priced under 5x what it costs, or when a meter could
answer a cap with a bill.
"""
from __future__ import annotations

import dataclasses
import inspect
import math
import subprocess
import sys
from pathlib import Path

import pytest

from finops import license as lic
from finops import margin_guard as mg
from finops.llm_prices import MODEL_PRICES

FLOOR_CASES = ("expected", "high")
DOC = Path(__file__).resolve().parents[1] / "docs" / "PRICING-MODEL.md"


def _paid(plans):
    return [p for p in plans.values() if p.paid]


# ── the 80% floor ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("plan_id", [p.id for p in _paid(mg.PROPOSED_PLANS)])
@pytest.mark.parametrize("billing", mg.BILLINGS)
@pytest.mark.parametrize("case", FLOOR_CASES)
def test_every_proposed_paid_plan_clears_80_percent(plan_id, billing, case):
    gm = mg.gross_margin(plan_id, case, billing)
    assert gm >= mg.MARGIN_FLOOR, (
        f"{plan_id} {billing} {case}: modeled gross margin {gm:.1%} is under "
        f"{mg.MARGIN_FLOOR:.0%}. Tighten a cap (AI credit, jobs, accounts, line items, "
        f"support budget) before touching the price. Lines: "
        f"{ {k: round(v, 2) for k, v in mg.plan_cogs(plan_id, case, billing).items()} }")


# The floor test only fails when COGS rises. These pin the cost lines to hand
# arithmetic, so a coefficient or formula that silently drops a cost (a line
# that goes to zero, a cap that stops being read) fails too.
_GOLDEN_HIGH_MONTHLY = {
    "cloud": {
        "payment": 6.879,           # $129 x (2.9% + 0.7% + 1.5% international) + $0.30
        "support": 4.0,             # 4 min at $1
        "llm": 4.0,                 # the whole $4 credit
        "compute": 1.4256,          # (7 x 30 x 300 + 2M items x 600 s + $4 x 150) s x $0.000022
        "database": 0.7248,         # (2 x 3 months x 0.3 + 5 x 13/12 x 0.05) GB x $0.35
        "object_storage": 0.038,    # (2 x 3 x 0.1 + 10 agents' 0.1M events x 13) GB x $0.02
        "observability": 1.5,
        "email": 0.2,               # 1,000 emails
        "guard_ingest": 0.02,       # 0.1M events x $0.20
        "platform_share": 4.6875,   # $300 / 64 units x weight 1
    },
    "team": {
        "payment": 51.3,            # $1,000 x 5.1% + $0.30
        "support": 40.0,
        "llm": 50.0,
        "compute": 16.269,          # (60 x 30 x 300 + 50 x 12 x 30 x 10 + 20 x 600 + 50 x 150) s
        "database": 14.4229,        # (20 x 6 x 0.3 + 50 x 25/12 x 0.05) GB x $0.35
        "object_storage": 0.74,     # (20 x 6 x 0.1 + 1M events x 25) GB x $0.02
        "observability": 1.5,
        "email": 4.0,
        "guard_ingest": 0.2,
        "platform_share": 18.75,    # $300 / 64 x 4
    },
}
_GOLDEN_HIGH_MONTHLY_TOTAL = {"cloud": 23.475, "team": 197.18}


@pytest.mark.parametrize("plan_id", sorted(_GOLDEN_HIGH_MONTHLY))
def test_high_case_cost_lines_match_hand_arithmetic(plan_id):
    lines = mg.plan_cogs(plan_id, "high", "monthly")
    want = _GOLDEN_HIGH_MONTHLY[plan_id]
    assert set(lines) == set(want)
    for k, v in want.items():
        assert lines[k] == pytest.approx(v, abs=1e-4), (plan_id, k, lines[k])
    assert sum(lines.values()) == pytest.approx(_GOLDEN_HIGH_MONTHLY_TOTAL[plan_id], abs=0.005)


def _doc_rows(heading: str) -> list[str]:
    lines = DOC.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(heading))
    rows, seen_table = [], False
    for ln in lines[start + 1:]:
        if ln.startswith("|"):
            seen_table = True
            if not ln.startswith(("| Plan |", "|---")):
                rows.append(ln.strip())
        elif seen_table:
            break
    return rows


def _table_rows(option: str, only: set[str] | None = None) -> list[str]:
    out = []
    for r in mg.table(mg.ladder(option)):
        if only and r["plan"] not in only:
            continue
        cogs = " / ".join(f"${r[f'cogs_{c}_usd']:,.2f}" for c in mg.CASES)
        gm = " / ".join(f"{r[f'margin_{c}']:.1%}" for c in mg.CASES)
        out.append(f"| {r['name']} | {r['billing']} | ${r['price_month_usd']:,.2f} | {cogs} | {gm} |")
    return out


def test_the_margin_tables_in_the_doc_match_the_model():
    # docs/PRICING-MODEL.md is what the founder reads. Regenerate its tables
    # from mg.table() when this fails.
    assert _doc_rows("Option A (proposed)") == _table_rows("A")
    assert _doc_rows("Option B,") == _table_rows("B", {"cloud", "growth"})


def test_clears_floor_is_judged_on_the_unrounded_margin():
    # A margin of 79.996% prints as 80.0% and is still under the floor.
    cloud = mg.PROPOSED_PLANS["cloud"]
    price = mg.min_monthly_price(cloud, "high", target=mg.MARGIN_FLOOR - 0.00004)
    thin = mg.with_price(cloud, price, annual_usd=12 * price)
    row = next(r for r in mg.table({"cloud": thin}) if r["billing"] == "monthly")
    assert row["margin_high"] == round(mg.MARGIN_FLOOR, 4)
    assert mg.gross_margin(thin, "high") < mg.MARGIN_FLOOR
    assert row["clears_floor"] is False


def test_every_paid_plan_on_sale_today_is_modeled_at_its_live_price():
    # A paid plan in license.PLANS that the guard does not model is a plan
    # nobody has checked. Pro and Team must be modeled at today's price; a
    # "custom" price (Enterprise) is modeled at its proposed floor.
    for plan_id, row in lic.PLANS.items():
        live = row.get("monthly_usd")
        if live is not None and live <= 0:
            continue                              # free and the trial
        assert plan_id in mg.LIVE_PLAN_MODEL, f"{plan_id} is paid and not modeled"
        modeled = mg.PROPOSED_PLANS[mg.LIVE_PLAN_MODEL[plan_id]]
        if live is not None:
            assert modeled.monthly_usd == float(live), plan_id
        for billing in mg.BILLINGS:
            for case in FLOOR_CASES:
                assert mg.gross_margin(modeled, case, billing) >= mg.MARGIN_FLOOR, (
                    plan_id, billing, case)


def test_the_high_case_spends_the_whole_ai_allowance():
    # The high case is every cost at its cap. A lighter demand estimate must
    # not be able to hide a credit that is too large for the price.
    for p in _paid(mg.PROPOSED_PLANS):
        if p.hosted:
            assert mg.plan_cogs(p, "high")["llm"] == p.caps.ai_allowance_usd_month, p.id


def test_both_price_options_are_computed_and_option_a_is_proposed():
    assert set(mg.PRICE_OPTIONS) == {"A", "B"}
    assert mg.PROPOSED_OPTION in mg.PRICE_OPTIONS
    for option in mg.PRICE_OPTIONS:
        rows = mg.table(mg.ladder(option))
        assert {r["plan"] for r in rows} >= {"pro", "cloud", "growth", "team"}
    assert mg.PROPOSED_PLANS["cloud"] == mg.PRICE_OPTIONS[mg.PROPOSED_OPTION]["cloud"]


# ── AI credit and add-ons ────────────────────────────────────────────────────

@pytest.mark.parametrize("option", sorted(mg.PRICE_OPTIONS))
def test_included_ai_allowance_is_at_most_7_percent_of_price(option):
    for p in _paid(mg.ladder(option)):
        assert p.caps.ai_allowance_usd_month <= mg.AI_CREDIT_MAX_SHARE * p.monthly_usd, (
            f"{p.id}: ${p.caps.ai_allowance_usd_month} credit on ${p.monthly_usd}")


def _spend_day(p, used_month, unit_cost):
    """Run units of AI work for one day as fast as the meters allow."""
    today = 0.0
    while True:
        day = mg.at_cap(p, "ai_daily_ceiling_usd", today, next_cost=unit_cost,
                        route="interactive")
        month = mg.at_cap(p, "ai_allowance_usd_month", used_month + today, next_cost=unit_cost,
                          route="interactive")
        if day not in (mg.OK, mg.NOTIFY) or month not in (mg.OK, mg.NOTIFY):
            return today
        today += unit_cost


@pytest.mark.parametrize("unit", ["brief_narrative", "chat_session", "root_cause_session"])
def test_daily_ceiling_cannot_spend_the_month_in_one_day(unit):
    # Behaviour, not a restatement of the formula: a tenant that asks for AI
    # work all day long, as fast as at_cap lets it, never passes the day's
    # ceiling, and needs at least 1 / AI_DAILY_SHARE days to spend the month.
    cost = mg.llm_unit_cost(unit, cached=False)
    for p in _paid(mg.PROPOSED_PLANS):
        caps = mg.metering_for(p)
        credit, ceiling = caps["ai_allowance_usd_month"], caps["ai_daily_ceiling_usd"]
        if credit <= 0:
            continue
        spent, days = 0.0, 0
        while days < 40:
            today = _spend_day(p, spent, cost)
            assert today <= ceiling + 1e-9, (p.id, unit, today, ceiling)
            if today == 0:
                break
            spent += today
            days += 1
        assert spent <= credit + 1e-9, (p.id, unit, spent)
        if spent >= credit * 0.99:
            assert days >= round(1 / mg.AI_DAILY_SHARE), (p.id, unit, days)


@pytest.mark.parametrize("addon_id", sorted(mg.ADDONS))
def test_every_priced_addon_is_at_least_5x_its_modeled_cost(addon_id):
    price, cost = mg.ADDONS[addon_id].monthly_usd, mg.addon_cost(addon_id)
    assert cost > 0, addon_id
    assert price >= mg.ADDON_MIN_MULTIPLE * cost, (
        f"{addon_id}: ${price} is {price / cost:.1f}x its ${cost:.2f} modeled cost")


# ── flat pricing ─────────────────────────────────────────────────────────────

_SPEND_WORDS = ("spend", "saving", "percent", "pct", "share_of", "bill")


def test_a_plan_price_is_a_constant_and_nothing_can_tie_it_to_spend():
    # Structure, not a value check: the price fields are plain numbers, the
    # functions that return a price take only the plan and the billing period,
    # and no field on a plan, its caps or an add-on is named for spend.
    for option in mg.PRICE_OPTIONS:
        for p in mg.ladder(option).values():
            assert type(p.monthly_usd) in (int, float), p.id
            assert p.annual_usd is None or type(p.annual_usd) in (int, float), p.id
            assert mg.plan_price(p, "monthly") == p.monthly_usd
    for cls in (mg.Plan, mg.Caps, mg.Usage, mg.Addon):
        for f in dataclasses.fields(cls):
            assert not any(w in f.name for w in _SPEND_WORDS), f"{cls.__name__}.{f.name}"
    assert list(inspect.signature(mg.plan_price).parameters) == ["plan", "billing"]
    for a in mg.ADDONS.values():
        assert type(a.monthly_usd) in (int, float), a.name


# ── model prices ─────────────────────────────────────────────────────────────

def test_llm_prices_has_every_model_the_guard_assumes():
    missing = sorted({u.model for u in mg.LLM_UNITS.values()} - set(MODEL_PRICES))
    assert not missing, f"finops.llm_prices has no price for {missing}"


def test_unit_costs_come_from_the_price_table(monkeypatch):
    # A price change in llm_prices flows through: double one model's rates and
    # the unit that uses it doubles.
    from finops import llm_prices
    unit = mg.LLM_UNITS["chat_session"]
    before = mg.llm_unit_cost("chat_session")
    p = llm_prices.MODEL_PRICES[unit.model]
    doubled = dataclasses.replace(p, input=2 * p.input, output=2 * p.output,
                                  cache_read=2 * p.cache_read)
    monkeypatch.setitem(llm_prices.MODEL_PRICES, unit.model, doubled)
    assert mg.llm_unit_cost("chat_session") == pytest.approx(2 * before)


# ── runtime meters ───────────────────────────────────────────────────────────

_BILLING_WORDS = ("bill", "charge", "overage", "invoice", "upcharge")


def test_at_cap_never_answers_an_included_meter_with_a_bill():
    for option in mg.PRICE_OPTIONS:
        plans = mg.ladder(option)
        for p in plans.values():
            caps = mg.metering_for(p)
            for meter in mg.METERS:
                cap = caps[meter]
                for used in (0, 0.5 * cap, 0.8 * cap, cap, 1.5 * cap, 10 * cap + 1, math.nan):
                    for route in mg.ROUTES:
                        for key in (False, True):
                            action = mg.at_cap(p, meter, used, route=route, next_cost=1,
                                               has_own_key=key)
                            assert action in mg.ACTIONS, (p.id, meter, action)
                            assert not any(w in action for w in _BILLING_WORDS), (
                                p.id, meter, action)
    for rule in mg.METER_RULES.values():
        assert rule.at_cap in mg.ACTIONS and rule.at_cap_interactive in mg.ACTIONS
        assert rule.at_cap not in (mg.OK, mg.NOTIFY)


def test_at_cap_thresholds():
    cloud = mg.PROPOSED_PLANS["cloud"]
    cap = cloud.caps.jobs_per_day
    assert mg.at_cap(cloud, "jobs_per_day", 0) == mg.OK
    assert mg.at_cap(cloud, "jobs_per_day", cap * mg.NOTIFY_AT) == mg.NOTIFY
    assert mg.at_cap(cloud, "jobs_per_day", cap) == mg.QUEUE_UNTIL_TOMORROW
    credit = cloud.caps.ai_allowance_usd_month
    assert mg.at_cap("cloud", "ai_allowance_usd_month", credit) == mg.DEGRADE_TO_CODE_ONLY
    assert mg.at_cap("cloud", "ai_allowance_usd_month", credit,
                     route="interactive") == mg.ASK_FOR_OWN_KEY
    assert mg.at_cap("cloud", "accounts", cloud.caps.accounts) == mg.HOLD_NEW_ACCOUNT
    assert mg.at_cap("cloud", "line_items_month",
                     cloud.caps.line_items_month) == mg.KEEP_AGGREGATES_PAUSE_DETAIL
    assert mg.at_cap("cloud", "guarded_agents", 10_000) == mg.LOCAL_GUARD_ONLY
    # Pro hosts nothing: its AI runs on the customer's key, its jobs locally.
    assert mg.at_cap("pro", "ai_allowance_usd_month", 0) == mg.ASK_FOR_OWN_KEY
    assert mg.at_cap("pro", "jobs_per_day", 0) == mg.RUN_LOCALLY
    with pytest.raises(KeyError):
        mg.at_cap("cloud", "seats", 1)
    with pytest.raises(ValueError):
        mg.at_cap("cloud", "jobs_per_day", 0, route="nightly")


def test_at_cap_counts_the_next_unit_so_the_last_one_cannot_overshoot():
    # The review's case: Growth's daily ceiling is $1.60, $1.59 is used, and a
    # root cause session costs $0.64. Judged on `used` alone it ran and the day
    # ended at $2.23.
    growth = mg.PROPOSED_PLANS["growth"]
    assert mg.metering_for(growth)["ai_daily_ceiling_usd"] == pytest.approx(1.60)
    for route in ("scheduled", "interactive"):
        action = mg.at_cap(growth, "ai_daily_ceiling_usd", 1.59, next_cost=0.64, route=route)
        assert action not in (mg.OK, mg.NOTIFY), route
    assert mg.at_cap(growth, "ai_daily_ceiling_usd", 1.59, next_cost=0.64,
                     route="interactive") == mg.ASK_FOR_OWN_KEY
    # A unit that lands exactly on the cap runs; the notify band counts it too.
    assert mg.at_cap(growth, "ai_daily_ceiling_usd", 1.0, next_cost=0.60) == mg.NOTIFY
    assert mg.at_cap(growth, "ai_daily_ceiling_usd", 1.0) == mg.OK
    assert mg.at_cap(growth, "ai_daily_ceiling_usd", 1.0, next_cost=0.30) == mg.NOTIFY
    # Counted meters: the 20th job of 20 runs, a 21st does not.
    assert mg.at_cap(growth, "jobs_per_day", 19, next_cost=1) == mg.NOTIFY
    assert mg.at_cap(growth, "jobs_per_day", 20, next_cost=1) == mg.QUEUE_UNTIL_TOMORROW


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, -1.0])
def test_a_broken_meter_reading_is_at_the_cap(bad):
    growth = mg.PROPOSED_PLANS["growth"]
    assert mg.at_cap(growth, "ai_daily_ceiling_usd", bad) == mg.DEGRADE_TO_CODE_ONLY
    assert mg.at_cap(growth, "ai_allowance_usd_month", bad,
                     route="interactive") == mg.ASK_FOR_OWN_KEY
    assert mg.at_cap(growth, "ai_daily_ceiling_usd", 0.0, next_cost=bad) == mg.DEGRADE_TO_CODE_ONLY
    assert mg.at_cap(growth, "jobs_per_day", bad) == mg.QUEUE_UNTIL_TOMORROW


def test_on_demand_scans_cannot_take_the_nightly_runs_slots():
    # Anomaly alerts ride on the nightly run. Fill the day with on-demand deep
    # scans first, as fast as at_cap allows: every account's nightly run must
    # still fit.
    for p in _paid(mg.PROPOSED_PLANS):
        caps = mg.metering_for(p)
        if caps["jobs_per_day"] <= 0:
            assert mg.at_cap(p, "jobs_per_day", 0, route="on_demand") == mg.RUN_LOCALLY
            continue
        on_demand = 0
        while mg.at_cap(p, "jobs_per_day", on_demand, route="on_demand",
                        next_cost=1) in (mg.OK, mg.NOTIFY):
            on_demand += 1
        assert on_demand == caps["jobs_per_day"] - caps["accounts"] > 0, p.id
        assert mg.at_cap(p, "jobs_per_day", on_demand, route="on_demand",
                         next_cost=1) == mg.QUEUE_UNTIL_TOMORROW
        total = on_demand
        for _ in range(int(caps["accounts"])):
            assert mg.at_cap(p, "jobs_per_day", total, route="scheduled",
                             next_cost=1) in (mg.OK, mg.NOTIFY), p.id
            total += 1
        assert total == caps["jobs_per_day"]
    # "interactive" on the jobs meter is on-demand work too.
    cloud = mg.PROPOSED_PLANS["cloud"]
    assert mg.at_cap(cloud, "jobs_per_day", 2, route="interactive") == mg.QUEUE_UNTIL_TOMORROW
    assert mg.at_cap(cloud, "jobs_per_day", 2, route="scheduled") == mg.OK


def test_a_tenant_with_its_own_key_is_never_degraded_at_an_ai_cap():
    # The promise: the customer's own key is always available for every AI
    # feature. With a key on file, an AI cap moves the work onto it.
    for p in _paid(mg.PROPOSED_PLANS):
        caps = mg.metering_for(p)
        for meter in ("ai_allowance_usd_month", "ai_daily_ceiling_usd"):
            for route in mg.ROUTES:
                with_key = mg.at_cap(p, meter, caps[meter], route=route, next_cost=0.5,
                                     has_own_key=True)
                assert with_key == mg.RUN_ON_OWN_KEY, (p.id, meter, route)
                assert with_key != mg.DEGRADE_TO_CODE_ONLY
    growth = mg.PROPOSED_PLANS["growth"]
    credit = growth.caps.ai_allowance_usd_month
    # Without a key the degrade and the ask stay.
    assert mg.at_cap(growth, "ai_allowance_usd_month", credit) == mg.DEGRADE_TO_CODE_ONLY
    assert mg.at_cap(growth, "ai_allowance_usd_month", credit,
                     route="interactive") == mg.ASK_FOR_OWN_KEY
    # Below the cap the included credit is used first, key or not.
    assert mg.at_cap(growth, "ai_allowance_usd_month", 0, has_own_key=True) == mg.OK
    # The key changes nothing on a meter that is not AI.
    assert mg.at_cap(growth, "jobs_per_day", 20, has_own_key=True) == mg.QUEUE_UNTIL_TOMORROW
    # The copy the customer reads at an AI cap says both halves.
    for meter in ("ai_allowance_usd_month", "ai_daily_ceiling_usd"):
        says = mg.METER_RULES[meter].says
        assert "If you have added your own model key, AI work continues on it" in says
        assert "If not," in says


def test_metering_surface_is_stable():
    # The hosted product imports these names; renaming one breaks it quietly.
    assert mg.METERS == ("ai_allowance_usd_month", "ai_daily_ceiling_usd", "jobs_per_day",
                         "accounts", "line_items_month", "guarded_agents")
    assert mg.ROUTES == ("scheduled", "interactive", "on_demand")
    for name in ("at_cap", "metering_for", "METER_RULES", "PROPOSED_PLANS", "plan_cogs",
                 "gross_margin", "table", "ROUTES", "RUN_ON_OWN_KEY"):
        assert name in mg.__all__ and hasattr(mg, name)
    for rule in mg.METER_RULES.values():
        # Customer copy (the hosted product shows it at the cap): no
        # exclamation points, no em dashes.
        assert rule.says and chr(33) not in rule.says and chr(0x2014) not in rule.says


def test_plan_cogs_rejects_an_unknown_case():
    with pytest.raises(ValueError):
        mg.plan_cogs("cloud", "worst")


# ── the founder's command ────────────────────────────────────────────────────

def _cli(home, *args):
    return subprocess.run(
        [sys.executable, "-c",
         "import sys; from finops.setup_wizard import main; main(sys.argv[1:])", *args],
        capture_output=True, text=True, timeout=60, check=False,
        env={"NABLE_NO_TELEMETRY": "1", "HOME": str(home), "PATH": "/usr/bin:/bin",
             "PYTHONPATH": ":".join(p for p in sys.path if p)},
    )


def test_pricing_margins_prints_both_options_and_every_case(tmp_path):
    r = _cli(tmp_path, "pricing", "margins")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "Option A" in out and "Option B" in out and "(proposed)" in out
    for name in ("Pro", "Cloud", "Growth", "Team", "Enterprise"):
        assert name in out
    assert "low / expected / high" in out


def test_pricing_margins_json(tmp_path):
    import json
    r = _cli(tmp_path, "pricing", "margins", "--option", "A", "--json", "--lines", "cloud")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["floor"] == mg.MARGIN_FLOOR
    assert all(row["clears_floor"] for row in doc["options"]["A"])
    assert set(doc["lines"]["A"]) == set(mg.CASES)


def test_pricing_is_not_in_the_default_help(tmp_path):
    r = _cli(tmp_path, "--help")
    assert "pricing" not in r.stdout
    assert "\nother\n" not in r.stdout
