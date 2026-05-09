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
"""
from __future__ import annotations

import os
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

_RULES_CACHE: dict | None = None


def _load_rules() -> dict:
    global _RULES_CACHE
    if _RULES_CACHE is not None:
        return _RULES_CACHE

    raw = os.environ.get("FINOPS_TAG_RULES", "")
    path = Path(raw).expanduser() if raw else Path.home() / ".finops" / "tag_rules.yaml"

    if not path.exists():
        _RULES_CACHE = {"rules": [], "team_aliases": {}}
        return _RULES_CACHE

    import yaml  # type: ignore[import]
    with open(path) as f:
        _RULES_CACHE = yaml.safe_load(f) or {"rules": [], "team_aliases": {}}
    return _RULES_CACHE


def reload_rules() -> None:
    global _RULES_CACHE
    _RULES_CACHE = None


def _resolve_alias(value: str, aliases: dict[str, list[str]]) -> str:
    lower = value.lower()
    for canonical, variants in aliases.items():
        if lower == canonical or lower in [v.lower() for v in variants]:
            return canonical
    return value


def tags_to_attribution(tags: dict[str, str]) -> dict[str, str]:
    """
    Given a dict of resource tags, return {team, service, environment}.
    Falls back to 'unattributed' for unmapped fields.
    """
    cfg = _load_rules()
    rules: list[dict] = cfg.get("rules", [])
    aliases: dict[str, list[str]] = cfg.get("team_aliases", {})

    result: dict[str, str] = {
        "team": "unattributed",
        "service": "",
        "environment": "",
    }

    # Sort rules by priority (lower = higher priority) then apply
    sorted_rules = sorted(rules, key=lambda r: r.get("priority", 100))
    lower_tags = {k.lower(): v for k, v in tags.items()}

    for rule in sorted_rules:
        tag_key = rule.get("tag_key", "").lower()
        tag_value_pattern = rule.get("tag_value_pattern", "*")
        maps_to_field = rule.get("maps_to_field", "")
        maps_to_value = rule.get("maps_to_value", "")

        if tag_key not in lower_tags:
            continue
        actual_value = lower_tags[tag_key]
        if not fnmatch(actual_value.lower(), tag_value_pattern.lower()):
            continue

        if maps_to_value:
            resolved = maps_to_value
        else:
            resolved = actual_value

        if maps_to_field == "team":
            resolved = _resolve_alias(resolved, aliases)
            result["team"] = resolved
        elif maps_to_field == "service":
            result["service"] = resolved
        elif maps_to_field == "environment":
            result["environment"] = resolved

    return result


def write_example_rules(path: Path | None = None) -> Path:
    """Write an example tag_rules.yaml to help users get started."""
    target = path or (Path.home() / ".finops" / "tag_rules.yaml")
    target.parent.mkdir(parents=True, exist_ok=True)

    content = """\
# FinOps tag attribution rules
# Map resource tags to team / service / environment

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
