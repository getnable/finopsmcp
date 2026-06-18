"""
Coarse environment buckets for the learning signal.

The signal rolls up per (source, bucket) so the loop can learn that, say, Spot is
fine for your non-prod batch jobs but never for prod. Buckets are deliberately COARSE
(env x resource-class, ~a dozen max): too many buckets and every one stays COLD
forever with no signal. env comes from a tag/name hint when we have it, else unknown.
"""
from __future__ import annotations

import re

_PROD = {"prod", "production", "prd"}
_NONPROD = {"nonprod", "non-prod", "dev", "develop", "development", "staging",
            "stage", "stg", "test", "testing", "qa", "sandbox", "sbx"}


def _env_class(environment: str | None) -> str:
    e = (environment or "").strip().lower()
    if e in _PROD:
        return "prod"
    if e in _NONPROD:
        return "nonprod"
    return "unknown"


def bucket_for(resource_type: str | None = None, environment: str | None = None) -> str:
    """A coarse 'env|resource-class' bucket, e.g. 'prod|ec2', 'nonprod|k8s', 'unknown|rds'."""
    env = _env_class(environment)
    rt = (resource_type or "").strip().lower()
    cls = re.sub(r"[^a-z0-9]+", "_", rt).strip("_")
    cls = cls.split("_")[0] if cls else "other"   # collapse 'k8s_workload' -> 'k8s'
    return f"{env}|{cls or 'other'}"
