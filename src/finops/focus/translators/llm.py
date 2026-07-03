"""FOCUS 1.2 translator for LLM / AI providers.

Every LLM connector (OpenAI, Anthropic, Bedrock, Vertex, OpenRouter, LiteLLM)
returns the same normalized shape:

    {
      "total_usd": float,
      "by_model": {model: usd, ...},
      "by_model_tokens": {model: {input_tokens, output_tokens,
                          cache_read_input_tokens, cache_creation_input_tokens,
                          request_count}, ...},
      "daily": [...],
      "source": "api" | "limited" | "none",
    }

This maps one such dict to FOCUS 1.2 records: one record per model, ServiceCategory
"AI and Machine Learning", ResourceType "Model", with token counts and request
volume preserved in Tags so unit-economics survive normalization. Token usage is
recorded even when a provider reports cost-only (Bedrock/Vertex), and cost is
recorded even when a gateway reports usage without dollars.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any

from ..schema import FocusRecord

_CATEGORY = "AI and Machine Learning"


def _period(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def llm_result_to_focus(
    result: dict[str, Any],
    *,
    provider: str,
    publisher: str | None = None,
    start_date: date,
    end_date: date,
) -> list[FocusRecord]:
    """Translate one LLM provider's normalized cost dict into FOCUS 1.2 records.

    Args:
        result:    The normalized LLM-connector dict (by_model + by_model_tokens).
        provider:  FOCUS ProviderName (e.g. "OpenAI", "OpenRouter").
        publisher: FOCUS PublisherName; defaults to provider.
        start_date/end_date: charge/billing period bounds.
    """
    if not isinstance(result, dict):
        return []
    publisher = publisher or provider
    ps, pe = _period(start_date), _period(end_date)
    # A gateway (self-hosted LiteLLM, proxy) can return malformed shapes; degrade
    # per-field rather than raising, or one bad provider drops every AI record.
    by_model = result.get("by_model")
    if not isinstance(by_model, dict):
        by_model = {}
    by_tokens = result.get("by_model_tokens")
    if not isinstance(by_tokens, dict):
        by_tokens = {}

    # Union of models that reported cost and models that reported only tokens, so
    # nothing is dropped (gateways may report usage without a dollar figure).
    models = list(dict.fromkeys([*by_model.keys(), *by_tokens.keys()]))
    records: list[FocusRecord] = []
    for model in models:
        try:
            amount = round(float(by_model.get(model, 0.0) or 0.0), 10)
        except (TypeError, ValueError):
            amount = 0.0
        if not math.isfinite(amount):
            amount = 0.0
        tags: dict[str, str] = {}
        tok = by_tokens.get(model)
        for k, v in (tok.items() if isinstance(tok, dict) else ()):
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                tags[str(k)] = str(v)
        model = str(model)
        records.append(FocusRecord(
            BilledCost=amount,
            EffectiveCost=amount,
            ListCost=amount,
            ResourceId=model,
            ResourceName=model,
            ResourceType="Model",
            ServiceName=model,
            ServiceCategory=_CATEGORY,
            ProviderName=provider,
            PublisherName=publisher,
            RegionId=None,
            RegionName=None,
            BillingPeriodStart=ps,
            BillingPeriodEnd=pe,
            ChargePeriodStart=ps,
            ChargePeriodEnd=pe,
            ChargeCategory="Usage",
            ChargeDescription=None,
            CommitmentDiscountId=None,
            CommitmentDiscountType=None,
            Tags=tags,
            SubAccountId=None,
            SubAccountName=None,
        ))
    return records
