"""
Langfuse connector — LLM observability cost & usage data.

Required env vars:
  LANGFUSE_PUBLIC_KEY   — from Langfuse project settings
  LANGFUSE_SECRET_KEY   — from Langfuse project settings
  LANGFUSE_HOST         — optional; defaults to https://cloud.langfuse.com
                          Set to your self-hosted URL if applicable.

What this provides:
  • Total LLM spend broken down by model
  • Daily token usage (input / output / total)
  • Trace and observation counts (volume signals)
  • Per-model cost efficiency (cost per 1k tokens)
"""
from __future__ import annotations

import os
from base64 import b64encode
from datetime import date, timedelta
from typing import Any

import httpx

from ..base import BaseConnector, CostEntry, CostSummary


class LangfuseConnector(BaseConnector):
    provider = "langfuse"

    def __init__(self) -> None:
        self._public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
        self._secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")
        host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
        self._base = f"{host}/api/public"

    async def is_configured(self) -> bool:
        return bool(self._public_key and self._secret_key)

    def _auth(self) -> str:
        token = b64encode(f"{self._public_key}:{self._secret_key}".encode()).decode()
        return f"Basic {token}"

    def _headers(self) -> dict:
        return {
            "Authorization": self._auth(),
            "Accept": "application/json",
        }

    async def get_costs(
        self,
        start_date: date,
        end_date: date,
        granularity: str = "MONTHLY",
        group_by: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> CostSummary:
        """
        Pull daily metrics from Langfuse and aggregate by model.
        Returns cost breakdown where Langfuse has model pricing configured.
        """
        params = {
            "fromTimestamp": start_date.isoformat() + "T00:00:00Z",
            "toTimestamp": end_date.isoformat() + "T23:59:59Z",
            "limit": 90,  # up to 90 days
        }

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self._base}/metrics/daily",
                headers=self._headers(),
                params=params,
            )
            r.raise_for_status()
            data = r.json()

        by_model: dict[str, float] = {}
        by_model_tokens: dict[str, dict] = {}
        total = 0.0
        entries: list[CostEntry] = []

        for day_record in data.get("data", []):
            day_date = day_record.get("date", "")
            for usage in day_record.get("usage", []):
                model = usage.get("model") or "unknown-model"
                input_cost  = float(usage.get("inputCost") or 0)
                output_cost = float(usage.get("outputCost") or 0)
                total_cost  = float(usage.get("totalCost") or 0)

                # Use totalCost if present; otherwise sum input + output
                cost = total_cost if total_cost else (input_cost + output_cost)
                total += cost
                by_model[model] = by_model.get(model, 0.0) + cost

                # Accumulate token stats for metadata
                if model not in by_model_tokens:
                    by_model_tokens[model] = {"input": 0, "output": 0, "total": 0}
                by_model_tokens[model]["input"]  += int(usage.get("inputUsage") or 0)
                by_model_tokens[model]["output"] += int(usage.get("outputUsage") or 0)
                by_model_tokens[model]["total"]  += int(usage.get("totalUsage") or 0)

                if cost > 0:
                    entries.append(CostEntry(
                        provider="langfuse",
                        account_id=self._public_key[:8] + "...",
                        account_name="Langfuse",
                        service=model,
                        region="",
                        amount=cost,
                        metadata={
                            "date": day_date,
                            "input_tokens": int(usage.get("inputUsage") or 0),
                            "output_tokens": int(usage.get("outputUsage") or 0),
                        },
                    ))

        return CostSummary(
            provider="langfuse",
            start_date=start_date,
            end_date=end_date,
            total_usd=total,
            by_service=dict(sorted(by_model.items(), key=lambda x: -x[1])),
            by_account={"langfuse": total},
            by_region={},
            entries=entries,
        )

    async def get_costs_as_focus(
        self,
        start_date: date,
        end_date: date,
        granularity: str = "MONTHLY",
    ) -> list:
        """Return Langfuse cost as FOCUS 1.2 records (per-model LLM observability spend)."""
        from ...focus.translators.generic import saas_focus_records

        summary = await self.get_costs(start_date, end_date, granularity=granularity)
        return saas_focus_records(
            summary,
            provider="Langfuse",
            publisher="Langfuse",
            category="AI and Machine Learning",
            start_date=start_date,
            end_date=end_date,
        )

    async def list_accounts(self) -> list[dict[str, str]]:
        """Return the Langfuse project name."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"{self._base}/projects",
                    headers=self._headers(),
                )
                if r.status_code == 200:
                    projects = r.json().get("data", [])
                    return [{"id": p.get("id", ""), "name": p.get("name", "Langfuse")} for p in projects]
        except Exception:
            pass
        return [{"id": "default", "name": "Langfuse"}]

    # ── Extended analytics ────────────────────────────────────────────────────

    async def get_usage_by_model(
        self,
        start_date: date,
        end_date: date,
    ) -> dict:
        """
        Detailed token and cost breakdown by model, including cost-per-1k-token efficiency.
        """
        params = {
            "fromTimestamp": start_date.isoformat() + "T00:00:00Z",
            "toTimestamp": end_date.isoformat() + "T23:59:59Z",
            "limit": 90,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self._base}/metrics/daily",
                headers=self._headers(),
                params=params,
            )
            r.raise_for_status()
            data = r.json()

        aggregated: dict[str, dict] = {}

        for day_record in data.get("data", []):
            for usage in day_record.get("usage", []):
                model = usage.get("model") or "unknown-model"
                if model not in aggregated:
                    aggregated[model] = {
                        "model": model,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                        "input_cost_usd": 0.0,
                        "output_cost_usd": 0.0,
                        "total_cost_usd": 0.0,
                    }
                agg = aggregated[model]
                agg["input_tokens"]    += int(usage.get("inputUsage") or 0)
                agg["output_tokens"]   += int(usage.get("outputUsage") or 0)
                agg["total_tokens"]    += int(usage.get("totalUsage") or 0)
                agg["input_cost_usd"]  += float(usage.get("inputCost") or 0)
                agg["output_cost_usd"] += float(usage.get("outputCost") or 0)
                tc = float(usage.get("totalCost") or 0)
                agg["total_cost_usd"]  += tc if tc else (
                    float(usage.get("inputCost") or 0) + float(usage.get("outputCost") or 0)
                )

        # Add efficiency metric
        results = []
        for agg in sorted(aggregated.values(), key=lambda x: -x["total_cost_usd"]):
            t = agg["total_tokens"]
            c = agg["total_cost_usd"]
            agg["cost_per_1k_tokens"] = round(c / t * 1000, 6) if t > 0 else 0
            results.append(agg)

        grand_total = sum(a["total_cost_usd"] for a in results)
        grand_tokens = sum(a["total_tokens"] for a in results)

        return {
            "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
            "total_cost_usd": round(grand_total, 4),
            "total_tokens": grand_tokens,
            "models": results,
        }

    async def get_trace_volume(
        self,
        start_date: date,
        end_date: date,
    ) -> dict:
        """
        Daily trace and observation counts — useful for understanding usage spikes.
        """
        params = {
            "fromTimestamp": start_date.isoformat() + "T00:00:00Z",
            "toTimestamp": end_date.isoformat() + "T23:59:59Z",
            "limit": 90,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self._base}/metrics/daily",
                headers=self._headers(),
                params=params,
            )
            r.raise_for_status()
            data = r.json()

        daily = []
        total_traces = 0
        total_observations = 0

        for day_record in data.get("data", []):
            traces = int(day_record.get("countTraces") or 0)
            observations = int(day_record.get("countObservations") or 0)
            total_traces += traces
            total_observations += observations
            daily.append({
                "date": day_record.get("date", ""),
                "traces": traces,
                "observations": observations,
            })

        return {
            "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
            "total_traces": total_traces,
            "total_observations": total_observations,
            "daily": sorted(daily, key=lambda x: x["date"]),
        }
