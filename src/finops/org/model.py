# SPDX-License-Identifier: Apache-2.0
"""Org facts: the envelope, validation, stable keys, precedence and queries.

A fact is one statement about this org ("aws_account 123 is owned by
payments", "the tag key `costcenter` means team") with where it came from, how
sure its proposer was, and whether a human has said yes. Proposers (adapters,
MCP tools, inference) only ever write `status: proposed`. A human confirms or
rejects through the CLI, or by merging a PR that adds a confirmed fact: the
loader trusts file contents, and that trust is the PR-review path.

On a rejected fact, `confirmed_by` and `confirmed_at` record who rejected it
and when. They are the fields a human decision writes, whichever way it went.

Precedence for one question (a fact kind about one subject): confirmed beats
proposed; among confirmed, the newest confirmed_at; among proposed, the
highest confidence, then the newest. Rejected and expired facts are ignored.
A confirmed fact past its review_after is "stale": still used, flagged, asked
again.

Nothing here imports more than the standard library. The guard hook will read
this model on every agent tool call, and its budget is about 100 ms.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

log = logging.getLogger("finops.org")

FACT_KINDS = ("owner", "team", "environment", "tag_key", "tag_alias", "account", "threshold")
# "environment" is a subject too, so a threshold can be scoped to one (policy.yaml).
SUBJECT_KINDS = ("aws_account", "gcp_project", "azure_subscription", "k8s_namespace",
                 "repo_path", "service", "tag_value", "resource", "team", "org",
                 "environment")
ACCOUNT_KINDS = ("aws_account", "gcp_project", "azure_subscription")
STATUSES = ("proposed", "confirmed", "rejected", "expired")
LIVE = ("confirmed", "proposed")
ENVIRONMENTS = ("prod", "nonprod", "dr", "sandbox", "shared", "unknown")
CANONICAL_TAG_KEYS = ("team", "environment", "service", "cost_center", "owner")

# One file per fact kind, so a diff reads as "ownership changed" or "a tag
# alias was added" and never both at once.
FILE_FOR_KIND = {
    "owner": "owners.yaml",
    "team": "teams.yaml",
    "environment": "environments.yaml",
    "tag_key": "tags.yaml",
    "tag_alias": "tags.yaml",
    "account": "accounts.yaml",
    "threshold": "policy.yaml",
}
KNOWN_FILES = tuple(sorted(set(FILE_FOR_KIND.values())))
HEADER = "# nable org model v1"
FIELD_ORDER = ("fact", "subject", "value", "source", "confidence", "status", "proposed_at",
               "confirmed_by", "confirmed_at", "review_after", "dollars_monthly")
_DATE_FIELDS = ("proposed_at", "confirmed_at", "review_after")

# Environment tag values that mean one of ENVIRONMENTS. A match here is our
# reading, not the org's, so it is never reported as confirmed.
_ENV_WORDS = {
    "prod": ("prod", "production", "prd", "live"),
    "nonprod": ("nonprod", "non-prod", "dev", "development", "staging", "stage", "test",
                "testing", "qa", "uat", "preprod", "pre-prod"),
    "dr": ("dr", "disaster-recovery", "standby"),
    "sandbox": ("sandbox", "sbx", "playground", "lab"),
    "shared": ("shared", "common", "platform"),
}


def local_today() -> date:
    """Today where the person is: a fact confirmed at 9pm in California is
    confirmed that day, not the next one."""
    return datetime.now(UTC).astimezone().date()


class FactError(ValueError):
    """A fact that does not fit the schema. The message says which field."""


@dataclass(frozen=True)
class Subject:
    kind: str
    id: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.id}"

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.id}


def _norm_repo_path(raw: str) -> str:
    p = raw.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.strip("/")
    return p or "."


def subject_of(x: Subject | dict | str) -> Subject:
    """A Subject from a Subject, {kind, id}, or "kind:id". Raises FactError."""
    if isinstance(x, Subject):
        return x
    if isinstance(x, str):
        kind, sep, ident = x.partition(":")
        if not sep:
            raise FactError(f"subject {x!r} is not kind:id")
        x = {"kind": kind, "id": ident}
    if not isinstance(x, dict):
        raise FactError(f"subject must be a mapping with kind and id, got {type(x).__name__}")
    kind = str(x.get("kind") or "").strip()
    if kind not in SUBJECT_KINDS:
        raise FactError(f"subject.kind {kind!r} is not one of {', '.join(SUBJECT_KINDS)}")
    raw = x.get("id")
    if isinstance(raw, bool) or raw is None or not isinstance(raw, (str, int)):
        raise FactError("subject.id must be a string")
    ident = str(raw).strip()
    if not ident:
        raise FactError("subject.id is empty")
    if kind == "aws_account" and isinstance(raw, int) and len(ident) < 12:
        # An unquoted id in YAML is an int, and an int has lost its leading zeros.
        ident = ident.zfill(12)
    if kind == "repo_path":
        ident = _norm_repo_path(ident)
    return Subject(kind, ident)


def fact_key(fact: str, subject: Subject, value: dict[str, Any]) -> str:
    """The stable id shown in the CLI: sha1 over (fact, subject.kind,
    subject.id, the value as canonical JSON), first 10 hex characters."""
    blob = json.dumps([fact, subject.kind, subject.id, value], sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]


def _str_list(v: Any, name: str) -> list[str]:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(s, str) and s.strip() for s in v):
        raise FactError(f"{name} must be a list of strings")
    return [s.strip() for s in v]


def _opt_str(value: dict, name: str) -> None:
    if name in value and value[name] is not None and not isinstance(value[name], str):
        raise FactError(f"value.{name} must be a string")


def _req_str(value: dict, name: str) -> None:
    if not isinstance(value.get(name), str) or not value[name].strip():
        raise FactError(f"value.{name} is required")


def _number(v: Any, name: str, *, lo: float = 0.0, hi: float | None = None) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise FactError(f"{name} must be a number")
    v = float(v)
    if math.isnan(v) or v < lo or (hi is not None and v > hi):
        raise FactError(f"{name} {v} is out of range")
    return v


def validate_value(kind: str, subject: Subject, value: Any) -> dict[str, Any]:
    """A cleaned copy of `value` for this fact kind. Raises FactError.

    Keys the schema does not name are kept: a newer nable, or a person, may
    know something this version does not, and dropping it would lose a fact."""
    if not isinstance(value, dict):
        raise FactError("value must be a mapping")
    v = dict(value)
    if kind == "owner":
        _req_str(v, "team")
        _opt_str(v, "channel")
        if v.get("people") is not None:
            v["people"] = _str_list(v["people"], "value.people")
    elif kind == "team":
        if subject.kind != "team":
            raise FactError("a team fact's subject.kind must be team")
        _opt_str(v, "name")
        _opt_str(v, "channel")
        _opt_str(v, "parent")
        for name in ("aliases", "people"):
            if v.get(name) is not None:
                v[name] = _str_list(v[name], f"value.{name}")
    elif kind == "environment":
        if v.get("env") not in ENVIRONMENTS:
            raise FactError(f"value.env must be one of {', '.join(ENVIRONMENTS)}")
    elif kind == "tag_key":
        if subject.kind != "org":
            raise FactError("a tag_key fact's subject.kind must be org")
        if v.get("canonical") not in CANONICAL_TAG_KEYS:
            raise FactError(f"value.canonical must be one of {', '.join(CANONICAL_TAG_KEYS)}")
        v["keys"] = _str_list(v.get("keys"), "value.keys")
        if not v["keys"]:
            raise FactError("value.keys is empty")
    elif kind == "tag_alias":
        if subject.kind != "tag_value":
            raise FactError("a tag_alias fact's subject.kind must be tag_value")
        if v.get("canonical_key") not in CANONICAL_TAG_KEYS:
            raise FactError(
                f"value.canonical_key must be one of {', '.join(CANONICAL_TAG_KEYS)}")
        _req_str(v, "canonical_value")
    elif kind == "account":
        if subject.kind not in ACCOUNT_KINDS:
            raise FactError(f"an account fact's subject.kind must be one of "
                            f"{', '.join(ACCOUNT_KINDS)}")
        for name in ("name", "business_unit", "cost_center"):
            _opt_str(v, name)
    elif kind == "threshold":
        if subject.kind not in ("team", "environment", "org"):
            raise FactError("a threshold fact's subject.kind must be team, environment or org")
        present = [n for n in ("max_auto_monthly_usd", "velocity_cap_usd")
                   if v.get(n) is not None]
        if not present:
            raise FactError("value needs max_auto_monthly_usd or velocity_cap_usd")
        for n in present:
            v[n] = _number(v[n], f"value.{n}")
    else:
        raise FactError(f"fact {kind!r} is not one of {', '.join(FACT_KINDS)}")
    return v


def _iso_date(v: Any, name: str) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, str):
        try:
            return date.fromisoformat(v.strip()[:10]).isoformat()
        except ValueError:
            pass
    raise FactError(f"{name} must be a date (YYYY-MM-DD)")


@dataclass
class Fact:
    fact: str
    subject: Subject
    value: dict[str, Any]
    source: str
    confidence: float = 0.5
    status: str = "proposed"
    proposed_at: str | None = None
    confirmed_by: str | None = None
    confirmed_at: str | None = None
    review_after: str | None = None
    dollars_monthly: float | None = None
    # Where it was read from: a file name in the org dir, or "legacy". Not saved.
    origin: str | None = field(default=None, compare=False, repr=False)

    @property
    def key(self) -> str:
        return fact_key(self.fact, self.subject, self.value)

    @property
    def slot(self) -> tuple[str, str, str, str]:
        """What one fact answers. Two facts in a slot with different values
        disagree; a tag_key or tag_alias slot also carries what it maps to,
        since the org subject holds one tag_key fact per canonical key."""
        disc = ""
        if self.fact == "tag_key":
            disc = str(self.value.get("canonical", ""))
        elif self.fact == "tag_alias":
            disc = str(self.value.get("canonical_key", ""))
        sid = self.subject.id.lower() if self.subject.kind == "tag_value" else self.subject.id
        return (self.fact, self.subject.kind, sid, disc)

    @property
    def live(self) -> bool:
        return self.status in LIVE

    @property
    def confirmed(self) -> bool:
        return self.status == "confirmed"

    def is_stale(self, today: date | None = None) -> bool:
        if self.status != "confirmed" or not self.review_after:
            return False
        return self.review_after < (today or local_today()).isoformat()

    def to_dict(self) -> dict[str, Any]:
        """The saved form, fields in schema order, empty fields left out."""
        out: dict[str, Any] = {"fact": self.fact, "subject": self.subject.to_dict(),
                               "value": dict(self.value), "source": self.source,
                               "confidence": self.confidence, "status": self.status}
        for name in FIELD_ORDER[6:]:
            val = getattr(self, name)
            if val is not None:
                out[name] = val
        return out

    def summary(self) -> dict[str, Any]:
        """The compact form tools and --json return."""
        d = self.to_dict()
        d["subject"] = str(self.subject)
        d["key"] = self.key
        if self.is_stale():
            d["stale"] = True
        if self.origin == "legacy":
            d["legacy"] = True
        return d

    @classmethod
    def from_dict(cls, raw: Any, *, origin: str | None = None) -> Fact:
        if not isinstance(raw, dict):
            raise FactError("entry is not a mapping")
        kind = raw.get("fact")
        if kind not in FACT_KINDS:
            raise FactError(f"fact {kind!r} is not one of {', '.join(FACT_KINDS)}")
        subject = subject_of(raw.get("subject"))
        value = validate_value(kind, subject, raw.get("value"))
        status = raw.get("status") or "proposed"
        if status not in STATUSES:
            raise FactError(f"status {status!r} is not one of {', '.join(STATUSES)}")
        source = raw.get("source")
        if source is None:
            source = f"file:{origin}" if origin else "unknown"
        if not isinstance(source, str) or not source.strip():
            raise FactError("source must be a non-empty string")
        conf = raw.get("confidence")
        confidence = (1.0 if status == "confirmed" else 0.5) if conf is None else \
            _number(conf, "confidence", hi=1.0)
        by = raw.get("confirmed_by")
        if by is not None and not isinstance(by, str):
            raise FactError("confirmed_by must be a string")
        dollars = raw.get("dollars_monthly")
        if dollars is not None:
            dollars = _number(dollars, "dollars_monthly")
        return cls(fact=kind, subject=subject, value=value, source=source.strip(),
                   confidence=confidence, status=status,
                   proposed_at=_iso_date(raw.get("proposed_at"), "proposed_at"),
                   confirmed_by=by or None,
                   confirmed_at=_iso_date(raw.get("confirmed_at"), "confirmed_at"),
                   review_after=_iso_date(raw.get("review_after"), "review_after"),
                   dollars_monthly=dollars, origin=origin)


def parse_facts(entries: Any, where: str, *, origin: str | None = None,
                warnings: list[str] | None = None) -> list[Fact]:
    """Facts from a loaded YAML list. A bad entry is skipped with a warning
    naming the file and its index; nothing here raises."""
    out: list[Fact] = []
    if entries is None:
        return out
    if not isinstance(entries, list):
        _warn(warnings, f"{where}: expected a list of facts, got {type(entries).__name__}; "
                        "file ignored")
        return out
    for i, raw in enumerate(entries):
        try:
            out.append(Fact.from_dict(raw, origin=origin))
        except FactError as e:
            _warn(warnings, f"{where}[{i}]: skipped, {e}")
        except Exception as e:  # noqa: BLE001 - a bad entry never stops a load
            _warn(warnings, f"{where}[{i}]: skipped, {type(e).__name__}: {e}")
    return out


def _warn(sink: list[str] | None, msg: str) -> None:
    log.warning("org model: %s", msg)
    if sink is not None:
        sink.append(msg)


def _rank(f: Fact) -> tuple:
    """Sort key for precedence: the maximum wins. A file fact beats a legacy
    one on a tie, then the key keeps the order deterministic."""
    file_first = f.origin != "legacy"
    if f.status == "confirmed":
        return (1, f.confirmed_at or "", file_first, f.key)
    return (0, f.confidence, f.proposed_at or "", file_first, f.key)


def pick(facts: Iterable[Fact]) -> Fact | None:
    """The fact that answers, by precedence. None when nothing live is left."""
    live = [f for f in facts if f.live]
    return max(live, key=_rank) if live else None


def ranked(facts: Iterable[Fact]) -> list[Fact]:
    """Live facts, the winner first."""
    return sorted((f for f in facts if f.live), key=_rank, reverse=True)


def sort_key(f: Fact) -> tuple[str, str, str, str]:
    """The on-disk order: kind, subject kind, subject id, then key."""
    return (f.fact, f.subject.kind, f.subject.id, f.key)


@dataclass
class Resolved:
    """An answer to "who owns this", and how much to trust it."""
    team: str | None
    channel: str | None = None
    people: list[str] = field(default_factory=list)
    confirmed: bool = False
    source: str = ""
    key: str | None = None
    stale: bool = False
    matched: str | None = None     # the subject the answer is about, e.g. repo_path:infra

    def to_dict(self) -> dict[str, Any]:
        return {"team": self.team, "channel": self.channel, "people": list(self.people),
                "confirmed": self.confirmed, "source": self.source, "key": self.key,
                "stale": self.stale, "matched": self.matched}


def _is_subject_like(x: Any) -> bool:
    if isinstance(x, Subject):
        return True
    if isinstance(x, dict):
        return set(x) == {"kind", "id"} and x.get("kind") in SUBJECT_KINDS
    if isinstance(x, str):
        return x.partition(":")[0] in SUBJECT_KINDS
    return False


class OrgModel:
    """Every fact that applies, file facts over legacy ones, with queries."""

    def __init__(self, facts: list[Fact], *, dir: Any = None, dir_source: str = "",
                 warnings: list[str] | None = None) -> None:
        self.facts = facts
        self.dir = dir
        self.dir_source = dir_source
        self.warnings = warnings if warnings is not None else []
        self._slots: dict[tuple, list[Fact]] | None = None

    # ── lookup ────────────────────────────────────────────────────────────────

    def _index(self) -> dict[tuple, list[Fact]]:
        if self._slots is None:
            idx: dict[tuple, list[Fact]] = {}
            for f in self.facts:
                idx.setdefault(f.slot, []).append(f)
            self._slots = idx
        return self._slots

    def by_kind(self, kind: str) -> list[Fact]:
        return [f for f in self.facts if f.fact == kind]

    def find(self, key: str) -> list[Fact]:
        """Facts whose key is `key`, or starts with it (4 characters at least)."""
        key = (key or "").strip().lower()
        exact = [f for f in self.facts if f.key == key]
        if exact or len(key) < 4:
            return exact
        return [f for f in self.facts if f.key.startswith(key)]

    def candidates(self, fact: str, subject: Subject | dict | str, *,
                   canonical: str = "") -> list[Fact]:
        s = subject_of(subject)
        sid = s.id.lower() if s.kind == "tag_value" else s.id
        return list(self._index().get((fact, s.kind, sid, canonical), ()))

    def resolve(self, fact: str, subject: Subject | dict | str, *,
                canonical: str = "") -> Fact | None:
        """The fact that answers (fact, subject), by precedence. `canonical`
        picks the tag_key (canonical) or tag_alias (canonical_key) slot."""
        return pick(self.candidates(fact, subject, canonical=canonical))

    def status_counts(self) -> dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for f in self.facts:
            out[f.status] = out.get(f.status, 0) + 1
        return out

    def kind_counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for f in self.facts:
            out.setdefault(f.fact, {s: 0 for s in STATUSES})[f.status] += 1
        return out

    def stale(self, today: date | None = None) -> list[Fact]:
        return [f for f in self.facts if f.is_stale(today)]

    def conflicts(self) -> list[tuple[Fact, Fact]]:
        """(confirmed winner, proposal that disagrees with it) pairs: a
        proposal never overwrites a confirmed fact, so it waits here."""
        out: list[tuple[Fact, Fact]] = []
        for facts in self._index().values():
            winner = pick(facts)
            if winner is None or not winner.confirmed:
                continue
            for f in facts:
                if f.status == "proposed" and f.key != winner.key:
                    out.append((winner, f))
        return out

    def proposals(self, kind: str | None = None) -> list[Fact]:
        return [f for f in self.facts if f.status == "proposed" and (kind is None or f.fact == kind)]

    # ── teams and aliases ─────────────────────────────────────────────────────

    def _team_fact(self, name: str) -> Fact | None:
        return self.resolve("team", Subject("team", name))

    def _alias(self, raw: str, canonical_keys: tuple[str, ...]) -> Fact | None:
        best: list[Fact] = []
        for ck in canonical_keys:
            best.extend(self.candidates("tag_alias", Subject("tag_value", raw), canonical=ck))
        return pick(best)

    def canonical_team(self, name: str) -> tuple[str, Fact | None]:
        """(canonical team name, the fact that said so). A team fact's own id
        first, then its aliases, then a tag_alias with canonical_key team."""
        low = name.strip().lower()
        teams = ranked(self.by_kind("team"))
        for f in teams:
            if f.subject.id.lower() == low:
                return f.subject.id, f
        for f in teams:
            names = [str(f.value.get("name") or "")] + list(f.value.get("aliases") or [])
            if any(n.lower() == low for n in names if n):
                return f.subject.id, f
        alias = self._alias(name, ("team",))
        if alias is not None:
            return str(alias.value["canonical_value"]), alias
        return name.strip(), None

    def _team_answer(self, team: str, *, confirmed: bool, source: str, key: str | None,
                     stale: bool = False, matched: str | None = None,
                     channel: str | None = None, people: list[str] | None = None) -> Resolved:
        tf = self._team_fact(team)
        if tf is not None:
            channel = channel or tf.value.get("channel")
            people = people or list(tf.value.get("people") or [])
        return Resolved(team=team, channel=channel, people=list(people or []),
                        confirmed=confirmed, source=source, key=key, stale=stale,
                        matched=matched)

    # ── owner_of ──────────────────────────────────────────────────────────────

    def _owner_fact(self, s: Subject) -> Fact | None:
        f = self.resolve("owner", s)
        if f is not None:
            return f
        if s.kind == "repo_path":
            return self._longest_prefix("owner", s.id)
        if s.kind == "k8s_namespace" and "/" in s.id:
            # "cluster/namespace" falls back to a fact about the bare namespace.
            return self.resolve("owner", Subject("k8s_namespace", s.id.rsplit("/", 1)[1]))
        return None

    def _longest_prefix(self, kind: str, path: str) -> Fact | None:
        """The fact for the longest repo_path that contains `path`, matched on
        whole path components ("infra/pay" does not contain "infra/payments")."""
        path = _norm_repo_path(path)
        best: tuple[int, Fact] | None = None
        by_prefix: dict[str, list[Fact]] = {}
        for f in self.by_kind(kind):
            if f.subject.kind == "repo_path" and f.live:
                by_prefix.setdefault(f.subject.id, []).append(f)
        for prefix, facts in by_prefix.items():
            if prefix == "." or path == prefix or path.startswith(prefix + "/"):
                depth = 0 if prefix == "." else prefix.count("/") + 1
                if best is None or depth > best[0]:
                    winner = pick(facts)
                    if winner is not None:
                        best = (depth, winner)
        return best[1] if best else None

    def owner_of(self, subject: Subject | dict | str) -> Resolved | None:
        """Who owns this subject, or None. See Resolved.confirmed before
        letting the answer enable anything."""
        s = subject_of(subject)
        if s.kind == "tag_value":
            return self._team_from_value(s.id, ("team",), matched=str(s),
                                         key_fact=None, default_confirmed=True)
        if s.kind == "team":
            team, tf = self.canonical_team(s.id)
            if tf is None:
                return None
            return self._team_answer(team, confirmed=tf.confirmed, source=tf.source,
                                     key=tf.key, stale=tf.is_stale(), matched=str(s))
        f = self._owner_fact(s)
        if f is None:
            return None
        team, via = self.canonical_team(str(f.value["team"]))
        confirmed = f.confirmed and (via is None or via.fact == "team" or via.confirmed)
        return self._team_answer(team, confirmed=confirmed, source=f.source, key=f.key,
                                 stale=f.is_stale(), matched=str(f.subject),
                                 channel=f.value.get("channel"),
                                 people=list(f.value.get("people") or []))

    # ── tags ──────────────────────────────────────────────────────────────────

    def tag_keys(self, canonical: str) -> list[tuple[str, Fact | None]]:
        """(tag key, the fact that says it means `canonical`), confirmed facts
        first. With no fact at all for team or environment, the conventional
        key is used, unconfirmed."""
        facts = ranked(self.candidates("tag_key", Subject("org", "org"), canonical=canonical))
        out: list[tuple[str, Fact | None]] = []
        seen: set[str] = set()
        for f in facts:
            for k in f.value.get("keys") or []:
                if k.lower() not in seen:
                    seen.add(k.lower())
                    out.append((k, f))
        if not facts:
            defaults = {"team": ("team",), "environment": ("environment", "env")}
            out = [(k, None) for k in defaults.get(canonical, ())]
        return out

    def _team_from_value(self, raw: str, canonical_keys: tuple[str, ...], *,
                         matched: str | None, key_fact: Fact | None,
                         default_confirmed: bool) -> Resolved | None:
        alias = self._alias(raw, canonical_keys + (("team",) if "team" not in canonical_keys
                                                   else ()))
        key_ok = key_fact.confirmed if key_fact is not None else default_confirmed
        if alias is not None:
            team = str(alias.value["canonical_value"])
            team, _ = self.canonical_team(team)
            return self._team_answer(team, confirmed=key_ok and alias.confirmed,
                                     source=alias.source, key=alias.key,
                                     stale=alias.is_stale(), matched=matched)
        team, tf = self.canonical_team(raw)
        if tf is not None:
            return self._team_answer(team, confirmed=key_ok and tf.confirmed, source=tf.source,
                                     key=tf.key, stale=tf.is_stale(), matched=matched)
        return None

    def team_for_tags(self, tags: dict[str, Any]) -> Resolved | None:
        """The team a resource's tags name, read through the org's tag_key and
        tag_alias facts. Team keys first, then owner, then cost_center."""
        if not tags:
            return None
        lower = {str(k).lower(): str(v).strip() for k, v in tags.items()
                 if v is not None and str(v).strip()}
        for canonical in ("team", "owner", "cost_center"):
            for k, kf in self.tag_keys(canonical):
                raw = lower.get(k.lower())
                if not raw:
                    continue
                where = f"tag:{k}={raw}"
                r = self._team_from_value(raw, (canonical,), matched=where, key_fact=kf,
                                          default_confirmed=False)
                if r is not None:
                    return r
                if canonical == "team":
                    # The tag names a team nobody has described yet: still the
                    # team, as sure as the key it came from.
                    return self._team_answer(raw, confirmed=bool(kf and kf.confirmed),
                                             source=kf.source if kf else "default:team-tag",
                                             key=kf.key if kf else None, matched=where)
                if canonical == "owner":
                    return Resolved(team=None, people=[raw], confirmed=bool(kf and kf.confirmed),
                                    source=kf.source if kf else "", key=kf.key if kf else None,
                                    matched=where)
        return None

    # ── environment ───────────────────────────────────────────────────────────

    def environment_of(self, x: Subject | dict | str) -> tuple[str, bool]:
        """(env, confirmed) for a subject or a tag dict. ("unknown", False)
        when nothing says. Only a confirmed answer may enable an action."""
        if _is_subject_like(x):
            s = subject_of(x)
            f = self.resolve("environment", s)
            if f is None and s.kind == "repo_path":
                f = self._longest_prefix("environment", s.id)
            if f is None and s.kind == "k8s_namespace" and "/" in s.id:
                f = self.resolve("environment", Subject("k8s_namespace", s.id.rsplit("/", 1)[1]))
            if f is None:
                return "unknown", False
            return str(f.value["env"]), f.confirmed
        if not isinstance(x, dict):
            return "unknown", False
        lower = {str(k).lower(): str(v).strip() for k, v in x.items()
                 if v is not None and str(v).strip()}
        for k, kf in self.tag_keys("environment"):
            raw = lower.get(k.lower())
            if not raw:
                continue
            key_ok = bool(kf and kf.confirmed)
            alias = self._alias(raw, ("environment",))
            if alias is not None and alias.value["canonical_value"] in ENVIRONMENTS:
                return str(alias.value["canonical_value"]), key_ok and alias.confirmed
            if raw.lower() in ENVIRONMENTS:
                return raw.lower(), key_ok
            for env, words in _ENV_WORDS.items():
                if raw.lower() in words:
                    return env, False
            return "unknown", False
        return "unknown", False

    # ── thresholds ────────────────────────────────────────────────────────────

    def threshold_for(self, team: str | None = None, env: str | None = None) -> dict[str, Any]:
        """Per-scope policy overrides: org, then environment, then team, the
        narrower scope winning. Confirmed facts only: a proposed threshold is a
        guess, and a guess must not raise what may run unasked."""
        out: dict[str, Any] = {}
        scope: dict[str, str] = {}
        subjects: list[Subject] = [Subject("org", "org")]
        if env:
            subjects.append(Subject("environment", env))
        if team:
            subjects.append(Subject("team", self.canonical_team(team)[0]))
        for s in subjects:
            confirmed = [f for f in self.candidates("threshold", s) if f.confirmed]
            f = pick(confirmed)
            if f is None:
                continue
            for name in ("max_auto_monthly_usd", "velocity_cap_usd"):
                if f.value.get(name) is not None:
                    out[name] = float(f.value[name])
                    scope[name] = str(s)
        if out:
            out["scope"] = scope
        return out
