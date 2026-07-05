"""Policy-bounded action gate: the advisory half (B1) of the cost guardrail.

An agent describes a remediation action it is considering; this module checks it
against a human-authored policy and returns allow / block / escalate. ADVICE ONLY:
nable never executes the action, a human applies it. This is the seed of the
request-path guardrail. The auto-execute half (B2) is a separate, explicit decision
and is intentionally NOT implemented here, propose-only stays fully intact.
"""
from __future__ import annotations

import os
from typing import Any

GATE_ALLOW = "allow"        # reversible, allowlisted, in budget: a human can apply it
GATE_BLOCK = "block"        # not in the human's allowlist: do not propose applying it
GATE_ESCALATE = "escalate"  # one-way door or over budget: a human must review first

# Remediation action types nable can propose, classified by Bezos door. Two-way =
# reversible (a PR you can revert, an instance you can restart). One-way =
# irreversible or a financial commitment.
TWO_WAY_DOORS = {
    "rightsizing", "tag_fix", "gp2_to_gp3", "graviton_migration",
    "spot_migration", "stop_idle", "schedule_nonprod", "ticket",
    # Reversible infrastructure mutations. These MUST be known here: the shell
    # guard classifies `terraform apply` as infra_apply and allows it by default,
    # so the MCP gate has to agree or the two halves contradict each other (the
    # gate used to BLOCK terraform_apply as "unknown" while the guard waved the
    # same command through). Budget/threshold escalation still applies.
    "infra_apply", "terraform_apply", "helm_upgrade", "kubectl_apply",
}
ONE_WAY_DOORS = {
    "idle_cleanup", "delete_resource", "terminate_instance",
    "release_ip", "purchase_commitment", "snapshot_delete",
}

DEFAULT_POLICY: dict[str, Any] = {
    "allowed_action_types": sorted(TWO_WAY_DOORS),  # reversible actions that are in-policy
    "max_auto_monthly_usd": 500.0,                  # a cost increase above this escalates
    "escalate_one_way_doors": True,                 # irreversible / financial always need a human
}


def door_of(action_type: str) -> str:
    if action_type in ONE_WAY_DOORS:
        return "one_way"
    if action_type in TWO_WAY_DOORS:
        return "two_way"
    return "unknown"


def is_one_way(action_type: str) -> bool:
    return action_type in ONE_WAY_DOORS


def load_policy() -> dict[str, Any]:
    """The default policy with optional env overrides, so a human can author the
    policy without a config system:
      FINOPS_POLICY_MAX_AUTO_USD       a dollar threshold (float)
      FINOPS_POLICY_ALLOWED_ACTIONS    comma-separated action types
    """
    pol: dict[str, Any] = dict(DEFAULT_POLICY)
    pol["allowed_action_types"] = list(DEFAULT_POLICY["allowed_action_types"])

    mx = os.getenv("FINOPS_POLICY_MAX_AUTO_USD", "").strip()
    if mx:
        try:
            pol["max_auto_monthly_usd"] = float(mx)
        except ValueError:
            pass

    al = os.getenv("FINOPS_POLICY_ALLOWED_ACTIONS", "").strip()
    if al:
        pol["allowed_action_types"] = [a.strip() for a in al.split(",") if a.strip()]
    return pol


def evaluate_action_gate(
    action_type: str,
    monthly_delta_usd: float = 0.0,
    cost_verdict: str | None = None,
    *,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Advisory gate for a proposed remediation action.

    action_type: e.g. "rightsizing" (reversible) or "idle_cleanup" (one-way).
    monthly_delta_usd: the action's cost impact (negative = a saving).
    cost_verdict: the preflight verdict ("ok"/"warn"/"over_budget"/"no_budget"), if known.

    Returns {gate, reason, action_type, door, monthly_delta_usd}. nable never
    executes; this advises a human. Pure, never raises on normal input.
    """
    pol = policy or load_policy()
    delta = float(monthly_delta_usd or 0.0)
    door = door_of(action_type)
    out: dict[str, Any] = {
        "action_type": action_type,
        "door": door,
        "monthly_delta_usd": round(delta, 2),
    }

    # 1. One-way doors always escalate (irreversible or a financial commitment).
    if door == "one_way" and pol.get("escalate_one_way_doors", True):
        out["gate"] = GATE_ESCALATE
        out["reason"] = (f"'{action_type}' is a one-way door (irreversible or a financial "
                         "commitment); a human must review and apply it.")
        return out

    # 2. Not in the human's allowlist -> block.
    if action_type not in set(pol.get("allowed_action_types", [])):
        out["gate"] = GATE_BLOCK
        out["reason"] = (f"'{action_type}' is not in your allowlist of permitted actions; "
                         "nable will not propose applying it.")
        return out

    # 3. Over budget (per the cost preflight) -> escalate.
    if cost_verdict == "over_budget":
        out["gate"] = GATE_ESCALATE
        out["reason"] = ("This change would push you over budget; a human should review it "
                         "before it is applied.")
        return out

    # 4. Cost increase above the auto threshold -> escalate (savings are always fine).
    cap = float(pol.get("max_auto_monthly_usd", 500.0))
    if delta > cap:
        out["gate"] = GATE_ESCALATE
        out["reason"] = (f"The +${delta:,.0f}/mo impact is over your ${cap:,.0f} auto threshold; "
                         "a human should review it.")
        return out

    # 5. Reversible, allowlisted, within budget and threshold.
    out["gate"] = GATE_ALLOW
    out["reason"] = (f"'{action_type}' is reversible, in your allowlist, and within budget; "
                     "a human can apply it within your policy.")
    return out
