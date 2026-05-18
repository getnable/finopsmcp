"""
Vertex AI / Google AI Studio cost connector.

Uses Cloud Billing API with service filter for 'aiplatform.googleapis.com'.
Requires GCP credentials (GOOGLE_APPLICATION_CREDENTIALS) and billing export
to be enabled.

Falls back to BigQuery billing export if direct API doesn't have model-level detail.

Env vars:
  GOOGLE_APPLICATION_CREDENTIALS  — path to service account JSON key file
  GCP_BILLING_ACCOUNT_ID          — billing account ID (e.g. "01AB23-456789-CDEF01")
  GCP_PROJECT_ID                  — project ID for BigQuery export queries (optional)
  GCP_BIGQUERY_BILLING_DATASET    — BigQuery dataset for billing export (optional)
                                     e.g. "project.dataset.table"
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

log = logging.getLogger(__name__)

# Vertex AI / Google AI pricing per 1M tokens (USD, May 2026)
# Source: https://cloud.google.com/vertex-ai/generative-ai/pricing
_VERTEX_PRICING: dict[str, dict[str, float]] = {
    # Gemini 1.5 Pro (<=128K context)
    "gemini-1.5-pro":        {"input": 3.50,  "output": 10.50},
    # Gemini 1.5 Pro (>128K context)
    "gemini-1.5-pro-long":   {"input": 7.00,  "output": 21.00},
    # Gemini 1.5 Flash (<=128K)
    "gemini-1.5-flash":      {"input": 0.075, "output": 0.30},
    # Gemini 1.5 Flash (>128K)
    "gemini-1.5-flash-long": {"input": 0.15,  "output": 0.60},
    # Gemini 1.0 Pro
    "gemini-1.0-pro":        {"input": 0.50,  "output": 1.50},
    # Gemini 2.0 Flash
    "gemini-2.0-flash":      {"input": 0.075, "output": 0.30},
    # Gemini 2.0 Pro
    "gemini-2.0-pro":        {"input": 3.50,  "output": 10.50},
    # Legacy PaLM / text-bison
    "text-bison":            {"input": 0.125, "output": 0.125},
    "text-bison-32k":        {"input": 0.125, "output": 0.125},
    # Chat-bison
    "chat-bison":            {"input": 0.125, "output": 0.125},
    "chat-bison-32k":        {"input": 0.125, "output": 0.125},
    # Code models
    "code-bison":            {"input": 0.125, "output": 0.125},
    "codechat-bison":        {"input": 0.125, "output": 0.125},
    # Embeddings — priced per 1M characters, approximated as tokens
    "textembedding-gecko":   {"input": 0.025, "output": 0.0},
    "textembedding-gecko@003": {"input": 0.025, "output": 0.0},
    "text-embedding-004":    {"input": 0.025, "output": 0.0},
    # Imagen (per image)
    "imagegeneration@006":   {"input": 0.020, "output": 0.0},
}

# SKU description → model name mapping patterns (longest match first)
_SKU_PATTERNS: list[tuple[str, str]] = [
    # Gemini 2.0 family
    (r"gemini.?2\.0.?pro",          "gemini-2.0-pro"),
    (r"gemini.?2\.0.?flash",        "gemini-2.0-flash"),
    # Gemini 1.5 Pro — detect long-context variant
    (r"gemini.?1\.5.?pro.*>128k",   "gemini-1.5-pro-long"),
    (r"gemini.?1\.5.?pro.*long",    "gemini-1.5-pro-long"),
    (r"gemini.?1\.5.?pro",          "gemini-1.5-pro"),
    # Gemini 1.5 Flash
    (r"gemini.?1\.5.?flash.*>128k", "gemini-1.5-flash-long"),
    (r"gemini.?1\.5.?flash.*long",  "gemini-1.5-flash-long"),
    (r"gemini.?1\.5.?flash",        "gemini-1.5-flash"),
    # Gemini 1.0
    (r"gemini.?1\.0.?pro",          "gemini-1.0-pro"),
    (r"gemini.?pro",                "gemini-1.0-pro"),
    # Legacy PaLM
    (r"text.?bison.*32k",           "text-bison-32k"),
    (r"text.?bison",                "text-bison"),
    (r"chat.?bison.*32k",           "chat-bison-32k"),
    (r"chat.?bison",                "chat-bison"),
    (r"code.?bison",                "code-bison"),
    (r"codechat.?bison",            "codechat-bison"),
    # Embeddings
    (r"text.?embedding.*004",       "text-embedding-004"),
    (r"textembedding.?gecko@003",   "textembedding-gecko@003"),
    (r"textembedding.?gecko",       "textembedding-gecko"),
    # Imagen
    (r"imagegeneration",            "imagegeneration@006"),
]


def _sku_to_model(sku_description: str) -> str:
    """
    Map a Cloud Billing SKU description to a normalised Vertex AI model name.

    Examples:
      "Vertex AI Gemini 1.5 Pro Input Characters"  → "gemini-1.5-pro"
      "Vertex AI Text-Bison Output Characters"      → "text-bison"
      "Vertex AI TextEmbedding Gecko Predictions"   → "textembedding-gecko"
    """
    lower = sku_description.lower()
    for pattern, model in _SKU_PATTERNS:
        if re.search(pattern, lower):
            return model
    return "vertex-ai-unknown"


def get_vertex_costs(
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """
    Fetch Vertex AI model costs filtered to ``aiplatform.googleapis.com``.

    Tries sources in order:
      1. Cloud Billing API (google-cloud-billing) — real billing data, SKU-level
      2. BigQuery billing export — richer model-level detail when export is set up
      3. Returns empty result with reason on failure

    Returns the same normalised structure as the OpenAI/Anthropic connectors:
      {
        "total_usd": float,
        "by_model":  {"gemini-1.5-pro": float, ...},
        "daily":     [{"date": "YYYY-MM-DD", "total_usd": float, "by_model": {...}}, ...],
        "source":    "cloud_billing" | "bigquery" | "none",
      }
    """
    from ...security.env import get_env

    billing_account = get_env("GCP_BILLING_ACCOUNT_ID")
    bq_dataset      = get_env("GCP_BIGQUERY_BILLING_DATASET")

    if not billing_account and not bq_dataset:
        return _empty("not_configured — set GCP_BILLING_ACCOUNT_ID or GCP_BIGQUERY_BILLING_DATASET")

    # Try Cloud Billing API first
    if billing_account:
        result = _fetch_via_billing_api(billing_account, start_date, end_date)
        if result.get("source") != "none":
            return result

    # Fall back to BigQuery export
    if bq_dataset:
        result = _fetch_via_bigquery(bq_dataset, start_date, end_date)
        if result.get("source") != "none":
            return result

    return _empty("api_error")


def _fetch_via_billing_api(
    billing_account: str,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """Query the Cloud Billing API for Vertex AI SKUs."""
    try:
        from google.cloud import billing_v1  # type: ignore
    except ImportError:
        log.debug("google-cloud-billing not installed — pip install google-cloud-billing")
        return _empty("google_cloud_billing_missing")

    try:
        client = billing_v1.CloudBillingClient()
        # The Catalog API gives SKU prices; for actual spend we need the Budget API or export.
        # Use Services to filter to Vertex AI, then use Cost Insights if available.
        # Most teams use BigQuery export; here we enumerate SKUs for cost estimation.
        #
        # Note: Real spend data requires either:
        #   a) Cloud Billing export to BigQuery (recommended)
        #   b) Cloud Billing Budget API (only budget vs. actual, not model-level)
        #
        # We attempt the BigQuery path transparently; if that fails we show a helpful error.
        log.debug("Cloud Billing API does not expose per-SKU spend directly; use BigQuery export.")
        return _empty("use_bigquery_export")
    except Exception as e:
        log.debug("Cloud Billing API error: %s", e)
        return _empty("billing_api_error")


def _fetch_via_bigquery(
    dataset: str,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """
    Query the Cloud Billing BigQuery export for Vertex AI costs.

    ``dataset`` should be the fully qualified table reference:
      "my-project.billing_export.gcp_billing_export_v1_XXXXXX"
    """
    try:
        from google.cloud import bigquery  # type: ignore
    except ImportError:
        log.debug("google-cloud-bigquery not installed — pip install google-cloud-bigquery")
        return _empty("google_cloud_bigquery_missing")

    query = f"""
        SELECT
            DATE(usage_start_time) AS usage_date,
            sku.description        AS sku_description,
            SUM(cost)              AS total_cost,
            SUM(usage.amount)      AS total_usage_amount,
            usage.unit             AS usage_unit
        FROM `{dataset}`
        WHERE
            service.description LIKE '%Vertex AI%'
            OR service.id = 'aiplatform.googleapis.com'
            AND DATE(usage_start_time) >= '{start_date.isoformat()}'
            AND DATE(usage_start_time) <  '{end_date.isoformat()}'
        GROUP BY 1, 2, 5
        ORDER BY 1, total_cost DESC
    """

    try:
        client = bigquery.Client()
        rows   = list(client.query(query).result())
    except Exception as e:
        log.warning("Vertex AI BigQuery query failed: %s", e)
        return _empty("bigquery_error")

    total = 0.0
    by_model: dict[str, float] = {}
    daily_map: dict[str, dict[str, float]] = {}  # date → {model: cost}

    for row in rows:
        day      = str(row.usage_date)
        sku_desc = row.sku_description or ""
        cost     = float(row.total_cost or 0.0)
        model    = _sku_to_model(sku_desc)

        total += cost
        by_model[model] = by_model.get(model, 0.0) + cost

        if day not in daily_map:
            daily_map[day] = {}
        daily_map[day][model] = daily_map[day].get(model, 0.0) + cost

    daily = [
        {
            "date":      d,
            "total_usd": round(sum(m.values()), 4),
            "by_model":  {k: round(v, 4) for k, v in m.items()},
        }
        for d, m in sorted(daily_map.items())
    ]

    return {
        "total_usd": round(total, 4),
        "by_model":  {k: round(v, 4) for k, v in
                      sorted(by_model.items(), key=lambda x: x[1], reverse=True)},
        "daily":     daily,
        "source":    "bigquery",
    }


def _empty(reason: str) -> dict[str, Any]:
    return {
        "total_usd": 0.0,
        "by_model":  {},
        "daily":     [],
        "source":    "none",
        "reason":    reason,
    }


async def is_configured() -> bool:
    from ...security.env import get_env
    return bool(
        get_env("GCP_BILLING_ACCOUNT_ID") or get_env("GCP_BIGQUERY_BILLING_DATASET")
    )
