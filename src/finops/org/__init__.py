# SPDX-License-Identifier: Apache-2.0
"""The org model: who owns what in this org, as plain YAML the customer owns.

Facts are proposed by adapters, MCP tools and inference, and confirmed or
rejected only by a human (the `nable org` CLI, or a merged PR that adds a
confirmed fact). They live in `nable.org/` (see store.py for where that is).

    from finops import org
    m = org.load()
    m.owner_of("aws_account:123456789012")   # Resolved | None
    org.team_for_tags({"Team": "pay"})       # through tag_key and tag_alias facts
    org.propose(org.make_fact("owner", "repo_path:infra/payments",
                              {"team": "payments"}, source="codeowners:infra/payments/"))
    org.run_adapters()                        # CODEOWNERS, Terraform, AWS Organizations,
                                              # tags, workload: proposals only

Importing this package costs the standard library only: PyYAML loads on the
first read, SQLAlchemy only inside coverage(). The guard hook imports it.
"""
from __future__ import annotations

from typing import Any

from .coverage import coverage
from .legacy import legacy_facts
from .model import (
    CANONICAL_TAG_KEYS,
    ENVIRONMENTS,
    FACT_KINDS,
    FILE_FOR_KIND,
    STATUSES,
    SUBJECT_KINDS,
    Fact,
    FactError,
    OrgModel,
    Resolved,
    Subject,
    fact_key,
    subject_of,
)
from .questions import Question, bulk_facts, questions
from .store import (
    ADAPTERS,
    ORG_DIR_NAME,
    AdapterRun,
    OrgError,
    confirm,
    confirm_many,
    export,
    git_root,
    import_legacy,
    load,
    make_fact,
    propose,
    propose_many,
    reject,
    reject_many,
    repo_path_of,
    resolve_dir,
    run_adapters,
    set_fact,
)

__all__ = [
    "ADAPTERS", "CANONICAL_TAG_KEYS", "ENVIRONMENTS", "FACT_KINDS", "FILE_FOR_KIND",
    "ORG_DIR_NAME", "STATUSES", "SUBJECT_KINDS", "AdapterRun", "Fact", "FactError", "OrgError",
    "OrgModel", "Question", "Resolved", "Subject", "bulk_facts", "confirm", "confirm_many",
    "coverage", "environment_of", "export", "fact_key", "git_root", "import_legacy",
    "legacy_facts", "load", "make_fact", "owner_of", "propose", "propose_many", "questions",
    "reject", "reject_many", "repo_path_of", "resolve_dir", "run_adapters", "set_fact",
    "subject_of", "team_for_tags", "threshold_for",
]


def owner_of(subject: Subject | dict | str, *, model: OrgModel | None = None) -> Resolved | None:
    """Who owns `subject` (a Subject, {kind, id} or "kind:id"). None when
    nothing says. Resolved.confirmed is False for a proposal: a consumer may
    cite it, but must not let it enable anything."""
    return (model or load()).owner_of(subject)


def team_for_tags(tags: dict[str, Any], *, model: OrgModel | None = None) -> Resolved | None:
    """The team a tag set names, through tag_key and tag_alias facts."""
    return (model or load()).team_for_tags(tags)


def environment_of(x: Subject | dict | str, *, model: OrgModel | None = None) -> tuple[str, bool]:
    """(env, confirmed) for a subject or a tag dict; ("unknown", False) when
    nothing says. Only a confirmed nonprod may make something eligible."""
    return (model or load()).environment_of(x)


def threshold_for(team: str | None = None, env: str | None = None, *,
                  model: OrgModel | None = None) -> dict[str, Any]:
    """Confirmed per-scope thresholds: {max_auto_monthly_usd?, velocity_cap_usd?,
    scope: {field: "team:payments"}}, or {} when none applies."""
    return (model or load()).threshold_for(team, env)
