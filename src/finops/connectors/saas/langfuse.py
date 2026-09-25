"""
Langfuse connector — LLM observability cost & usage data.

Required env vars:
  LANGFUSE_PUBLIC_KEY   — from Langfuse project settings
  LANGFUSE_SECRET_KEY   — from Langfuse project settings
  LANGFUSE_HOST         — optional; defaults to https://cloud.langfuse.com
                          Set to your self-hosted URL if applicable.

What this provides:
  • Total LLM spend broken down by model
  • Spend by trace tag, user id and session id (get_cost_attribution)
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


# ── Cost attribution: spend by trace tag, user or session ────────────────────
# The Metrics API sums totalCost grouped by one dimension. The v2 endpoint
# (/api/public/v2/metrics, observations view) groups by `tags` but rejects the
# high-cardinality userId and sessionId as grouping dimensions; the v1 endpoint
# (/api/public/metrics, traces view) groups by all three. v1 is deprecated on
# Langfuse Cloud and absent from Langfuse v4, so tags try v2 first and user
# and session need v1.
_ROW_LIMIT = 1000
_V2_METRICS = "/api/public/v2/metrics"
_V1_METRICS = "/api/public/metrics"
_FIELDS = {"tag": "tags", "user": "userId", "session": "sessionId"}
_UNASSIGNED = {"tag": "(untagged)", "user": "(no user id)", "session": "(no session id)"}
_NOT_AVAILABLE = {
    "project": ("A Langfuse key is scoped to one project, so it cannot split spend across "
                "projects. Tag traces with the product area and use dimension='tag'."),
    "workspace": "Langfuse has no workspace field. Tag traces and use dimension='tag'.",
    "api_key": "Langfuse traces do not record which provider API key served them.",  # pragma: allowlist secret
    "team": ("Langfuse traces have no team field. Put the team in a trace tag and use "
             "dimension='tag'."),
}


class _MetricsUnavailable(RuntimeError):
    """The server has no such metrics endpoint (404/405/410)."""


def _metrics_rows(host: str, auth: str, path: str, view: str, field: str,
                  start_date: date, end_date: date) -> list[dict[str, Any]]:
    import json

    query = {
        "view": view,
        "dimensions": [{"field": field}],
        "metrics": [{"measure": "totalCost", "aggregation": "sum"}],
        "filters": [],
        "fromTimestamp": f"{start_date.isoformat()}T00:00:00Z",
        "toTimestamp": f"{(end_date + timedelta(days=1)).isoformat()}T00:00:00Z",
        "orderBy": [{"field": "sum_totalCost", "direction": "desc"}],
        "config": {"row_limit": _ROW_LIMIT},
    }
    resp = httpx.get(f"{host}{path}", params={"query": json.dumps(query)},
                     headers={"Authorization": auth, "Accept": "application/json"},
                     timeout=30)
    if resp.status_code in (404, 405, 410):
        raise _MetricsUnavailable(f"{path} answered {resp.status_code}")
    resp.raise_for_status()
    data = resp.json() or {}
    return data.get("data", []) if isinstance(data, dict) else []


def _row_cost(row: dict[str, Any]) -> float | None:
    """totalCost of one metrics row, or None when it cannot be read. A null sum
    is Langfuse's own zero (no priced generations); garbage is not a $0."""
    # Rows name each metric "<aggregation>_<measure>"; accept the reverse too.
    raw = row.get("sum_totalCost", row.get("totalCost_sum"))
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def get_cost_attribution(dimension: str, start_date: date, end_date: date) -> dict[str, Any]:
    """
    Langfuse-observed LLM spend for [start_date, end_date] split by trace tag,
    user id or session id, in the shared attribution shape (_attribution.py).

    Spend is Langfuse's own cost calculation for the generations it traced
    (source="api"), not a provider invoice. A trace with several tags counts
    under each tag (groups_overlap); total_usd is the traced spend itself.
    """
    from ...security.env import get_env
    from ._attribution import groups_result, unread, unsupported

    if dimension in _NOT_AVAILABLE:
        return unsupported(_NOT_AVAILABLE[dimension])
    if dimension not in _FIELDS:
        return unsupported(f"Langfuse cannot attribute cost by '{dimension}'.")
    public, secret = get_env("LANGFUSE_PUBLIC_KEY"), get_env("LANGFUSE_SECRET_KEY")
    if not (public and secret):
        return unread("not_configured")
    host = (get_env("LANGFUSE_HOST") or "https://cloud.langfuse.com").rstrip("/")
    auth = "Basic " + b64encode(f"{public}:{secret}".encode()).decode()
    field = _FIELDS[dimension]

    try:
        if dimension == "tag":
            try:
                rows = _metrics_rows(host, auth, _V2_METRICS, "observations", field,
                                     start_date, end_date)
            except _MetricsUnavailable:
                rows = _metrics_rows(host, auth, _V1_METRICS, "traces", field,
                                     start_date, end_date)
        else:
            rows = _metrics_rows(host, auth, _V1_METRICS, "traces", field,
                                 start_date, end_date)
    except _MetricsUnavailable:
        if dimension == "tag":
            return unread("endpoint_missing", "This Langfuse server has no metrics API.")
        return unsupported(
            f"This Langfuse server has no v1 metrics endpoint (removed in Langfuse v4), "
            f"and the v2 metrics API does not group by {field}.")
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (401, 403):
            return unread("credential_invalid",
                          f"Langfuse refused LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY ({e}).",
                          source="error")
        return unread("api_error", str(e))

    sums: dict[str | None, float] = {}
    traced = 0.0
    unreadable = 0
    for row in rows:
        cost = _row_cost(row)
        if cost is None:
            unreadable += 1
            continue
        traced += cost
        value = row.get(field)
        if dimension == "tag":
            tags = [value] if isinstance(value, str) and value else list(value or [])
            for tag in tags or [None]:
                sums[tag] = sums.get(tag, 0.0) + cost
        else:
            sums[value or None] = sums.get(value or None, 0.0) + cost

    note = "Spend as Langfuse calculates it for traced generations, not the provider invoice."
    overlap = dimension == "tag"
    if overlap:
        note += (" A trace with several tags counts under each tag, so tag rows can add up "
                 "to more than total_usd.")
    out = groups_result(sums, source="api", unassigned=_UNASSIGNED[dimension],
                        note=note, groups_overlap=overlap)
    out["total_usd"] = round(traced, 4)
    if len(rows) >= _ROW_LIMIT:
        out["truncated"] = True
        out["note"] += (f" Langfuse returned its {_ROW_LIMIT}-row maximum, so the smallest "
                        f"groups and their spend are missing.")
    if unreadable:
        out["unreadable_rows"] = unreadable
        out["note"] += f" {unreadable} row(s) had no readable cost and are excluded."
    return out
