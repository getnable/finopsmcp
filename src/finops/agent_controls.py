"""Agent cost controls: the pure helpers behind the pre-action gate.

An agent calls `check_action_policy` before it acts. These functions add the parts
that make the verdict useful in the agent's loop: a cheaper alternative when one
genuinely exists, the remediation posture (propose by default; auto is a later
opt-in mode), and a data-age helper so a budget verdict can say how fresh it is.

Pure and import-light on purpose: no server or DB import here, so it stays testable
in environments where the full MCP server import cannot load.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any

# Spot instances run well below on-demand for interruptible workloads. The exact
# discount varies by instance type and Availability Zone; ~70% off on-demand is a
# widely-cited typical figure. Every number derived from it is labeled an estimate
# and never presented as a measured saving.
_SPOT_OF_ONDEMAND = 0.30  # spot price ~= 30% of on-demand (a ~70% discount)

# Recognizes an EC2/RDS-style instance type, ANCHORED to a real size suffix so it
# does not match storage classes ("s3.standard"), version strings ("v2.0"), or
# arbitrary dotted tokens. Matches m5.large, r6g.4xlarge, m5.16xlarge, t3.micro,
# db.r6g.xlarge, c5.metal.
_INSTANCE_RE = re.compile(
    r"\b(?:db\.|cache\.)?[a-z]+\d[a-z]*\.(?:nano|micro|small|medium|large|\d*xlarge|metal)\b",
    re.IGNORECASE,
)
# GPU, accelerator, and bare-metal families. Spot for these is capacity-constrained
# and its discount varies far from the ~70% average, so a blended spot estimate
# would mislead. We only offer spot for general/compute/memory families.
_NON_SPOT_RE = re.compile(
    r"\bmetal\b|\b(?:p\d|g\d|inf\d|trn\d|dl\d)[a-z]*\.", re.IGNORECASE
)


def suggest_cheaper_path(
    breakdown: list[dict[str, Any]] | None,
    monthly_delta_usd: float | None,
) -> dict[str, Any] | None:
    """A conservative, honest cheaper alternative for a proposed change, or None.

    v1 covers the common, high-value case: a compute (instance) ADD. For those it
    offers the spot-priced equivalent as a clearly labeled estimate. It returns None
    for savings, non-compute changes, and anything it cannot price honestly, so the
    agent never sees a fabricated number.
    """
    if not breakdown or monthly_delta_usd is None or monthly_delta_usd <= 0:
        return None

    compute_add = 0.0
    resources: list[str] = []
    for line in breakdown:
        if not isinstance(line, dict) or line.get("action") != "add":
            continue
        delta = float(line.get("monthly_delta") or 0.0)
        if delta <= 0:
            continue
        blob = f"{line.get('resource_type', '')} {line.get('detail', '')}"
        if not _INSTANCE_RE.search(blob):
            continue  # not a recognizable compute instance add
        if _NON_SPOT_RE.search(blob):
            continue  # GPU / accelerator / metal: a blended spot estimate would mislead
        compute_add += delta
        resources.append(str(line.get("address") or line.get("resource_type") or "resource"))

    if compute_add <= 0:
        return None

    spot_monthly = round(compute_add * _SPOT_OF_ONDEMAND, 2)
    saving = round(compute_add - spot_monthly, 2)
    if saving <= 0:
        return None

    return {
        "summary": "Run the added compute on spot instead of on-demand.",
        "estimated_monthly_usd": spot_monthly,
        "estimated_saving_usd": saving,
        "applies_to": resources[:5],
        "basis": ("Spot is typically ~70% below on-demand, but it varies by instance "
                  "type and Availability Zone, so this is an estimate, not a measured "
                  "saving. Spot suits interruptible workloads."),
        "is_estimate": True,
    }


def remediation_status() -> dict[str, Any]:
    """The remediation posture for this instance. Default 'propose' (advisory).

    'auto' is the opt-in, bounded mode specced for a later release. Until it ships,
    `applied` is always False and nable only proposes, even if the env asks for auto.
    """
    mode = (os.environ.get("FINOPS_REMEDIATION_MODE") or "propose").strip().lower()
    if mode not in ("propose", "auto"):
        mode = "propose"
    out: dict[str, Any] = {"mode": mode, "applied": False}
    if mode == "auto":
        out["note"] = ("auto-remediation is not enabled in this build; nable is "
                       "proposing. A human still approves and applies every change.")
    return out


def data_age_hours(as_of_iso: str | None) -> float | None:
    """Whole-tenths of hours between an ISO-8601 timestamp and now (UTC), or None."""
    if not as_of_iso:
        return None
    try:
        ts = datetime.fromisoformat(str(as_of_iso).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    return round(max(0.0, hours), 1)  # clamp: a future timestamp (clock skew) reads as age 0
