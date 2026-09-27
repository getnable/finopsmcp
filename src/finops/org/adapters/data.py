# SPDX-License-Identifier: Apache-2.0
"""The local cost history, as the adapters read it.

One read of the nable database, the latest month only, the same month
coverage() counts, so a question's dollars and the coverage number agree:

  accounts    (provider, account) -> dollars          cost_snapshots
  teams       account -> team tag value -> dollars    attributed_costs
  envs        account -> environment value -> dollars attributed_costs
  org_accounts                                        org_accounts (synced by
                                                      connectors/aws_org.py)
  inventory   active resources with tags and cost     resource_inventory
  tag_rules   rows of the tag_rules table
  namespaces  (cluster, namespace) -> {usd, labels}   kubernetes_costs, the
                                                      latest snapshot of each

Nothing here calls a cloud API. A database that does not exist is not
created (get_engine would): it is reported in `not_read` and the data is
empty. Tests build a CostData directly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..model import ACCOUNT_KINDS, Subject

PROVIDER_KIND = {"aws": "aws_account", "gcp": "gcp_project", "azure": "azure_subscription"}
KIND_PROVIDER = {v: k for k, v in PROVIDER_KIND.items()}
UNATTRIBUTED = ("", "unattributed", "untagged", "unknown", "none", "null", "n/a")


def _tags(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        d = raw
    else:
        try:
            d = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
    if not isinstance(d, dict):
        return {}
    return {str(k): str(v) for k, v in d.items() if v is not None and str(v).strip()}


@dataclass
class CostData:
    month: str | None = None
    # (first day, last day) of the spend window coverage.read_spend chose.
    window: tuple[str, str] | None = None
    accounts: dict[tuple[str, str], float] = field(default_factory=dict)
    teams: dict[str, dict[str, float]] = field(default_factory=dict)
    envs: dict[str, dict[str, float]] = field(default_factory=dict)
    org_accounts: list[dict[str, Any]] = field(default_factory=list)
    inventory: list[dict[str, Any]] = field(default_factory=list)
    tag_rules: list[dict[str, Any]] = field(default_factory=list)
    namespaces: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    read: list[str] = field(default_factory=list)
    not_read: list[str] = field(default_factory=list)

    # ── lookups ───────────────────────────────────────────────────────────────

    def account_usd(self, kind: str, account: str) -> float | None:
        provider = KIND_PROVIDER.get(kind)
        usd = self.accounts.get((provider, account)) if provider else None
        return round(usd, 2) if usd else None

    def namespace_usd(self, namespace: str) -> float | None:
        """A namespace's spend: "cluster/ns" exactly, or the bare name summed
        across the clusters it appears in."""
        if "/" in namespace:
            cluster, _, ns = namespace.rpartition("/")
            got = self.namespaces.get((cluster, ns))
            return round(got["usd"], 2) if got and got["usd"] else None
        usd = sum(v["usd"] for (_, ns), v in self.namespaces.items() if ns == namespace)
        return round(usd, 2) if usd else None

    def resource_usd(self, rid: str) -> float | None:
        usd = sum(r["usd"] for r in self.inventory
                  if rid and (r["resource_id"] == rid or r.get("arn") == rid))
        return round(usd, 2) if usd else None

    def usd(self, s: Subject) -> float | None:
        if s.kind in ACCOUNT_KINDS:
            return self.account_usd(s.kind, s.id)
        if s.kind == "k8s_namespace":
            return self.namespace_usd(s.id)
        if s.kind == "resource":
            return self.resource_usd(s.id)
        if s.kind == "tag_value":
            low = s.id.lower()
            usd = sum(v for parts in self.teams.values() for t, v in parts.items()
                      if t.lower() == low)
            return round(usd, 2) if usd else None
        return None

    def account_name(self, kind: str, account: str) -> str | None:
        provider = KIND_PROVIDER.get(kind)
        for row in self.org_accounts:
            if row["provider"] == provider and row["account_id"] == account:
                return row.get("name") or None
        return None

    # ── the one read ──────────────────────────────────────────────────────────

    @classmethod
    def from_local(cls) -> CostData:
        """The latest month of the local history. Never raises."""
        from ..coverage import _db_path, read_spend
        out = cls()
        try:
            path = _db_path()
            if path is not None and not path.exists():
                out.not_read.append(f"cost history: {path} does not exist yet")
                return out
        except Exception as e:  # noqa: BLE001 - reported, never raised
            out.not_read.append(f"cost history: {type(e).__name__}: {e}")
            return out
        sp = read_spend()
        out.month, out.accounts, out.teams = sp.month, dict(sp.accounts), dict(sp.teams)
        out.window = (sp.start, sp.through) if sp.start and sp.through else None
        out.read.extend(sp.read)
        out.not_read.extend(sp.not_read)
        try:
            out._read_rest()
        except Exception as e:  # noqa: BLE001 - reported, never raised
            out.not_read.append(f"cost history: could not be read ({type(e).__name__}: {e})")
        return out

    def _read_rest(self) -> None:
        from sqlalchemy import func, select

        from ...storage.db import (
            attributed_costs,
            get_engine,
            kubernetes_costs,
            org_accounts,
            resource_inventory,
            tag_rules,
        )
        with get_engine().connect() as conn:
            if self.month:
                day = attributed_costs.c.snapshot_date
                when = (day.like(f"{self.month}-%") if self.window is None
                        else (day >= self.window[0]) & (day <= self.window[1]))
                rows = conn.execute(
                    select(attributed_costs.c.account_id, attributed_costs.c.environment,
                           func.sum(attributed_costs.c.amount_usd))
                    .where(when)
                    .group_by(attributed_costs.c.account_id,
                              attributed_costs.c.environment)).all()
                for account, env, usd in rows:
                    if usd:
                        self.envs.setdefault(str(account), {})[str(env or "")] = float(usd)
            for r in conn.execute(select(org_accounts)).mappings():
                self.org_accounts.append({
                    "provider": str(r["cloud_provider"] or "aws").lower(),
                    "account_id": str(r["account_id"]).strip(),
                    "name": str(r["account_name"] or "").strip(),
                    "parent_id": str(r["parent_id"] or "").strip(),
                    "status": str(r["status"] or "ACTIVE").upper(),
                    "tags": _tags(r["tags"]),
                    "is_management_account": bool(r["is_management_account"]),
                })
            if self.org_accounts:
                self.read.append(f"org_accounts: {len(self.org_accounts)} accounts")
            for r in conn.execute(select(resource_inventory)
                                  .where(resource_inventory.c.is_active.is_(True))).mappings():
                meta = _tags(r["metadata"])
                self.inventory.append({
                    "provider": str(r["provider"]), "account_id": str(r["account_id"]),
                    "resource_id": str(r["resource_id"]),
                    "arn": meta.get("arn") or None,
                    "type": str(r["resource_type"]), "name": str(r["resource_name"] or ""),
                    "tags": _tags(r["tags"]), "usd": float(r["monthly_cost_usd"] or 0.0),
                })
            if self.inventory:
                self.read.append(f"resource_inventory: {len(self.inventory)} resources")
            for r in conn.execute(select(tag_rules)).mappings():
                self.tag_rules.append({k: r[k] for k in ("provider", "tag_key",
                                                         "tag_value_pattern", "maps_to_field",
                                                         "maps_to_value", "priority")})
            latest = (select(kubernetes_costs.c.cluster, kubernetes_costs.c.namespace,
                             func.max(kubernetes_costs.c.snapshot_date).label("d"))
                      .group_by(kubernetes_costs.c.cluster, kubernetes_costs.c.namespace)
                      .subquery())
            rows = conn.execute(
                select(kubernetes_costs.c.cluster, kubernetes_costs.c.namespace,
                       kubernetes_costs.c.monthly_cost_usd, kubernetes_costs.c.labels)
                .join(latest, (kubernetes_costs.c.cluster == latest.c.cluster)
                      & (kubernetes_costs.c.namespace == latest.c.namespace)
                      & (kubernetes_costs.c.snapshot_date == latest.c.d))).all()
            for cluster, ns, usd, labels in rows:
                slot = self.namespaces.setdefault((str(cluster), str(ns)),
                                                  {"usd": 0.0, "labels": {}})
                slot["usd"] += float(usd or 0.0)
                slot["labels"].update(_tags(labels))
            if self.namespaces:
                self.read.append(f"kubernetes_costs: {len(self.namespaces)} namespaces")
