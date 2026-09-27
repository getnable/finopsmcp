# SPDX-License-Identifier: Apache-2.0
"""Org model MCP tools: read the model, and propose facts to it.

Read and propose only. There is no confirm or reject tool, on purpose: a fact
becomes true for this org when a person says so (`nable org confirm` in their
terminal, or a merged PR), never because an agent called a tool. Every
proposal comes back with the exact command a human runs to confirm it.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from ..server import mcp

_MAX_FACTS = 200

# The schema an agent sees. "threshold" is listed so that proposing one
# reaches the refusal that says who sets it, not a bare validation error.
FactKind = Literal["owner", "team", "environment", "tag_key", "tag_alias", "account",
                   "threshold"]
SubjectKind = Literal["aws_account", "gcp_project", "azure_subscription", "k8s_namespace",
                      "repo_path", "service", "tag_value", "resource", "team", "org",
                      "environment"]


@mcp.tool()
def get_org_model(kind: str | None = None, status: str | None = None) -> dict:
    """
    The org model: who owns what (owners, teams, environments, tag keys and
    aliases, accounts, thresholds), each fact with its status (proposed,
    confirmed, rejected, expired), source and review key.

    Only confirmed facts are the org's word; proposed ones are guesses waiting
    for a person.

    Args:
        kind: Optional filter: owner | team | environment | tag_key | tag_alias | account | threshold.
        status: Optional filter: proposed | confirmed | rejected | expired.

    Examples:
        - "Who owns our AWS accounts?"
        - "What does nable know about our teams?"
    """
    from .. import org
    m = org.load()
    facts = [f for f in m.facts
             if (kind is None or f.fact == kind) and (status is None or f.status == status)]
    facts.sort(key=lambda f: (f.status != "confirmed", f.fact, str(f.subject)))
    out: dict[str, Any] = {
        "dir": str(m.dir), "dir_source": m.dir_source,
        "counts": m.status_counts(),
        "facts": [f.summary() for f in facts[:_MAX_FACTS]],
        "conflicts": len(m.conflicts()), "stale": len(m.stale()),
    }
    if len(facts) > _MAX_FACTS:
        out["truncated"] = f"{len(facts) - _MAX_FACTS} more; filter by kind or status"
    if m.warnings:
        out["warnings"] = m.warnings[:20]
    return out


@mcp.tool()
def get_org_coverage() -> dict:
    """
    How much of the latest month's spend has a confirmed owner, a proposed
    one, or none, by team, with the largest unowned accounts. Says "not read"
    when there is no cost history, rather than a percentage.

    Examples:
        - "How much of our spend has an owner?"
    """
    from .. import org
    return org.coverage()


@mcp.tool()
def list_org_questions(limit: int = 10) -> dict:
    """
    The few questions that would map the most spend to an owner, most dollars
    first, each with a default answer and the command a person runs to answer
    it. Ask the user these; do not answer them yourself.

    Examples:
        - "What does nable need to know about our org?"
    """
    from .. import org
    qs = org.questions(max(1, min(int(limit), 50)))
    return {"questions": [q.to_dict() for q in qs],
            "note": "Only a person confirms or rejects. Show them the question and "
                    "the command; do not run it for them."}


@mcp.tool()
def propose_org_fact(
    fact: Annotated[FactKind, Field(description="What kind of fact. threshold is refused: "
                                                "a person sets thresholds.")],
    subject_kind: Annotated[SubjectKind, Field(description="What the fact is about.")],
    subject_id: Annotated[str, Field(description='e.g. "123456789012", "infra/payments", '
                                                 '"payments" (team), "org" (tag_key).')],
    value: Annotated[dict, Field(description="Shape depends on fact: see the tool description.")],
    source: Annotated[str, Field(description='Where the evidence is, e.g. '
                                             '"codeowners:infra/payments/".')],
    confidence: Annotated[float, Field(ge=0, le=1, description="Your confidence.")] = 0.5,
    dollars_monthly: Annotated[float | None, Field(
        description="Spend this fact governs, if known; ranks the question.")] = None,
) -> dict:
    """
    Propose one fact about this org from evidence you found. Saved as
    proposed only; a person confirms it with the returned command. A rejected
    fact is not proposed again; a proposal never replaces a confirmed fact.

    value by fact:
      owner {team, channel?, people?}     subject: account, repo_path, k8s_namespace, ...
      team {name, aliases?, channel?, people?}      subject_kind team
      environment {env: prod|nonprod|dr|sandbox|shared|unknown}
      tag_key {canonical: team|environment|service|cost_center|owner, keys: [str]}  subject org:org
      tag_alias {canonical_key, canonical_value}    subject_kind tag_value
      account {name?, business_unit?, cost_center?} subject: an account
    Thresholds are not proposed: a person sets them (`nable org set threshold`).

    Examples:
        - "CODEOWNERS says infra/payments/ is @payments-team; propose that"
    """
    from .. import org
    if fact == "threshold":
        return {"result": "refused", "error": (
            "Thresholds decide what runs without asking, so a person sets them: "
            "`nable org set threshold --subject team:TEAM --max-auto-usd N` in their "
            "terminal. Tell the user the figure you would suggest and why.")}
    # Whatever an agent says, the source says an agent said it: provenance a
    # proposal carries into review ("human", "legacy:...", a real adapter's
    # name) cannot be claimed through this tool.
    src = (source or "").strip() or "unspecified"
    if not src.startswith("agent:"):
        src = f"agent:{src}"
    try:
        f = org.make_fact(fact, {"kind": subject_kind, "id": subject_id}, value,
                          source=src, confidence=confidence, dollars_monthly=dollars_monthly)
        result = org.propose(f)
    except (org.FactError, org.OrgError) as e:
        return {"error": str(e), "result": "invalid"}
    out: dict[str, Any] = {
        "result": result, "key": f.key, "status": "proposed",
        "fact": {**f.summary(), "status": "proposed"},
        "confirm_command": f"nable org confirm {f.key}",
        "reject_command": f"nable org reject {f.key}",
    }
    if result == "suppressed_rejected":
        out["status"] = "rejected"
        out["note"] = "A person rejected this fact before; it was not proposed again."
    elif result == "duplicate":
        same = org.load().find(f.key)
        out["status"] = same[0].status if same else "proposed"
        out["note"] = "Already in the model; nothing written."
    elif result == "conflict":
        out["note"] = ("Saved as a proposal beside a confirmed fact that says otherwise. "
                       "The confirmed fact keeps answering until a person confirms this one.")
    else:
        out["note"] = ("Saved as a proposal. Only a person confirms it: show them the "
                       "confirm_command to run in their terminal.")
    return out
