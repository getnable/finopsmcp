"""
Duplicate-capability scanner.

A cost breakdown by service tells you WHAT you spend on, but not whether two
services are doing the same job and you're paying for both. Every line item
looks legitimate on its own, so a plain bill breakdown never surfaces this;
you have to already suspect it and go looking. This scanner looks for spend
patterns where multiple connected providers or AWS services serve the same
underlying capability at once.

V1 covers the two clusters with the least false-positive risk:
  - LLM inference paths: 2+ of {Bedrock, direct OpenAI, Anthropic, Vertex,
    OpenRouter, LiteLLM, Together, Replicate, Modal} carrying real spend at
    once usually means one is a leftover from testing or a migration that
    never got cleaned up, not a deliberate multi-provider setup.
  - Managed search/retrieval: 2+ of {Kendra, OpenSearch Service} carrying AWS
    spend at once.

Deliberately NOT covered yet: overlapping database engines (RDS + DocumentDB,
etc). Multi-database architectures are extremely often intentional, different
data models for different workloads, so a same-purpose-engine detector there
would be wrong more often than right. Skip rather than ship a noisy finding.
"""
from __future__ import annotations

from .envelope import INFERRED, Finding

# Human-readable names for the LLM provider keys returned by
# connectors.llm_costs.get_all_llm_costs()["by_provider"].
_LLM_PROVIDER_LABELS = {
    "bedrock": "AWS Bedrock",
    "openai": "OpenAI (direct API)",
    "anthropic": "Anthropic (direct API)",
    "vertex": "Google Vertex AI",
    "openrouter": "OpenRouter",
    "litellm": "LiteLLM proxy",
    "modal": "Modal",
    "together": "Together AI",
    "replicate": "Replicate",
}

# Ignore noise-level spend (a stray test call) so one real path never gets
# flagged against a leftover cent from a curiosity request.
_NOISE_FLOOR_USD = 1.0

# AWS Cost Explorer service names that provide overlapping managed search /
# retrieval capability.
_SEARCH_SERVICE_NAMES = {
    "Amazon Kendra": "Kendra",
    "Amazon OpenSearch Service": "OpenSearch",
    "Amazon Elasticsearch Service": "Elasticsearch Service",
}


def find_duplicate_llm_paths(llm_by_provider: dict[str, float]) -> Finding | None:
    """Flag when 2+ LLM inference paths carry real spend at once.

    Args:
        llm_by_provider: the "by_provider" dict from get_all_llm_costs(), e.g.
            {"bedrock": 3568.13, "anthropic": 42.10, "openai": 0.0}.
    """
    active = {
        _LLM_PROVIDER_LABELS.get(k, k): round(v, 2)
        for k, v in (llm_by_provider or {}).items()
        if v and v > _NOISE_FLOOR_USD
    }
    if len(active) < 2:
        return None

    names = sorted(active, key=lambda k: -active[k])
    total = round(sum(active.values()), 2)
    smaller_total = round(total - active[names[0]], 2)

    return Finding(
        source="duplicate_capability",
        title="Two or more billing paths for LLM inference",
        why=(
            f"{' and '.join(names)} all show real spend in the same window. "
            "Each is a separate way to run model inference, and paying for "
            "more than one at once is usually a leftover from testing or a "
            "migration that never got cleaned up, not a deliberate "
            "multi-provider setup."
        ),
        evidence=INFERRED,
        confidence="medium",
        why_unsure=(
            "Running multiple inference paths on purpose (failover, "
            "per-team routing, a deliberate model comparison) is a real "
            "pattern too, so this is a 'worth a look' flag, not a claim "
            "that spend is wasted."
        ),
        assumptions=[
            "Every active provider above the noise floor ($1/mo) is "
            "counted; this does not know whether the smaller path is "
            "intentional."
        ],
        rough_monthly=smaller_total,
        confirm_steps=[
            f"Check whether {names[-1]} is still actively used, or whether "
            "it's a leftover key/credential from an earlier setup.",
            "If it's intentional (failover, A/B routing), no action "
            "needed, this flag can be dismissed.",
        ],
        remediation=[
            "Consolidate onto one inference path, or route deliberately "
            "(e.g. Bedrock for production, direct API for local dev) so "
            "mixed spend is a choice, not an accident.",
        ],
        metadata={"active_paths": active, "total_monthly_usd": total},
    )


def find_duplicate_search_services(aws_by_service: dict[str, float]) -> Finding | None:
    """Flag when 2+ AWS managed search/retrieval services carry real spend at once.

    Args:
        aws_by_service: the AWS "by_service" dict (Cost Explorer service names).
    """
    active = {
        label: round(aws_by_service[svc_name], 2)
        for svc_name, label in _SEARCH_SERVICE_NAMES.items()
        if aws_by_service.get(svc_name, 0) and aws_by_service[svc_name] > _NOISE_FLOOR_USD
    }
    if len(active) < 2:
        return None

    names = sorted(active, key=lambda k: -active[k])
    total = round(sum(active.values()), 2)

    return Finding(
        source="duplicate_capability",
        title="Two managed search/retrieval services running at once",
        why=(
            f"{' and '.join(names)} both show real spend. Both are managed "
            "search/retrieval services, and running two at once is more "
            "often an unfinished migration than a deliberate split."
        ),
        evidence=INFERRED,
        confidence="low",
        why_unsure=(
            "Different search backends for genuinely different workloads "
            "(log search vs. document Q&A, for instance) is a legitimate "
            "reason to run both, so this needs a human look, not an "
            "automatic merge."
        ),
        rough_monthly=total,
        confirm_steps=[
            f"Check what each of {', '.join(names)} actually indexes, if "
            "it's the same documents or data, one is likely redundant.",
        ],
        remediation=[
            "If they serve the same data, pick one and decommission the "
            "other.",
        ],
        metadata={"active_services": active},
    )


def scan_duplicate_capabilities(
    llm_by_provider: dict[str, float] | None = None,
    aws_by_service: dict[str, float] | None = None,
) -> list[Finding]:
    """Run every duplicate-capability check and return the findings that fired."""
    findings: list[Finding] = []
    if llm_by_provider:
        f = find_duplicate_llm_paths(llm_by_provider)
        if f:
            findings.append(f)
    if aws_by_service:
        f = find_duplicate_search_services(aws_by_service)
        if f:
            findings.append(f)
    return findings
