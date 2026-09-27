# SPDX-License-Identifier: Apache-2.0
"""Data-pack content: six small schemas, their validators, and typed loaders.

Nothing a data pack ships can execute. YAML is read with yaml.safe_load, so a
`!!python/object` tag is a parse error rather than a constructor call. Text is
filled in by `render()`, a string.Template over dotted keys that reads dict
entries and nothing else: no attribute access, no calls, no format specs, no
Jinja. A report template called `showback.md.j2` is plain text with `${...}`
placeholders; `{{ ''.__class__ }}` in it stays exactly those characters.

    policies     rules: {id, description, applies_to, match, effect}
    guard_rules  rules: {id, target, pattern, verdict (ask|deny), reason, price_hint}
    playbooks    playbooks: {id, finding_type, iac, description, placeholders,
                 diff_template, verify, rollback}
    price_books  rates: {provider, sku, unit, rate, currency, effective_from,
                 effective_to, note}, as YAML or CSV
    reports      any UTF-8 text file
    skills       SKILL.md with name and description frontmatter

The rule language (policies) is deliberately small: a `match` holds `all`
and/or `any` lists of conditions `{field, op, value | value_from}` over a
finding or action dict. Ops: eq, ne, in, not_in, regex, gt, gte, lt, lte,
exists. `field` and `value_from` are dotted paths into the dict. A condition on
a missing field is false. Effects only ever flag, escalate or block: a pack
policy cannot allow anything, so it can never loosen an org policy.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
import string
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .errors import Problem

MAX_CONTENT_BYTES = 1024 * 1024
MAX_TEXT_BYTES = 256 * 1024
MAX_REGEX_LEN = 500
# What a regex condition or guard pattern reads of its input, at most. Keeps a
# bad pattern's worst case bounded by the input rather than by the caller.
MAX_MATCH_INPUT = 4096

_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,79}$")
_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*$")
_PLACEHOLDER = re.compile(r"^[a-z_][a-z0-9_]*$")
_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# The data content types, in [provides] order. manifest.DATA_KINDS is the same.
KINDS = ("policies", "guard_rules", "playbooks", "price_books", "reports", "skills")
OPS = ("eq", "ne", "in", "not_in", "regex", "gt", "gte", "lt", "lte", "exists")
APPLIES_TO = ("finding", "action")
POLICY_ACTIONS = ("flag", "escalate", "block")
SEVERITIES = ("info", "low", "medium", "high", "critical")
GUARD_TARGETS = ("command", "mcp")
GUARD_VERDICTS = ("ask", "deny")
# Most permissive first. tighten() only ever moves right.
VERDICT_ORDER = ("allow", "warn", "ask", "deny")
IAC_KINDS = ("terraform", "helm", "kubernetes", "cloudformation", "cdk", "pulumi")
UNITS = ("hour", "month", "gb", "gb-month", "request", "1k-requests", "1m-requests",
         "1k-tokens", "1m-tokens", "seat-month", "unit")
SKILL_KEYS = ("name", "description", "license")

_MISSING = object()


# ── the safe formatter ────────────────────────────────────────────────────────

class _Template(string.Template):
    # ${a.b.c}: dotted keys into nested dicts. Case-insensitive like the base.
    idpattern = r"[a-z_][a-z0-9_]*(?:\.[a-z0-9_]+)*"


def lookup(obj: Any, path: str) -> Any:
    """Walk `path` ("a.b.c") through nested mappings. Dict keys only: never an
    attribute, never an index, never a call. _MISSING when absent."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, Mapping) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


class _Values(Mapping):
    """The mapping render() substitutes from: scalars only, as text."""

    def __init__(self, values: Mapping[str, Any]):
        self._values = values

    def __getitem__(self, key: str) -> str:
        v = lookup(self._values, key)
        if v is _MISSING or v is None:
            raise KeyError(key)
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float, str, date)):
            return str(v)
        raise KeyError(key)  # a list or dict stays a literal placeholder

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0


def render(text: str, values: Mapping[str, Any]) -> str:
    """Fill `${dotted.key}` placeholders from `values`. A placeholder with no
    scalar value stays as written; nothing in `text` is evaluated."""
    return _Template(text).safe_substitute(_Values(values))


def placeholders(text: str) -> list[str]:
    return list(dict.fromkeys(_Template(text).get_identifiers()))


# ── the rule language ─────────────────────────────────────────────────────────

def _number(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


@dataclass(frozen=True)
class Condition:
    field: str
    op: str
    value: Any = None
    value_from: str | None = None
    regex: re.Pattern[str] | None = None

    def test(self, obj: Mapping[str, Any]) -> bool:
        got = lookup(obj, self.field)
        if self.op == "exists":
            return (got is not _MISSING) is bool(self.value)
        if got is _MISSING:
            return False
        want = lookup(obj, self.value_from) if self.value_from else self.value
        if want is _MISSING:
            return False
        if self.op == "eq":
            return got == want
        if self.op == "ne":
            return got != want
        if self.op == "in":
            return isinstance(want, (list, tuple)) and got in want
        if self.op == "not_in":
            return isinstance(want, (list, tuple)) and got not in want
        if self.op == "regex":
            return isinstance(got, str) and self.regex is not None \
                and self.regex.search(got[:MAX_MATCH_INPUT]) is not None
        a, b = _number(got), _number(want)
        if a is None or b is None:
            return False
        return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[self.op]


@dataclass(frozen=True)
class PolicyRule:
    id: str
    description: str
    applies_to: str
    all: tuple[Condition, ...]
    any: tuple[Condition, ...]
    action: str
    severity: str
    message: str
    pack: str = ""

    def matches(self, obj: Mapping[str, Any]) -> bool:
        if not isinstance(obj, Mapping):
            return False
        if self.all and not all(c.test(obj) for c in self.all):
            return False
        if self.any and not any(c.test(obj) for c in self.any):
            return False
        return bool(self.all or self.any)

    def evaluate(self, obj: Mapping[str, Any]) -> dict[str, Any] | None:
        """The rule's effect on `obj`, or None when it does not match."""
        if not self.matches(obj):
            return None
        return {"rule": self.id, "pack": self.pack, "action": self.action,
                "severity": self.severity, "message": render(self.message, obj)}


def _compile_regex(pattern: Any, where: str, problems: list[Problem]) -> re.Pattern[str] | None:
    if not isinstance(pattern, str) or not pattern:
        problems.append(Problem(where, "must be a non-empty regular expression"))
        return None
    if len(pattern) > MAX_REGEX_LEN:
        problems.append(Problem(where, f"is longer than {MAX_REGEX_LEN} characters"))
        return None
    try:
        return re.compile(pattern)
    except re.error as e:
        problems.append(Problem(where, f"is not a valid regular expression ({e})"))
        return None


def _condition(raw: Any, where: str, problems: list[Problem]) -> Condition | None:
    if not isinstance(raw, dict):
        problems.append(Problem(where, "must be a mapping {field, op, value}"))
        return None
    for k in raw:
        if k not in ("field", "op", "value", "value_from"):
            problems.append(Problem(f"{where}.{k}",
                                    "is not a condition key; known: field, op, value, value_from"))
    fld, op = raw.get("field"), raw.get("op")
    n = len(problems)
    if not isinstance(fld, str) or not _PATH.match(fld):
        problems.append(Problem(f"{where}.field", "must be a dotted path such as spend.growth_pct"))
    if op not in OPS:
        problems.append(Problem(f"{where}.op", f"{op!r} is not one of {', '.join(OPS)}"))
    has_v, has_from = "value" in raw, "value_from" in raw
    if has_v == has_from and op != "exists":
        problems.append(Problem(where, "needs exactly one of value or value_from"))
    vf = raw.get("value_from")
    if has_from and (not isinstance(vf, str) or not _PATH.match(vf)):
        problems.append(Problem(f"{where}.value_from", "must be a dotted path"))
    value = raw.get("value")
    rx = None
    if op in ("in", "not_in") and has_v and not isinstance(value, list):
        problems.append(Problem(f"{where}.value", f"{op} needs a list"))
    if op in ("gt", "gte", "lt", "lte") and has_v and _number(value) is None:
        problems.append(Problem(f"{where}.value", f"{op} needs a finite number"))
    if op == "regex":
        if has_from:
            problems.append(Problem(f"{where}.value_from", "regex needs a literal pattern"))
        else:
            rx = _compile_regex(value, f"{where}.value", problems)
    if op == "exists" and not isinstance(raw.get("value", True), bool):
        problems.append(Problem(f"{where}.value", "exists takes true or false"))
    if len(problems) > n:
        return None
    if op == "exists":
        value = raw.get("value", True)
    return Condition(fld, op, value, vf if has_from else None, rx)


def _strict_keys(raw: dict, allowed: tuple[str, ...], where: str, problems: list[Problem]) -> None:
    for k in raw:
        if k not in allowed:
            problems.append(Problem(f"{where}.{k}", "is not a field; known: " + ", ".join(allowed)))


def _text(raw: Any, where: str, problems: list[Problem], *, limit: int = 2000,
          required: bool = True) -> str:
    if raw is None and not required:
        return ""
    if not isinstance(raw, str) or not raw.strip():
        problems.append(Problem(where, "must be non-empty text"))
        return ""
    if len(raw) > limit:
        problems.append(Problem(where, f"must be {limit} characters or fewer"))
    if _CONTROL.search(raw):
        problems.append(Problem(where, "contains control characters"))
    return raw.strip()


def _items(doc: Any, key: str, rel: str, problems: list[Problem]) -> list[Any]:
    if not isinstance(doc, dict):
        problems.append(Problem(rel, f"must be a mapping with a {key!r} list"))
        return []
    for k in doc:
        if k not in (key, "version"):
            problems.append(Problem(f"{rel}: {k}", f"is not a top-level key; known: {key}, version"))
    if doc.get("version", 1) != 1:
        problems.append(Problem(f"{rel}: version", "the only schema version is 1"))
    items = doc.get(key)
    if not isinstance(items, list) or not items:
        problems.append(Problem(f"{rel}: {key}", "must be a non-empty list"))
        return []
    return items


def _unique(item_id: str, seen: set[str], where: str, problems: list[Problem]) -> None:
    if item_id in seen:
        problems.append(Problem(where, f"id {item_id!r} is used twice"))
    seen.add(item_id)


def parse_policies(doc: Any, rel: str, problems: list[Problem]) -> list[PolicyRule]:
    out: list[PolicyRule] = []
    seen: set[str] = set()
    for i, r in enumerate(_items(doc, "rules", rel, problems)):
        where = f"{rel}: rules[{i}]"
        if not isinstance(r, dict):
            problems.append(Problem(where, "must be a mapping"))
            continue
        n = len(problems)
        _strict_keys(r, ("id", "description", "applies_to", "match", "effect"), where, problems)
        rid = r.get("id")
        if not isinstance(rid, str) or not _ID.match(rid):
            problems.append(Problem(f"{where}.id", "must be a short lowercase id"))
        else:
            _unique(rid, seen, f"{where}.id", problems)
        desc = _text(r.get("description"), f"{where}.description", problems)
        applies = r.get("applies_to", "finding")
        if applies not in APPLIES_TO:
            problems.append(Problem(f"{where}.applies_to",
                                    f"{applies!r} is not one of {', '.join(APPLIES_TO)}"))
        m = r.get("match")
        conds: dict[str, list[Condition]] = {"all": [], "any": []}
        if not isinstance(m, dict) or not m:
            problems.append(Problem(f"{where}.match", "must be a mapping with all and/or any"))
        else:
            _strict_keys(m, ("all", "any"), f"{where}.match", problems)
            for part in ("all", "any"):
                if part not in m:
                    continue
                if not isinstance(m[part], list) or not m[part]:
                    problems.append(Problem(f"{where}.match.{part}",
                                            "must be a non-empty list of conditions"))
                    continue
                for j, c in enumerate(m[part]):
                    cond = _condition(c, f"{where}.match.{part}[{j}]", problems)
                    if cond:
                        conds[part].append(cond)
        eff = r.get("effect")
        action = severity = message = ""
        if not isinstance(eff, dict):
            problems.append(Problem(f"{where}.effect", "must be a mapping {action, severity, message}"))
        else:
            _strict_keys(eff, ("action", "severity", "message"), f"{where}.effect", problems)
            action = eff.get("action")
            if action not in POLICY_ACTIONS:
                extra = (" (a pack policy cannot allow anything, so it can never loosen an org "
                         "policy)" if action == "allow" else "")
                problems.append(Problem(f"{where}.effect.action",
                                        f"{action!r} is not one of {', '.join(POLICY_ACTIONS)}{extra}"))
            severity = eff.get("severity", "medium")
            if severity not in SEVERITIES:
                problems.append(Problem(f"{where}.effect.severity",
                                        f"{severity!r} is not one of {', '.join(SEVERITIES)}"))
            message = _text(eff.get("message"), f"{where}.effect.message", problems, limit=1000)
            if message and not _Template(message).is_valid():
                problems.append(Problem(f"{where}.effect.message",
                                        "has a malformed ${...} placeholder (write $$ for a dollar sign)"))
        if len(problems) == n:
            out.append(PolicyRule(rid, desc, applies, tuple(conds["all"]), tuple(conds["any"]),
                                  action, severity, message))
    return out


# ── guard rules ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GuardRule:
    """A pattern the guard may use to tighten a verdict, never to loosen one."""

    id: str
    target: str
    pattern: re.Pattern[str]
    verdict: str
    reason: str
    price_hint: dict[str, Any] | None = None
    pack: str = ""

    def matches_command(self, command: str) -> bool:
        return self.target == "command" and isinstance(command, str) \
            and self.pattern.search(command[:MAX_MATCH_INPUT]) is not None

    def matches_tool(self, tool: str, args: Any = None) -> bool:
        """MCP calls are matched as "<tool name> <args as sorted JSON>"."""
        if self.target != "mcp" or not isinstance(tool, str):
            return False
        try:
            blob = json.dumps(args if args is not None else {}, sort_keys=True, default=str)
        except (TypeError, ValueError):
            blob = ""
        return self.pattern.search(f"{tool} {blob}"[:MAX_MATCH_INPUT]) is not None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "pack": self.pack, "target": self.target,
                "pattern": self.pattern.pattern, "verdict": self.verdict,
                "reason": self.reason, "price_hint": self.price_hint}


def parse_guard_rules(doc: Any, rel: str, problems: list[Problem]) -> list[GuardRule]:
    out: list[GuardRule] = []
    seen: set[str] = set()
    for i, r in enumerate(_items(doc, "rules", rel, problems)):
        where = f"{rel}: rules[{i}]"
        if not isinstance(r, dict):
            problems.append(Problem(where, "must be a mapping"))
            continue
        n = len(problems)
        _strict_keys(r, ("id", "target", "pattern", "verdict", "reason", "price_hint"),
                     where, problems)
        rid = r.get("id")
        if not isinstance(rid, str) or not _ID.match(rid):
            problems.append(Problem(f"{where}.id", "must be a short lowercase id"))
        else:
            _unique(rid, seen, f"{where}.id", problems)
        target = r.get("target", "command")
        if target not in GUARD_TARGETS:
            problems.append(Problem(f"{where}.target",
                                    f"{target!r} is not one of {', '.join(GUARD_TARGETS)}"))
        rx = _compile_regex(r.get("pattern"), f"{where}.pattern", problems)
        if rx is not None and rx.search("") is not None:
            problems.append(Problem(f"{where}.pattern",
                                    "matches an empty string, so it would fire on every call"))
        verdict = r.get("verdict")
        if verdict not in GUARD_VERDICTS:
            problems.append(Problem(f"{where}.verdict",
                                    f"{verdict!r} is not ask or deny: a guard pack may only "
                                    "tighten, so allow and warn are not verdicts it can give"))
        reason = _text(r.get("reason"), f"{where}.reason", problems, limit=500)
        hint = r.get("price_hint")
        if hint is not None:
            if not isinstance(hint, dict):
                problems.append(Problem(f"{where}.price_hint", "must be a mapping {monthly_usd, note}"))
                hint = None
            else:
                _strict_keys(hint, ("monthly_usd", "note"), f"{where}.price_hint", problems)
                usd = _number(hint.get("monthly_usd"))
                if usd is None or usd < 0:
                    problems.append(Problem(f"{where}.price_hint.monthly_usd",
                                            "must be a finite number of 0 or more"))
                note = hint.get("note")
                if note is not None:
                    _text(note, f"{where}.price_hint.note", problems, limit=300)
                hint = {"monthly_usd": usd, "note": note} if usd is not None else None
        if len(problems) == n and rx is not None:
            out.append(GuardRule(rid, target, rx, verdict, reason, hint))
    return out


def tighten(verdict: str, rules: list[GuardRule], *, command: str | None = None,
            tool: str | None = None, args: Any = None) -> dict[str, Any]:
    """The verdict after pack guard rules, which is never looser than `verdict`.

    For the guard's later wiring: it passes its own verdict in and takes the
    stricter of the two out. Packs can move allow to ask and ask to deny; they
    cannot move anything the other way, and a verdict this does not know (for
    example fail_open) passes through untouched."""
    out = {"verdict": verdict, "rules": []}
    if verdict not in VERDICT_ORDER:
        return out
    best = VERDICT_ORDER.index(verdict)
    for r in rules:
        hit = (command is not None and r.matches_command(command)) or \
              (tool is not None and r.matches_tool(tool, args))
        if not hit:
            continue
        out["rules"].append({"id": r.id, "pack": r.pack, "verdict": r.verdict,
                             "reason": r.reason, "price_hint": r.price_hint})
        best = max(best, VERDICT_ORDER.index(r.verdict))
    out["verdict"] = VERDICT_ORDER[best]
    return out


# ── playbooks ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Playbook:
    id: str
    finding_type: str
    iac: str
    description: str
    placeholders: dict[str, str]
    diff_template: str
    verify: dict[str, Any]
    rollback: str
    pack: str = ""

    def render(self, values: Mapping[str, Any]) -> str:
        """The proposed diff. Every declared placeholder is required, values
        must be plain strings or numbers, and nothing is evaluated."""
        missing = [p for p in self.placeholders if p not in values]
        if missing:
            raise ValueError(f"playbook {self.id} needs {', '.join(missing)}")
        subs: dict[str, str] = {}
        for p in self.placeholders:
            v = values[p]
            if isinstance(v, bool) or not isinstance(v, (str, int, float)):
                raise TypeError(f"playbook {self.id}: {p} must be a string or a number")
            s = str(v)
            if len(s) > 1000 or _CONTROL.search(s):
                raise ValueError(f"playbook {self.id}: {p} is too long or has control characters")
            subs[p] = s
        return _Template(self.diff_template).substitute(subs)


def parse_playbooks(doc: Any, rel: str, problems: list[Problem]) -> list[Playbook]:
    out: list[Playbook] = []
    seen: set[str] = set()
    for i, p in enumerate(_items(doc, "playbooks", rel, problems)):
        where = f"{rel}: playbooks[{i}]"
        if not isinstance(p, dict):
            problems.append(Problem(where, "must be a mapping"))
            continue
        n = len(problems)
        _strict_keys(p, ("id", "finding_type", "iac", "description", "placeholders",
                         "diff_template", "verify", "rollback"), where, problems)
        pid = p.get("id")
        if not isinstance(pid, str) or not _ID.match(pid):
            problems.append(Problem(f"{where}.id", "must be a short lowercase id"))
        else:
            _unique(pid, seen, f"{where}.id", problems)
        ft = p.get("finding_type")
        if not isinstance(ft, str) or not _ID.match(ft):
            problems.append(Problem(f"{where}.finding_type", "must be a finding type id"))
        iac = p.get("iac")
        if iac not in IAC_KINDS:
            problems.append(Problem(f"{where}.iac", f"{iac!r} is not one of {', '.join(IAC_KINDS)}"))
        desc = _text(p.get("description"), f"{where}.description", problems)
        ph = p.get("placeholders") or {}
        if not isinstance(ph, dict) or not all(isinstance(k, str) and _PLACEHOLDER.match(k)
                                               and isinstance(v, str) for k, v in ph.items()):
            problems.append(Problem(f"{where}.placeholders",
                                    "must map lowercase names to a one-line description"))
            ph = {}
        tmpl = p.get("diff_template")
        if not isinstance(tmpl, str) or not tmpl.strip():
            problems.append(Problem(f"{where}.diff_template", "must be non-empty text"))
            tmpl = ""
        elif len(tmpl) > MAX_TEXT_BYTES:
            problems.append(Problem(f"{where}.diff_template", "is too long"))
        elif not _Template(tmpl).is_valid():
            problems.append(Problem(f"{where}.diff_template",
                                    "has a malformed ${...} placeholder (write $$ for a dollar sign)"))
        else:
            used = set(_Template(tmpl).get_identifiers())
            for name in sorted(used - set(ph)):
                problems.append(Problem(f"{where}.diff_template",
                                        f"uses ${{{name}}}, which placeholders does not declare"))
            for name in sorted(set(ph) - used):
                problems.append(Problem(f"{where}.placeholders",
                                        f"{name} is declared but the template never uses it"))
        verify = p.get("verify")
        if not isinstance(verify, dict):
            problems.append(Problem(f"{where}.verify", "must be a mapping {check, within_days}"))
            verify = {}
        else:
            _strict_keys(verify, ("check", "within_days"), f"{where}.verify", problems)
            _text(verify.get("check"), f"{where}.verify.check", problems, limit=500)
            wd = verify.get("within_days", 7)
            if isinstance(wd, bool) or not isinstance(wd, int) or not 1 <= wd <= 90:
                problems.append(Problem(f"{where}.verify.within_days",
                                        "must be a whole number of days from 1 to 90"))
        rollback = _text(p.get("rollback"), f"{where}.rollback", problems, limit=1000)
        if len(problems) == n:
            out.append(Playbook(pid, ft, iac, desc, dict(ph), tmpl,
                                {"check": verify.get("check", "").strip(),
                                 "within_days": verify.get("within_days", 7)}, rollback))
    return out


# ── price books ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PriceRate:
    provider: str
    sku: str
    unit: str
    rate: float
    currency: str
    effective_from: date
    effective_to: date | None = None
    note: str = ""
    pack: str = ""

    def in_effect(self, on: date) -> bool:
        return self.effective_from <= on and (self.effective_to is None or on <= self.effective_to)

    def to_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "sku": self.sku, "unit": self.unit,
                "rate": self.rate, "currency": self.currency,
                "effective_from": self.effective_from.isoformat(),
                "effective_to": self.effective_to.isoformat() if self.effective_to else None,
                "note": self.note, "pack": self.pack}


def _date(raw: Any) -> date | None:
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw.strip())
        except ValueError:
            return None
    return None


_RATE_KEYS = ("provider", "sku", "unit", "rate", "currency", "effective_from", "effective_to",
              "note")


def _rate(r: Any, where: str, problems: list[Problem]) -> PriceRate | None:
    if not isinstance(r, dict):
        problems.append(Problem(where, "must be a mapping"))
        return None
    n = len(problems)
    _strict_keys(r, _RATE_KEYS, where, problems)
    prov = r.get("provider")
    if not isinstance(prov, str) or not _PROVIDER.match(prov.strip().lower()):
        problems.append(Problem(f"{where}.provider", "must be a provider id such as aws"))
    sku = r.get("sku")
    if not isinstance(sku, str) or not sku.strip() or len(sku) > 200 or _CONTROL.search(sku):
        problems.append(Problem(f"{where}.sku", "must be an instance type or SKU, 1 to 200 characters"))
    unit = r.get("unit")
    if unit not in UNITS:
        problems.append(Problem(f"{where}.unit", f"{unit!r} is not one of {', '.join(UNITS)}"))
    rate = r.get("rate")
    if isinstance(rate, str):
        try:
            rate = float(rate)
        except ValueError:
            rate = None
    rate_n = _number(rate)
    if rate_n is None or rate_n < 0:
        problems.append(Problem(f"{where}.rate", "must be a finite number of 0 or more"))
    cur = r.get("currency", "USD")
    if not isinstance(cur, str) or not _CURRENCY.match(cur):
        problems.append(Problem(f"{where}.currency", "must be a three-letter code such as USD"))
    start = _date(r.get("effective_from"))
    if start is None:
        problems.append(Problem(f"{where}.effective_from", "must be a date such as 2026-01-01"))
    end_raw = r.get("effective_to")
    end = _date(end_raw) if end_raw not in (None, "") else None
    if end_raw not in (None, "") and end is None:
        problems.append(Problem(f"{where}.effective_to", "must be a date such as 2026-12-31"))
    if start and end and end < start:
        problems.append(Problem(f"{where}.effective_to", "is before effective_from"))
    note = r.get("note") or ""
    if not isinstance(note, str) or len(note) > 300:
        problems.append(Problem(f"{where}.note", "must be text of 300 characters or fewer"))
    if len(problems) > n:
        return None
    return PriceRate(prov.strip().lower(), sku.strip(), unit, rate_n, cur, start, end, note.strip())


def parse_price_book(doc: Any, rel: str, problems: list[Problem]) -> list[PriceRate]:
    out = []
    for i, r in enumerate(_items(doc, "rates", rel, problems)):
        rate = _rate(r, f"{rel}: rates[{i}]", problems)
        if rate:
            out.append(rate)
    return out


def parse_price_csv(text: str, rel: str, problems: list[Problem]) -> list[PriceRate]:
    reader = csv.DictReader(io.StringIO(text))
    cols = reader.fieldnames or []
    for c in cols:
        if c not in _RATE_KEYS:
            problems.append(Problem(f"{rel}: column {c}", "is not a field; known: "
                                    + ", ".join(_RATE_KEYS)))
    out = []
    for i, row in enumerate(reader):
        clean = {k: v for k, v in row.items() if k in _RATE_KEYS and v not in (None, "")}
        rate = _rate(clean, f"{rel}: row {i + 2}", problems)
        if rate:
            out.append(rate)
    if not out and not problems:
        problems.append(Problem(rel, "has no rates"))
    return out


# ── reports and skills ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ReportTemplate:
    """Text with ${dotted.key} placeholders. Never Jinja, whatever its name."""

    path: str
    text: str
    pack: str = ""

    def render(self, values: Mapping[str, Any]) -> str:
        return render(self.text, values)


@dataclass(frozen=True)
class Skill:
    path: str
    name: str
    description: str
    body: str
    text: str
    license: str | None = None
    pack: str = ""


def parse_skill(text: str, rel: str, problems: list[Problem]) -> Skill | None:
    n = len(problems)
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        problems.append(Problem(rel, "must start with --- frontmatter holding name and description"))
        return None
    lines = text.splitlines(keepends=True)
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        problems.append(Problem(rel, "frontmatter is not closed with ---"))
        return None
    try:
        meta = yaml.safe_load("".join(lines[1:end]))
    except yaml.YAMLError as e:
        problems.append(Problem(rel, f"frontmatter is not valid YAML ({_yaml_reason(e)})"))
        return None
    if not isinstance(meta, dict):
        problems.append(Problem(rel, "frontmatter must be a mapping with name and description"))
        return None
    for k in meta:
        if k == "allowed-tools":
            problems.append(Problem(f"{rel}: allowed-tools",
                                    "a pack skill cannot pre-approve tools for an agent"))
        elif k not in SKILL_KEYS:
            problems.append(Problem(f"{rel}: {k}",
                                    "is not a frontmatter key; known: " + ", ".join(SKILL_KEYS)))
    name = meta.get("name")
    if not isinstance(name, str) or not _SKILL_NAME.match(name):
        problems.append(Problem(f"{rel}: name", "must be lowercase letters, digits and hyphens"))
    desc = _text(meta.get("description"), f"{rel}: description", problems, limit=1024)
    lic = meta.get("license")
    if lic is not None and not isinstance(lic, str):
        problems.append(Problem(f"{rel}: license", "must be text"))
    body = "".join(lines[end + 1:]).strip()
    if not body:
        problems.append(Problem(rel, "has no instructions after the frontmatter"))
    if len(problems) > n:
        return None
    return Skill(rel, name, desc, body, text, lic)


# ── files ─────────────────────────────────────────────────────────────────────

def _yaml_reason(e: Exception) -> str:
    first = str(e).splitlines()[0] if str(e) else type(e).__name__
    return first[:200]


def read_text(path: Path, rel: str, problems: list[Problem], *, limit: int) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            problems.append(Problem(rel, "is not a regular file"))
            return None
        if path.stat().st_size > limit:
            problems.append(Problem(rel, f"is larger than {limit} bytes"))
            return None
        raw = path.read_bytes()
    except OSError as e:
        problems.append(Problem(rel, f"could not be read ({e.strerror or e})"))
        return None
    if b"\x00" in raw:
        problems.append(Problem(rel, "is binary; content files must be text"))
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        problems.append(Problem(rel, "is not UTF-8 text"))
        return None


def _load_yaml(text: str, rel: str, problems: list[Problem]) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        # A !!python/object tag lands here: safe_load constructs no objects.
        problems.append(Problem(rel, f"is not valid YAML for a data pack ({_yaml_reason(e)})"))
        return None


def load_file(kind: str, path: Path, rel: str, problems: list[Problem]) -> list[Any]:
    """Load one content file of `kind`, appending any problems. Returns the
    typed items (empty when the file is invalid)."""
    limit = MAX_TEXT_BYTES if kind in ("reports", "skills") else MAX_CONTENT_BYTES
    text = read_text(path, rel, problems, limit=limit)
    if text is None:
        return []
    n = len(problems)
    if kind == "reports":
        return [ReportTemplate(rel, text)]
    if kind == "skills":
        if path.name != "SKILL.md":
            problems.append(Problem(rel, "a skill file must be named SKILL.md"))
            return []
        s = parse_skill(text, rel, problems)
        return [s] if s else []
    if kind == "price_books" and path.suffix.lower() == ".csv":
        items: list[Any] = parse_price_csv(text, rel, problems)
    else:
        if path.suffix.lower() not in (".yaml", ".yml"):
            problems.append(Problem(rel, f"{kind} files must be .yaml or .yml"
                                    + (" (or .csv)" if kind == "price_books" else "")))
            return []
        doc = _load_yaml(text, rel, problems)
        if len(problems) > n:
            return []
        parser = {"policies": parse_policies, "guard_rules": parse_guard_rules,
                  "playbooks": parse_playbooks, "price_books": parse_price_book}[kind]
        items = parser(doc, rel, problems)
    return items if len(problems) == n else []


@dataclass
class PackContent:
    """Everything a pack's [provides] loads to, by kind, plus what was wrong."""

    items: dict[str, list[Any]] = field(default_factory=dict)
    files: dict[str, list[str]] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {k: len(v) for k, v in self.items.items() if v}


def load_content(root: Path, provides: Mapping[str, tuple[str, ...]]) -> PackContent:
    """Expand each [provides] glob under `root` and load what it names.

    A glob that matches nothing is a problem (a typo would otherwise ship an
    empty pack that looks fine). Matches are sorted, so load order, and with it
    which rule wins a tie, never depends on the filesystem."""
    out = PackContent()
    root = Path(root)
    for kind, globs in provides.items():
        items: list[Any] = []
        rels: list[str] = []
        for g in globs:
            matches = sorted(p for p in root.glob(g) if p.is_file() and not p.is_symlink())
            if not matches:
                out.problems.append(Problem(f"provides.{kind}", f"{g!r} matches no file"))
                continue
            for p in matches:
                rel = p.relative_to(root).as_posix()
                if rel in rels:
                    continue
                rels.append(rel)
                items.extend(load_file(kind, p, rel, out.problems))
        out.items[kind] = items
        out.files[kind] = rels
    return out
