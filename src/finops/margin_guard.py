# SPDX-License-Identifier: Apache-2.0
"""Margin guard: what each paid plan costs nable to serve, and the caps that hold it.

The founder's rule is two sentences. Gross margin is protected at 80% on every
paid plan. Pricing is flat: a plan costs the same whatever the customer's cloud
bill or savings. This module is the arithmetic behind the first sentence and a
structure that makes the second one checkable; tests/test_margin_guard.py fails
the build when either stops being true.

Three things live here, stdlib only, so the hosted product (a private repo) can
import them without the rest of nable:

  * the cost model: COSTS (every coefficient with its source), LLM_UNITS priced
    through finops.llm_prices, the plans (PROPOSED_PLANS, and both PRICE_OPTIONS
    the founder is choosing between), ADDONS, and plan_cogs / gross_margin /
    table();
  * the runtime meters: per plan, the included AI credit, its daily ceiling,
    jobs per day, connected accounts, billing line items and guarded agents
    (metering_for), and what the product does at each cap (METER_RULES,
    at_cap). Nothing past a cap is ever billed on its own: the product runs AI
    work on the customer's own key when one is on file, and otherwise degrades
    to a cheaper path, queues, or asks the customer to choose;
  * the cases. "low" is a light tenant, "expected" a typical one, and "high" is
    every capped driver at its cap in the same month: the whole AI credit spent
    with no cache hits, every job slot used for a full scan, every account and
    line item at the limit, the support budget used, an international card, and
    only about 40 hosted tenants sharing the platform. The 80% floor applies to
    expected and high, on monthly and annual billing.

The numbers are a model, not a bill. The hosted product meters the same
quantities per tenant per day, and the monthly margin report runs plan_cogs on
metered quantities instead of modeled ones, so model and bill are compared with
one function. docs/PRICING-MODEL.md names the assumptions that move the result
most and should be checked against the first real hosted invoices.

Nothing here changes a price, but some of it is customer-facing copy:
METER_RULES[...].says is what the hosted product tells a customer at a cap, so
it must stay true of what at_cap does (the tests hold it to that, and to no
exclamation points or em dashes). Plan.includes is proposal copy for the
founder, not yet shown to anyone.

The prices customers see today are finops.license.PLANS: monthly Pro and Team.
The Pro and Team monthly prices below are read from it. Everything else is a
proposal that needs the founder's approval and a Stripe product before it
appears anywhere: Cloud, Growth and Enterprise, and every annual price,
including Pro's and Team's (license.PLANS carries no annual price today).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

__all__ = [
    "ACTIONS",
    "ADDONS",
    "ADDON_MIN_MULTIPLE",
    "AI_CREDIT_MAX_SHARE",
    "AI_DAILY_SHARE",
    "ASK_FOR_OWN_KEY",
    "BILLINGS",
    "CASES",
    "COSTS",
    "DEGRADE_TO_CODE_ONLY",
    "HOLD_NEW_ACCOUNT",
    "KEEP_AGGREGATES_PAUSE_DETAIL",
    "LIVE_PLAN_MODEL",
    "LLM_UNITS",
    "LOCAL_GUARD_ONLY",
    "MARGIN_FLOOR",
    "METERS",
    "METER_RULES",
    "NOTIFY",
    "NOTIFY_AT",
    "OK",
    "PRICE_OPTIONS",
    "PROPOSED_OPTION",
    "PROPOSED_PLANS",
    "QUEUE_UNTIL_TOMORROW",
    "ROUTES",
    "RUN_LOCALLY",
    "RUN_ON_OWN_KEY",
    "Addon",
    "Caps",
    "LlmUnit",
    "MeterRule",
    "Plan",
    "Range",
    "Usage",
    "addon_cost",
    "at_cap",
    "gross_margin",
    "ladder",
    "llm_unit_cost",
    "metering_for",
    "min_monthly_price",
    "plan_cogs",
    "plan_price",
    "table",
    "with_price",
]

CASES = ("low", "expected", "high")
BILLINGS = ("monthly", "annual")

MARGIN_FLOOR = 0.80          # founder rule: every paid plan, expected and high case
AI_CREDIT_MAX_SHARE = 0.07   # included AI credit is at most 7% of the monthly price
ADDON_MIN_MULTIPLE = 5.0     # a priced add-on is at least 5x its modeled cost at its cap
NOTIFY_AT = 0.80             # admins are told when a meter reaches 80% of its cap
AI_DAILY_SHARE = 0.10        # one day can use at most a tenth of the month's AI credit


@dataclass(frozen=True)
class Range:
    """One quantity in the low, expected and high case."""
    low: float
    expected: float
    high: float

    def at(self, case: str) -> float:
        if case not in CASES:
            raise ValueError(f"case must be one of {CASES}, not {case!r}")
        return getattr(self, case)


# ── Cost coefficients (list prices, USD) ─────────────────────────────────────
# Each line says where the number comes from. "Assumption" means nobody has
# measured it yet: those are the lines to replace with metered numbers first.
COSTS: dict = {
    # Compute: pooled serverless workers, 1 vCPU and 2 GiB, scale to zero.
    # GCP Cloud Run tier-1 list rate, $0.000018 per vCPU-second plus
    # $0.000002 per GiB-second, no free tier credited.
    "worker_second_usd": 0.000018 + 2 * 0.000002,
    # One job is one run on one connected account: the nightly scan, anomaly
    # check and brief inputs, or an on-demand deep scan. Mostly API wait.
    # Assumption (pricing analysis, 2026-09-27). The high case prices every job
    # slot as a full scan.
    "worker_s_per_job": Range(60, 120, 300),
    # One intraday anomaly check on one account: read the latest aggregates and
    # run the detector. Deterministic code, no model. Assumption.
    "worker_s_per_check": Range(1, 3, 10),
    # Parse and aggregate a billing export (Parquet). Assumption.
    "worker_s_per_million_line_items": 600,
    # Async worker time while a model call runs: about 15 s a session at
    # about $0.10 a session. Assumption.
    "worker_s_per_llm_usd": 150,
    # Pooled Postgres storage. Neon list $0.35 per GB-month (Cloud SQL SSD is
    # about $0.17, so this is the conservative one).
    "db_gb_month_usd": 0.35,
    # Resource-level detail kept in Postgres, rows plus indexes, about 300 bytes
    # a line item. Assumption.
    "db_gb_per_million_line_items": 0.30,
    # Daily aggregates per connected account per year of retention. Assumption.
    "db_gb_per_account_year": 0.05,
    # GCS or S3 standard list, $0.02 per GB-month.
    "object_gb_month_usd": 0.02,
    # Parquet snapshots of ingested line items for the detail window. Assumption.
    "object_gb_per_million_line_items": 0.10,
    # Guard audit events kept for the retention window, about 1 KB each.
    "object_gb_per_million_guard_events": 1.0,
    # Guard audit events per guarded agent per month. Assumption.
    "guard_events_per_agent_month": Range(500, 2_000, 10_000),
    # Ingest compute for guard audit events. Assumption.
    "guard_event_per_million_usd": 0.20,
    # Email delivery: Amazon SES $0.10 per 1,000 plus sending overhead.
    "email_per_1000_usd": 0.20,
    # Logs, traces and error tracking allocated per hosted tenant. Assumption.
    "observability_per_tenant_usd": Range(0.25, 0.75, 1.50),
    # Shared platform: pooled database compute, queue, load balancer, secrets,
    # backups, the always-on API. Assumption, to be replaced by the real bill.
    "platform_fixed_usd_month": 300.0,
    # Allocation units the platform is shared over: hosted tenants weighted
    # 1 / 2 / 4 for Cloud / Growth / Team at a 60/30/10 mix. About 300 / 100 /
    # 40 tenants. The high case is the 40-tenant launch.
    "platform_units": Range(480, 160, 64),
    # Card payments: Stripe 2.9% + $0.30, plus Stripe Billing 0.7% on
    # subscriptions; the high case adds 1.5% for international cards.
    "card_pct": Range(0.036, 0.036, 0.051),
    "card_fixed_usd": 0.30,
    # Annual and Enterprise invoices paid by ACH Direct Debit: Stripe 0.8%
    # capped at $5 a payment, plus the Stripe fee on the invoice itself.
    "ach_pct": 0.008,
    "ach_cap_usd": 5.0,
    # Assumption: these invoices are subscription invoices (an annual or
    # monthly Stripe subscription with send_invoice collection), so Stripe
    # Billing's 0.7% of billing volume applies, the same 0.7% card_pct carries.
    # Stripe Invoicing's 0.4% is for one-off invoices outside a subscription;
    # it is not used because nothing here is invoiced that way. Standard
    # pay-as-you-go rates, no negotiated discount.
    "invoicing_pct": 0.007,
    # Support: founder or engineer time, $60 an hour loaded. Assumption.
    "support_usd_per_minute": 1.00,
    # License issuing, the Stripe webhook and the email login for a local plan.
    "license_infra_usd": Range(0.02, 0.05, 0.10),
}


@dataclass(frozen=True)
class LlmUnit:
    """One unit of hosted model work. Token counts are assumptions from the
    pricing analysis; the model's price is read from finops.llm_prices."""
    model: str
    input_tokens: int
    cached_tokens: int        # of input_tokens, read from the prompt cache when caching works
    output_tokens: int
    interactive: bool         # a person is waiting (chat, RCA) rather than a scheduled run


# Cheapest model that holds quality per route: Claude Haiku 4.5 for the brief
# narrative, Claude Sonnet 5 for critique, triage and chat, Claude Opus 5.5 for
# root cause. Prices are read from finops.llm_prices, so a change to that table
# flows through here. Cache writes are not charged: the prefix is small next to
# the reads it serves, and the high case spends the whole credit regardless.
LLM_UNITS: dict[str, LlmUnit] = {
    "brief_narrative": LlmUnit("claude-haiku-4-5", 20_000, 8_000, 1_000, False),
    "finding_critique": LlmUnit("claude-sonnet-5", 3_000, 0, 500, False),
    "anomaly_triage": LlmUnit("claude-sonnet-5", 60_000, 40_000, 3_000, False),
    "chat_session": LlmUnit("claude-sonnet-5", 150_000, 120_000, 4_000, True),
    "root_cause_session": LlmUnit("claude-opus-5-5", 300_000, 200_000, 10_000, True),
}


def llm_unit_cost(unit: str, *, cached: bool = True) -> float:
    """List-price USD for one unit of model work, from finops.llm_prices."""
    from .llm_prices import MODEL_PRICES
    u = LLM_UNITS[unit]
    price = MODEL_PRICES[u.model]
    hit = u.cached_tokens if cached else 0
    return price.cost(input_tokens=u.input_tokens - hit, cache_read_tokens=hit,
                      output_tokens=u.output_tokens)


# ── Plans ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Caps:
    """What a plan includes, and so the most it can cost. Hosted meters are
    enforced at runtime (METER_RULES); support is a modeled budget, not a
    customer meter, and a plan whose tenants run over it for two months in a
    row gets its caps reviewed."""
    accounts: int = 0                   # connected cloud accounts, subscriptions, projects
    jobs_per_day: int = 0               # runs on one account: nightly scan or on-demand deep scan
    checks_per_account_day: int = 0     # intraday anomaly checks, deterministic, never dropped
    line_items_month: int = 0           # billing export line items ingested a month
    ai_credit_usd_month: float = 0.0    # hosted model work on nable's key, at list price
    guarded_agents: int = 0             # agents syncing shared guard policy and audit trail
    retention_months: int = 0           # daily aggregates
    detail_days: int = 0                # resource-level detail
    support_min_month: float = 0.0      # modeled support budget (not a meter)

    @property
    def ai_daily_ceiling_usd(self) -> float:
        return round(self.ai_credit_usd_month * AI_DAILY_SHARE, 2)


@dataclass(frozen=True)
class Usage:
    """A light (low) and a typical (expected) tenant, below the caps. The high
    case is not stored: it is the caps."""
    accounts: tuple[float, float] = (0, 0)
    jobs_per_day: tuple[float, float] = (0, 0)
    line_items_month: tuple[float, float] = (0, 0)
    guarded_agents: tuple[float, float] = (0, 0)
    support_min_month: tuple[float, float] = (0, 0)
    emails_month: Range = Range(0, 0, 0)
    # Units of hosted model work a month (low, expected). Past the credit the
    # customer's own key or a pack pays, so nable's cost is min(demand, credit).
    llm_units_month: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Plan:
    """One plan. The price is a constant per plan: nothing here reads the
    customer's cloud spend or savings, and the test checks that it cannot."""
    id: str
    name: str
    monthly_usd: float
    annual_usd: float | None             # None: no annual option
    runs: str                            # "local", "pooled", "dedicated" or "self_hosted"
    caps: Caps
    usage: Usage
    includes: str                        # what the plan includes, in plain words
    annual_pay: str = "card"             # "card" or "invoice" (ACH)
    monthly_pay: str = "card"
    platform_weight: int = 0             # share of the pooled platform
    dedicated_infra_usd: Range = Range(0, 0, 0)

    @property
    def hosted(self) -> bool:
        return self.runs in ("pooled", "dedicated")

    @property
    def paid(self) -> bool:
        return self.monthly_usd > 0


def _live_monthly(plan_id: str) -> float:
    """Today's price for a plan that exists, read from finops.license.PLANS so
    the two cannot drift."""
    from .license import PLANS
    return float(PLANS[plan_id]["monthly_usd"])


# Annual prices are proposals on every plan, Pro and Team included: they are
# not on sale until license.PLANS and Stripe carry them. Only the monthly Pro
# and Team prices are live.
_PRO = Plan(
    id="pro", name="Pro", monthly_usd=_live_monthly("pro"), annual_usd=250.0,
    runs="local",
    caps=Caps(support_min_month=2.5),
    usage=Usage(support_min_month=(0.25, 1.5)),
    includes="The local tool plus the Pro features on one install. Runs on the "
             "customer's machine; AI features use the customer's own model key; "
             "nothing hosted. Email support.",
)

_TEAM = Plan(
    id="team", name="Team", monthly_usd=_live_monthly("team"), annual_usd=11_000.0,
    runs="pooled", annual_pay="invoice", platform_weight=4,
    caps=Caps(accounts=50, jobs_per_day=60, checks_per_account_day=12,
              line_items_month=20_000_000, ai_credit_usd_month=50.0,
              guarded_agents=100, retention_months=25, detail_days=180,
              support_min_month=40),
    usage=Usage(accounts=(10, 25), jobs_per_day=(10, 30),
                line_items_month=(2_000_000, 8_000_000), guarded_agents=(10, 40),
                support_min_month=(15, 30), emails_month=Range(1_000, 5_000, 20_000),
                llm_units_month={"brief_narrative": (34, 34), "finding_critique": (30, 150),
                                 "anomaly_triage": (10, 40), "chat_session": (20, 240),
                                 "root_cause_session": (2, 10)}),
    includes="Growth plus: 50 cloud accounts, the nightly run on each, anomaly "
             "checks every 2 hours, 10 on-demand deep scans a day, 100 guarded "
             "agents, SCIM, audit export, a shared Slack channel for support and "
             "$50 a month of hosted AI. Unlimited seats.",
)

_ENTERPRISE = Plan(
    id="enterprise", name="Enterprise (dedicated)", monthly_usd=4_000.0,
    annual_usd=44_000.0, runs="dedicated", monthly_pay="invoice", annual_pay="invoice",
    platform_weight=4,
    caps=Caps(accounts=200, jobs_per_day=240, checks_per_account_day=12,
              line_items_month=60_000_000, ai_credit_usd_month=75.0,
              guarded_agents=500, retention_months=37, detail_days=365,
              support_min_month=180),
    usage=Usage(accounts=(30, 80), jobs_per_day=(30, 100),
                line_items_month=(10_000_000, 30_000_000), guarded_agents=(50, 200),
                support_min_month=(60, 120), emails_month=Range(2_000, 10_000, 40_000),
                llm_units_month={"brief_narrative": (34, 34), "finding_critique": (50, 200),
                                 "anomaly_triage": (20, 60), "chat_session": (40, 300),
                                 "root_cause_session": (4, 15)}),
    # A dedicated HA Postgres (2 vCPU, 8 GB, 100 GB SSD, Cloud SQL list), a
    # dedicated worker service with one warm instance, and its own logging and
    # storage. Workers, database and storage for the tenant's jobs are all in
    # here, so the pooled compute and storage lines are not charged again.
    dedicated_infra_usd=Range(250, 320, 420),
    includes="Team on a dedicated database and workers, a DPA, custom retention, "
             "invoicing and an SLA. AI runs on the customer's own key by "
             "default, with $75 a month of included AI.",
)

_ENTERPRISE_SELF_HOSTED = Plan(
    id="enterprise_self_hosted", name="Enterprise (self-hosted)", monthly_usd=2_500.0,
    annual_usd=27_500.0, runs="self_hosted", monthly_pay="invoice", annual_pay="invoice",
    caps=Caps(support_min_month=300),
    usage=Usage(support_min_month=(120, 240)),
    includes="A license to run nable in the customer's own cloud, with support. "
             "The customer's infrastructure and model key; nothing hosted by nable.",
)


def _cloud(monthly: float, annual: float, caps: Caps, jobs_words: str) -> Plan:
    return Plan(
        id="cloud", name="Cloud", monthly_usd=monthly, annual_usd=annual,
        runs="pooled", platform_weight=1, caps=caps,
        usage=Usage(accounts=(1, 3), jobs_per_day=(1, 4),
                    line_items_month=(50_000, 500_000), guarded_agents=(1, 4),
                    support_min_month=(1, min(3, caps.support_min_month)),
                    emails_month=Range(100, 300, 1_000),
                    llm_units_month={"brief_narrative": (0, 30), "finding_critique": (0, 20),
                                     "anomaly_triage": (2, 6)}),
        includes=(f"Hosted: {caps.accounts} cloud accounts, {jobs_words}, daily and "
                  "weekly briefs by email, Slack and Teams, a dashboard, 13 months "
                  f"of history, shared guard policy for {caps.guarded_agents} agents, "
                  f"${caps.ai_credit_usd_month:g} a month of hosted AI (brief narrative, "
                  "critique, anomaly triage); chat on the customer's own key. "
                  "Unlimited seats."),
    )


def _growth(monthly: float, annual: float, caps: Caps, jobs_words: str) -> Plan:
    return Plan(
        id="growth", name="Growth", monthly_usd=monthly, annual_usd=annual,
        runs="pooled", annual_pay="invoice", platform_weight=2, caps=caps,
        usage=Usage(accounts=(3, 8), jobs_per_day=(3, 12),
                    line_items_month=(500_000, 3_000_000), guarded_agents=(3, 12),
                    support_min_month=(5, 12), emails_month=Range(300, 1_500, 5_000),
                    llm_units_month={"brief_narrative": (30, 34), "finding_critique": (10, 50),
                                     "anomaly_triage": (5, 15), "chat_session": (6, 60),
                                     "root_cause_session": (1, 4)}),
        includes=(f"Cloud plus: {caps.accounts} cloud accounts, {jobs_words}, the "
                  "hosted @nable Slack bot and chat remediation, SSO, 25 months of "
                  f"history, {caps.guarded_agents} guarded agents, "
                  f"${caps.ai_credit_usd_month:g} a month of hosted AI. Unlimited seats."),
    )


# Option A keeps $129 / $399 and tightens the included caps until the high case
# clears 80%. Option B is the pricing analyst's caps at $149 / $499. Pro, Team
# and Enterprise are the same in both: Pro and Team prices are today's.
PRICE_OPTIONS: dict[str, dict[str, Plan]] = {
    "A": {
        "cloud": _cloud(129.0, 1_419.0, Caps(
            accounts=5, jobs_per_day=7, line_items_month=2_000_000,
            ai_credit_usd_month=4.0, guarded_agents=10, retention_months=13,
            detail_days=90, support_min_month=4),
            "a nightly scan, brief and anomaly check on each, and 2 on-demand deep "
            "scans a day"),
        "growth": _growth(399.0, 4_389.0, Caps(
            accounts=15, jobs_per_day=20, checks_per_account_day=4,
            line_items_month=6_000_000, ai_credit_usd_month=16.0, guarded_agents=25,
            retention_months=25, detail_days=180, support_min_month=18),
            "the nightly run on each, anomaly checks every 6 hours, and 5 on-demand "
            "deep scans a day"),
    },
    "B": {
        "cloud": _cloud(149.0, 1_639.0, Caps(
            accounts=5, jobs_per_day=24, line_items_month=2_000_000,
            ai_credit_usd_month=6.0, guarded_agents=10, retention_months=13,
            detail_days=90, support_min_month=8),
            "24 jobs a day"),
        "growth": _growth(499.0, 5_489.0, Caps(
            accounts=20, jobs_per_day=96, line_items_month=10_000_000,
            ai_credit_usd_month=24.0, guarded_agents=25, retention_months=25,
            detail_days=180, support_min_month=25),
            "96 jobs a day including intraday anomaly checks"),
    },
}

_FREE = Plan(
    id="free", name="Free", monthly_usd=0.0, annual_usd=None, runs="local",
    caps=Caps(), usage=Usage(),
    includes="The local tool: CLI, MCP server, the guard in every agent, every "
             "connector, scans, anomaly detection and briefs on request.",
)


def ladder(option: str) -> dict[str, Plan]:
    """The whole ladder, Free to Enterprise, under one of PRICE_OPTIONS."""
    o = PRICE_OPTIONS[option]
    return {"free": _FREE, "pro": _PRO, "cloud": o["cloud"], "growth": o["growth"],
            "team": _TEAM, "enterprise": _ENTERPRISE,
            "enterprise_self_hosted": _ENTERPRISE_SELF_HOSTED}


PROPOSED_OPTION = "A"
PROPOSED_PLANS: dict[str, Plan] = ladder(PROPOSED_OPTION)

# Which modeled plan answers for each paid entry in finops.license.PLANS today.
# "enterprise" is "custom" there; its modeled price is the proposed floor.
LIVE_PLAN_MODEL: dict[str, str] = {"pro": "pro", "team": "team", "enterprise": "enterprise"}


# ── The model ─────────────────────────────────────────────────────────────────

def _plan(plan: Plan | str, plans: dict[str, Plan] | None = None) -> Plan:
    if isinstance(plan, Plan):
        return plan
    return (plans or PROPOSED_PLANS)[plan]


def plan_price(plan: Plan | str, billing: str = "monthly") -> float:
    """Revenue a month. A constant per plan and billing period."""
    p = _plan(plan)
    if billing == "monthly":
        return p.monthly_usd
    if billing == "annual":
        return (p.annual_usd if p.annual_usd is not None else 12 * p.monthly_usd) / 12
    raise ValueError(f"billing must be one of {BILLINGS}, not {billing!r}")


def _payment(p: Plan, case: str, billing: str) -> float:
    k = COSTS
    charge = p.monthly_usd if billing == "monthly" else plan_price(p, "annual") * 12
    per_month = 1 if billing == "monthly" else 12
    pay = p.monthly_pay if billing == "monthly" else p.annual_pay
    if pay == "invoice":
        fee = min(charge * k["ach_pct"], k["ach_cap_usd"]) + charge * k["invoicing_pct"]
    else:
        fee = charge * k["card_pct"].at(case) + k["card_fixed_usd"]
    return fee / per_month


def _driver(p: Plan, name: str, case: str) -> float:
    """A capped driver: the cap in the high case, the usage otherwise."""
    if case == "high":
        return float(getattr(p.caps, name))
    low, expected = getattr(p.usage, name)
    return float(low if case == "low" else expected)


def _llm(p: Plan, case: str) -> float:
    credit = p.caps.ai_credit_usd_month
    if case == "high":
        return credit                   # the whole credit, spent
    i = 0 if case == "low" else 1
    demand = math.fsum(n[i] * llm_unit_cost(unit, cached=True)
                 for unit, n in p.usage.llm_units_month.items())
    return min(demand, credit)          # past the credit: the customer's key or a pack


def plan_cogs(plan: Plan | str, case: str, billing: str = "monthly",
              plans: dict[str, Plan] | None = None) -> dict[str, float]:
    """Modeled cost to serve one tenant for one month, by line."""
    p = _plan(plan, plans)
    if case not in CASES:
        raise ValueError(f"case must be one of {CASES}, not {case!r}")
    k = COSTS
    out: dict[str, float] = {}
    if not p.paid:
        return out
    out["payment"] = _payment(p, case, billing)
    out["support"] = _driver(p, "support_min_month", case) * k["support_usd_per_minute"]
    if not p.hosted:
        out["license_infra"] = k["license_infra_usd"].at(case)
        return out

    accounts = _driver(p, "accounts", case)
    jobs = _driver(p, "jobs_per_day", case)
    line_items_m = _driver(p, "line_items_month", case) / 1e6
    agents = _driver(p, "guarded_agents", case)
    detail_months = min(p.caps.detail_days / 30, p.caps.retention_months)
    retention_years = p.caps.retention_months / 12
    guard_events_m = agents * k["guard_events_per_agent_month"].at(case) / 1e6

    out["llm"] = _llm(p, case)
    worker_s = (jobs * 30 * k["worker_s_per_job"].at(case)
                + accounts * p.caps.checks_per_account_day * 30 * k["worker_s_per_check"].at(case)
                + line_items_m * k["worker_s_per_million_line_items"]
                + out["llm"] * k["worker_s_per_llm_usd"])
    db_gb = (line_items_m * detail_months * k["db_gb_per_million_line_items"]
             + accounts * retention_years * k["db_gb_per_account_year"])
    object_gb = (line_items_m * detail_months * k["object_gb_per_million_line_items"]
                 + guard_events_m * p.caps.retention_months
                 * k["object_gb_per_million_guard_events"])
    if p.runs == "dedicated":
        # Its own database and workers carry the tenant's jobs and storage.
        out["dedicated_infra"] = p.dedicated_infra_usd.at(case)
    else:
        out["compute"] = worker_s * k["worker_second_usd"]
        out["database"] = db_gb * k["db_gb_month_usd"]
        out["object_storage"] = object_gb * k["object_gb_month_usd"]
        out["observability"] = k["observability_per_tenant_usd"].at(case)
    out["email"] = p.usage.emails_month.at(case) / 1000 * k["email_per_1000_usd"]
    out["guard_ingest"] = guard_events_m * k["guard_event_per_million_usd"]
    out["platform_share"] = (k["platform_fixed_usd_month"] / k["platform_units"].at(case)
                             * p.platform_weight)
    return out


def gross_margin(plan: Plan | str, case: str, billing: str = "monthly",
                 plans: dict[str, Plan] | None = None) -> float:
    """1 - COGS / revenue for one tenant-month. A free plan has no margin."""
    p = _plan(plan, plans)
    revenue = plan_price(p, billing)
    if revenue <= 0:
        raise ValueError(f"{p.id} is free: it has no gross margin")
    # math.fsum: exact, so a total on a rounding boundary rounds the same way
    # on every Python (3.12's sum() compensates, 3.11's does not).
    return 1 - math.fsum(plan_cogs(p, case, billing).values()) / revenue


def min_monthly_price(plan: Plan | str, case: str, target: float = MARGIN_FLOOR) -> float:
    """Smallest monthly price at which `plan` reaches `target` in `case`, caps unchanged."""
    p = _plan(plan)
    d = plan_cogs(p, case)
    fixed = math.fsum(d.values()) - d["payment"]
    if p.monthly_pay == "invoice":
        # ACH is capped: at these prices it is the $5 cap plus Invoicing.
        return (fixed + COSTS["ach_cap_usd"]) / (1 - target - COSTS["invoicing_pct"])
    return (fixed + COSTS["card_fixed_usd"]) / (1 - target - COSTS["card_pct"].at(case))


# ── Add-ons ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Addon:
    name: str
    monthly_usd: float        # flat; never a share of spend
    what: str


ADDONS: dict[str, Addon] = {
    "fleet_guard_25_agents": Addon("Fleet Guard, 25 more guarded agents", 49.0,
                                   "shared policy, audit trail and fleet enforcement"),
    "extra_10_accounts": Addon("10 more connected accounts", 49.0,
                               "a nightly run and anomaly checks on each of 10 more accounts"),
    "line_items_10m": Addon("10M more billing line items a month", 49.0,
                            "resource-level detail for a larger billing export"),
    "extra_retention_12mo": Addon("12 more months of history", 29.0,
                                  "daily aggregates kept a year longer, up to 100 accounts"),
    "ai_credit_pack": Addon("AI credit pack", 50.0,
                            "$10 of hosted model work at list price; the customer's "
                            "own key is always the zero-markup alternative"),
    "compliance_pack": Addon("Compliance pack", 199.0,
                             "7-year guard ledger retention, SIEM and audit evidence "
                             "export, DPA"),
}


def addon_cost(addon_id: str) -> float:
    """Modeled monthly cost of one add-on at its own cap, high-case coefficients."""
    k = COSTS
    job_s, check_s = k["worker_s_per_job"].high, k["worker_s_per_check"].high
    events = k["guard_events_per_agent_month"].high / 1e6
    if addon_id == "fleet_guard_25_agents":
        ev_m = 25 * events
        storage = ev_m * 25 * k["object_gb_per_million_guard_events"] * k["object_gb_month_usd"]
        return ev_m * k["guard_event_per_million_usd"] + storage + 3.0   # 3 min support
    if addon_id == "extra_10_accounts":
        worker = 10 * 30 * (job_s + 24 * check_s) * k["worker_second_usd"]
        db = 10 * (25 / 12) * k["db_gb_per_account_year"] * k["db_gb_month_usd"]
        return worker + db + 2.0                                           # 2 min support
    if addon_id == "line_items_10m":
        worker = 10 * k["worker_s_per_million_line_items"] * k["worker_second_usd"]
        db = 10 * 6 * k["db_gb_per_million_line_items"] * k["db_gb_month_usd"]
        obj = 10 * 6 * k["object_gb_per_million_line_items"] * k["object_gb_month_usd"]
        return worker + db + obj
    if addon_id == "extra_retention_12mo":
        return 100 * 1 * k["db_gb_per_account_year"] * k["db_gb_month_usd"]
    if addon_id == "ai_credit_pack":
        return 10.0                                                        # list model cost
    if addon_id == "compliance_pack":
        ev_m = 500 * events                                                # 500 agents, 7 years
        storage = ev_m * 84 * k["object_gb_per_million_guard_events"] * k["object_gb_month_usd"]
        return storage + 30.0                                              # 30 min support
    raise KeyError(addon_id)


# ── Runtime meters ────────────────────────────────────────────────────────────
# The hosted product imports these. A meter's `used` is what the tenant has
# consumed so far in the meter's window; at_cap says what the product does with
# the next unit of work. No action bills anything: past a cap the product
# degrades to a cheaper path, queues, or asks the customer to choose (their own
# key, an add-on, or waiting), and says so where the customer will see it.
#
# at_cap is a pure function of one reading. It cannot stop two workers that
# read the same `used` from both going ahead, so the hosted side must reserve
# atomically: check and increment the meter in one transaction (for example
# UPDATE ... SET used = used + :next_cost WHERE used + :next_cost <= :cap
# RETURNING used), run the work only if the reservation succeeded, and settle
# an AI reservation to the priced response.usage afterwards.

OK = "ok"
NOTIFY = "notify_admins"                                   # proceed, and tell admins the burn rate
DEGRADE_TO_CODE_ONLY = "degrade_to_code_only_brief"         # the deterministic brief, rules-only root cause
ASK_FOR_OWN_KEY = "ask_for_customer_model_key"             # or a pack, asked in the thread before running
QUEUE_UNTIL_TOMORROW = "queue_until_tomorrow"              # by priority, for the next day's window
HOLD_NEW_ACCOUNT = "hold_new_account"                      # connected accounts keep running
KEEP_AGGREGATES_PAUSE_DETAIL = "keep_aggregates_pause_resource_detail"
LOCAL_GUARD_ONLY = "local_guard_only"                      # the free local guard, no shared policy sync
RUN_LOCALLY = "run_locally_on_request"                     # not hosted on this plan
RUN_ON_OWN_KEY = "run_on_customer_model_key"               # the customer's key is on file: use it

ACTIONS = frozenset({OK, NOTIFY, DEGRADE_TO_CODE_ONLY, ASK_FOR_OWN_KEY,
                     QUEUE_UNTIL_TOMORROW, HOLD_NEW_ACCOUNT,
                     KEEP_AGGREGATES_PAUSE_DETAIL, LOCAL_GUARD_ONLY, RUN_LOCALLY,
                     RUN_ON_OWN_KEY})

# "scheduled": nable started it (the nightly run, a brief). "interactive": a
# person asked (chat, root cause). "on_demand": a person asked for a deep scan;
# it is interactive work, and on jobs_per_day it may only use the on-demand
# slots, never the nightly run's.
ROUTES = ("scheduled", "interactive", "on_demand")
_AI_METERS = frozenset({"ai_credit_usd_month", "ai_daily_ceiling_usd"})


@dataclass(frozen=True)
class MeterRule:
    unit: str
    window: str
    at_cap: str                      # scheduled work
    at_cap_interactive: str          # a person asked (chat, RCA, a deep scan)
    says: str                        # what the customer is told at the cap: customer copy


METER_RULES: dict[str, MeterRule] = {
    "ai_credit_usd_month": MeterRule(
        "USD of model work at list price, from response.usage priced by finops.llm_prices",
        "calendar month", DEGRADE_TO_CODE_ONLY, ASK_FOR_OWN_KEY,
        "Your included AI credit is used for this month. If you have added your own "
        "model key, AI work continues on it at no markup. If not, scheduled briefs "
        "continue without the narrative, and chat and root cause ask before running "
        "on your own key or a credit pack."),
    "ai_daily_ceiling_usd": MeterRule(
        "USD of model work at list price", "calendar day (UTC)",
        DEGRADE_TO_CODE_ONLY, ASK_FOR_OWN_KEY,
        "Today's share of the AI credit is used, so one day cannot spend the month. "
        "If you have added your own model key, AI work continues on it now. If not, "
        "briefs run without the narrative until tomorrow, and chat asks for your key "
        "or waits until tomorrow."),
    "jobs_per_day": MeterRule(
        "runs on one account (nightly scan or on-demand deep scan)", "calendar day (UTC)",
        QUEUE_UNTIL_TOMORROW, QUEUE_UNTIL_TOMORROW,
        "Today's runs are used. This one is queued for tomorrow. On-demand scans "
        "never use the slots kept for each account's nightly scan and anomaly check."),
    "accounts": MeterRule(
        "connected cloud accounts, subscriptions and projects", "current",
        HOLD_NEW_ACCOUNT, HOLD_NEW_ACCOUNT,
        "Your plan's accounts are all connected. The ones you have keep running; "
        "to add this one, remove another or add 10 more accounts."),
    "line_items_month": MeterRule(
        "billing export line items ingested", "calendar month",
        KEEP_AGGREGATES_PAUSE_DETAIL, KEEP_AGGREGATES_PAUSE_DETAIL,
        "This month's line items are in. Daily totals, anomaly checks and briefs "
        "continue; resource-level detail resumes next month or with the add-on."),
    "guarded_agents": MeterRule(
        "agents syncing shared guard policy and the team audit trail", "current",
        LOCAL_GUARD_ONLY, LOCAL_GUARD_ONLY,
        "This agent keeps the local guard. It joins shared policy and the team "
        "audit trail when a slot frees up or with Fleet Guard."),
}

METERS = tuple(METER_RULES)

_METER_CAP = {
    "ai_credit_usd_month": lambda c: c.ai_credit_usd_month,
    "ai_daily_ceiling_usd": lambda c: c.ai_daily_ceiling_usd,
    "jobs_per_day": lambda c: c.jobs_per_day,
    "accounts": lambda c: c.accounts,
    "line_items_month": lambda c: c.line_items_month,
    "guarded_agents": lambda c: c.guarded_agents,
}


def metering_for(plan: Plan | str, plans: dict[str, Plan] | None = None) -> dict[str, float]:
    """The runtime caps for one plan, keyed by meter name."""
    p = _plan(plan, plans)
    return {m: _METER_CAP[m](p.caps) for m in METERS}


def at_cap(plan: Plan | str, meter: str, used: float, *, route: str = "scheduled",
           next_cost: float = 0.0, has_own_key: bool = False,
           plans: dict[str, Plan] | None = None) -> str:
    """What the hosted product does with the next unit of work on `meter`.

    `used` is the meter's reading in its window before that unit, and
    `next_cost` is the unit's size: its estimated list-price USD on an AI
    meter, 1 for a job, an account or an agent, the export's line items on
    line_items_month. The unit is judged by where it would leave the meter:
    at the cap if the meter is already at its cap or `used + next_cost` would
    pass it, so the last unit cannot overshoot; NOTIFY (the work runs and
    admins are told) if it would reach NOTIFY_AT of the cap; otherwise OK. A
    reading or cost that is not a finite number of at least zero is treated as
    at the cap, so a broken meter fails closed. At the cap the meter's rule
    applies: rule.at_cap for route "scheduled", rule.at_cap_interactive for
    "interactive" and "on_demand".

    jobs_per_day reserves the nightly run: on route "on_demand" (or
    "interactive"), `used` counts only today's on-demand runs and is held to
    jobs_per_day minus the plan's accounts, so on-demand scans can never push
    an account's nightly scan and anomaly check off the day. On "scheduled",
    `used` counts every run today.

    `has_own_key`: the tenant has put its own model key on file. At the cap of
    an AI meter the work then runs on that key (RUN_ON_OWN_KEY), scheduled or
    not, and is never degraded. Without a key, scheduled work degrades and a
    person is asked.

    A meter the plan does not include (cap 0) is work that runs on the
    customer's machine or key. Never a charge.

    This answers for one reading and cannot serialise parallel callers: the
    hosted side must check and increment the meter in one transaction and run
    the work only if that reservation succeeded (see the note above METER_RULES).
    """
    if meter not in METER_RULES:
        raise KeyError(f"unknown meter {meter!r}; meters are {METERS}")
    if route not in ROUTES:
        raise ValueError(f"route must be one of {ROUTES}, not {route!r}")
    rule = METER_RULES[meter]
    p = _plan(plan, plans)
    cap = metering_for(p)[meter]
    ai = meter in _AI_METERS
    if cap <= 0:
        if ai:
            return RUN_ON_OWN_KEY if has_own_key else ASK_FOR_OWN_KEY
        return RUN_LOCALLY
    if meter == "jobs_per_day" and route != "scheduled":
        cap = cap - p.caps.accounts                 # the nightly slots are not on offer

    def _bad(x: float) -> bool:
        try:
            return not math.isfinite(x) or x < 0
        except TypeError:
            return True

    if _bad(used) or _bad(next_cost) or cap <= 0 or used >= cap or used + next_cost > cap:
        if ai and has_own_key:
            return RUN_ON_OWN_KEY
        return rule.at_cap if route == "scheduled" else rule.at_cap_interactive
    if used + next_cost >= NOTIFY_AT * cap:
        return NOTIFY
    return OK


# ── The table the founder reads ───────────────────────────────────────────────

def table(plans: dict[str, Plan] | None = None) -> list[dict]:
    """One row per paid plan and billing period: price, COGS and margin per case."""
    plans = plans or PROPOSED_PLANS
    rows = []
    for p in plans.values():
        if not p.paid:
            continue
        for billing in BILLINGS:
            row: dict = {"plan": p.id, "name": p.name, "billing": billing,
                         "price_month_usd": round(plan_price(p, billing), 2),
                         "ai_credit_usd_month": p.caps.ai_credit_usd_month}
            for case in CASES:
                cogs = math.fsum(plan_cogs(p, case, billing).values())
                row[f"cogs_{case}_usd"] = round(cogs, 2)
                row[f"margin_{case}"] = round(gross_margin(p, case, billing), 4)
            # On the unrounded margin: 79.996% rounds to 0.8000 and is still under.
            row["clears_floor"] = all(gross_margin(p, c, billing) >= MARGIN_FLOOR
                                      for c in ("expected", "high"))
            rows.append(row)
    return rows


def with_price(plan: Plan, monthly_usd: float, annual_usd: float | None = None) -> Plan:
    """A copy of `plan` at another price, for what-if rows. Caps unchanged."""
    return replace(plan, monthly_usd=monthly_usd,
                   annual_usd=annual_usd if annual_usd is not None else plan.annual_usd)
