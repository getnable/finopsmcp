"""Exercise nable's multi-provider aggregation with synthetic data across every
cloud + SaaS connector, so we can see how the real fan-out actually reacts:
output shape, ordering, and whether it runs in parallel or serial.

Not a unit test. A behavior probe: each connector is patched to return realistic
cost data after a fixed ~0.30s "billing API" delay, then we call the real tool
functions (no demo canned-responses on these) and time them. If the fan-out is
parallel, N providers finish in ~0.30s; if serial, in ~N*0.30s.
"""
from __future__ import annotations

import asyncio
import os
import time

# Demo mode so require_pro() lets the cross-cloud tools run; the aggregate tools
# below are NOT in the demo canned-response set, so they run the real code path.
os.environ["FINOPS_DEMO_MODE"] = "1"
os.environ["FINOPS_DEMO_FORCE"] = "1"
# Disable the read-through cache so every provider runs our synthetic get_costs
# (otherwise stale disk-cache entries from prior sessions leak in) and so the
# timing measures a true cold fan-out, not cache hits.
os.environ["FINOPS_CACHE_DISABLED"] = "1"

from datetime import date  # noqa: E402

from finops import server as srv  # noqa: E402
from finops.connectors.base import CostEntry, CostSummary  # noqa: E402

# Realistic-ish monthly spend + a couple services per provider.
SYNTH = {
    "aws":           (128_400.0, {"EC2": 61_000, "S3": 22_400, "RDS": 45_000}),
    "azure":         (38_900.0,  {"Virtual Machines": 20_000, "Storage": 8_900, "SQL DB": 10_000}),
    "gcp":           (54_200.0,  {"Compute Engine": 30_000, "BigQuery": 14_200, "GCS": 10_000}),
    "datadog":       (9_800.0,   {"Infrastructure": 6_000, "APM": 3_800}),
    "langfuse":      (450.0,     {"Traces": 450}),
    "snowflake":     (22_300.0,  {"Compute": 18_000, "Storage": 4_300}),
    "github":        (2_100.0,   {"Actions": 1_400, "Seats": 700}),
    "mongodb_atlas": (7_600.0,   {"Clusters": 6_800, "Backup": 800}),
    "vercel":        (1_900.0,   {"Bandwidth": 1_100, "Builds": 800}),
    "cloudflare":    (1_200.0,   {"Workers": 700, "R2": 500}),
    "pagerduty":     (960.0,     {"Seats": 960}),
    "twilio":        (3_400.0,   {"SMS": 2_600, "Voice": 800}),
    "new_relic":     (5_100.0,   {"Telemetry": 5_100}),
    "databricks":    (41_000.0,  {"Jobs Compute": 28_000, "All-Purpose": 13_000}),
}

LATENCY_S = 0.30  # simulated per-provider billing-API round trip


def _make_get_costs(provider: str):
    total, services = SYNTH[provider]

    async def _get_costs(start_date, end_date, granularity="MONTHLY",
                         group_by=None, filters=None) -> CostSummary:
        await asyncio.sleep(LATENCY_S)  # stand in for a real billing API call
        entries = [
            CostEntry(provider=provider, account_id=f"{provider}-acct",
                      account_name=provider, service=s, region="us-east-1", amount=float(a))
            for s, a in services.items()
        ]
        return CostSummary(
            provider=provider, start_date=start_date, end_date=end_date,
            total_usd=float(total), by_service={s: float(a) for s, a in services.items()},
            by_account={f"{provider}-acct": float(total)}, by_region={"us-east-1": float(total)},
            entries=entries,
        )

    return _get_costs


def _patch_all():
    async def _true(self=None):
        return True
    for name, conn in srv._ALL_CONNECTORS.items():
        if name not in SYNTH:
            continue
        conn.is_configured = (lambda: _true())  # type: ignore[assignment]
        conn.get_costs = _make_get_costs(name)   # type: ignore[assignment]


async def _timed(label: str, coro):
    t0 = time.perf_counter()
    result = await coro
    dt = time.perf_counter() - t0
    return label, dt, result


async def main() -> None:
    _patch_all()
    n = len(SYNTH)
    print(f"\n{'='*70}\nnable multi-provider probe: {n} connectors, "
          f"{LATENCY_S*1000:.0f}ms simulated latency each")
    print(f"parallel would finish ~{LATENCY_S:.2f}s, serial ~{n*LATENCY_S:.2f}s\n{'='*70}")

    # 1) list_connected_providers — the health/roster fan-out
    label, dt, res = await _timed("list_connected_providers", srv.list_connected_providers())
    # Find whichever key holds the provider list, so we report the real count.
    lists = {k: v for k, v in res.items() if isinstance(v, list)}
    ncon = max((len(v) for v in lists.values()), default=0)
    print(f"\n[{dt:6.3f}s] list_connected_providers -> keys={list(res.keys())}, "
          f"largest list={ncon}")

    # 2) get_total_spend_all_sources — the grand-total fan-out (the real test)
    label, dt, res = await _timed("get_total_spend_all_sources",
                                  srv.get_total_spend_all_sources())
    print(f"[{dt:6.3f}s] get_total_spend_all_sources")
    if "grand_total_formatted" in res:
        print(f"          grand total : {res['grand_total_formatted']}  "
              f"(cloud {res.get('cloud_total_formatted')} / saas {res.get('saas_total_formatted')})")
        print(f"          cloud/saas  : {res.get('cloud_pct')}% / {res.get('saas_pct')}%")
        print(f"          providers   : {len(res.get('by_provider', {}))}")
        top = res.get("top_services", [])[:5]
        print("          top services: " + ", ".join(
            f"{s['service']} {s['formatted']}" for s in top))
    else:
        print(f"          -> {res}")

    # 3) verdict on parallelism
    verdict = "PARALLEL ✓" if dt < (n * LATENCY_S) / 2 else "SERIAL ✗ (fan-out not concurrent)"
    print(f"\n{'='*70}\nfan-out verdict: {verdict}  "
          f"({dt:.3f}s for {n} providers @ {LATENCY_S:.2f}s each)\n{'='*70}\n")


if __name__ == "__main__":
    asyncio.run(main())
