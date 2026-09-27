# SPDX-License-Identifier: Apache-2.0
"""Org-context adapters: read a source of org truth, propose facts.

Each adapter is a function `propose(ctx) -> list[Fact]` over an
AdapterContext. It reads (the repo on disk, the local cost history through
ctx.data) and returns proposals; it never writes, never calls a cloud API,
never runs a subprocess, and whatever status it sets, the store writes its
facts as proposed. That shape is the one an adapter pack will have: the
registry names an entry point ("module:function"), the core builds the
context, calls it, and alone decides what reaches `nable.org/`.

    codeowners   CODEOWNERS -> owner facts for repo paths that hold IaC
    terraform    Terraform files and local state -> owners of accounts,
                 namespaces and resources (through CODEOWNERS), environments,
                 tag keys from default_tags
    aws_org      the synced org_accounts table -> account names, business
                 units, environments, owners named by account tags
    tags         attributed_costs, resource_inventory and the tag_rules table
                 -> tag keys, team aliases, the teams tagged spend names
    workload     context/workload.py's classifier -> environments of the
                 accounts and namespaces the cost history has seen

They run in that order, and each sees what the earlier ones proposed in the
same run (ctx.prior): terraform needs CODEOWNERS owners, tags anchors its
aliases on the teams already named.

Importing this package costs the standard library only; the adapters load
when `nable org init` runs them.
"""
from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..model import Fact, OrgModel, Subject, subject_of


@dataclass(frozen=True)
class Adapter:
    """One registered adapter. `entry` is "module:function" (loaded on first
    use, as a pack's entry point will be) or the function itself. `reads`
    declares what it looks at, the way a pack manifest declares capabilities."""
    id: str
    entry: str | Callable[[AdapterContext], Iterable[Fact]]
    reads: tuple[str, ...] = ()
    description: str = ""

    def load(self) -> Callable[[AdapterContext], Iterable[Fact]]:
        if callable(self.entry):
            return self.entry
        module, _, attr = self.entry.partition(":")
        return getattr(importlib.import_module(module), attr or "propose")


ADAPTERS: list[Adapter | Callable[[AdapterContext], Iterable[Fact]]] = [
    Adapter("codeowners", "finops.org.adapters.codeowners:propose", ("repo",),
            "CODEOWNERS owners of the repo paths that hold infrastructure code"),
    Adapter("terraform", "finops.org.adapters.terraform:propose",
            ("repo", "org.owners", "cost.accounts", "cost.namespaces", "cost.resources"),
            "Terraform providers, namespaces, local state and default_tags"),
    Adapter("aws_org", "finops.org.adapters.aws_org:propose",
            ("org_accounts", "cost.accounts", "cost.teams", "org.owners"),
            "AWS Organizations accounts synced to the org_accounts table"),
    Adapter("tags", "finops.org.adapters.tags:propose",
            ("cost.teams", "cost.environments", "resource_inventory", "tag_rules",
             "org.owners"),
            "Tag keys and team tag values in the cost history"),
    Adapter("workload", "finops.org.adapters.workload:propose",
            ("cost.accounts", "cost.environments", "cost.namespaces", "org_accounts"),
            "Environments of the accounts and namespaces the cost history has seen"),
]


def adapter_id(a: Adapter | Callable[..., Any]) -> str:
    if isinstance(a, Adapter):
        return a.id
    return getattr(a, "__name__", None) or getattr(a, "name", None) or "adapter"


@dataclass
class AdapterContext:
    """What an adapter may read. `model` is the org model as loaded (file and
    legacy facts); `prior` holds what earlier adapters proposed in this run;
    `data` the local cost history (read on first use when not given)."""
    model: OrgModel
    repos: list[Path] = field(default_factory=list)
    data: Any = None
    prior: list[Fact] = field(default_factory=list)
    _view: OrgModel | None = field(default=None, repr=False)
    _view_n: int = field(default=-1, repr=False)

    @property
    def cost(self) -> Any:
        """The local cost history (adapters.data.CostData), read once."""
        if self.data is None:
            from .data import CostData
            self.data = CostData.from_local()
        return self.data

    def view(self) -> OrgModel:
        """The model as it stands with this run's earlier proposals added."""
        if self._view is None or self._view_n != len(self.prior):
            self._view = OrgModel([*self.model.facts, *self.prior], dir=self.model.dir)
            self._view_n = len(self.prior)
        return self._view

    def live(self, fact: str, subject: Subject | str) -> list[Fact]:
        """Live facts (model and prior) of one kind about one subject."""
        s = subject_of(subject)
        return [f for f in self.view().facts
                if f.fact == fact and f.subject == s and f.live]

    def owner_fact(self, subject: Subject | str) -> Fact | None:
        """The owner fact that answers for `subject` (repo paths by longest
        prefix), counting this run's proposals."""
        return self.view()._owner_fact(subject_of(subject))

    def repo_label(self, repo: Path) -> str:
        """"" for the first repo (the one nable runs in), "<name>/" for the
        rest, to prefix source locators with."""
        if not self.repos or repo == self.repos[0]:
            return ""
        return f"{repo.name}/"

    def usd(self, subject: Subject | str) -> float | None:
        return self.cost.usd(subject_of(subject))


__all__ = ["ADAPTERS", "Adapter", "AdapterContext", "adapter_id"]
