"""
Spend x utilization correlator: the watchdog's first concrete piece.

It fuses two read-only signals that already exist in nable and turns them into
one ranked view of UNDERUTILIZED resources, each with the dollar waste it
represents and a PREPARED remediation ready to be pushed for one-click approval.

Signals reused (no boto3 duplicated here):
  - Running-but-underutilized resources come from analyze_rightsizing
    (recommendations/rightsizing.py). It already owns the Compute Optimizer /
    CloudWatch CPU + memory logic and carries the dollar saving.
  - Zero-utilization idle resources come from scan_idle_resources
    (cleanup/idle.py). It already owns the EBS / EIP / snapshot / stopped-EC2 /
    load-balancer detection and carries the dollar waste.

The correlator ranks by dollar waste and marks each finding with a utilization
score, so an owner sees the worst waste first rather than a flat anomaly list.

HARD RULE: this module is READ-ONLY and PROPOSE-ONLY. It reads the two scans and
builds descriptions. It never mutates cloud state and it never invokes a
remediation. A PreparedRemediation carries the fix as data plus an inert handle;
running it is a separate, human-approved step outside this module.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# A resource whose average CPU (or the idle scan) puts it at or below this
# fraction of capacity is "underutilized" rather than merely anomalous. Idle
# resources score 0.0 by definition (nothing is using them).
_UNDERUTILIZED_CPU_PCT = 20.0


@dataclass
class PreparedRemediation:
    """The exact fix for a finding, prepared and ready to push for approval.

    PREPARED, NEVER EXECUTED. `kind` names the prepare path; `command` carries
    the precise action as data (a CLI command, a Terraform address, a PR
    target). `prepare_via` names the existing nable entry point that would run
    the prepare step once a human approves. Nothing here calls it.
    """

    kind: str                       # "rightsizing_pr" | "idle_cleanup" | "manual"
    title: str                      # one-line summary of the fix
    command: str                    # the exact action, as data (never run here)
    prepare_via: str                # existing nable entry point for the prepare step
    requires_approval: bool = True  # always True; the human tap is mandatory
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class CorrelatedFinding:
    """One underutilized resource: what it costs, how little it is used, and the
    prepared fix. Sortable by monthly_waste_usd (worst first)."""

    resource_id: str
    resource_type: str              # "ec2" | "ebs_volume" | "elastic_ip" | ...
    name: str
    region: str
    account_id: str
    provider: str
    monthly_waste_usd: float
    utilization_pct: float | None   # avg CPU %, or 0.0 for idle, None if unknown
    signal: str                     # "rightsizing" | "idle"
    reason: str
    remediation: PreparedRemediation
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def annual_waste_usd(self) -> float:
        return round(self.monthly_waste_usd * 12, 2)


# ── prepared-remediation builders ─────────────────────────────────────────────


def _prepare_rightsizing(rec: Any) -> PreparedRemediation:
    """Build the prepared fix for a rightsizing finding.

    The prepare step reuses open_rightsizing_pr, which patches Terraform and
    opens a PR (it does NOT apply Terraform; the human merges and applies). We
    carry the target so the push handler can call it after approval. We do not
    call it here.
    """
    target = rec.recommended_type or ""
    if rec.resource_type == "ec2" and target:
        cmd = (
            f"aws ec2 modify-instance-attribute --instance-id {rec.instance_id} "
            f"--instance-type {target}  # requires stop/start; prefer the Terraform PR"
        )
    else:
        cmd = f"Right-size {rec.instance_id} to {target or 'recommended size'}"
    return PreparedRemediation(
        kind="rightsizing_pr",
        title=rec.title,
        command=cmd,
        prepare_via="finops.remediation.rightsizing_pr.open_rightsizing_pr",
        params={
            "resource_id": rec.instance_id,
            "from_instance_type": rec.instance_type,
            "instance_type": target,
            "resource_type": rec.resource_type,
            "region": rec.region,
        },
    )


def _prepare_idle_cleanup(res: Any) -> PreparedRemediation:
    """Build the prepared fix for an idle-resource finding.

    Idle cleanup has no PR path; we carry the precise CLI command as data. The
    matching read-only verifier (verifiers.verify_idle_cleanup) confirms the
    resource is gone after a human runs it. We never run it.
    """
    rid = res.resource_id
    region = res.region
    if res.resource_type == "ebs_volume":
        cmd = f"aws ec2 delete-volume --volume-id {rid} --region {region}"
    elif res.resource_type == "elastic_ip":
        cmd = f"aws ec2 release-address --allocation-id {rid} --region {region}"
    elif res.resource_type == "snapshot":
        cmd = f"aws ec2 delete-snapshot --snapshot-id {rid} --region {region}"
    elif res.resource_type == "stopped_ec2":
        cmd = f"aws ec2 terminate-instances --instance-ids {rid} --region {region}"
    elif res.resource_type == "load_balancer":
        arn = res.metadata.get("lb_arn", rid)
        cmd = f"aws elbv2 delete-load-balancer --load-balancer-arn {arn} --region {region}"
    else:
        cmd = f"Remove idle {res.resource_type} {rid} in {region}"
    return PreparedRemediation(
        kind="idle_cleanup",
        title=f"Clean up idle {res.resource_type} {res.name or rid}",
        command=cmd,
        prepare_via="finops.recommendations.verifiers.verify_idle_cleanup",
        params={
            "resource_id": rid,
            "resource_type": res.resource_type,
            "region": region,
            "protected": res.protected,
        },
    )


# ── correlation ───────────────────────────────────────────────────────────────


def _findings_from_rightsizing(recs: list[Any]) -> list[CorrelatedFinding]:
    out: list[CorrelatedFinding] = []
    for rec in recs:
        # analyze_rightsizing already filters to over-provisioned resources, but
        # guard on the utilization gate so the correlator only surfaces genuine
        # underutilization, not a borderline Compute Optimizer nudge.
        cpu = rec.avg_cpu_pct
        if cpu is not None and cpu > _UNDERUTILIZED_CPU_PCT:
            continue
        waste = round(float(rec.monthly_savings or 0.0), 2)
        if waste <= 0:
            continue
        out.append(CorrelatedFinding(
            resource_id=rec.instance_id,
            resource_type=rec.resource_type,
            name=rec.name,
            region=rec.region,
            account_id=rec.account_id,
            provider="aws",
            monthly_waste_usd=waste,
            utilization_pct=round(float(cpu), 1) if cpu is not None else None,
            signal="rightsizing",
            reason=rec.description,
            remediation=_prepare_rightsizing(rec),
            metadata={
                "current_type": rec.instance_type,
                "recommended_type": rec.recommended_type,
                "avg_mem_pct": rec.avg_mem_pct,
                "confidence": rec.confidence,
                "source": rec.source,
            },
        ))
    return out


def _findings_from_idle(resources: list[Any]) -> list[CorrelatedFinding]:
    out: list[CorrelatedFinding] = []
    for res in resources:
        waste = round(float(res.monthly_cost_usd or 0.0), 2)
        if waste <= 0:
            continue
        out.append(CorrelatedFinding(
            resource_id=res.resource_id,
            resource_type=res.resource_type,
            name=res.name,
            region=res.region,
            account_id=res.account_id,
            provider="aws",
            monthly_waste_usd=waste,
            utilization_pct=0.0,  # idle by definition
            signal="idle",
            reason=res.reason,
            remediation=_prepare_idle_cleanup(res),
            metadata={
                "idle_days": res.idle_days,
                "protected": res.protected,
                **res.metadata,
            },
        ))
    return out


def correlate_spend_and_utilization(
    regions: list[str] | None = None,
    min_monthly_waste: float = 5.0,
    include_idle: bool = True,
    include_rightsizing: bool = True,
    _rightsizing_recs: list[Any] | None = None,
    _idle_resources: list[Any] | None = None,
) -> list[CorrelatedFinding]:
    """
    Return underutilized resources ranked by monthly dollar waste (descending).

    Fuses analyze_rightsizing (running but underused) and scan_idle_resources
    (idle) into one list. Each finding carries a PreparedRemediation. Read-only.

    The two underscore-prefixed params inject pre-fetched scan results, which is
    how the tests exercise this without live boto3. In production they stay None
    and the real scans run.
    """
    findings: list[CorrelatedFinding] = []

    if include_rightsizing:
        recs = _rightsizing_recs
        if recs is None:
            try:
                from ..recommendations.rightsizing import analyze_rightsizing
                recs = analyze_rightsizing(regions=regions, min_monthly_savings=min_monthly_waste)
            except Exception as exc:  # noqa: BLE001
                log.warning("Watchdog rightsizing scan failed: %s", exc)
                recs = []
        findings.extend(_findings_from_rightsizing(recs))

    if include_idle:
        resources = _idle_resources
        if resources is None:
            try:
                from ..cleanup.idle import scan_idle_resources
                resources = scan_idle_resources(regions=regions)
            except Exception as exc:  # noqa: BLE001
                log.warning("Watchdog idle scan failed: %s", exc)
                resources = []
        findings.extend(_findings_from_idle(resources))

    findings = [f for f in findings if f.monthly_waste_usd >= min_monthly_waste]
    findings.sort(key=lambda f: f.monthly_waste_usd, reverse=True)
    return findings


def correlation_summary(findings: list[CorrelatedFinding]) -> dict[str, Any]:
    """Ranked, token-bounded summary of the correlated findings.

    Totals cover the full population; only the detail list is capped, matching
    the pattern in idle_resources_summary / rightsizing_summary."""
    total_waste = sum(f.monthly_waste_usd for f in findings)
    by_signal: dict[str, float] = {}
    for f in findings:
        by_signal[f.signal] = round(by_signal.get(f.signal, 0.0) + f.monthly_waste_usd, 2)

    rows = [
        {
            "resource_id": f.resource_id,
            "resource_type": f.resource_type,
            "name": f.name,
            "region": f.region,
            "provider": f.provider,
            "monthly_waste_usd": f.monthly_waste_usd,
            "utilization_pct": f.utilization_pct,
            "signal": f.signal,
            "reason": f.reason,
            "remediation": {
                "kind": f.remediation.kind,
                "title": f.remediation.title,
                "command": f.remediation.command,
                "prepare_via": f.remediation.prepare_via,
                "requires_approval": f.remediation.requires_approval,
            },
        }
        for f in sorted(findings, key=lambda f: f.monthly_waste_usd, reverse=True)
    ]
    from ..token_budget import fit_to_budget
    kept, omitted = fit_to_budget(rows)

    out: dict[str, Any] = {
        "total_findings": len(findings),
        "total_monthly_waste_usd": round(total_waste, 2),
        "total_annual_waste_usd": round(total_waste * 12, 2),
        "waste_by_signal": by_signal,
        "findings": kept,
        "propose_only": True,
        "note": (
            "Every finding carries a PREPARED remediation. Nothing is applied. "
            "A fix runs only after a human approves it."
        ),
    }
    if omitted:
        out["findings_truncated"] = True
        out["findings_omitted"] = omitted
        out["hint"] = (
            f"Showing the {len(kept)} costliest of {len(findings)} findings to "
            f"bound token cost. Narrow with regions for the rest."
        )
    return out
