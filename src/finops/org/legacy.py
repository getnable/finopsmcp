# SPDX-License-Identifier: Apache-2.0
"""Facts nable already holds in older files, read as org facts.

A human wrote each of these sources, so their facts arrive confirmed, with a
`legacy:` source naming the file or variable:

  tag_rules.yaml (attribution/mapper.py)   tag_key, tag_alias and team facts
  accounts.yaml (accounts.py)              account facts, and an owner fact
                                           where an entry's tags name a team
  FINOPS_REQUIRED_TAGS, FINOPS_PROTECTED_TAGS
                                           tag_key facts, for the keys whose
                                           name says what they mean

FINOPS_GUARD_TEAM and FINOPS_GUARD_ACCOUNT are not facts: they scope one
process, not the org, and stay where they are.

The paths are computed on every call, not at import, so a temporary HOME or
FINOPS_* variable set by a test or a profile switch is honoured. Nothing here
raises: an unreadable source is a warning and no facts.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .model import Fact, FactError, Subject, _warn

# Tag key spellings whose meaning is in the name. A key that is not here (an
# "app" or "product" tag) is left for a person to map.
_KEY_MEANING = {
    "team": "team", "owner": "owner", "owned-by": "owner", "owned_by": "owner",
    "service": "service", "env": "environment", "environment": "environment",
    "stage": "environment", "cost_center": "cost_center", "costcenter": "cost_center",
    "cost-center": "cost_center", "cost_centre": "cost_center", "costcentre": "cost_center",
}
_FIELD_MEANING = {"team": "team", "environment": "environment", "service": "service"}
_GLOB = set("*?[")


def tag_rules_path() -> Path:
    raw = os.environ.get("FINOPS_TAG_RULES", "")
    return Path(raw).expanduser() if raw else Path.home() / ".finops" / "tag_rules.yaml"


def accounts_path() -> Path:
    raw = os.environ.get("FINOPS_ACCOUNTS_FILE", "")
    return Path(raw).expanduser() if raw else Path.home() / ".finops-mcp" / "accounts.yaml"


def _mtime_date(p: Path) -> str | None:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime, tz=UTC).astimezone().date().isoformat()
    except OSError:
        return None


def _read_yaml(p: Path, warnings: list[str] | None) -> Any:
    if not p.is_file():
        return None
    from .store import safe_yaml
    try:
        return safe_yaml(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - a broken legacy file is a warning
        _warn(warnings, f"{p}: not readable as YAML ({type(e).__name__}); no legacy facts from it")
        return None


def _fact(kind: str, subject: Subject, value: dict[str, Any], source: str,
          when: str | None) -> Fact | None:
    try:
        return Fact.from_dict({"fact": kind, "subject": subject.to_dict(), "value": value,
                               "source": source, "confidence": 1.0, "status": "confirmed",
                               "proposed_at": when, "confirmed_at": when}, origin="legacy")
    except FactError:
        return None


def _from_tag_rules(warnings: list[str] | None) -> list[Fact]:
    p = tag_rules_path()
    cfg = _read_yaml(p, warnings)
    if not isinstance(cfg, dict):
        return []
    src, when = "legacy:tag_rules.yaml", _mtime_date(p)
    out: list[Fact] = []
    keys: dict[str, list[str]] = {}
    rules = [r for r in (cfg.get("rules") or []) if isinstance(r, dict)]
    for r in sorted(rules, key=lambda r: r.get("priority", 100)
                    if isinstance(r.get("priority", 100), (int, float)) else 100):
        tag_key = str(r.get("tag_key") or "").strip()
        field = _FIELD_MEANING.get(str(r.get("maps_to_field") or ""))
        if not tag_key or field is None:
            continue
        pattern = str(r.get("tag_value_pattern", "*") or "*")
        to_value = str(r.get("maps_to_value") or "").strip()
        if pattern == "*" and not to_value:
            if tag_key.lower() not in [k.lower() for k in keys.get(field, [])]:
                keys.setdefault(field, []).append(tag_key)
        elif to_value and not (_GLOB & set(pattern)):
            # One literal value renamed: an alias. A glob stays with the mapper,
            # which still applies it; the org model has no pattern facts.
            f = _fact("tag_alias", Subject("tag_value", pattern),
                      {"canonical_key": field, "canonical_value": to_value}, src, when)
            if f:
                out.append(f)
    for field, ks in keys.items():
        f = _fact("tag_key", Subject("org", "org"), {"canonical": field, "keys": ks}, src, when)
        if f:
            out.append(f)
    aliases = cfg.get("team_aliases") or {}
    if isinstance(aliases, dict):
        for canonical, variants in aliases.items():
            name = str(canonical).strip()
            if not name:
                continue
            vs = [str(v).strip() for v in (variants or []) if str(v).strip()] \
                if isinstance(variants, list) else []
            f = _fact("team", Subject("team", name), {"name": name, "aliases": vs}, src, when)
            if f:
                out.append(f)
            for v in vs:
                a = _fact("tag_alias", Subject("tag_value", v),
                          {"canonical_key": "team", "canonical_value": name}, src, when)
                if a:
                    out.append(a)
    return out


def _from_accounts(warnings: list[str] | None) -> list[Fact]:
    p = accounts_path()
    data = _read_yaml(p, warnings)
    if not isinstance(data, dict):
        return []
    src, when = "legacy:accounts.yaml", _mtime_date(p)
    out: list[Fact] = []
    for e in data.get("accounts") or []:
        if not isinstance(e, dict) or not e.get("account_id"):
            continue
        tags = e.get("tags") if isinstance(e.get("tags"), dict) else {}
        low = {str(k).lower(): str(v).strip() for k, v in tags.items() if v is not None}
        value: dict[str, Any] = {}
        if e.get("name"):
            value["name"] = str(e["name"])
        bu = low.get("business_unit") or low.get("businessunit") or low.get("bu")
        cc = low.get("cost_center") or low.get("costcenter") or low.get("cost-center")
        if bu:
            value["business_unit"] = bu
        if cc:
            value["cost_center"] = cc
        subject = Subject("aws_account", str(e["account_id"]).strip())
        f = _fact("account", subject, value, src, when)
        if f:
            out.append(f)
        if low.get("team"):
            o = _fact("owner", subject, {"team": low["team"]}, src, when)
            if o:
                out.append(o)
    return out


def _from_env() -> list[Fact]:
    out: list[Fact] = []
    for var in ("FINOPS_REQUIRED_TAGS", "FINOPS_PROTECTED_TAGS"):
        raw = os.environ.get(var)
        if not raw:
            continue           # the built-in defaults are nable's, not the org's
        keys: dict[str, list[str]] = {}
        for item in raw.split(","):
            k = item.split("=", 1)[0].strip()
            meaning = _KEY_MEANING.get(k.lower())
            if meaning and k not in keys.get(meaning, []):
                keys.setdefault(meaning, []).append(k)
        for meaning, ks in keys.items():
            f = _fact("tag_key", Subject("org", "org"), {"canonical": meaning, "keys": ks},
                      f"legacy:{var}", None)
            if f:
                out.append(f)
    return out


def legacy_facts(*, warnings: list[str] | None = None) -> list[Fact]:
    """Every legacy fact, confirmed, deduplicated by key, in source order."""
    out: list[Fact] = []
    seen: set[str] = set()
    for f in [*_from_tag_rules(warnings), *_from_accounts(warnings), *_from_env()]:
        if f.key not in seen:
            seen.add(f.key)
            out.append(f)
    return out
