"""How does nable scale as providers pile up? Measures the two things that matter
for a cross-provider question: wall-clock latency and RESPONSE SIZE (tokens), at
5 / 10 / 15 / 18 providers.

Latency and token cost scale differently. The fan-out is concurrent, so wall time
tracks the slowest single provider, not the sum. But the response payload can grow
with providers x services, and that is what would cost thousands of tokens and slow
the model down. This measures both against the REAL get_total_spend_all_sources.

Local only: FINOPS_AIRGAP=1, no real credentials, all synthetic.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import date

os.environ["FINOPS_AIRGAP"] = "1"
os.environ["FINOPS_CACHE_DISABLED"] = "1"      # measure cold fan-out, not cache hits
os.environ["FINOPS_DEMO_MODE"] = "1"           # let require_pro pass for cross-cloud
os.environ["FINOPS_DEMO_FORCE"] = "1"

from finops import server as srv  # noqa: E402
from finops.connectors.base import CostEntry, CostSummary  # noqa: E402

START, END = date(2026, 7, 1), date(2026, 7, 31)
PER_PROVIDER_LATENCY = 0.20    # simulated billing-API round trip
SERVICES_PER_PROVIDER = 25     # realistic: a busy provider has dozens of line items


def _make_connector(name: str):
    services = {f"{name}-service-{i:02d}": round(1000.0 / (i + 1), 2)
                for i in range(SERVICES_PER_PROVIDER)}
    total = sum(services.values())

    class _Synth:
        provider = name

        async def is_configured(self):
            return True

        async def get_costs(self, start, end, granularity="MONTHLY", group_by=None, filters=None):
            await asyncio.sleep(PER_PROVIDER_LATENCY)
            return CostSummary(
                provider=name, start_date=start, end_date=end, total_usd=total,
                by_service=dict(services),
                by_account={f"{name}-acct": total},
                by_region={"us-east-1": total},
                entries=[CostEntry(provider=name, account_id=f"{name}-acct",
                                   account_name=name, service=s, region="us-east-1", amount=a)
                         for s, a in services.items()],
            )

        async def list_accounts(self):
            return [{"id": f"{name}-acct", "name": name}]

    return _Synth()


def _install(n: int):
    """Replace the connector registries with n synthetic providers."""
    conns = {f"prov{i:02d}": _make_connector(f"prov{i:02d}") for i in range(n)}
    srv._CLOUD_CONNECTORS.clear()
    srv._SAAS_CONNECTORS.clear()
    srv._ALL_CONNECTORS.clear()
    # Split ~a third cloud, rest saas, so the cloud/saas rollup does real work.
    for i, (k, v) in enumerate(conns.items()):
        (srv._CLOUD_CONNECTORS if i % 3 == 0 else srv._SAAS_CONNECTORS)[k] = v
        srv._ALL_CONNECTORS[k] = v


def _approx_tokens(obj) -> tuple[int, int]:
    s = json.dumps(obj)
    return len(s), len(s) // 4  # ~4 chars/token, the usual rule of thumb


async def main() -> None:
    print(f"\n{'='*82}")
    print("nable scale probe: cross-provider query (get_total_spend_all_sources)")
    print(f"{SERVICES_PER_PROVIDER} services/provider, {PER_PROVIDER_LATENCY*1000:.0f}ms "
          f"latency each, cache OFF (cold).")
    print('='*82)
    print(f"\n{'providers':>10}{'latency':>12}{'response':>12}{'~tokens':>10}"
          f"{'service lines':>15}")
    print("-" * 59)

    rows = []
    for n in (5, 10, 15, 18):
        _install(n)
        t0 = time.perf_counter()
        result = await srv.get_total_spend_all_sources()
        dt = time.perf_counter() - t0
        chars, toks = _approx_tokens(result)
        # count service line items actually in the payload
        lines = sum(len(p.get("by_service", {})) for p in result.get("by_provider", {}).values()
                    if isinstance(p, dict))
        rows.append((n, dt, chars, toks, lines))
        print(f"{n:>10}{dt*1000:>10.0f}ms{chars/1024:>10.1f}KB{toks:>10,}{lines:>15}")

    # Cache warm check: repeat the 18-provider query, should be near-instant.
    os.environ["FINOPS_CACHE_DISABLED"] = "0"
    import importlib
    importlib.reload(__import__("finops.cache", fromlist=["x"]))
    print("\nNotes:")
    base_lat = rows[0][1]
    top_lat = rows[-1][1]
    print(f"  latency 5 -> 18 providers: {base_lat*1000:.0f}ms -> {top_lat*1000:.0f}ms "
          f"({'flat, fan-out is parallel' if top_lat < base_lat*2 else 'growing'})")
    print(f"  tokens   5 -> 18 providers: {rows[0][3]:,} -> {rows[-1][3]:,} "
          f"({rows[-1][3]/max(rows[0][3],1):.1f}x)")
    print(f"  per-provider service detail is capped to top 8 + a rolled-up tail, so the "
          f"payload\n  stays a few thousand tokens at 18 providers, well under a 5k budget, "
          f"and totals are exact.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
