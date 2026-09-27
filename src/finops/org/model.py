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

Freezes and approval chains are policy too. A freeze (a window with a UTC
offset on each end, a reason, and a mode: ask or deny) only restricts, so
every live freeze counts, a proposal included, and a proposal may only make
the guard ask. An approval chain names who reviews a class of change on a
team or an environment; only a confirmed one names anybody. Several of
either may hold for one subject: a freeze's slot is its start, an approval's
its action classes.

A repo_path subject names a path inside one repo. In a `nable.org/` stored at
the root of that repo the path may be bare ("infra/payments"); anywhere else
(the nable data dir, FINOPS_ORG_DIR) it carries the repo it is in:
"github.com/acme/payments//infra" (the remote, else the root directory's
name). A bare repo_path fact outside a repo belongs to no repo: it is still
shown, and never counts as confirmed where a confirmation picks a team.

Nothing here imports more than the standard library. The guard hook will read
this model on every agent tool call, and its budget is about 100 ms.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import cached_property
from typing import Any

log = logging.getLogger("finops.org")

FACT_KINDS = ("owner", "team", "environment", "tag_key", "tag_alias", "account", "threshold",
              "freeze", "approval")
# "environment" is a subject too, so a threshold can be scoped to one (policy.yaml).
SUBJECT_KINDS = ("aws_account", "gcp_project", "azure_subscription", "k8s_namespace",
                 "repo_path", "service", "tag_value", "resource", "team", "org",
                 "environment")
ACCOUNT_KINDS = ("aws_account", "gcp_project", "azure_subscription")
STATUSES = ("proposed", "confirmed", "rejected", "expired")
LIVE = ("confirmed", "proposed")
ENVIRONMENTS = ("prod", "nonprod", "dr", "sandbox", "shared", "unknown")
CANONICAL_TAG_KEYS = ("team", "environment", "service", "cost_center", "owner")
# A change freeze covers the whole org, a team, an environment or one account.
FREEZE_SUBJECTS = ("org", "team", "environment", *ACCOUNT_KINDS)
FREEZE_MODES = ("ask", "deny")
# An approval chain is a team's or an environment's.
APPROVAL_SUBJECTS = ("team", "environment")
# Who an approval names: a GitHub login ("github:alice"), a GitHub team slug
# in the repo's organisation ("team:platform"), a Jira account id, a Linear
# user id, or an email address (shown, never sent to).
APPROVER_KINDS = ("github", "team", "jira", "linear", "email")

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
    "freeze": "freezes.yaml",
    "approval": "approvals.yaml",
}
KNOWN_FILES = tuple(sorted(set(FILE_FOR_KIND.values())))
HEADER = "# nable org model v1"
FIELD_ORDER = ("fact", "subject", "value", "source", "confidence", "status", "proposed_at",
               "confirmed_by", "confirmed_at", "review_after", "dollars_monthly")
_KNOWN_FIELDS = frozenset(FIELD_ORDER)
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


def _norm_path(raw: str) -> str:
    p = raw.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.strip("/")
    return p or "."


def split_repo_path(ident: str) -> tuple[str | None, str]:
    """(repo, path) for a repo_path id: "github.com/acme/x//infra" is
    ("github.com/acme/x", "infra"); a bare "infra" is (None, "infra")."""
    repo, sep, path = ident.partition("//")
    if not sep:
        return None, ident
    return repo, path


def _norm_repo_path(raw: str) -> str:
    """A repo_path id in its one spelling: the repo part (when there is one)
    lowercased with no slashes around it, the path relative with no ./ or
    trailing slash, "." for the root."""
    text = raw.strip().replace("\\", "/")
    repo, sep, path = text.partition("//")
    if sep:
        repo = repo.strip().strip("/").lower()
        if repo:
            return f"{repo}//{_norm_path(path)}"
        text = path
    return _norm_path(text)


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
        # An unquoted id in YAML is an int, and an int has lost its leading
        # zeros, or was read as octal (012345670123 is 1402433619): the digits
        # on the page are gone, so no id is made up from what is left.
        raise FactError(f"subject.id {ident} is not a 12-digit account id: quote account "
                        "ids in YAML (id: \"012345678901\")")
    if kind == "repo_path":
        ident = _norm_repo_path(ident)
    return Subject(kind, ident)


def fact_key(fact: str, subject: Subject, value: dict[str, Any]) -> str:
    """The stable id shown in the CLI: sha1 over (fact, subject.kind,
    subject.id, the value as canonical JSON), first 10 hex characters."""
    blob = json.dumps([fact, subject.kind, subject.id, value], sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]


def _low(v: Any) -> str:
    return str(v if v is not None else "").strip().lower()


def core_value(fact: str, value: dict[str, Any]) -> tuple:
    """What a fact says, normalised: the schema's own fields, lowercased,
    lists sorted, keys the schema does not name left out. Two facts in one
    slot with the same core say the same thing (a rejection of one holds
    for the other)."""
    v = value

    def many(name: str) -> tuple[str, ...]:
        return tuple(sorted({_low(x) for x in (v.get(name) or []) if _low(x)}))

    if fact == "owner":
        return (_low(v.get("team")),)
    if fact == "team":
        return (many("aliases"), _low(v.get("channel")), many("people"), _low(v.get("parent")))
    if fact == "environment":
        return (_low(v.get("env")),)
    if fact == "tag_key":
        return (many("keys"),)
    if fact == "tag_alias":
        return (_low(v.get("canonical_value")),)
    if fact == "account":
        return tuple(_low(v.get(n)) for n in ("name", "business_unit", "cost_center"))
    if fact == "threshold":
        return tuple(v.get(n) for n in ("max_auto_monthly_usd", "velocity_cap_usd"))
    if fact == "freeze":
        return (_utc_text(v.get("start")), _utc_text(v.get("end")), _low(v.get("mode") or "ask"),
                _low(v.get("reason")))
    if fact == "approval":
        return (many("action_classes"), many("approvers"), v.get("min", 1),
                bool(v.get("change_ticket")))
    return (json.dumps(v, sort_keys=True, default=str),)


def parse_when(v: Any, name: str = "time") -> datetime:
    """An aware datetime from an ISO 8601 string (or a datetime YAML already
    read) that carries a UTC offset: "2026-11-27T00:00:00-05:00" or
    "...T05:00:00Z". A time without one could be any of 24 hours, so it is
    refused rather than read as some zone. Raises FactError."""
    if isinstance(v, datetime):
        dt = v
    elif isinstance(v, str) and v.strip():
        try:
            dt = datetime.fromisoformat(v.strip())
        except ValueError:
            raise FactError(f"{name} {v!r} is not an ISO 8601 date and time") from None
    else:
        raise FactError(f"{name} must be an ISO 8601 date and time with a UTC offset")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise FactError(f"{name} {v!s} has no UTC offset: write it as 2026-11-27T00:00:00-05:00 "
                        "or 2026-11-27T05:00:00Z")
    return dt


def _utc_text(v: Any) -> str:
    """The instant `v` names, in UTC, as text ("" when it names none): what
    two spellings of one time are compared by."""
    try:
        return parse_when(v).astimezone(UTC).isoformat()
    except FactError:
        return _low(v)


_ACTION_CLASS_RE = r"^(?:\*|[a-z][a-z0-9_]{0,63})$"


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
    elif kind == "freeze":
        if subject.kind not in FREEZE_SUBJECTS:
            raise FactError(f"a freeze fact's subject.kind must be one of "
                            f"{', '.join(FREEZE_SUBJECTS)}")
        start = parse_when(v.get("start"), "value.start")
        end = parse_when(v.get("end"), "value.end")
        if end <= start:
            raise FactError("value.end must be after value.start")
        # Kept in the offset it was written in (a person reads it), compared in UTC.
        v["start"], v["end"] = start.isoformat(), end.isoformat()
        _req_str(v, "reason")
        v["reason"] = v["reason"].strip()
        mode = v.get("mode") if v.get("mode") is not None else "ask"
        if mode not in FREEZE_MODES:
            raise FactError(f"value.mode must be one of {', '.join(FREEZE_MODES)}")
        v["mode"] = mode
    elif kind == "approval":
        if subject.kind not in APPROVAL_SUBJECTS:
            raise FactError(f"an approval fact's subject.kind must be one of "
                            f"{', '.join(APPROVAL_SUBJECTS)}")
        classes = [c.lower() for c in _str_list(v.get("action_classes"), "value.action_classes")]
        if not classes:
            raise FactError("value.action_classes is empty")
        bad = [c for c in classes if not re.match(_ACTION_CLASS_RE, c)]
        if bad:
            raise FactError(f"value.action_classes: {bad[0]!r} is not an action class "
                            "(rightsizing, delete_resource, ..., or *)")
        v["action_classes"] = list(dict.fromkeys(classes))
        approvers = _str_list(v.get("approvers"), "value.approvers")
        if not approvers:
            raise FactError("value.approvers is empty")
        for a in approvers:
            k, sep, ident = a.partition(":")
            if not sep or k.strip().lower() not in APPROVER_KINDS or not ident.strip() \
                    or any(c.isspace() for c in ident.strip()):
                raise FactError(f"value.approvers: {a!r} is not kind:id with kind one of "
                                f"{', '.join(APPROVER_KINDS)}")
        v["approvers"] = list(dict.fromkeys(
            f"{a.partition(':')[0].strip().lower()}:{a.partition(':')[2].strip()}"
            for a in approvers))
        least = v.get("min") if v.get("min") is not None else 1
        if isinstance(least, bool) or not isinstance(least, int) or least < 1:
            raise FactError("value.min must be a whole number of 1 or more")
        if least > len(v["approvers"]):
            raise FactError(f"value.min {least} is more than the {len(v['approvers'])} "
                            "approver(s) named")
        v["min"] = least
        if v.get("change_ticket") is not None and not isinstance(v["change_ticket"], bool):
            raise FactError("value.change_ticket must be true or false")
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
    # What a person wrote that this version does not model: envelope keys
    # (`extra`), subject keys (`subject_extra`) and the comment lines just
    # above the entry (`comments`). Written back as they were.
    extra: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
    subject_extra: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
    comments: list[str] = field(default_factory=list, compare=False, repr=False)
    # The directory it was read from, whether that directory is trusted (an
    # org dir a cloned repo ships is not, until a person says so), the repo a
    # bare repo_path in it belongs to (only a nable.org/ at a repo's root has
    # one), and its layer: the model the working directory chose (2), the
    # nable data dir's under it (1), legacy files (0). None of it is saved.
    src_dir: str | None = field(default=None, compare=False, repr=False)
    trusted: bool = field(default=True, compare=False, repr=False)
    anchor: str | None = field(default=None, compare=False, repr=False)
    layer: int = field(default=2, compare=False, repr=False)

    @cached_property
    def key(self) -> str:
        # Computed once: every sort and index uses it, and a sha1 over the
        # value's JSON each time was most of a 5,000-fact query.
        return fact_key(self.fact, self.subject, self.value)

    @cached_property
    def slot(self) -> tuple[str, str, str, str]:
        """What one fact answers. Two facts in a slot with different values
        disagree; a tag_key or tag_alias slot also carries what it maps to,
        since the org subject holds one tag_key fact per canonical key."""
        disc = ""
        if self.fact == "tag_key":
            disc = str(self.value.get("canonical", ""))
        elif self.fact == "tag_alias":
            disc = str(self.value.get("canonical_key", ""))
        elif self.fact == "freeze":
            # One window per start: a subject may have several freezes, and a
            # person moving the end of one replaces it.
            disc = _utc_text(self.value.get("start"))
        elif self.fact == "approval":
            disc = ",".join(sorted(str(c) for c in self.value.get("action_classes") or []))
        sid =self.subject.id.lower() if self.subject.kind == "tag_value" else self.subject.id
        return (self.fact, self.subject.kind, sid, disc)

    @property
    def said(self) -> tuple:
        """(slot, what it says): a rejection suppresses every proposal with
        the same, whatever its spelling or extra value keys."""
        return (self.slot, core_value(self.fact, self.value))

    @property
    def live(self) -> bool:
        return self.status in LIVE

    @property
    def confirmed(self) -> bool:
        return self.status == "confirmed"

    @property
    def loose(self) -> bool:
        """A bare repo_path fact that belongs to no repo (stored outside one)."""
        return (self.subject.kind == "repo_path" and self.anchor is None
                and split_repo_path(self.subject.id)[0] is None)

    @property
    def file(self) -> str | None:
        """The file it was read from, as a path, when known."""
        if self.origin and self.origin != "legacy" and self.src_dir:
            return f"{self.src_dir.rstrip('/')}/{self.origin}"
        return None

    def is_stale(self, today: date | None = None) -> bool:
        if self.status != "confirmed" or not self.review_after:
            return False
        return self.review_after < (today or local_today()).isoformat()

    def to_dict(self) -> dict[str, Any]:
        """The saved form, fields in schema order, empty fields left out,
        then whatever else the entry held."""
        subject: dict[str, Any] = self.subject.to_dict()
        for k, v in self.subject_extra.items():
            subject.setdefault(k, v)
        out: dict[str, Any] = {"fact": self.fact, "subject": subject,
                               "value": dict(self.value), "source": self.source,
                               "confidence": self.confidence, "status": self.status}
        for name in FIELD_ORDER[6:]:
            val = getattr(self, name)
            if val is not None:
                out[name] = val
        for k, v in self.extra.items():
            out.setdefault(k, v)
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
        if not self.trusted:
            d["untrusted"] = True
        if self.loose:
            d["no_repo"] = True
        return d

    def to_cache(self) -> list[Any]:
        """The fact as a JSON row for nable's own caches (guard_org, the
        store's parse cache): already validated, so from_cache skips it."""
        return [self.fact, self.subject.kind, self.subject.id, self.value, self.source,
                self.confidence, self.status, self.proposed_at, self.confirmed_by,
                self.confirmed_at, self.review_after, self.dollars_monthly, self.extra,
                self.subject_extra]

    @classmethod
    def from_cache(cls, row: list[Any], *, origin: str | None = None) -> Fact:
        """A to_cache() row back, without validating it again: a cache holds
        only what from_dict accepted. A malformed row raises (a cache miss)."""
        (kind, skind, sid, value, source, conf, status, proposed_at, by, confirmed_at,
         review_after, dollars, extra, subject_extra) = row
        if kind not in FACT_KINDS or status not in STATUSES or not isinstance(value, dict):
            raise ValueError("not a cached fact")
        return cls(fact=kind, subject=Subject(str(skind), str(sid)), value=value,
                   source=str(source), confidence=float(conf), status=status,
                   proposed_at=proposed_at, confirmed_by=by, confirmed_at=confirmed_at,
                   review_after=review_after, dollars_monthly=dollars, origin=origin,
                   extra=dict(extra or {}), subject_extra=dict(subject_extra or {}))

    @classmethod
    def from_dict(cls, raw: Any, *, origin: str | None = None) -> Fact:
        if not isinstance(raw, dict):
            raise FactError("entry is not a mapping")
        kind = raw.get("fact")
        if kind not in FACT_KINDS:
            raise FactError(f"fact {kind!r} is not one of {', '.join(FACT_KINDS)}")
        raw_subject = raw.get("subject")
        subject = subject_of(raw_subject)
        subject_extra = ({k: v for k, v in raw_subject.items() if k not in ("kind", "id")}
                         if isinstance(raw_subject, dict) else {})
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
        extra = {k: v for k, v in raw.items() if k not in _KNOWN_FIELDS}
        return cls(fact=kind, subject=subject, value=value, source=source.strip(),
                   confidence=confidence, status=status,
                   proposed_at=_iso_date(raw.get("proposed_at"), "proposed_at"),
                   confirmed_by=by or None,
                   confirmed_at=_iso_date(raw.get("confirmed_at"), "confirmed_at"),
                   review_after=_iso_date(raw.get("review_after"), "review_after"),
                   dollars_monthly=dollars, origin=origin, extra=extra,
                   subject_extra=subject_extra)


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


def _layer(f: Fact) -> int:
    return 0 if f.origin == "legacy" else f.layer


def _rank(f: Fact) -> tuple:
    """Sort key for precedence: the maximum wins. Among confirmed facts the
    working directory's model beats the data dir's under it, which beats a
    legacy file, then the newest confirmation; among proposals the layer only
    breaks a tie. The key keeps the order deterministic."""
    if f.status == "confirmed":
        return (1, _layer(f), f.confirmed_at or "", f.key)
    return (0, f.confidence, f.proposed_at or "", _layer(f), f.key)


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




_THRESHOLD_FIELDS = ("max_auto_monthly_usd", "velocity_cap_usd")


class OrgModel:
    """Every fact that applies, file facts over legacy ones, with queries.

    Queries take `strict`: what the guard asks with, where an answer picks a
    team or a threshold. Under it a fact confirmed in an org dir nobody has
    trusted (a `nable.org/` a cloned repo ships), or a bare repo_path fact
    that belongs to no repo, counts as a proposal. A citation may still show
    it, as "likely".

    A confirmed answer is never redirected or downgraded by a proposal: a
    confirmed owner's team is read through confirmed team and alias facts
    only, and the first confirmed owner on the way up from a subject answers
    before any proposal does."""

    def __init__(self, facts: list[Fact], *, dir: Any = None, dir_source: str = "",
                 warnings: list[str] | None = None, layers: list[Any] | None = None) -> None:
        self.facts = facts
        self.dir = dir
        self.dir_source = dir_source
        self.warnings = warnings if warnings is not None else []
        # store.Layer for each directory read, the working directory's first.
        self.layers = layers if layers is not None else []
        self._n = -1
        self._reset()

    def _reset(self) -> None:
        self._slots: dict[tuple, list[Fact]] | None = None
        self._keys: dict[str, list[Fact]] | None = None
        self._kinds: dict[str, list[Fact]] | None = None
        self._repo: dict[str, dict[str, list[tuple[str | None, Fact]]]] = {}
        self._teams: dict[tuple[bool, bool], tuple[dict[str, Fact], dict[str, Fact]]] = {}
        self._canon: dict[tuple[str, bool, bool], tuple[str, Fact | None]] = {}
        self._owned: dict[bool, dict[str, Fact]] = {}

    def _fresh(self) -> None:
        # The indexes are built once per model; a list someone appended to
        # since is indexed again.
        if self._n != len(self.facts):
            self._reset()
            self._n = len(self.facts)

    @staticmethod
    def _sure(f: Fact | None, strict: bool) -> bool:
        """Confirmed, and under `strict` also trusted and tied to a repo."""
        if f is None or not f.confirmed:
            return False
        return not strict or (f.trusted and not f.loose)

    # ── lookup ────────────────────────────────────────────────────────────────

    def _index(self) -> dict[tuple, list[Fact]]:
        self._fresh()
        if self._slots is None:
            idx: dict[tuple, list[Fact]] = {}
            for f in self.facts:
                idx.setdefault(f.slot, []).append(f)
            self._slots = idx
        return self._slots

    def _by_key(self) -> dict[str, list[Fact]]:
        self._fresh()
        if self._keys is None:
            idx: dict[str, list[Fact]] = {}
            for f in self.facts:
                idx.setdefault(f.key, []).append(f)
            self._keys = idx
        return self._keys

    def by_kind(self, kind: str) -> list[Fact]:
        self._fresh()
        if self._kinds is None:
            idx: dict[str, list[Fact]] = {}
            for f in self.facts:
                idx.setdefault(f.fact, []).append(f)
            self._kinds = idx
        return list(self._kinds.get(kind, ()))

    def find(self, key: str) -> list[Fact]:
        """Facts whose key is `key`, or starts with it (4 characters at least)."""
        key = (key or "").strip().lower()
        keys = self._by_key()
        exact = list(keys.get(key, ()))
        if exact or len(key) < 4:
            return exact
        return [f for k, fs in keys.items() if k.startswith(key) for f in fs]

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

    def without_repo(self) -> list[Fact]:
        """Live bare repo_path facts stored outside a repo: they name a path
        in no repo in particular, so the guard never scopes a team by them."""
        return [f for f in self.facts if f.live and f.loose]

    def conflicts(self) -> list[tuple[Fact, Fact]]:
        """(confirmed winner, fact that disagrees with it) pairs: a proposal,
        which never overwrites a confirmed fact, so it waits here; or a second
        confirmed fact in the same file set (two branches that each confirmed
        an answer, merged), which a person has to settle."""
        out: list[tuple[Fact, Fact]] = []
        for facts in self._index().values():
            if len(facts) < 2:
                continue
            winner = pick(facts)
            if winner is None or not winner.confirmed:
                continue
            for f in facts:
                if f.key == winner.key:
                    continue
                merged = (f.confirmed and "legacy" not in (f.origin, winner.origin)
                          and f.src_dir == winner.src_dir)
                if f.status == "proposed" or merged:
                    out.append((winner, f))
        return out

    def proposals(self, kind: str | None = None) -> list[Fact]:
        facts = self.facts if kind is None else self.by_kind(kind)
        return [f for f in facts if f.status == "proposed"]

    # ── teams and aliases ─────────────────────────────────────────────────────

    def _team_table(self, confirmed_only: bool, strict: bool
                    ) -> tuple[dict[str, Fact], dict[str, Fact]]:
        """({lower team id: fact}, {lower name or alias: fact}) over each
        team's winning fact, best first. Built once per model and mode."""
        k = (confirmed_only, strict)
        if k not in self._teams:
            winners: list[Fact] = []
            for slot, facts in self._index().items():
                if slot[0] != "team":
                    continue
                cand = [f for f in facts if f.live and
                        (not confirmed_only or self._sure(f, strict))]
                w = pick(cand)
                if w is not None:
                    winners.append(w)
            winners.sort(key=_rank, reverse=True)
            ids: dict[str, Fact] = {}
            names: dict[str, Fact] = {}
            for f in winners:
                ids.setdefault(f.subject.id.lower(), f)
            for f in winners:
                for n in [str(f.value.get("name") or ""), *(f.value.get("aliases") or [])]:
                    if n:
                        names.setdefault(n.lower(), f)
            self._teams[k] = (ids, names)
        return self._teams[k]

    def _owned_by(self, strict: bool) -> dict[str, Fact]:
        """{lower team name: a confirmed owner fact naming it}."""
        self._fresh()
        if strict not in self._owned:
            out: dict[str, Fact] = {}
            for f in self.by_kind("owner"):
                if self._sure(f, strict):
                    out.setdefault(str(f.value["team"]).strip().lower(), f)
            self._owned[strict] = out
        return self._owned[strict]

    def _team_fact(self, name: str, *, sure_only: bool = False,
                   strict: bool = False) -> Fact | None:
        facts = self.candidates("team", Subject("team", name))
        if sure_only:
            facts = [f for f in facts if self._sure(f, strict)]
        return pick(facts)

    def _alias(self, raw: str, canonical_keys: tuple[str, ...], *, sure_only: bool = False,
               strict: bool = False) -> Fact | None:
        if not raw.strip():
            return None
        best: list[Fact] = []
        for ck in canonical_keys:
            best.extend(self.candidates("tag_alias", Subject("tag_value", raw), canonical=ck))
        if sure_only:
            best = [f for f in best if self._sure(f, strict)]
        return pick(best)

    def canonical_team(self, name: str, *, confirmed_only: bool = False,
                       strict: bool = False) -> tuple[str, Fact | None]:
        """(canonical team name, the fact that said so). A team fact's own id
        first, then its name and aliases, then a tag_alias with canonical_key
        team. confirmed_only reads confirmed facts only: what a confirmed
        answer is read through, so a proposal can neither send it to another
        team nor make it look unconfirmed."""
        low = name.strip().lower()
        ck = (low, confirmed_only, strict)
        hit = self._canon.get(ck) if self._n == len(self.facts) else None
        if hit is None:
            ids, names = self._team_table(confirmed_only, strict)
            f = ids.get(low) or names.get(low)
            if f is not None:
                hit = (f.subject.id, f)
            else:
                alias = self._alias(name, ("team",), sure_only=confirmed_only, strict=strict)
                hit = ((str(alias.value["canonical_value"]), alias) if alias is not None
                       else (name.strip(), None))
            self._canon[ck] = hit
        return hit

    def _team_answer(self, team: str, *, confirmed: bool, source: str, key: str | None,
                     strict: bool = False, stale: bool = False, matched: str | None = None,
                     channel: str | None = None, people: list[str] | None = None) -> Resolved:
        # A confirmed answer takes its channel and people from a confirmed
        # team fact only: a proposal must not reroute a confirmed owner.
        tf = self._team_fact(team, sure_only=confirmed, strict=strict)
        if tf is not None:
            channel = channel or tf.value.get("channel")
            people = people or list(tf.value.get("people") or [])
        return Resolved(team=team, channel=channel, people=list(people or []),
                        confirmed=confirmed, source=source, key=key, stale=stale,
                        matched=matched)

    # ── owner_of ──────────────────────────────────────────────────────────────

    def _repo_index(self, kind: str) -> dict[str, list[tuple[str | None, Fact]]]:
        """{path: [(repo named in the id or None, fact)]} for kind's repo_path facts."""
        self._fresh()
        if kind not in self._repo:
            idx: dict[str, list[tuple[str | None, Fact]]] = {}
            for f in self.by_kind(kind):
                if f.subject.kind == "repo_path":
                    repo, path = split_repo_path(f.subject.id)
                    idx.setdefault(path, []).append((repo, f))
            self._repo[kind] = idx
        return self._repo[kind]

    def _repo_levels(self, kind: str, s: Subject) -> list[list[Fact]]:
        """The repo_path facts about `s` and every directory above it, deepest
        first, matched on whole path components ("infra/pay" is not under
        "infra/payments"). A query naming a repo ("repo//path") matches facts
        about that repo, bare facts from a nable.org/ in it, and bare facts
        that belong to no repo; a bare query matches bare facts."""
        qrepo, qpath = split_repo_path(s.id)
        idx = self._repo_index(kind)
        parts = [] if qpath == "." else qpath.split("/")
        prefixes = ["/".join(parts[:i]) for i in range(len(parts), 0, -1)] + ["."]
        out: list[list[Fact]] = []
        for p in prefixes:
            facts: list[Fact] = []
            for frepo, f in idx.get(p, ()):
                if qrepo is None:
                    if frepo is not None:
                        continue
                else:
                    home = frepo if frepo is not None else f.anchor
                    if home is not None and home != qrepo:
                        continue
                facts.append(f)
            if facts:
                out.append(facts)
        return out

    def _owner_levels(self, s: Subject) -> list[list[Fact]]:
        """The owner facts to try for `s`, most specific first: the subject,
        then a bare namespace for "cluster/namespace", then for a repo path
        each directory above it."""
        if s.kind == "repo_path":
            return self._repo_levels("owner", s)
        levels = [self.candidates("owner", s)]
        if s.kind == "k8s_namespace" and "/" in s.id:
            # "cluster/namespace" falls back to a fact about the bare namespace.
            levels.append(self.candidates("owner", Subject("k8s_namespace",
                                                            s.id.rsplit("/", 1)[1])))
        return levels

    def _owner_fact(self, s: Subject, *, strict: bool = False) -> Fact | None:
        """The owner fact that answers for `s`: the first confirmed one on the
        way up, else the most specific proposal."""
        best: Fact | None = None
        for facts in self._owner_levels(s):
            live = [f for f in facts if f.live]
            if not live:
                continue
            sure = [f for f in live if self._sure(f, strict)]
            if sure:
                return pick(sure)
            if best is None:
                best = pick(live)
        return best

    def owner_of(self, subject: Subject | dict | str, *, strict: bool = False
                 ) -> Resolved | None:
        """Who owns this subject, or None. See Resolved.confirmed before
        letting the answer enable anything."""
        s = subject_of(subject)
        if s.kind == "tag_value":
            return self._team_from_value(s.id, ("team",), matched=str(s), key_fact=None,
                                         default_confirmed=True, strict=strict)
        if s.kind == "team":
            team, tf = self.canonical_team(s.id, confirmed_only=True, strict=strict)
            if tf is None:
                team, tf = self.canonical_team(s.id, strict=strict)
            if tf is None:
                return None
            sure = self._sure(tf, strict)
            return self._team_answer(team, confirmed=sure, strict=strict, source=tf.source,
                                     key=tf.key, stale=tf.is_stale(), matched=str(s))
        f = self._owner_fact(s, strict=strict)
        if f is None:
            return None
        sure = self._sure(f, strict)
        team, _ = self.canonical_team(str(f.value["team"]), confirmed_only=sure, strict=strict)
        return self._team_answer(team, confirmed=sure, strict=strict, source=f.source,
                                 key=f.key, stale=f.is_stale(), matched=str(f.subject),
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
                         default_confirmed: bool, strict: bool = False) -> Resolved | None:
        keys = canonical_keys + (("team",) if "team" not in canonical_keys else ())
        key_ok = self._sure(key_fact, strict) if key_fact is not None else default_confirmed
        # Confirmed readings first, through confirmed facts only; then guesses.
        alias = self._alias(raw, keys, sure_only=True, strict=strict)
        if alias is not None:
            team, _ = self.canonical_team(str(alias.value["canonical_value"]),
                                          confirmed_only=True, strict=strict)
            return self._team_answer(team, confirmed=key_ok, strict=strict,
                                     source=alias.source, key=alias.key,
                                     stale=alias.is_stale(), matched=matched)
        team, tf = self.canonical_team(raw, confirmed_only=True, strict=strict)
        if tf is not None:
            return self._team_answer(team, confirmed=key_ok, strict=strict, source=tf.source,
                                     key=tf.key, stale=tf.is_stale(), matched=matched)
        named = self._owned_by(strict).get(raw.strip().lower())
        if named is not None:
            # A team a confirmed owner fact names is a team the org confirmed:
            # a proposed alias does not turn it into another one.
            return self._team_answer(str(named.value["team"]), confirmed=key_ok,
                                     strict=strict, source=named.source, key=named.key,
                                     matched=matched)
        alias = self._alias(raw, keys)
        if alias is not None:
            team, _ = self.canonical_team(str(alias.value["canonical_value"]))
            return self._team_answer(team, confirmed=False, source=alias.source,
                                     key=alias.key, stale=alias.is_stale(), matched=matched)
        team, tf = self.canonical_team(raw)
        if tf is not None:
            return self._team_answer(team, confirmed=False, source=tf.source, key=tf.key,
                                     stale=tf.is_stale(), matched=matched)
        return None

    def team_for_tags(self, tags: dict[str, Any], *, strict: bool = False) -> Resolved | None:
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
                                          default_confirmed=False, strict=strict)
                if r is not None:
                    return r
                key_ok = self._sure(kf, strict)
                if canonical == "team":
                    # The tag names a team nobody has described yet: still the
                    # team, as sure as the key it came from.
                    return self._team_answer(raw, confirmed=key_ok, strict=strict,
                                             source=kf.source if kf else "default:team-tag",
                                             key=kf.key if kf else None, matched=where)
                if canonical == "owner":
                    return Resolved(team=None, people=[raw], confirmed=key_ok,
                                    source=kf.source if kf else "", key=kf.key if kf else None,
                                    matched=where)
        return None

    # ── environment ───────────────────────────────────────────────────────────

    def environment_of(self, x: Subject | dict | str, *, strict: bool = False
                       ) -> tuple[str, bool]:
        """(env, confirmed) for a subject or a tag dict. ("unknown", False)
        when nothing says. Only a confirmed answer may enable an action. The
        most specific fact answers, a proposal included: a guess about a
        subdirectory may only take a confirmation away."""
        if _is_subject_like(x):
            s = subject_of(x)
            if s.kind == "repo_path":
                levels = self._repo_levels("environment", s)
            else:
                levels = [self.candidates("environment", s)]
                if s.kind == "k8s_namespace" and "/" in s.id:
                    levels.append(self.candidates(
                        "environment", Subject("k8s_namespace", s.id.rsplit("/", 1)[1])))
            for facts in levels:
                f = pick(facts)
                if f is not None:
                    return str(f.value["env"]), self._sure(f, strict)
            return "unknown", False
        if not isinstance(x, dict):
            return "unknown", False
        lower = {str(k).lower(): str(v).strip() for k, v in x.items()
                 if v is not None and str(v).strip()}
        for k, kf in self.tag_keys("environment"):
            raw = lower.get(k.lower())
            if not raw:
                continue
            key_ok = self._sure(kf, strict)
            alias = (self._alias(raw, ("environment",), sure_only=True, strict=strict)
                     or self._alias(raw, ("environment",)))
            if alias is not None and alias.value["canonical_value"] in ENVIRONMENTS:
                return (str(alias.value["canonical_value"]),
                        key_ok and self._sure(alias, strict))
            if raw.lower() in ENVIRONMENTS:
                return raw.lower(), key_ok
            for env, words in _ENV_WORDS.items():
                if raw.lower() in words:
                    return env, False
            return "unknown", False
        return "unknown", False

    # ── thresholds ────────────────────────────────────────────────────────────

    def threshold_for(self, team: str | None = None, env: str | None = None, *,
                      strict: bool = False, ceiling: dict[str, float] | None = None
                      ) -> dict[str, Any]:
        """Per-scope policy overrides: org, then environment, then team, the
        narrower scope winning. Confirmed facts only: a proposed threshold is a
        guess, and a guess must not raise what may run unasked. The team is
        read through confirmed team and alias facts only.

        Under `strict`, a threshold from an org dir nobody trusted (one a
        cloned repo ships) may only lower a figure: it applies where it is
        below what the trusted facts say, or, with none, below `ceiling` (the
        policy's own figures), and is ignored otherwise.

        {max_auto_monthly_usd?, velocity_cap_usd?, scope: {field: "team:x"},
        files: {field: path of the file it came from}}, or {}."""
        out: dict[str, Any] = {}
        scope: dict[str, str] = {}
        files: dict[str, str] = {}
        subjects: list[Subject] = [Subject("org", "org")]
        if env:
            subjects.append(Subject("environment", env))
        if team:
            subjects.append(Subject("team", self.canonical_team(team, confirmed_only=True,
                                                                strict=strict)[0]))
        lower: list[tuple[Subject, Fact]] = []
        for s in subjects:
            confirmed = [f for f in self.candidates("threshold", s) if f.confirmed]
            f = pick([g for g in confirmed if g.trusted or not strict])
            lower += [(s, g) for g in confirmed if strict and not g.trusted]
            if f is None:
                continue
            for name in _THRESHOLD_FIELDS:
                if f.value.get(name) is not None:
                    out[name] = float(f.value[name])
                    scope[name] = str(s)
                    files[name] = f.file or ""
        for s, g in lower:
            for name in _THRESHOLD_FIELDS:
                if g.value.get(name) is None:
                    continue
                cap = out.get(name, (ceiling or {}).get(name))
                if cap is None or float(g.value[name]) >= float(cap):
                    continue
                out[name] = float(g.value[name])
                scope[name] = str(s)
                files[name] = g.file or ""
        if out:
            out["scope"] = scope
            named = {k: v for k, v in files.items() if v}
            if named:
                out["files"] = named
        return out

    # ── freezes and approval chains ───────────────────────────────────────────

    def _team_is(self, name: str, team: str | None, strict: bool) -> bool:
        """Whether team `name` (a fact's subject) is `team`, read through
        confirmed team and alias facts only."""
        if not team:
            return False
        a = self.canonical_team(name, confirmed_only=True, strict=strict)[0]
        b = self.canonical_team(team, confirmed_only=True, strict=strict)[0]
        return a.strip().lower() == b.strip().lower()

    def freezes_at(self, at: datetime | None = None, *, team: str | None = None,
                   envs: Iterable[str] = (), accounts: Iterable[str] = (),
                   strict: bool = False) -> list[tuple[Fact, bool]]:
        """The live freeze facts in force at `at` (default now) over this
        scope: the org, the team, any of the environments, any of the account
        subjects ("aws_account:123..."). (fact, sure) pairs, sure first, then
        deny before ask, then the latest end: a fact is sure when it is
        confirmed (under `strict`, also trusted); anything else is a
        proposal, which may only make the guard ask.

        Every live fact counts, not only each slot's winner: a freeze is a
        restriction, and a proposal or an untrusted repo's word that outranks
        a confirmed freeze in its slot must not take that freeze away."""
        now = (at or datetime.now(UTC)).astimezone(UTC)
        env_set = {e.strip().lower() for e in envs if e}
        acct_set = {str(a).strip() for a in accounts if a}
        out: list[tuple[Fact, bool]] = []
        for f in self.by_kind("freeze"):
            if not f.live:
                continue
            try:
                start = parse_when(f.value.get("start")).astimezone(UTC)
                end = parse_when(f.value.get("end")).astimezone(UTC)
            except FactError:
                continue
            if not start <= now < end:
                continue
            s = f.subject
            covers = (s.kind == "org"
                      or (s.kind == "team" and self._team_is(s.id, team, strict))
                      or (s.kind == "environment" and s.id.strip().lower() in env_set)
                      or (s.kind in ACCOUNT_KINDS and str(s) in acct_set))
            if covers:
                out.append((f, self._sure(f, strict)))
        out.sort(key=lambda fs: (_utc_text(fs[0].value.get("end")), fs[0].key), reverse=True)
        out.sort(key=lambda fs: (not fs[1], fs[0].value.get("mode") != "deny"))
        return out

    def freezes(self, *, live_only: bool = True) -> list[Fact]:
        """Every freeze fact, the soonest to end first."""
        facts = [f for f in self.by_kind("freeze") if f.live or not live_only]
        return sorted(facts, key=lambda f: (_utc_text(f.value.get("end")), f.key))

    def approvals_for(self, action_class: str, *, team: str | None = None,
                      envs: Iterable[str] = (), strict: bool = True) -> list[Fact]:
        """The confirmed approval facts (under `strict`, trusted too) for
        `action_class` over the team and the environments: what a pull
        request requests reviews from and a ticket adds watchers from. A
        proposal names nobody: review requests go to people a person
        named. The winner of each slot, team facts first."""
        wanted = (action_class or "").strip().lower()
        env_set = {e.strip().lower() for e in envs if e}
        slots: dict[tuple, list[Fact]] = {}
        for f in self.by_kind("approval"):
            if not self._sure(f, strict):
                continue
            classes = [str(c).lower() for c in f.value.get("action_classes") or []]
            if wanted not in classes and "*" not in classes:
                continue
            s = f.subject
            if (s.kind == "team" and self._team_is(s.id, team, strict)) or \
                    (s.kind == "environment" and s.id.strip().lower() in env_set):
                slots.setdefault(f.slot, []).append(f)
        out = [w for w in (pick(fs) for fs in slots.values()) if w is not None]
        return sorted(out, key=lambda f: (f.subject.kind != "team", str(f.subject), f.key))
