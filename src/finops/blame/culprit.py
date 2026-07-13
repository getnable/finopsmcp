"""Cost-to-code blame orchestrator.

find_cost_culprit(resource_id, tf_dir, ...) resolves a cloud resource to its
Terraform block, blames the sizing line to the commit that last changed it,
resolves that commit to its PR + author, drafts a propose-only revert diff, and
persists the finding to savings_recommendations (source="blame").

Propose-only: with dry_run=True (default) nothing is written to the repo and no PR
is opened; the tool only reports the culprit and the revert diff.
"""
from __future__ import annotations

import logging
from typing import Any

from ..tagging.tf_state import build_id_map, resolve_recommendation
from ..tagging.hcl_patcher import (
    find_resource_file,
    find_sizing_attr_line,
    generate_rightsizing_diff,
)
from .git_blame import blame_sizing_commit, previous_sizing_value, resolve_pr_for_commit

log = logging.getLogger(__name__)


def _provider_for(tf_resource_type: str) -> str:
    if tf_resource_type.startswith("aws_"):
        return "aws"
    if tf_resource_type.startswith("google_"):
        return "gcp"
    if tf_resource_type.startswith("azurerm_"):
        return "azure"
    return tf_resource_type.split("_", 1)[0] or "unknown"


def _price_delta(
    current_value: str, previous_value: str | None, provider: str
) -> tuple[float, str]:
    """Monthly $ the sizing bump is costing at the customer's EFFECTIVE rate — what
    they actually pay after RIs/Savings Plans/negotiated discounts — not on-demand
    list price. Returns (usd_per_month, basis) where basis is 'effective',
    'on-demand' (no private pricing detected or detection failed), or 'unpriced'
    (a size the price table doesn't cover, or no earlier size to compare).

    Reverting the bump saves this much per month; that is the finding's savings.
    """
    if provider != "aws" or not previous_value or previous_value == current_value:
        return 0.0, "unpriced"
    # Reuse the rightsizing engine's on-demand price table (per-hour x 730).
    from ..recommendations.rightsizing import _monthly_cost

    cur = _monthly_cost(current_value)
    prev = _monthly_cost(previous_value)
    if cur <= 0 or prev <= 0:
        return 0.0, "unpriced"  # a size not in the table; don't fabricate a number
    delta_ondemand = cur - prev
    if delta_ondemand <= 0:
        return 0.0, "no-increase"
    # Convert on-demand delta to the customer's actual effective rate.
    try:
        from ..recommendations.rate_detector import detect_effective_rates

        profile = detect_effective_rates()
        delta = profile.apply_to_public_price(delta_ondemand, service="Amazon EC2")
        basis = "effective" if profile.has_private_pricing else "on-demand"
    except Exception as exc:  # detection is best-effort; fall back to list price
        log.debug("effective-rate detection failed, using on-demand: %s", exc)
        delta, basis = delta_ondemand, "on-demand"
    return round(max(delta, 0.0), 2), basis


def _unresolved(stage: str, reason: str, **extra: Any) -> dict:
    return {"resolved": False, "stage": stage, "reason": reason, **extra}


def _persist_finding(*, provider, resource_id, rtype, rname, current_value,
                     previous_value, commit, pr, account_id, region,
                     estimated_savings, price_basis) -> int | None:
    from ..recommendations.savings_tracker import record_recommendation

    recommended_config = {
        "tf_resource_type": rtype,
        "tf_resource_name": rname,
        "from_instance_type": current_value,   # the culprit (current) size
        "instance_type": previous_value,       # revert target (pre-change size)
        "culprit_commit": commit.sha,
        "culprit_author": commit.author,
        "culprit_summary": commit.summary,
        "culprit_authored_date": commit.authored_date,
        "culprit_pr": pr.get("number") if pr else None,
        "culprit_pr_url": pr.get("url") if pr else None,
        "price_basis": price_basis,
    }
    pr_label = f"PR #{pr['number']}" if pr and pr.get("number") else f"commit {commit.sha[:8]}"
    cost_note = ""
    if estimated_savings > 0:
        rate_word = "your effective rate" if price_basis == "effective" else "on-demand rates"
        cost_note = f" Costing ~${estimated_savings:,.0f}/mo at {rate_word}."
    description = (
        f"{rtype}.{rname} was resized to {current_value} by {pr_label} "
        f"({commit.summary!r}, {commit.author}). Revert restores "
        f"{previous_value or 'the previous size'}.{cost_note}"
    )
    return record_recommendation(
        source="blame",
        provider=provider,
        resource_id=resource_id,
        resource_type=rtype,
        resource_name=rname,
        current_config={"instance_type": current_value},
        recommended_config=recommended_config,
        description=description,
        estimated_monthly_savings_usd=estimated_savings,
        account_id=account_id,
        region=region,
    )


def find_cost_culprit(
    resource_id: str,
    tf_dir: str,
    resource_name: str | None = None,
    github_repo: str | None = None,
    dry_run: bool = True,
    persist: bool = True,
) -> dict:
    """Trace a resource's cost-driving sizing change to its commit/PR. On any
    broken hop returns {"resolved": False, "stage": ..., "reason": ...} so the
    caller can explain exactly where the chain stopped."""
    # Hop 1: resource id -> Terraform address (reuses the rightsizing resolver).
    try:
        id_map = build_id_map(tf_dir)
    except RuntimeError as exc:
        return _unresolved(
            "tfstate",
            f"Terraform state not available in {tf_dir} ({exc}). Run from your IaC "
            f"directory where terraform.tfstate or `terraform show -json` works.",
        )
    match = resolve_recommendation(tf_dir, resource_id, resource_name, id_map=id_map)
    if not match:
        return _unresolved(
            "resource",
            f"{resource_id} is not managed in this Terraform state. It may live in "
            f"another module/repo, or not be Terraform-managed.",
        )
    rtype, rname = match["tf_resource_type"], match["tf_resource_name"]

    # Hop 2: Terraform address -> .tf file. Missing => block deleted/moved.
    file_path = find_resource_file(tf_dir, rtype, rname)
    if not file_path:
        return _unresolved(
            "block",
            f"Resource {rtype}.{rname} is in state but its block is not in any .tf "
            f"file under {tf_dir} (deleted, renamed, or in an unscanned module).",
            tf_address=f"{rtype}.{rname}",
        )

    # Hop 3: locate the sizing literal line.
    loc = find_sizing_attr_line(file_path, rtype, rname)
    if loc is None:
        return _unresolved(
            "sizing_line",
            f"Could not find a literal sizing attribute for {rtype}.{rname} in "
            f"{file_path}. The size may be a variable or module output.",
            tf_address=f"{rtype}.{rname}", file_path=file_path,
        )
    line_no, current_value = loc

    # Hop 4: blame the sizing line -> commit.
    try:
        commit = blame_sizing_commit(tf_dir, file_path, line_no)
    except RuntimeError as exc:
        return _unresolved(
            "git",
            f"git blame failed in {tf_dir} (not a git repo, or {file_path} is "
            f"untracked): {exc}",
            file_path=file_path,
        )
    if commit is None:
        return _unresolved(
            "commit",
            f"The sizing line for {rtype}.{rname} is uncommitted (a working-tree "
            f"edit), so there is no commit to blame yet.",
            file_path=file_path, current_value=current_value,
        )

    # Hop 5: previous value (revert target) + commit -> PR.
    previous_value = previous_sizing_value(tf_dir, commit.sha, file_path, rtype, rname)
    pr = resolve_pr_for_commit(github_repo, commit.sha) if github_repo else None

    provider = _provider_for(rtype)

    # Hop 6: propose-only revert diff.
    revert_diff = None
    if previous_value and previous_value != current_value:
        revert_diff = generate_rightsizing_diff(file_path, rtype, rname, previous_value)

    # Price the bump at the customer's effective rate (what reverting saves).
    monthly_cost_added, price_basis = _price_delta(current_value, previous_value, provider)

    account_id = match.get("account_id", "") if isinstance(match, dict) else ""

    rec_id = None
    if persist:
        try:
            rec_id = _persist_finding(
                provider=provider, resource_id=resource_id, rtype=rtype, rname=rname,
                current_value=current_value, previous_value=previous_value,
                commit=commit, pr=pr, account_id=account_id, region="",
                estimated_savings=monthly_cost_added, price_basis=price_basis,
            )
        except Exception as exc:
            log.warning("could not persist blame finding: %s", exc)

    result: dict[str, Any] = {
        "resolved": True,
        "resource_id": resource_id,
        "tf_address": f"{rtype}.{rname}",
        "file_path": file_path,
        "sizing_line": line_no,
        "current_value": current_value,
        "previous_value": previous_value,
        "monthly_cost_added_usd": monthly_cost_added,
        "price_basis": price_basis,
        "commit": {
            "sha": commit.sha, "author": commit.author,
            "author_email": commit.author_email,
            "authored_date": commit.authored_date, "summary": commit.summary,
        },
        "pull_request": pr,
        "revert_available": revert_diff is not None,
        "revert_diff": revert_diff,
        "savings_recommendation_id": rec_id,
        "dry_run": dry_run,
        "propose_only": True,
    }
    if pr is None and github_repo:
        result["pr_note"] = (
            "No pull request resolved for this commit (no GITHUB_TOKEN, the commit "
            "predates PR history, or it was pushed directly)."
        )
    if previous_value is None:
        result["revert_note"] = (
            "This commit introduced the resource (no prior size), so there is no "
            "earlier value to revert to."
        )
    if price_basis == "unpriced" and previous_value:
        result["price_note"] = (
            "Size not in the price table, so the monthly cost of the bump is not "
            "estimated. The size change and culprit commit are still resolved."
        )

    if not dry_run:
        if not github_repo:
            result["pr_error"] = "github_repo is required to open a revert PR."
        elif revert_diff is None:
            result["pr_error"] = "No revert diff to open a PR for."
        else:
            result["revert_pr"] = _open_revert_pr(
                tf_dir=tf_dir, github_repo=github_repo, file_path=file_path,
                rtype=rtype, rname=rname, previous_value=previous_value,
                current_value=current_value, commit=commit,
            )
    return result


def _open_revert_pr(*, tf_dir, github_repo, file_path, rtype, rname, previous_value,
                    current_value, commit,
                    branch="fix/cost-culprit-revert", base_branch="main") -> dict:
    """Open a propose-only revert PR restoring the pre-change size. Only invoked
    with dry_run=False. Reuses the rightsizing git + PR plumbing. Opening a PR is a
    proposal, not an apply; nable never merges."""
    from ..remediation.rightsizing_pr import _git, _validate_git_ref
    from ..tagging.hcl_patcher import apply_rightsizing_fix
    from ..integrations.ticketing import create_github_pr

    _validate_git_ref(branch, "branch")
    _validate_git_ref(base_branch, "base_branch")
    if not apply_rightsizing_fix(file_path, rtype, rname, previous_value):
        return {"error": "Revert produced no change to the .tf file."}
    try:
        _git(tf_dir, "checkout", "-b", branch)
        _git(tf_dir, "add", "--", file_path)
        _git(tf_dir, "commit", "-m",
             f"revert(cost): restore {rtype}.{rname} to {previous_value}\n\n"
             f"Reverts the sizing change in {commit.sha} ({commit.summary}) that "
             f"raised cost.\n\nCo-Authored-By: nable FinOps MCP <noreply@getnable.com>")
        _git(tf_dir, "push", "-u", "origin", branch)
    except RuntimeError as exc:
        return {"error": f"Git operation failed: {exc}", "branch": branch}
    body = (
        f"## Cost-to-code revert\n\n"
        f"`{rtype}.{rname}` was resized from **{previous_value}** to **{current_value}** "
        f"in commit `{commit.sha[:8]}` ({commit.summary}) by {commit.author}. This PR "
        f"restores **{previous_value}**.\n\nReview the diff, then `terraform plan` "
        f"before applying. Proposed by nable; nothing is applied until you merge.\n\n"
        f"---\nGenerated by [nable FinOps MCP](https://getnable.com)"
    )
    try:
        pr_resp = create_github_pr(
            repo=github_repo,
            title=f"revert(cost): restore {rtype}.{rname} to {previous_value}",
            body=body, head=branch, base=base_branch,
        )
    except Exception as exc:
        return {"error": f"PR creation failed: {exc}", "branch": branch}
    return {"pr_url": pr_resp.get("html_url"), "branch": branch}
