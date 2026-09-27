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
import subprocess
import sys

import pytest

from finops import license as lic
from finops import margin_guard as mg
from finops.llm_prices import MODEL_PRICES

FLOOR_CASES = ("expected", "high")


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


def test_the_high_case_spends_the_whole_ai_credit():
    # The high case is every cost at its cap. A lighter demand estimate must
    # not be able to hide a credit that is too large for the price.
    for p in _paid(mg.PROPOSED_PLANS):
        if p.hosted:
            assert mg.plan_cogs(p, "high")["llm"] == p.caps.ai_credit_usd_month, p.id


def test_both_price_options_are_computed_and_option_a_is_proposed():
    assert set(mg.PRICE_OPTIONS) == {"A", "B"}
    assert mg.PROPOSED_OPTION in mg.PRICE_OPTIONS
    for option in mg.PRICE_OPTIONS:
        rows = mg.table(mg.ladder(option))
        assert {r["plan"] for r in rows} >= {"pro", "cloud", "growth", "team"}
    assert mg.PROPOSED_PLANS["cloud"] == mg.PRICE_OPTIONS[mg.PROPOSED_OPTION]["cloud"]


# ── AI credit and add-ons ────────────────────────────────────────────────────

@pytest.mark.parametrize("option", sorted(mg.PRICE_OPTIONS))
def test_included_ai_credit_is_at_most_7_percent_of_price(option):
    for p in _paid(mg.ladder(option)):
        assert p.caps.ai_credit_usd_month <= mg.AI_CREDIT_MAX_SHARE * p.monthly_usd, (
            f"{p.id}: ${p.caps.ai_credit_usd_month} credit on ${p.monthly_usd}")


def test_daily_ceiling_cannot_spend_the_month_in_one_day():
    for p in mg.PROPOSED_PLANS.values():
        m = mg.metering_for(p)
        assert m["ai_daily_ceiling_usd"] <= m["ai_credit_usd_month"] * mg.AI_DAILY_SHARE + 0.005


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
                for used in (0, 0.5 * cap, 0.8 * cap, cap, 1.5 * cap, 10 * cap + 1):
                    for route in ("scheduled", "interactive"):
                        action = mg.at_cap(p, meter, used, route=route)
                        assert action in mg.ACTIONS, (p.id, meter, action)
                        assert not any(w in action for w in _BILLING_WORDS), (p.id, meter, action)
    for rule in mg.METER_RULES.values():
        assert rule.at_cap in mg.ACTIONS and rule.at_cap_interactive in mg.ACTIONS
        assert rule.at_cap not in (mg.OK, mg.NOTIFY)


def test_at_cap_thresholds():
    cloud = mg.PROPOSED_PLANS["cloud"]
    cap = cloud.caps.jobs_per_day
    assert mg.at_cap(cloud, "jobs_per_day", 0) == mg.OK
    assert mg.at_cap(cloud, "jobs_per_day", cap * mg.NOTIFY_AT) == mg.NOTIFY
    assert mg.at_cap(cloud, "jobs_per_day", cap) == mg.QUEUE_UNTIL_TOMORROW
    credit = cloud.caps.ai_credit_usd_month
    assert mg.at_cap("cloud", "ai_credit_usd_month", credit) == mg.DEGRADE_TO_CODE_ONLY
    assert mg.at_cap("cloud", "ai_credit_usd_month", credit,
                     route="interactive") == mg.ASK_FOR_OWN_KEY
    assert mg.at_cap("cloud", "accounts", cloud.caps.accounts) == mg.HOLD_NEW_ACCOUNT
    assert mg.at_cap("cloud", "line_items_month",
                     cloud.caps.line_items_month) == mg.KEEP_AGGREGATES_PAUSE_DETAIL
    assert mg.at_cap("cloud", "guarded_agents", 10_000) == mg.LOCAL_GUARD_ONLY
    # Pro hosts nothing: its AI runs on the customer's key, its jobs locally.
    assert mg.at_cap("pro", "ai_credit_usd_month", 0) == mg.ASK_FOR_OWN_KEY
    assert mg.at_cap("pro", "jobs_per_day", 0) == mg.RUN_LOCALLY
    with pytest.raises(KeyError):
        mg.at_cap("cloud", "seats", 1)


def test_metering_surface_is_stable():
    # The hosted product imports these names; renaming one breaks it quietly.
    assert mg.METERS == ("ai_credit_usd_month", "ai_daily_ceiling_usd", "jobs_per_day",
                         "accounts", "line_items_month", "guarded_agents")
    for name in ("at_cap", "metering_for", "METER_RULES", "PROPOSED_PLANS", "plan_cogs",
                 "gross_margin", "table"):
        assert name in mg.__all__ and hasattr(mg, name)
    for rule in mg.METER_RULES.values():
        # Customer copy: no exclamation points, no em dashes.
        assert rule.says and chr(33) not in rule.says and chr(0x2014) not in rule.says


def test_plan_cogs_rejects_an_unknown_case():
    with pytest.raises(ValueError):
        mg.plan_cogs("cloud", "worst")


# ── the founder's command ────────────────────────────────────────────────────

def _cli(*args):
    return subprocess.run(
        [sys.executable, "-c",
         "import sys; from finops.setup_wizard import main; main(sys.argv[1:])", *args],
        capture_output=True, text=True, timeout=60, check=False,
        env={"NABLE_NO_TELEMETRY": "1", "HOME": "/tmp", "PATH": "/usr/bin:/bin",
             "PYTHONPATH": ":".join(p for p in sys.path if p)},
    )


def test_pricing_margins_prints_both_options_and_every_case():
    r = _cli("pricing", "margins")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "Option A" in out and "Option B" in out and "(proposed)" in out
    for name in ("Pro", "Cloud", "Growth", "Team", "Enterprise"):
        assert name in out
    assert "low / expected / high" in out


def test_pricing_margins_json():
    import json
    r = _cli("pricing", "margins", "--option", "A", "--json", "--lines", "cloud")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["floor"] == mg.MARGIN_FLOOR
    assert all(row["clears_floor"] for row in doc["options"]["A"])
    assert set(doc["lines"]["A"]) == set(mg.CASES)


def test_pricing_is_not_in_the_default_help():
    r = _cli("--help")
    assert "pricing" not in r.stdout
    assert "\nother\n" not in r.stdout
