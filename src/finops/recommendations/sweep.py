# SPDX-License-Identifier: Apache-2.0
"""One cost sweep, shared by every tool that reports savings.

Why this module exists, stated plainly: the sweep that runs 21 scanners and
normalises their output into findings existed three times. run_full_cost_audit
had the maintained copy. export_cost_report_csv and publish_cost_report_to_notion
had forks of an older version, and the forks had drifted:

  - Both imported `scan_spot_adoption_opportunities`, a name that has never
    existed in recommendations.spot_adoption. The import sits above the scanner
    dispatch, so BOTH TOOLS RAISED ImportError on every single invocation. Not
    degraded, not partial: dead, for as long as the fork has existed.
  - Both called the spot scanner as `f(aws_client=aws, regions=regions)`, but
    the real function takes only `regions`. Fixing the name alone would have
    swapped ImportError for TypeError.
  - Both gathered the scanners as bare coroutines on the running event loop, so
    they ran back to back and blocked it. The audit moved to threads with a
    deadline; the forks never got that.
  - Neither attached `resource_id`, so neither could collapse mutually exclusive
    fixes on one resource. The 141-156% overstatement that was fixed in the audit
    was still live in both exports.

Four defects, one cause: the same logic written down three times. So it is
written down once here, and the three tools call it. A fourth caller cannot
drift because there is nothing left to drift from.

Nothing in this module talks to finops.server, so it is importable from any tool
module without the import cycle that shape usually brings.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from ..aws_prices import PUBLIC_IPV4_PER_MONTH
from .envelope import INVESTIGATION, magnitude_band

log = logging.getLogger(__name__)

# How long the whole sweep may take before we return what we have. One stuck
# region or throttled API must not hang a user's request for minutes.
DEFAULT_DEADLINE_S = 90

# What the sweep asks cleanup.idle to scan. elastic_ip is left out because the
# ipv4 scanner reports the same unattached addresses as one aggregate.
IDLE_RESOURCE_TYPES = ["ebs_volume", "snapshot", "stopped_ec2", "load_balancer"]

# Compute Savings Plan and Reserved Instance savings are in no sweep total.
# analyze_commitments runs in the sweep, so a coverage read that fails is named
# beside the total, but it returns a CommitmentAnalysis, which normalise does
# not read, and that is deliberate: a plan discounts the same instance hours
# the per-resource rows already claim (a Graviton move, Spot, a stop, a
# schedule), so adding it would count those dollars twice. Nothing said so,
# and an account whose largest saving is a plan got a total with no word on
# where that saving is reported. Every caller prints this beside its total.
# (A Database Savings Plan is a row of its own, netted of idle RDS.)
COMMITMENTS_NOT_IN_TOTAL = (
    "Compute Savings Plan and Reserved Instance savings are not part of this total: "
    "get_commitment_analysis reports them separately, because a plan discounts the "
    "same instance hours these per-resource savings already count.")


# cleanup.idle files no evidence class of its own. Its rows are the claims
# run_deep_audit classifies in analyzers.waste_evidence, so each takes that
# class: an old snapshot priced at its full volume size is an inference there
# (old_unmanaged_snapshot), and so it is here. A type not listed (a stopped
# instance's attached storage) is a measured state.
_IDLE_EVIDENCE_CLASS = {
    "ebs_volume": "unattached_ebs_volume",
    "snapshot": "old_unmanaged_snapshot",
    "load_balancer": "idle_load_balancer",
}


def _idle_is_inferred(resource_type: str) -> bool:
    from ..analyzers.waste_evidence import INFERRED, spec_for

    cls = _IDLE_EVIDENCE_CLASS.get(resource_type)
    return cls is not None and spec_for(cls).evidence == INFERRED


def _filed_as_inference(data: Any) -> bool:
    """Whether the scanner's own envelope (envelope.Finding, on its result or
    on its rows) filed what it found as an investigation: an inference it
    could not measure. A scanner that says nothing counts as measured, as a
    finding with no classification does in `nable scan`."""
    if isinstance(data, dict):
        env = data.get("finding")
        return isinstance(env, dict) and env.get("kind") == INVESTIGATION
    if isinstance(data, list):
        return any(isinstance(r, dict) and isinstance(r.get("finding"), dict)
                   and r["finding"].get("kind") == INVESTIGATION for r in data)
    return False


def split_total(findings: list[dict]) -> tuple[float, float, int]:
    """(recoverable, to confirm, how many to confirm), per month.

    The same split `nable scan` makes (cli_scan._recoverable_split):
    recoverable is the rows measured, and the rows their scanner filed as an
    inference are "to confirm", shown beside it and not added in. A row with
    no figure (an investigation, a retracted claim) is in neither."""
    recoverable = sum(f.get("monthly_savings") or 0 for f in findings
                      if not f.get("to_confirm"))
    confirm = [f for f in findings
               if f.get("to_confirm") and f.get("monthly_savings") is not None]
    return recoverable, sum(f["monthly_savings"] for f in confirm), len(confirm)


def to_confirm_line(to_confirm: float, n: int) -> str:
    """The sentence every caller prints beside its recoverable total."""
    return (f"${to_confirm:,.2f}/mo to confirm, not counted as recoverable: {n} "
            f"finding{'s' if n != 1 else ''} rest{'s' if n == 1 else ''} on an inference "
            "its check could not measure (marked \"to confirm\").")


@dataclass
class SweepResult:
    """What every caller gets: findings, and an honest account of what failed."""

    findings: list[dict] = field(default_factory=list)
    # Scanners that could not run: they raised, said they could not read, or
    # found nothing with every read failed. None of their findings are in.
    errors: list[str] = field(default_factory=list)
    learned_note: str | None = None
    timed_out: bool = False
    # Why each scanner in `errors` could not run, and the scanners that ran
    # with some of their reads failed (so their findings may be incomplete).
    error_reasons: dict[str, str] = field(default_factory=dict)
    partial: dict[str, str] = field(default_factory=dict)
    scanners_run: int = 0

    def coverage_lines(self) -> list[str]:
        """What a report must say about the checks that did not fully run."""
        lines: list[str] = []
        if self.errors:
            lines.append(
                f"{len(self.errors)} of {self.scanners_run} checks could not run, so "
                "their savings are not in this report: "
                + "; ".join(f"{n} ({self.error_reasons.get(n, 'no result')})"
                            for n in self.errors))
        if self.partial:
            lines.append(
                "Checks that ran with some reads failed, so they may be missing "
                "findings: " + "; ".join(f"{n} ({why})" for n, why in self.partial.items()))
        return lines


def _call(name, fn, **kwargs):
    """Run one scanner. Returns (name, result, error, reads): the AWS reads it
    made are recorded, because most scanners swallow a failed region and return
    an empty result that is otherwise indistinguishable from a clean one."""
    from ..aws_read_failures import carry_into_default_executor, watching

    async def _on_this_loop():
        carry_into_default_executor(asyncio.get_running_loop())
        return await fn(**kwargs)

    with watching() as reads:
        try:
            res = asyncio.run(_on_this_loop()) if asyncio.iscoroutinefunction(fn) else fn(**kwargs)
            return name, res, None, reads
        except Exception as exc:
            log.warning("audit scanner %s failed: %s", name, exc)
            return name, None, f"{type(exc).__name__}: {exc}", reads


def _declared_failure(data) -> str | None:
    """A scanner result that says itself that it could not run."""
    if isinstance(data, dict):
        if data.get("error"):
            return str(data["error"])
        if data.get("data_incomplete"):
            return "data incomplete"
        if data.get("status") == "not_evaluated":
            return str(data.get("message") or "not evaluated")
    recs = getattr(data, "recommendations", None)
    if isinstance(recs, list) and any(
            isinstance(r, dict) and r.get("type") == "coverage_unavailable" for r in recs):
        return "commitment coverage could not be read"
    return None


def build_specs(aws: Any, regions: list[str] | None) -> list[tuple[str, Any, dict]]:
    """The scanner table: (name, callable, kwargs). Imports are local so a tool
    that never sweeps does not pay for 21 modules at import time."""
    from .graviton import scan_graviton_opportunities
    from .public_ipv4 import audit_public_ipv4
    from .lambda_concurrency import scan_lambda_concurrency_waste as _lc
    from .s3_bucket_keys import scan_s3_bucket_key_opportunities as _s3bk
    from .nonprod_scheduler import identify_nonprod_resources
    from .rds_snapshots import audit_rds_manual_snapshots as _rds_snap
    from .spot_adoption import recommend_spot_adoption as _spot
    from .cloudwatch_cardinality import audit_cloudwatch_metric_cardinality as _cw_card
    from .cloudwatch_alarms import audit_cloudwatch_orphaned_alarms as _cw_alarms
    from .cloudwatch_logs_ia import audit_cloudwatch_logs_ia_opportunities as _cw_logs
    from .lambda_snapstart import recommend_lambda_snapstart as _snapstart
    from .nlb_cross_zone import audit_nlb_cross_zone_costs as _nlb
    from .s3_intelligent_tiering import audit_s3_intelligent_tiering as _s3it
    from .s3_transfer_acceleration import audit_s3_transfer_acceleration as _s3ta
    from .ebs_snapshot_replication import audit_ebs_snapshot_replication as _ebs_rep
    from .database_savings_plans import recommend_database_savings_plans as _dbsp
    from .textract_env import scan_textract_environment_waste as _textract
    from .bedrock_routing import recommend_bedrock_model_routing as _bedrock
    from .commitments import analyze_commitments as _commitments
    from ..cleanup.idle import scan_idle_resources as _idle_resources
    from ..analyzers.waste import scan_all_regions_rds_idle as _scan_all_regions_rds_idle

    return [
        ("graviton",       scan_graviton_opportunities, dict(aws_client=aws, regions=regions)),
        ("ipv4",           audit_public_ipv4,           dict(aws_client=aws, regions=regions)),
        ("lambda_pc",      _lc,                         dict(aws_client=aws, regions=regions)),
        ("s3_bucket_keys", _s3bk,                       dict(aws_client=aws)),
        ("nonprod",        identify_nonprod_resources,  dict(aws_client=aws, regions=regions)),
        ("rds_snapshots",  _rds_snap,                   dict(aws_client=aws, regions=regions)),
        ("spot",           _spot,                       dict(regions=regions)),
        ("cw_cardinality", _cw_card,                    dict(aws_client=aws, regions=regions)),
        ("cw_alarms",      _cw_alarms,                  dict(aws_client=aws, regions=regions)),
        ("cw_logs_ia",     _cw_logs,                    dict(aws_client=aws, regions=regions)),
        ("snapstart",      _snapstart,                  dict(aws_client=aws, regions=regions)),
        ("nlb",            _nlb,                         dict(aws_client=aws, regions=regions)),
        ("s3_it",          _s3it,                       dict(aws_client=aws)),
        ("s3_ta",          _s3ta,                       dict(aws_client=aws)),
        ("ebs_rep",        _ebs_rep,                    dict(aws_client=aws, regions=regions)),
        ("db_sp",          _dbsp,                       dict()),
        ("textract",       _textract,                   dict()),
        ("bedrock",        _bedrock,                    dict()),
        ("commitments",    _commitments,                dict()),
        # Every type but elastic_ip: unattached Elastic IPs are already in the
        # ipv4 aggregate above, and scanning them here too counted each one
        # twice in the sweep total.
        ("idle_resources", _idle_resources,             dict(regions=regions, resource_types=IDLE_RESOURCE_TYPES)),
        ("idle_rds",       _scan_all_regions_rds_idle,  dict(regions=regions)),
    ]


def _region_note(region: Any) -> str:
    """"; priced at us-east-1 list rates ..." for a row outside us-east-1 that
    was priced from nable's us-east-1 tables, else "" (aws_prices)."""
    from ..aws_prices import us_east_1_list_note

    note = us_east_1_list_note(region if isinstance(region, str) else None)
    return f"; {note}" if note else ""


def _listed(region: Any, basis: str | None = None) -> dict:
    """{"us_east_1_list": True} for a row outside us-east-1 priced at us-east-1
    list rates, so the tables can mark the row; a basis naming the region's
    floor (aws_prices.ec2_hourly_in_region) was priced in the region."""
    if not _region_note(region) or (basis and "floor" in basis):
        return {}
    return {"us_east_1_list": True}


def normalise(name, data) -> list[dict]:
    if data is None:
        return []
    out = []
    try:
        if name == "graviton" and isinstance(data, list):
            for r in data:
                s = r.get("savings_estimate", 0) or 0
                if s > 0:
                    # current cost rides along so the critique can hold the
                    # claim against it (a saving cannot exceed the resource).
                    # A gap between two types' rates: a regional floor on each
                    # side does not bound it, so it stays at us-east-1 list.
                    out.append({"title": f"Migrate {r.get('instance_id','?')} ({r.get('instance_type','?')} → {r.get('graviton_equivalent','?')})", "monthly_savings": s, "category": "Compute", "detail": f"{r.get('savings_pct',0):.0f}% saving, {r.get('region','')}{_region_note(r.get('region'))}", "resource_id": r.get("instance_id", ""), "region": r.get("region", ""), "current_monthly_cost_usd": r.get("current_monthly_cost_estimate"), **_listed(r.get("region"))})
        elif name == "ipv4":
            waste = data.get("total_monthly_waste", 0) or 0
            if waste > 0:
                # The total covers unattached EIPs AND EIPs held by stopped
                # instances, so the title counts both: "Release 2" over a
                # figure for 5 addresses read as $9.13 per IP.
                n_unattached = len(data.get("unattached_eips", []))
                n_stopped = len(data.get("stopped_instance_eips", []))
                parts = [f"{n_unattached} unattached"] if n_unattached else []
                if n_stopped:
                    parts.append(f"{n_stopped} on stopped instances")
                away = sorted({e["region"] for e in data.get("unattached_eips", [])
                               + data.get("stopped_instance_eips", [])
                               if isinstance(e, dict) and _region_note(e.get("region"))})
                note = (f"; {', '.join(away)} priced at the us-east-1 list rate"
                        if away else "")
                out.append({"title": f"Release {n_unattached + n_stopped} idle Elastic IP(s) ({', '.join(parts)})", "monthly_savings": waste, "category": "Network", "detail": f"${waste:.2f}/mo, ${PUBLIC_IPV4_PER_MONTH:.2f} per IP{note}", **({"us_east_1_list": True} if away else {})})
        elif name == "lambda_pc" and isinstance(data, list):
            for r in data:
                s = r.get("wasted_monthly_cost", 0) or 0
                if s > 0:
                    out.append({"title": f"Reduce provisioned concurrency on {r.get('function_name','?')}", "monthly_savings": s, "category": "Compute", "resource_id": r.get("function_name", ""), "detail": f"{r.get('avg_utilization_pct',0)*100:.0f}% utilization"})
        elif name == "s3_bucket_keys" and isinstance(data, list):
            for r in data:
                s = r.get("estimated_savings", 0) or 0
                if s > 0:
                    out.append({"title": f"Enable S3 Bucket Key on {r.get('bucket_name','?')}", "monthly_savings": s, "category": "Storage", "resource_id": r.get("bucket_name", ""), "detail": "Up to 99% KMS cost reduction"})
        elif name == "nonprod":
            items = data.get("schedulable_instances", []) if isinstance(data, dict) else []
            for r in items:
                s = r.get("potential_monthly_savings", 0) or 0
                if s > 0:
                    region = r.get("region") or ""
                    where = (f", {region}; on-demand at the {r['price_basis']}"
                             if _region_note(region) and r.get("price_basis") else "")
                    out.append({"title": f"Schedule non-prod instance {r.get('name', r.get('instance_id','?'))}", "monthly_savings": s, "category": "Compute", "resource_id": r.get("instance_id", ""), "region": region, "detail": f"env={r.get('environment','?')}, {r.get('idle_hours_per_week',0):.0f} idle hrs/wk{where}", **_listed(region, r.get("price_basis"))})
        elif name == "rds_snapshots":
            items = data.get("orphaned_snapshots", []) + data.get("old_snapshots", []) if isinstance(data, dict) else []
            total = data.get("potential_monthly_savings", 0) if isinstance(data, dict) else 0
            if total > 0:
                # The scanner prices each snapshot at its database's allocated
                # storage and calls the figure an upper bound; the row says so.
                out.append({"title": f"Delete {len(items)} old/orphaned RDS manual snapshots", "monthly_savings": total, "category": "Storage", "detail": f"${total:.2f}/mo at $0.095/GB-month of allocated storage, an upper bound"})
        elif name == "spot" and isinstance(data, list):
            for r in data:
                s = r.get("monthly_savings", 0) or 0
                if s > 0 and r.get("recommendation") == "RECOMMENDED":
                    region = r.get("region") or ""
                    where = (f", {region}; on-demand at the {r['ondemand_basis']}"
                             if _region_note(region) and r.get("ondemand_basis") else "")
                    out.append({"title": f"Convert {r.get('instance_id','?')} ({r.get('instance_type','?')}) to Spot", "monthly_savings": s, "category": "Compute", "resource_id": r.get("instance_id", ""), "region": region, "detail": f"{r.get('savings_pct',0):.0f}% saving{where}", **_listed(region, r.get("ondemand_basis"))})
        elif name == "cw_cardinality":
            # The scanner returns a dict; reading only a list dropped every
            # finding it made from the sweep.
            items = data.get("high_cardinality_namespaces", []) if isinstance(data, dict) else data
            for r in items if isinstance(items, list) else []:
                s = r.get("estimated_monthly_cost", 0) or 0
                if s > 0:
                    # Never a figure in a total. What a namespace's metrics cost is
                    # a list-price upper bound on a bill, not a saving: what a
                    # cardinality cut saves depends on which dimensions are
                    # dropped, which nothing here has measured (the scanner files
                    # it as an INFERRED investigation). It stays listed, with a
                    # size band, as the investigation it is.
                    out.append({"title": f"Review CloudWatch metric cardinality in {r.get('namespace','?')}", "monthly_savings": None, "kind": INVESTIGATION, "magnitude": magnitude_band(s), "rough_monthly": s, "category": "Observability", "detail": f"{r.get('metric_count',0)} metrics; ${s:,.2f}/mo of metric list cost at stake, an upper bound and not a saving"})
        elif name == "cw_alarms":
            items = data.get("orphaned_alarms", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            total = sum(r.get("monthly_cost", 0) for r in items)
            if total > 0:
                out.append({"title": f"Delete {len(items)} orphaned CloudWatch alarm(s)", "monthly_savings": total, "category": "Observability", "detail": f"${total:.2f}/mo"})
        elif name == "cw_logs_ia":
            # Same as cw_cardinality: the scanner returns a dict of candidates.
            items = data.get("candidates", []) if isinstance(data, dict) else data
            items = items if isinstance(items, list) else []
            total = sum(r.get("monthly_savings", 0) or 0 for r in items)
            if total > 0:
                out.append({"title": f"Move {len(items)} log group(s) to Infrequent Access", "monthly_savings": total, "category": "Observability", "detail": "50% ingestion cost reduction"})
        elif name == "snapstart" and isinstance(data, list):
            total = sum(r.get("monthly_pc_cost", 0) for r in data if r.get("recommendation") == "ENABLE_SNAPSTART_REPLACE_PC")
            if total > 0:
                out.append({"title": f"Enable Lambda SnapStart on {len([r for r in data if r.get('recommendation')=='ENABLE_SNAPSTART_REPLACE_PC'])} Java function(s)", "monthly_savings": total, "category": "Compute", "detail": "Replaces provisioned concurrency for free"})
        elif name == "nlb" and isinstance(data, list):
            for r in data:
                s = r.get("estimated_cross_az_cost", 0) or 0
                if s > 10:
                    out.append({"title": f"Disable cross-zone on NLB {r.get('nlb_name','?')}", "monthly_savings": s, "category": "Network", "resource_id": r.get("nlb_name", ""), "detail": f"${s:.2f}/mo cross-AZ charges"})
        elif name == "s3_it" and isinstance(data, list):
            waste = [r for r in data if isinstance(r.get("recommendation"), str) and r["recommendation"].startswith("LIKELY_WASTE")]
            total = sum((r.get("net_monthly_cost") or 0) for r in waste)
            if total > 0:
                out.append({"title": f"Disable S3 Intelligent-Tiering on {len(waste)} bucket(s) with small objects", "monthly_savings": total, "category": "Storage", "detail": "Monitoring fee exceeds tiering savings"})
        elif name == "s3_ta":
            items = data.get("findings", data) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            waste = [r for r in items if r.get("likely_waste")]
            total = sum(r.get("monthly_ta_cost", 0) for r in waste)
            if total > 0:
                out.append({"title": f"Disable S3 Transfer Acceleration on {len(waste)} bucket(s)", "monthly_savings": total, "category": "Storage", "detail": f"${total:.2f}/mo surcharge"})
        elif name == "ebs_rep":
            total = data.get("potential_monthly_savings", 0) if isinstance(data, dict) else 0
            sets = [f for f in (data.get("cross_region_findings") or [])
                    if f.get("orphaned") or f.get("excess_copies") or f.get("has_old_copies")
                    ] if isinstance(data, dict) else []
            if total > 0:
                # Never a figure in a total. The scanner bills incremental snapshots
                # at the full volume size and files the result as an INFERRED upper
                # bound, and the copies it counts (orphaned ones above all) are the
                # same snapshots idle_resources prices one by one: a 100 GB volume's
                # two orphaned copies were $10 here on top of the $10 already there.
                # It stays listed, with a size band, as the investigation it is.
                out.append({"title": f"Review {len(sets)} cross-region EBS snapshot set(s) for stale copies", "monthly_savings": None, "kind": INVESTIGATION, "magnitude": magnitude_band(total), "rough_monthly": total, "category": "Storage", "detail": "Upper bound at full volume size; the same snapshots may already be listed as idle snapshots"})
        elif name == "db_sp":
            s = data.get("estimated_monthly_savings", 0) if isinstance(data, dict) else 0
            if s > 0:
                out.append({"title": "Purchase Database Savings Plan for RDS/Aurora", "monthly_savings": s, "category": "Commitments", "detail": f"20% off eligible instance hours, ${s:.2f}/mo saving"})
        elif name == "textract":
            waste = data.get("estimated_monthly_waste", 0) if isinstance(data, dict) else 0
            callers = data.get("non_prod_callers", []) if isinstance(data, dict) else []
            if waste > 0:
                out.append({"title": f"Disable Textract in non-prod ({len(callers)} caller(s))", "monthly_savings": waste, "category": "AI/ML", "detail": f"${waste:.2f}/mo from QA/staging environments"})
        elif name == "bedrock":
            opps = data.get("routing_opportunities", []) if isinstance(data, dict) else []
            total = data.get("total_monthly_savings", 0) if isinstance(data, dict) else 0
            if total > 0:
                models = [o.get("current_model", "?") for o in opps[:2]]
                out.append({"title": f"Route Bedrock tasks to cheaper models ({', '.join(models)})", "monthly_savings": total, "category": "AI/ML", "detail": f"Short tasks to Haiku, ${total:.2f}/mo saving"})
        elif name == "commitments":
            s = data.get("estimated_monthly_savings", 0) if isinstance(data, dict) else 0
            coverage = data.get("current_coverage_pct", 0) if isinstance(data, dict) else 0
            if s > 0 and coverage < 80:
                out.append({"title": f"Purchase Savings Plans / Reserved Instances ({coverage:.0f}% covered)", "monthly_savings": s, "category": "Commitments", "detail": f"${s:.2f}/mo saving at current spend"})
        elif name == "idle_resources" and isinstance(data, list):
            for r in data:
                if getattr(r, "protected", False) or r.monthly_cost_usd <= 0:
                    continue
                # savings == the resource's own cost by construction (delete
                # it, stop paying it); carrying the cost makes that invariant
                # checkable by the critique instead of assumed.
                out.append({"title": f"{r.resource_type.replace('_', ' ').title()}: {r.name or r.resource_id}", "monthly_savings": r.monthly_cost_usd, "category": "Idle/Orphaned", "detail": f"{r.reason}, {r.idle_text}, {r.region}{_region_note(r.region)}", "resource_id": r.resource_id, "region": r.region, "current_monthly_cost_usd": r.monthly_cost_usd, **_listed(r.region),
                            **({"to_confirm": True} if _idle_is_inferred(r.resource_type) else {})})
        elif name == "idle_rds" and isinstance(data, list):
            for r in data:
                s = r.get("estimated_monthly_savings", 0) or 0
                if s > 0:
                    # lookback_days matches check_rds_idle's connection window;
                    # cost equals the saving by construction (stopped == unpaid).
                    out.append({"title": f"Stop or delete idle RDS instance {r.get('resource_id','?')}", "monthly_savings": s, "category": "Database", "detail": f"{r.get('engine','?')} {r.get('current_class','?')}, {r.get('region','?')}, no connections in 14d{_region_note(r.get('region'))}", "resource_id": r.get("resource_id", ""), "region": r.get("region", ""), "current_monthly_cost_usd": s, "lookback_days": 14, **_listed(r.get("region"))})
    except Exception as exc:
        log.warning("audit norm failed for %s: %s", name, exc)
    return out


# Map each scanner to the ledger `source` the learning loop keys on, so the
# sweep can rank by what THIS account actually acts on, not just raw dollars.
# A scanner with no ledger source (or a cold one) simply keeps dollar order.
SOURCE_MAP = {
    "graviton": "graviton", "idle_resources": "idle", "commitments": "commitment",
    "spot": "spot", "db_sp": "commitment",
}


def collapse_per_resource(findings: list[dict]) -> tuple[list[dict], int]:
    """Keep one finding per resource: the largest saving, alternatives attached.

    Findings with no resource_id are aggregates ("delete 12 orphaned alarms") and
    cannot double count a single resource, so they pass through untouched.

    Returns (kept, n_collapsed). n_collapsed is how many claims were folded away,
    which the caller reports rather than silently dropping: a total that quietly
    shrank is as hard to trust as one that was quietly inflated.
    """
    best: dict[str, dict] = {}
    passthrough: list[dict] = []
    order: list[str] = []

    for f in findings:
        rid = (f.get("resource_id") or "").strip()
        if not rid:
            passthrough.append(f)
            continue
        cur = best.get(rid)
        if cur is None:
            best[rid] = dict(f)
            order.append(rid)
            continue
        loser, winner = (cur, f) if (f.get("monthly_savings") or 0) > (cur.get("monthly_savings") or 0) else (f, cur)
        merged = dict(winner)
        alts = list(merged.get("alternatives") or []) + list(loser.get("alternatives") or [])
        alts.append({"title": loser.get("title", ""),
                     "monthly_savings": loser.get("monthly_savings")})
        merged["alternatives"] = alts
        best[rid] = merged

    collapsed = sum(len(b.get("alternatives") or []) for b in best.values())

    # Order is not ours to choose. By the time findings reach here the learning
    # rescorer has already ranked them by what this customer's ledger says gets
    # acted on and turns out accurate, which is worth more than sorting by the
    # biggest claim. Re-sorting on monthly_savings threw that away and put the
    # suppressed source back on top. So: rebuild in the incoming order, with each
    # resource represented once, at the position its best-ranked finding held.
    seen: set[str] = set()
    kept: list[dict] = []
    for f in findings:
        rid = (f.get("resource_id") or "").strip()
        if not rid:
            kept.append(f)
            continue
        if rid in seen:
            continue
        seen.add(rid)
        kept.append(best[rid])
    return kept, collapsed


def _net_database_plan_of_idle_rds(findings: list[dict], db_sp: Any,
                                   idle_rows: Any = None) -> None:
    """Take the idle-RDS overlap out of the Database Savings Plan saving, in place.

    The plan's saving is its discount on the uncovered RDS spend Cost Explorer
    reported on the families a plan covers, and that spend includes the
    instances idle_rds says to stop. Both cannot be banked in full: 20% off
    $10,000/mo is $2,000, stopping a $2,000/mo idle instance is $2,000, and done
    together the plan covers $8,000 for $1,600, $3,600 in all, not $4,000. The
    plan is reduced by its discount on the idle spend's uncovered share; the
    idle findings, which are per resource and certain, keep their full figure.

    The plan is sized only on DATABASE_SP_ELIGIBLE_FAMILIES, so an idle
    instance on another family (db.r5, db.t3) was never in it and stopping it
    takes nothing from it. An instance whose class cannot be read counts, so
    the plan is never left above what it can save.
    """
    from .database_savings_plans import DATABASE_SP_DISCOUNT_1YR_NO_UPFRONT as discount
    from .database_savings_plans import DATABASE_SP_ELIGIBLE_FAMILIES, _family

    classes = {(r.get("resource_id"), r.get("region")): r.get("current_class") or ""
               for r in (idle_rows if isinstance(idle_rows, list) else [])
               if isinstance(r, dict)}

    def _in_plan(f: dict) -> bool:
        family = _family(classes.get((f.get("resource_id"), f.get("region")), ""))
        return not family or family in DATABASE_SP_ELIGIBLE_FAMILIES

    plan = next((f for f in findings if f.get("_scanner") == "db_sp"), None)
    idle = sum(f.get("monthly_savings") or 0 for f in findings
               if f.get("_scanner") == "idle_rds" and _in_plan(f))
    if not plan or not idle or not isinstance(db_sp, dict):
        return
    uncovered_share = max(0.0, 1.0 - float(db_sp.get("current_sp_coverage_pct") or 0.0) / 100)
    overlap = min(plan.get("monthly_savings") or 0, discount * idle * uncovered_share)
    if overlap <= 0:
        return
    plan["monthly_savings"] = round((plan.get("monthly_savings") or 0) - overlap, 2)
    plan["detail"] = (f"{plan.get('detail', '')}; less ${overlap:,.2f}/mo for idle RDS "
                      "instances listed separately, which the plan would otherwise cover")


async def sweep(
    aws: Any,
    regions: list[str] | None = None,
    *,
    deadline_s: int | None = None,
) -> SweepResult:
    """Run every scanner, normalise, critique, and rank. Never raises.

    Each scanner makes blocking boto3 calls. Gathered as bare coroutines they
    share one event loop and run back-to-back, so the sweep would take the SUM of
    every scanner's time. Each runs in its own thread instead, so the sweep is
    bounded by the SLOWEST scanner, not their sum (measured ~5x on a real
    account). fn may be sync or async; an async one runs on a fresh loop in its
    thread, which is safe because no scanner shares a main-loop asyncio primitive
    (the cost cache uses a threading.Lock).

    The result is NOT collapsed. Callers slice to their own top-N first, then
    call collapse_per_resource, because collapsing before slicing would let a
    resource's alternatives push real findings off the end of the list.
    """
    specs = build_specs(aws, regions)
    if deadline_s is None:
        deadline_s = int(os.getenv("FINOPS_AUDIT_TIMEOUT", str(DEFAULT_DEADLINE_S)))

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[asyncio.to_thread(_call, n, fn, **kw) for n, fn, kw in specs]),
            timeout=deadline_s,
        )
    except asyncio.TimeoutError:
        log.warning("cost sweep hit the %ss deadline; returning early", deadline_s)
        return SweepResult(timed_out=True)

    out = SweepResult(scanners_run=len(results))
    raw: dict[str, Any] = {}
    for name, data, err, reads in results:
        found = normalise(name, data) if data is not None else []
        why = err or _declared_failure(data)
        if why is None and not found and reads.nothing_read:
            # Nothing found, and every read it made failed: it did not look.
            why = reads.sentence()
        if why is not None and not found:
            out.errors.append(name)
            out.error_reasons[name] = why
            continue
        # It ran but did not read everything, so its findings may be short: a
        # region the scanner dropped (the idle scan drops a region that raised),
        # a failure it declared beside the findings it did make, or AWS reads
        # that failed. Say which, beside the total.
        gaps: list[str] = []
        not_read = getattr(data, "regions_not_read", None)
        if not_read:
            gaps.append(f"could not read {', '.join(sorted(not_read))}")
        if why is not None:
            gaps.append(why)
        if reads.failed:
            gaps.append(reads.sentence())
        if gaps:
            out.partial[name] = "; ".join(gaps)
        raw[name] = data
        inferred = _filed_as_inference(data)
        for f in found:
            f["source"] = SOURCE_MAP.get(name, name)
            f["_scanner"] = name
            if inferred and f.get("monthly_savings") is not None:
                # Its scanner filed it as an inference: a figure to confirm,
                # not a recoverable one (split_total).
                f["to_confirm"] = True
            out.findings.append(f)

    _net_database_plan_of_idle_rds(out.findings, raw.get("db_sp"), raw.get("idle_rds"))
    for f in out.findings:
        f.pop("_scanner", None)

    # Sort by monthly savings descending first (the stable base order). `or 0`:
    # an investigation carries None, which a bare comparison cannot order.
    out.findings.sort(key=lambda x: x.get("monthly_savings") or 0, reverse=True)

    # Critique first, then rank. A finding whose claim was just retracted must not
    # be ranked on the dollar figure it lost, so this has to run BEFORE rescore.
    # Deterministic falsifiers only here (no network, no LLM unless opted in), so
    # a free sweep stays free. Never drops a finding: the worst case is a
    # downgrade to an investigation with a magnitude band instead of a figure.
    try:
        from .critique import critique
        out.findings = critique(out.findings, savings_key="monthly_savings")
    except Exception as exc:
        log.debug("critique skipped in sweep: %s", exc)

    # Learning loop: reorder by a learned score (savings x this account's
    # confidence in the source). Propose-only: nothing is hidden and spend numbers
    # are untouched; a cold ledger leaves the dollar order intact. Suppressed
    # sources sink to the bottom rather than being removed.
    try:
        from .learning import customer_signal, rescore
        sig = customer_signal()
        rs = rescore(out.findings, sig, savings_key="monthly_savings", source_key="source")
        out.findings = rs["ranked"] + rs["suppressed_for_you"]
        if any(s.get("coverage") != "COLD" for s in sig.get("by_source", [])):
            out.learned_note = (
                "Ranked using your ledger (act-rate and accuracy per source), "
                "propose-only. Call get_recommendation_learning() for the why."
            )
    except Exception as exc:
        log.debug("learning rescore skipped in sweep: %s", exc)

    return out
