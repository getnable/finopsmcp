# SPDX-License-Identifier: Apache-2.0
"""Who owns a finding, a recommendation or a ticket's subject, from the org
model (finops.org), for the consumers that route by owner: tickets and the
tools that list savings and waste.

    owner_for({"account_id": "123456789012", "tags": {"team": "pay"}})
    -> Owner(team="payments", channel="#payments-oncall", confirmed=True, ...)

The subjects tried, most specific first: the resource's tags (through the
org's tag_key and tag_alias facts), its Kubernetes namespace, its account,
and a team the finding already names. The first confirmed answer wins; with
none, the first proposed one, marked unconfirmed. A consumer may show an
unconfirmed owner ("likely"), and must not let it enable anything: a ticket
is assigned to a person only from a confirmed fact (Owner.people).

approvals_for() answers the other routing question, who reviews a class of
change: the confirmed approval facts for the owner's team and the confirmed
environments, which nable's pull requests request reviews from and its
tickets add as watchers.

Nothing here raises. No org model, or one that cannot be read, is no owner.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

_PROVIDER_KIND = {"aws": "aws_account", "gcp": "gcp_project", "azure": "azure_subscription"}
_NAMESPACE_KEYS = ("namespace", "k8s_namespace")
_ACCOUNT_KEYS = ("account_id", "account", "subscription_id", "project_id")


@dataclass
class Owner:
    team: str
    channel: str | None = None
    confirmed: bool = False
    # Only people a confirmed fact names: the only ones a ticket may be
    # assigned to. Empty for a proposal.
    people: list[str] = field(default_factory=list)
    matched: str | None = None

    def compact(self) -> dict[str, Any]:
        """The `owner` field a tool returns: team, channel, confirmed."""
        out: dict[str, Any] = {"team": self.team}
        if self.channel:
            out["channel"] = self.channel
        out["confirmed"] = self.confirmed
        return out

    def words(self) -> str:
        chan = f" ({self.channel})" if self.channel else ""
        if self.confirmed:
            return f"{self.team}{chan}"
        return f"likely {self.team}{chan}, not confirmed"


def load_model() -> Any:
    """The org model, or None when it cannot be read."""
    try:
        from . import org
        return org.load()
    except Exception as exc:  # noqa: BLE001 - no model is no owner
        log.debug("org model not read: %s", exc)
        return None


def _useful(model: Any) -> bool:
    """Whether the model can name an owner at all (skip the work if not)."""
    return any(f.live and f.fact in ("owner", "team", "tag_alias", "tag_key")
               for f in model.facts)


def _tags(item: dict[str, Any]) -> dict[str, Any] | None:
    """The resource's tags: `tags`, or those a tracked recommendation keeps
    in its current_config (a dict, or the JSON the database stores)."""
    tags = item.get("tags")
    if isinstance(tags, dict) and tags:
        return tags
    cfg = item.get("current_config")
    if isinstance(cfg, str) and '"tags"' in cfg:
        import json
        try:
            cfg = json.loads(cfg)
        except ValueError:
            return None
    if isinstance(cfg, dict) and isinstance(cfg.get("tags"), dict) and cfg["tags"]:
        return cfg["tags"]
    return None


def _subjects(item: dict[str, Any], team: str = "") -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    tags = _tags(item)
    if tags:
        out.append(("tags", tags))
    for k in _NAMESPACE_KEYS:
        ns = item.get(k)
        if isinstance(ns, str) and ns.strip():
            cluster = item.get("cluster")
            if isinstance(cluster, str) and cluster.strip():
                out.append(("subject", f"k8s_namespace:{cluster.strip()}/{ns.strip()}"))
            else:
                out.append(("subject", f"k8s_namespace:{ns.strip()}"))
            break
    kind = _PROVIDER_KIND.get(str(item.get("provider") or "aws").lower(), "aws_account")
    for k in _ACCOUNT_KEYS:
        acct = item.get(k)
        if isinstance(acct, (str, int)) and str(acct).strip() and str(acct) != "unknown":
            out.append(("subject", f"{kind}:{str(acct).strip()}"))
            break
    named = team or item.get("team")
    if isinstance(named, str) and named.strip() and named.strip() != "unattributed":
        out.append(("subject", f"team:{named.strip()}"))
    return out


def _confirmed_people(model: Any, r: Any) -> list[str]:
    """The people a confirmed fact behind `r` names: the owner fact's own,
    else a confirmed team fact's. Never a proposal's."""
    if not r.confirmed:
        return []
    for f in model.find(r.key or ""):
        if f.confirmed and f.fact in ("owner", "team") and f.value.get("people"):
            return list(f.value["people"])
    from .org import Subject
    tf = model.resolve("team", Subject("team", r.team))
    if tf is not None and tf.confirmed:
        return list(tf.value.get("people") or [])
    return []


def owner_for(item: dict[str, Any], *, model: Any = None, team: str = "") -> Owner | None:
    """The owner of what `item` is about (see the module doc), or None."""
    try:
        m = model if model is not None else load_model()
        if m is None or not _useful(m):
            return None
        first: Owner | None = None
        for how, what in _subjects(item, team):
            try:
                r = m.team_for_tags(what) if how == "tags" else m.owner_of(what)
            except Exception as exc:  # noqa: BLE001 - one odd subject is not the finding
                log.debug("owner of %s not read: %s", what, exc)
                continue
            if r is None or not r.team:
                continue
            o = Owner(team=r.team, channel=r.channel, confirmed=bool(r.confirmed),
                      people=_confirmed_people(m, r), matched=r.matched)
            if o.confirmed:
                return o
            first = first or o
        return first
    except Exception as exc:  # noqa: BLE001 - an owner is a nicety, never a failure
        log.debug("owner lookup failed: %s", exc)
        return None


@dataclass
class Approvals:
    """Who reviews a class of change, from the confirmed approval facts
    (finops.org) for its team and environments: what a pull request
    requests reviews from and a ticket adds as watchers. Each list holds the
    ids of one kind of approver ("github:alice" is "alice" in `github`)."""
    github: list[str] = field(default_factory=list)       # GitHub logins
    teams: list[str] = field(default_factory=list)        # GitHub team slugs
    jira: list[str] = field(default_factory=list)         # Jira account ids
    linear: list[str] = field(default_factory=list)       # Linear user ids
    email: list[str] = field(default_factory=list)        # shown, never sent to
    min: int = 1
    change_ticket: bool = False
    scopes: list[str] = field(default_factory=list)       # "team:payments", ...
    keys: list[str] = field(default_factory=list)

    @property
    def named(self) -> list[str]:
        return ([f"github:{x}" for x in self.github] + [f"team:{x}" for x in self.teams]
                + [f"jira:{x}" for x in self.jira] + [f"linear:{x}" for x in self.linear]
                + [f"email:{x}" for x in self.email])

    def words(self) -> str:
        """"1 of github:alice, team:platform (team:payments, from the org model)"."""
        return (f"{self.min} of {', '.join(self.named)} ({', '.join(self.scopes)}, "
                "from the org model)")


def _envs(model: Any, item: dict[str, Any]) -> list[str]:
    """The environments `item` is confirmed to be in."""
    out: dict[str, None] = {}
    for how, what in _subjects(item):
        if how == "subject" and str(what).startswith("team:"):
            continue
        try:
            env, sure = model.environment_of(what, strict=True)
        except Exception as exc:  # noqa: BLE001 - one odd subject is not the finding
            log.debug("environment of %s not read: %s", what, exc)
            continue
        if sure and env != "unknown":
            out[env] = None
    return list(out)


def approvals_for(items: list[dict[str, Any]], action_class: str, *, model: Any = None,
                  teams: list[str] | tuple[str, ...] = ()) -> Approvals | None:
    """The approvers the org confirmed for `action_class` over what `items`
    are about (their confirmed owners' teams, the environments they are
    confirmed to be in) and any `teams` the caller knows own the change (a
    pull request's repo path). None when no confirmed approval fact
    applies. Only confirmed, trusted facts name anybody: a review request
    goes to a person because a person said so. Never raises."""
    try:
        m = model if model is not None else load_model()
        if m is None or not any(f.fact == "approval" and f.confirmed for f in m.facts):
            return None
        facts: dict[str, Any] = {}
        scopes: list[tuple[str | None, list[str]]] = [(t, []) for t in teams if t]
        for it in items:
            o = owner_for(it, model=m)
            scopes.append((o.team if o is not None and o.confirmed else None, _envs(m, it)))
        for team, envs in scopes:
            for f in m.approvals_for(action_class, team=team, envs=envs, strict=True):
                facts.setdefault(f.key, f)
        if not facts:
            return None
        out = Approvals()
        for f in facts.values():
            for a in f.value.get("approvers") or []:
                kind, _, ident = str(a).partition(":")
                bucket = {"github": out.github, "team": out.teams, "jira": out.jira,
                          "linear": out.linear, "email": out.email}.get(kind)
                if bucket is not None and ident and ident not in bucket:
                    bucket.append(ident)
            out.min = max(out.min, int(f.value.get("min") or 1))
            out.change_ticket = out.change_ticket or bool(f.value.get("change_ticket"))
            if str(f.subject) not in out.scopes:
                out.scopes.append(str(f.subject))
            out.keys.append(f.key)
        return out
    except Exception as exc:  # noqa: BLE001 - reviewers are a nicety, never a failure
        log.debug("approval lookup failed: %s", exc)
        return None


def annotate(items: list[Any], *, model: Any = None, key: str = "owner",
             context: Any = None) -> list[Any]:
    """Add `owner` (team, channel, confirmed) to each dict in `items` whose
    owner the org model can name, in place; returns `items`. `context` fills
    fields an item lacks: a dict (the account a whole scan ran against), or
    a function of the item returning one. Loads the model once; with none,
    or none that names owners, it does nothing."""
    try:
        if not items:
            return items
        m = model if model is not None else load_model()
        if m is None or not _useful(m):
            return items
        for it in items:
            if not isinstance(it, dict) or key in it:
                continue
            extra = (context(it) if callable(context) else context) or {}
            probe = {**extra, **{k: v for k, v in it.items() if v is not None}}
            o = owner_for(probe, model=m)
            if o is not None:
                it[key] = o.compact()
    except Exception as exc:  # noqa: BLE001 - never break the listing
        log.debug("owner annotation skipped: %s", exc)
    return items
