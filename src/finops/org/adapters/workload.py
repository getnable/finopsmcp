# SPDX-License-Identifier: Apache-2.0
"""Workload: prod or not, for the accounts and namespaces the bill has seen.

Runs context/workload.py's classifier (the one the recommendation path
already trusts) over what the cost history carries: each account's name
(org_accounts, or an account fact), its Environment tag, and the environment
values its tagged spend carries in attributed_costs; each Kubernetes
namespace's name, labels and cluster (kubernetes_costs). Subjects another
adapter already gave an environment are left alone.

The classifier's asymmetry holds here too, and the spec's rule 4 with it:
a guess may restrict, never enable. So:
  - unknown stays unknown: no signal, no fact;
  - nonprod (and sandbox) only from evidence of weight two or more: a tag,
    an account name, a namespace name; never from a cluster or resource
    name alone;
  - an account whose tagged spend is split between prod and nonprod proposes
    nothing, whatever its name says;
  - prod from a weak signal is still proposed (it only restricts), at 0.4.

Confidence: an environment tag, or 90% of the account's tagged spend under
one environment covering at least half its spend: 0.8 prod, 0.75 nonprod.
A name: 0.6 prod and sandbox, 0.55 nonprod. Only a person's confirmation
makes a nonprod fact enable anything.
"""
from __future__ import annotations

import re
from typing import Any

from ..model import Fact, Subject
from ._common import dedupe, env_of_name, fact
from .data import KIND_PROVIDER, PROVIDER_KIND, UNATTRIBUTED

_SANDBOX_WORDS = {"sandbox", "sbx", "playground", "scratch", "lab", "experiment", "poc"}
_WEIGHT = (("tag ", 3), ("account ", 2), ("namespace ", 2), ("cluster ", 1),
           ("resource name ", 1))
_WORD = re.compile(r'contains "([^"]+)"')


def reading(*, tags: dict[str, str] | None = None, account_name: str | None = None,
            namespace: str | None = None, cluster: str | None = None) \
        -> tuple[str, int, str] | None:
    """(env, evidence weight, evidence) from the workload classifier, or None
    for unknown and for nonprod on weak evidence."""
    from ...context.workload import classify
    got = classify(tags=tags or {}, account_name=account_name, namespace=namespace,
                   cluster=cluster)
    if got.kind == "unknown" or not got.evidence:
        return None
    weight = max((w for e in got.evidence for p, w in _WEIGHT if e.startswith(p)), default=1)
    words = {m.group(1) for e in got.evidence for m in [_WORD.search(e)] if m}
    if got.kind == "prod":
        env = "prod"
    elif words & _SANDBOX_WORDS:
        env = "sandbox"
    else:
        env = "nonprod"                   # nonprod and ephemeral
    if env != "prod" and weight < 2:
        return None
    return env, weight, "; ".join(got.evidence)


def _confidence(env: str, weight: int) -> float:
    if weight >= 3:
        return 0.8 if env == "prod" else 0.75
    if weight == 2:
        return {"prod": 0.6, "sandbox": 0.6}.get(env, 0.55)
    return 0.4


def _tag_env(parts: dict[str, float], account_usd: float | None) -> tuple[str | None, bool]:
    """(the environment value an account's tagged spend carries, mixed?).
    One class must hold 90% of the tagged spend, and the tagged spend half
    the account's; a split between prod and nonprod is `mixed`."""
    by_class: dict[str, tuple[str, float]] = {}
    tagged = 0.0
    for v, usd in parts.items():
        if v.strip().lower() in UNATTRIBUTED or usd <= 0:
            continue
        got = env_of_name(v, accounts=False)
        if got is None:
            continue
        tagged += usd
        prev = by_class.get(got[0])
        by_class[got[0]] = (v if prev is None or usd > prev[1] else prev[0],
                            (prev[1] if prev else 0.0) + usd)
    if not by_class or tagged <= 0:
        return None, False
    _, (spelling, usd) = max(by_class.items(), key=lambda kv: kv[1][1])
    if usd < 0.9 * tagged:
        classes = set(by_class)
        return None, "prod" in classes and bool(classes - {"prod", "shared"})
    if account_usd and tagged < 0.5 * account_usd:
        return None, False
    return spelling, False


def _account_name(ctx: Any, kind: str, account: str) -> str | None:
    name = ctx.cost.account_name(kind, account)
    if name:
        return name
    f = ctx.view().resolve("account", Subject(kind, account))
    return (f.value.get("name") or None) if f is not None else None


def propose(ctx: Any) -> list[Fact]:
    cost = ctx.cost
    accounts: set[tuple[str, str]] = set(cost.accounts)
    gone = {(r["provider"], r["account_id"]) for r in cost.org_accounts
            if r.get("status") in ("SUSPENDED", "CLOSED", "PENDING_CLOSURE")}
    accounts |= {(r["provider"], r["account_id"]) for r in cost.org_accounts
                 if r["account_id"]}
    accounts -= gone
    accounts |= {("aws", a) for a in cost.envs}
    rows = {(r["provider"], r["account_id"]): r for r in cost.org_accounts}
    out: list[Fact | None] = []
    for provider, account in sorted(accounts):
        kind = PROVIDER_KIND.get(provider)
        if kind is None:
            continue
        if not account:
            continue
        s = Subject(kind, account)
        if ctx.live("environment", s):
            continue
        usd = cost.account_usd(kind, account)
        tags = {k: v for k, v in (rows.get((provider, account)) or {}).get("tags", {}).items()}
        value, mixed = _tag_env(cost.envs.get(account, {}), usd)
        if mixed:
            continue                      # half prod, half not: unknown stays unknown
        if value:
            tags = {**tags, "environment": value}
        got = reading(tags=tags, account_name=_account_name(ctx, kind, account))
        if got is None:
            continue
        env, weight, _ = got
        src = f"workload:{KIND_PROVIDER.get(kind, provider)}/{account}"
        out.append(fact("environment", s, {"env": env}, src, _confidence(env, weight), usd))

    by_ns: dict[str, dict[str, tuple[str, int]]] = {}
    for (cluster, ns), info in sorted(cost.namespaces.items()):
        got = reading(tags=info.get("labels") or {}, namespace=ns, cluster=cluster)
        if got is not None:
            by_ns.setdefault(ns, {})[cluster] = (got[0], got[1])
        else:
            by_ns.setdefault(ns, {})[cluster] = ("", 0)
    for ns, per in sorted(by_ns.items()):
        envs = {e for e, _ in per.values()}
        if envs == {""}:
            continue
        if len(envs) == 1:
            env = next(iter(envs))
            weight = min(w for _, w in per.values())
            targets = [(ns, weight)]
        else:
            targets = [(f"{c}/{ns}", w) for c, (e, w) in sorted(per.items()) if e]
        for sid, weight in targets:
            e = env if len(envs) == 1 else per[sid.rsplit("/", 1)[0]][0]
            s = Subject("k8s_namespace", sid)
            if ctx.live("environment", s):
                continue
            out.append(fact("environment", s, {"env": e}, f"workload:kubernetes/{sid}",
                            _confidence(e, weight), cost.namespace_usd(sid)))
    return dedupe(out)
