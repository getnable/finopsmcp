"""
Tag-to-team mapper. Reads rules from ~/.finops/tag_rules.yaml (or FINOPS_TAG_RULES).

Example tag_rules.yaml:
  rules:
    - tag_key: "team"
      maps_to_field: "team"
    - tag_key: "service"
      maps_to_field: "service"
    - tag_key: "env"
      maps_to_field: "environment"
    - tag_key: "environment"
      maps_to_field: "environment"
    - tag_key: "costcenter"
      maps_to_field: "team"

  # Optional: normalize free-form tag values to canonical team names
  team_aliases:
    platform: [infra, infrastructure, platform-eng]
    data: [analytics, ml, ml-platform, data-eng]
    frontend: [fe, web, ui]

On top of this file, the org model's confirmed facts (finops.org, the files in
nable.org/) add to it:
  tag_key facts    the keys that mean team, environment or service, read as
                   rules of the default priority (100), after this file's
                   rules of the same priority
  tag_alias facts  value aliases for team, environment and service; a
                   confirmed alias wins over this file's for the same value
  team facts       a team's name and aliases, as team aliases
Proposed facts are never read, and neither are facts that came from this file
(legacy:tag_rules.yaml, whether read in memory or imported by `nable org
init`), so nothing is counted twice. With no org model the mapping is exactly
this file's.
"""
from __future__ import annotations

import os
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

_RULES_CACHE: dict | None = None
# Rules and aliases never change between cost entries, but tags_to_attribution is
# called once per entry (thousands of times for a real org). Compiling the rules
# once (sort + field normalization + a flat alias lookup) turns each call from
# "re-sort R rules and rebuild every lowercased alias list" into a linear scan with
# O(1) alias resolution. _compiled() memoizes it; reload_rules() drops it.
_COMPILED: "_Compiled | None" = None


class _Compiled:
    """Pre-processed rules: sorted once, fields lowercased once, aliases flattened
    into a single {variant_lower: canonical} lookup so alias resolution is a dict
    hit instead of a scan over every alias list on every entry."""

    __slots__ = ("rules", "alias_lookup", "field_aliases")

    def __init__(self, cfg: dict, org: dict | None = None) -> None:
        org = org or {}
        raw_rules: list[dict] = list(cfg.get("rules", []) or []) + list(org.get("rules") or [])
        # Sort by priority (lower = higher priority) ONCE, then normalize the fields
        # the per-entry loop reads so it never lowercases the same literals again.
        self.rules: list[tuple[str, str, str, str]] = [
            (
                str(r.get("tag_key", "")).lower(),
                str(r.get("tag_value_pattern", "*")).lower(),
                str(r.get("maps_to_field", "")),
                str(r.get("maps_to_value", "")),
            )
            for r in sorted(raw_rules, key=lambda r: r.get("priority", 100))
        ]
        aliases: dict[str, list[str]] = cfg.get("team_aliases", {}) or {}
        lookup: dict[str, str] = {}
        for canonical, variants in aliases.items():
            lookup[str(canonical).lower()] = canonical
            for v in variants or []:
                lookup[str(v).lower()] = canonical
        field_aliases = {k: dict(v) for k, v in (org.get("aliases") or {}).items()}
        lookup.update(field_aliases.pop("team", {}))
        self.alias_lookup = lookup
        # environment and service value aliases, from the org model only.
        self.field_aliases = field_aliases


def _load_rules() -> dict:
    global _RULES_CACHE
    if _RULES_CACHE is not None:
        return _RULES_CACHE

    path = _rules_path()

    if not path.exists():
        _RULES_CACHE = {"rules": [], "team_aliases": {}}
        return _RULES_CACHE

    import yaml  # type: ignore[import]
    with open(path) as f:
        _RULES_CACHE = yaml.safe_load(f) or {"rules": [], "team_aliases": {}}
    return _RULES_CACHE


def _rules_path() -> Path:
    raw = os.environ.get("FINOPS_TAG_RULES", "")
    return Path(raw).expanduser() if raw else Path.home() / ".finops" / "tag_rules.yaml"


_ORG_FIELDS = ("team", "environment", "service")


def _org_layer() -> dict:
    """{"rules": [...], "aliases": {field: {value_lower: canonical}}} from the
    org model's confirmed facts, {} with none. Never raises: an org model that
    cannot be read leaves the mapping to tag_rules.yaml alone."""
    try:
        from .. import org
        from ..org.model import ranked
        model = org.load(legacy=False)
    except Exception:  # noqa: BLE001 - attribution must not depend on it
        return {}
    rules_file = _rules_path().exists()

    def usable(f) -> bool:
        # A fact imported from tag_rules.yaml is that file's rule again, and
        # the file itself is read above while it exists.
        return f.confirmed and not (rules_file and f.source.startswith("legacy:tag_rules"))

    rules: list[dict] = []
    seen: set[tuple[str, str]] = set()
    aliases: dict[str, dict[str, str]] = {}
    for f in ranked(model.by_kind("tag_key")):
        field = f.value.get("canonical")
        if not usable(f) or field not in _ORG_FIELDS:
            continue
        for k in f.value.get("keys") or []:
            if (k.lower(), field) not in seen:
                seen.add((k.lower(), field))
                rules.append({"tag_key": k, "maps_to_field": field, "priority": 100})
    # Winner last, so it is the one left in the lookup.
    for f in reversed(ranked(model.by_kind("tag_alias"))):
        field = f.value.get("canonical_key")
        if usable(f) and field in _ORG_FIELDS:
            aliases.setdefault(field, {})[f.subject.id.lower()] = str(f.value["canonical_value"])
    for f in reversed(ranked(model.by_kind("team"))):
        if not usable(f):
            continue
        team = aliases.setdefault("team", {})
        for name in [f.subject.id, f.value.get("name"), *(f.value.get("aliases") or [])]:
            if name:
                team[str(name).lower()] = f.subject.id
    if not rules and not aliases:
        return {}
    return {"rules": rules, "aliases": aliases}


def _compiled() -> _Compiled:
    global _COMPILED
    if _COMPILED is None:
        _COMPILED = _Compiled(_load_rules(), _org_layer())
    return _COMPILED


def reload_rules() -> None:
    global _RULES_CACHE, _COMPILED
    _RULES_CACHE = None
    _COMPILED = None


def _resolve_alias(value: str, alias_lookup: dict[str, str]) -> str:
    return alias_lookup.get(value.lower(), value)


def tags_to_attribution(tags: dict[str, str]) -> dict[str, str]:
    """
    Given a dict of resource tags, return {team, service, environment}.
    Falls back to 'unattributed' for unmapped fields.
    """
    compiled = _compiled()

    result: dict[str, str] = {
        "team": "unattributed",
        "service": "",
        "environment": "",
    }

    lower_tags = {k.lower(): v for k, v in tags.items()}

    # First matching rule wins, per field.
    #
    # Rules are sorted lowest-priority-number first, and the documented contract
    # is "lower number = higher priority", so the first rule that matches a field
    # is the most specific one the user wrote for it. Everything after it is a
    # fallback and must not overwrite.
    #
    # This used to let every matching rule assign, so the LAST rule won and the
    # priority number meant the exact opposite of what it says. Both examples in
    # the file write_example_rules() generates were wrong as a result: an explicit
    # "team" tag was overwritten by a "costcenter" code, and value normalisation
    # rules ("infra*" -> platform) never survived the plain rule beneath them.
    # Attribution feeds chargeback, so the symptom was a team disputing its bill
    # months later, not an error anyone could see.
    decided: set[str] = set()

    for tag_key, tag_value_pattern, maps_to_field, maps_to_value in compiled.rules:
        if maps_to_field in decided:
            continue  # a higher-priority rule already answered for this field
        if tag_key not in lower_tags:
            continue
        actual_value = lower_tags[tag_key]
        if not fnmatch(actual_value.lower(), tag_value_pattern):
            continue

        resolved = maps_to_value if maps_to_value else actual_value

        if maps_to_field == "team":
            result["team"] = _resolve_alias(resolved, compiled.alias_lookup)
        elif maps_to_field in ("service", "environment"):
            result[maps_to_field] = compiled.field_aliases.get(maps_to_field, {}).get(
                resolved.lower(), resolved)
        else:
            continue  # unknown target field, not a decision

        decided.add(maps_to_field)

    return result


def configured_tag_keys() -> list[str]:
    """The tag keys the rules actually reference, de-duplicated in first-seen order.

    What to ask AWS about when attribution comes back empty: checking every tag
    in the account would be noise, and these are the only ones that could have
    produced a team.
    """
    seen: list[str] = []
    for tag_key, _pattern, _field, _value in _compiled().rules:
        if tag_key and tag_key not in seen:
            seen.append(tag_key)
    return seen


def write_example_rules(path: Path | None = None) -> Path:
    """Write an example tag_rules.yaml to help users get started."""
    target = path or (Path.home() / ".finops" / "tag_rules.yaml")
    target.parent.mkdir(parents=True, exist_ok=True)

    content = """\
# FinOps tag attribution rules
# Map resource tags to team / service / environment
#
# The FIRST rule that matches a field wins. Rules are ordered by "priority",
# lowest number first, so put your most specific rules at the lowest numbers and
# your fallbacks at the highest. A rule for a field that has already been decided
# is skipped, which is what makes the fallbacks below safe to leave in place.

rules:
  # Map the "team" tag directly
  - tag_key: "team"
    maps_to_field: "team"
    priority: 10

  # Map "service" tag directly
  - tag_key: "service"
    maps_to_field: "service"
    priority: 10

  # Map "env" or "environment" tag to the environment field
  - tag_key: "env"
    maps_to_field: "environment"
    priority: 10

  - tag_key: "environment"
    maps_to_field: "environment"
    priority: 20

  # If "costcenter" exists, use it as team (lower priority than "team" tag)
  - tag_key: "costcenter"
    maps_to_field: "team"
    priority: 50

  # Map specific tag values to canonical names
  - tag_key: "team"
    tag_value_pattern: "infra*"
    maps_to_field: "team"
    maps_to_value: "platform"
    priority: 5

# Normalize free-form team names to canonical values
team_aliases:
  platform: [infra, infrastructure, platform-eng, sre]
  data: [analytics, ml, ml-platform, data-eng, dbt]
  frontend: [fe, web, ui, design]
  backend: [api, server, services]
"""
    target.write_text(content)
    return target
