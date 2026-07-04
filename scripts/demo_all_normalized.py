"""Inject every provider's NATIVE cost payload, run nable's real parsing +
FOCUS normalization, then summarize and time it.

Goal: see all providers end to end. Each provider is fed data shaped like what
its own API/export actually returns (Datadog charges array, MongoDB invoice line
items, GCP BigQuery rows with a credits[] array, etc.), then run through the
connector's real get_costs + get_costs_as_focus, so what you see is nable's
actual normalization, not a mock of it.

Robust by construction: every provider runs in its own try/except and reports
ok / skipped / error, so one connector can never sink the run. Output has three
parts: (1) native -> normalized per provider, (2) cross-provider summaries,
(3) timing + a complexity note.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import date
from types import SimpleNamespace

# ── LOCAL ONLY. This never reads your real provider credentials and never calls
#    any provider. Three guarantees, set before finops is imported:
#      1. FINOPS_AIRGAP=1  -> nable makes zero outbound calls (telemetry included).
#      2. Every known provider credential env var is SHADOWED with a dummy, so even
#         a coding mistake cannot pick up a real AWS/Datadog/etc. key.
#      3. Cloud costs are normalized from synthetic raw rows (no connector network
#         call), and SaaS/LLM run through a fake in-memory HTTP transport.
#    Run it on your own machine; nothing here can touch anyone else's account.
os.environ.setdefault("FINOPS_CACHE_DISABLED", "1")
os.environ["FINOPS_AIRGAP"] = "1"

# Shadow every provider credential the connectors look for. Dummy values only.
_SHADOW = [
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
    "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID",
    "GOOGLE_APPLICATION_CREDENTIALS", "GCP_SERVICE_ACCOUNT_KEY_PATH", "GCP_BILLING_ACCOUNT_IDS",
    "DATADOG_API_KEY", "DATADOG_APP_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
    "SNOWFLAKE_PRIVATE_KEY_PATH", "GITHUB_TOKEN", "MONGODB_ATLAS_PUBLIC_KEY",
    "MONGODB_ATLAS_PRIVATE_KEY", "VERCEL_TOKEN", "CLOUDFLARE_API_TOKEN",
    "PAGERDUTY_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "NEW_RELIC_API_KEY",
    "DATABRICKS_HOST", "OPENAI_API_KEY", "OPENAI_ADMIN_KEY", "ANTHROPIC_API_KEY",
]
for _k in _SHADOW:
    os.environ[_k] = "demo-shadowed-not-real"

# Synthetic connector config so the account/org-iterating connectors have something
# to iterate. Set BEFORE finops.server is imported (connectors read env at construction).
os.environ.update({
    "MONGODB_ATLAS_ORG_IDS": "org-1",
    "MONGODB_ATLAS_PUBLIC_KEY": "x", "MONGODB_ATLAS_PRIVATE_KEY": "x",
    "GITHUB_ORGS": "acme",
    "SNOWFLAKE_ACCOUNT": "acme", "SNOWFLAKE_USER": "svc",
    "SNOWFLAKE_PASSWORD": "x", "SNOWFLAKE_CREDIT_PRICE": "3.00",
    "DATABRICKS_ACCOUNT_ID": "acct-1", "DATABRICKS_TOKEN": "x",
    "DATABRICKS_DBU_PRICE": "0.55",
})

import httpx  # noqa: E402

from finops.focus import normalize  # noqa: E402
from finops.focus.translators.llm import llm_result_to_focus  # noqa: E402
from finops import server as srv  # noqa: E402  (import once, up front, so the one-time cost isn't charged to the first provider)

START, END = date(2026, 7, 1), date(2026, 7, 31)


# ── a fake httpx transport: hands each connector an ordered queue of native
#    responses, so we exercise the real parsing without a network call ──────────
class _FakeResp:
    def __init__(self, payload, status=200, text=""):
        self._payload, self.status_code, self.text = payload, status, text

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _FakeClient:
    """Returns queued responses in call order. Extra calls get an empty 200."""

    _queue: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def _next(self, *a, **k):
        if _FakeClient._queue:
            return _FakeClient._queue.pop(0)
        return _FakeResp({}, 200)

    get = _next
    post = _next


def _seed(responses: list):
    _FakeClient._queue = [
        r if isinstance(r, _FakeResp) else _FakeResp(r) for r in responses
    ]


# ── NATIVE payloads, shaped like each provider's real API/export ───────────────

# Cloud raw rows (routed through the bespoke translators via normalize()).
AWS_CUR = {
    "line_item_usage_account_id": "123456789012",
    "bill_billing_period_start_date": "2026-07-01T00:00:00Z",
    "bill_billing_period_end_date": "2026-08-01T00:00:00Z",
    "line_item_usage_start_date": "2026-07-04T00:00:00Z",
    "line_item_line_item_type": "SavingsPlanCoveredUsage",
    "product_servicename": "Amazon EC2", "product_instance_type": "m5.xlarge",
    "product_region": "us-east-1", "line_item_resource_id": "i-0abc123",
    "line_item_unblended_cost": "3.84", "pricing_public_on_demand_cost": "4.61",
    "savingsplan_savings_plan_effective_cost": "2.98",
    "savings_plan_savings_plan_a_r_n": "arn:aws:savingsplans::123:savingsplan/9f3",
    "resource_tags_user_team": "platform",
}
AZURE_EXPORT = {
    "SubscriptionId": "0000-1111", "SubscriptionName": "Production",
    "ServiceName": "Virtual Machines", "MeterSubCategory": "Dv3 Series",
    "ResourceId": "/subscriptions/0000/.../web-01", "ResourceName": "web-01",
    "ResourceLocation": "eastus", "CostInBillingCurrency": "5.12",
    "EffectiveCostInBillingCurrency": "3.90", "UnitPrice": "6.40",
    "BillingPeriodStartDate": "2026-07-01", "UsageDate": "2026-07-04",
    "ChargeType": "Usage", "BenefitName": "Reserved VM Instance",
    "ReservationId": "res-abc", "Tags": {"team": "data"},
}
GCP_BQ = {
    "service": {"description": "Compute Engine"},
    "location": {"region": "us-central1"},
    "project": {"id": "acme-prod", "name": "Acme Prod"},
    "cost": 6.30,
    "credits": [{"name": "Committed use discount: CPU", "amount": -1.85,
                 "type": "COMMITTED_USAGE_DISCOUNT", "id": "cud-n1"}],
    "usage_start_time": "2026-07-04T00:00:00Z", "labels": [{"key": "team", "value": "frontend"}],
}

# SaaS native API JSON (the exact keys each connector parses).
DATADOG = {"data": [{"attributes": {"org_name": "acme", "charges": [
    {"product_name": "infra_hosts", "cost": 6000.0},
    {"product_name": "apm_hosts", "cost": 3800.0}]}}]}
LANGFUSE = {"data": [{"date": "2026-07-04", "usage": [
    {"model": "gpt-4o", "inputCost": 220.0, "outputCost": 180.0, "totalCost": 400.0,
     "inputUsage": 5_000_000, "outputUsage": 1_200_000, "totalUsage": 6_200_000}]}]}
MONGODB = {"results": [{"startDate": "2026-07-01", "endDate": "2026-07-31",
    "amountBilledCents": 760000, "lineItems": [
        {"sku": "ATLAS_AWS_INSTANCE_M40", "totalPriceCents": 680000, "clusterName": "prod-0"},
        {"sku": "ATLAS_AWS_BACKUP", "totalPriceCents": 80000, "clusterName": "prod-0"}]}]}
VERCEL = {"invoices": [{"period": {"start": "2026-07-01", "end": "2026-07-31"}, "items": [
    {"name": "Bandwidth", "price": 1100.0}, {"name": "Build Minutes", "price": 800.0}]}]}
CLOUDFLARE_HISTORY = {"result": [
    {"type": "Workers Paid", "amount": 700.0, "zone": {"name": "acme.com"}},
    {"type": "R2 Storage", "amount": 500.0, "zone": {"name": "acme.com"}}]}
CLOUDFLARE_SUBS = {"result": []}
TWILIO = {"usage_records": [
    {"category": "sms", "price": 2600.0, "usage": "1300000", "usage_unit": "messages"},
    {"category": "calls", "price": 800.0, "usage": "40000", "usage_unit": "minutes"}],
    "next_page_uri": None}
PAGERDUTY = {"total": 42}
GITHUB_ACTIONS = {"total_paid_minutes_used": 14000, "included_minutes": 3000, "total_minutes_used": 17000}
GITHUB_PACKAGES = {"total_paid_bandwidth_gb": 120.0, "total_paid_storage_gb": 60.0}
GITHUB_COPILOT = {"seat_breakdown": {"active_this_cycle": 35}}
NEWRELIC_INGEST = [{"sum.GigabytesIngested": 5100.0}]
NEWRELIC_USERS = [{"latest.UserCount": 12}]
# Databricks account usage export is CSV, not JSON.
DATABRICKS_CSV = (
    "workspace_id,sku_name,cloud,usage_start_time,usage_end_time,usage_quantity,usage_unit\n"
    "ws-1,PREMIUM_ALL_PURPOSE_COMPUTE,AWS,2026-07-04,2026-07-05,13000,DBU\n"
    "ws-1,PREMIUM_JOBS_COMPUTE,AWS,2026-07-04,2026-07-05,28000,DBU\n"
)
# Snowflake ACCOUNT_USAGE rows come back from a cursor as tuples.
SNOWFLAKE_WH_ROWS = [("COMPUTE_WH", 6000.0), ("ETL_WH", 1400.0)]
SNOWFLAKE_STORAGE_ROWS = [(2.5, 0.3, 0.1)]
# LLM connectors emit a normalized {by_model, by_model_tokens} dict (OpenAI shape here).
OPENAI_RESULT = {
    "by_model": {"gpt-4o": 4200.0, "gpt-4o-mini": 380.0},
    "by_model_tokens": {
        "gpt-4o": {"input_tokens": 210_000_000, "output_tokens": 42_000_000},
        "gpt-4o-mini": {"input_tokens": 900_000_000, "output_tokens": 120_000_000},
    },
}
ANTHROPIC_RESULT = {
    "by_model": {"claude-opus-4-8": 6100.0, "claude-haiku-4-5": 240.0},
    "by_model_tokens": {
        "claude-opus-4-8": {"input_tokens": 180_000_000, "output_tokens": 30_000_000},
        "claude-haiku-4-5": {"input_tokens": 500_000_000, "output_tokens": 90_000_000},
    },
}


# ── per-provider drivers. Each returns a list[FocusRecord]. ─────────────────────
async def _drive_cloud(provider: str, raw: dict):
    return [normalize(provider, raw)]


async def _drive_connector(pool_key: str, seed: list, patch_httpx=True):
    """Run a registered connector's real get_costs_as_focus over seeded native data."""
    conn = srv._ALL_CONNECTORS[pool_key]

    async def _true():
        return True
    conn.is_configured = lambda: _true()
    _seed(seed)
    orig = httpx.AsyncClient
    if patch_httpx:
        httpx.AsyncClient = _FakeClient  # type: ignore[misc,assignment]
    try:
        return await conn.get_costs_as_focus(START, END)
    finally:
        httpx.AsyncClient = orig  # type: ignore[misc,assignment]


class _FakeCursor:
    """Returns queued results in call order (fetchall for the warehouse rows,
    fetchone for the single storage row); execute() is a no-op."""
    def __init__(self, results): self._results = list(results)
    def execute(self, *a, **k): return None
    def fetchall(self): return self._results.pop(0) if self._results else []
    def fetchone(self): return self._results.pop(0) if self._results else None
    def close(self): return None


class _FakeSFConn:
    def __init__(self, results): self._results = results
    def cursor(self): return _FakeCursor(self._results)
    def close(self): return None


async def _drive_snowflake():
    conn = srv._ALL_CONNECTORS["snowflake"]

    async def _true(): return True
    conn.is_configured = lambda: _true()
    conn._connect = lambda: _FakeSFConn([SNOWFLAKE_WH_ROWS, SNOWFLAKE_STORAGE_ROWS[0]])
    return await conn.get_costs_as_focus(START, END)


async def _drive_databricks():
    # Databricks account usage export is CSV text, so seed a text response.
    return await _drive_connector("databricks", [_FakeResp({}, text=DATABRICKS_CSV)])


async def _drive_llm(provider_focus_name: str, result: dict):
    """LLM providers converge on one dict; the llm translator maps it to FOCUS."""
    return llm_result_to_focus(result, provider=provider_focus_name,
                               start_date=START, end_date=END)


# provider -> (driver coroutine factory)
PROVIDERS = {
    "aws":      lambda: _drive_cloud("aws", AWS_CUR),
    "azure":    lambda: _drive_cloud("azure", AZURE_EXPORT),
    "gcp":      lambda: _drive_cloud("gcp", GCP_BQ),
    "datadog":  lambda: _drive_connector("datadog", [DATADOG]),
    "langfuse": lambda: _drive_connector("langfuse", [LANGFUSE]),
    "mongodb_atlas": lambda: _drive_connector("mongodb_atlas", [MONGODB]),
    "vercel":   lambda: _drive_connector("vercel", [VERCEL]),
    "cloudflare": lambda: _drive_connector("cloudflare", [CLOUDFLARE_HISTORY, CLOUDFLARE_SUBS]),
    "twilio":   lambda: _drive_connector("twilio", [TWILIO]),
    "pagerduty": lambda: _drive_connector("pagerduty", [PAGERDUTY]),
    "github":   lambda: _drive_connector("github", [GITHUB_ACTIONS, GITHUB_PACKAGES, GITHUB_COPILOT]),
    "new_relic": lambda: _drive_connector("new_relic", [
        {"data": {"actor": {"account": {"nrql": {"results": NEWRELIC_INGEST}}}}},
        {"data": {"actor": {"account": {"nrql": {"results": NEWRELIC_USERS}}}}},
    ]),
    "snowflake": lambda: _drive_snowflake(),
    "databricks": lambda: _drive_databricks(),
    "openai (LLM)": lambda: _drive_llm("OpenAI", OPENAI_RESULT),
    "anthropic (LLM)": lambda: _drive_llm("Anthropic", ANTHROPIC_RESULT),
}


def _fmt_money(v):
    return f"${v:,.2f}"


async def main() -> None:
    os.environ["GITHUB_ORGS"] = "acme"  # so the github connector iterates one org

    results: dict[str, list] = {}
    coverage: list[tuple[str, str, str]] = []
    t_total0 = time.perf_counter()
    timings: dict[str, float] = {}

    for name, factory in PROVIDERS.items():
        t0 = time.perf_counter()
        try:
            recs = await factory()
            timings[name] = time.perf_counter() - t0
            if recs:
                results[name] = recs
                coverage.append((name, "ok", f"{len(recs)} record(s)"))
            else:
                coverage.append((name, "empty", "0 records (parsed, no billable lines)"))
        except Exception as e:
            timings[name] = time.perf_counter() - t0
            coverage.append((name, "error", f"{type(e).__name__}: {e}"))
    t_total = time.perf_counter() - t_total0

    print(f"\n{'='*108}\nLOCAL ONLY: FINOPS_AIRGAP=1 (no outbound calls), all provider "
          f"credentials shadowed, all data synthetic.\n{'='*108}")

    # ── 1) native -> normalized, one row per FOCUS record ──────────────────────
    print(f"\n{'='*108}\n1. NATIVE  ->  NORMALIZED (FOCUS)\n{'='*108}")
    hdr = (f"{'Provider':<14}{'ServiceName':<26}{'Category':<26}"
           f"{'Billed':>12}{'Effective':>12}{'Commit':>14}")
    print(hdr + "\n" + "-" * len(hdr))
    for name, recs in results.items():
        for r in recs:
            print(f"{r.ProviderName:<14}{r.ServiceName[:25]:<26}{r.ServiceCategory[:25]:<26}"
                  f"{_fmt_money(r.BilledCost):>12}{_fmt_money(r.EffectiveCost):>12}"
                  f"{(r.CommitmentDiscountType or '-'):>14}")

    # ── 2) summaries over the unified records ──────────────────────────────────
    all_recs = [r for recs in results.values() for r in recs]
    grand = sum(r.BilledCost for r in all_recs)
    by_provider: dict[str, float] = {}
    by_category: dict[str, float] = {}
    by_commit: dict[str, float] = {}
    for r in all_recs:
        by_provider[r.ProviderName] = by_provider.get(r.ProviderName, 0.0) + r.BilledCost
        by_category[r.ServiceCategory] = by_category.get(r.ServiceCategory, 0.0) + r.BilledCost
        key = r.CommitmentDiscountType or "On-demand / usage"
        by_commit[key] = by_commit.get(key, 0.0) + r.EffectiveCost

    print(f"\n{'='*108}\n2. SUMMARIES ({len(all_recs)} records, {len(results)} providers, "
          f"grand total {_fmt_money(grand)})\n{'='*108}")
    print("\nBy provider:")
    for p, v in sorted(by_provider.items(), key=lambda x: -x[1]):
        print(f"  {p:<16}{_fmt_money(v):>14}   {v/grand*100:5.1f}%")
    print("\nBy FOCUS service category (cross-provider):")
    for c, v in sorted(by_category.items(), key=lambda x: -x[1]):
        print(f"  {c:<28}{_fmt_money(v):>14}   {v/grand*100:5.1f}%")
    print("\nBy commitment (effective cost):")
    for c, v in sorted(by_commit.items(), key=lambda x: -x[1]):
        print(f"  {c:<20}{_fmt_money(v):>14}")

    # ── 3) coverage + timing + complexity ──────────────────────────────────────
    print(f"\n{'='*108}\n3. COVERAGE, TIMING & COMPLEXITY\n{'='*108}")
    for name, status, detail in coverage:
        mark = {"ok": "✓", "empty": "·", "error": "✗"}[status]
        print(f"  {mark} {name:<16}{timings.get(name,0)*1000:7.2f}ms   {detail}")
    ok = sum(1 for _, s, _ in coverage if s == "ok")
    print(f"\n  {ok}/{len(coverage)} providers normalized. total wall {t_total*1000:.1f}ms "
          f"(serial here; live runs fan out concurrently).")
    print("\n  Complexity: normalization is O(L) in total line items across all providers,\n"
          "  one linear pass per connector, no nested scans. Summaries are O(L) dict\n"
          "  aggregation + O(P log P) / O(C log C) sorts on the small provider/category sets.\n")


if __name__ == "__main__":
    asyncio.run(main())
