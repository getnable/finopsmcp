#!/usr/bin/env python3
"""
Generate a CycloneDX 1.4 SBOM from pyproject.toml.

Usage:
    python scripts/generate_sbom.py
    # writes docs/sbom.json

The output is machine-readable and suitable for submission to GovCloud
procurement workflows, FedRAMP auditors, and supply-chain security tools.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, timezone, datetime
from pathlib import Path

# Resolve paths relative to repo root regardless of cwd
REPO_ROOT = Path(__file__).parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
OUT_FILE = REPO_ROOT / "docs" / "sbom.json"


def _parse_toml_simple(text: str) -> dict:
    """
    Minimal TOML parser for the fields we need.
    Handles [project], name, version, and the dependencies array.
    Avoids a tomllib import (Python 3.10 has tomllib but only as stdlib in 3.11+).
    """
    result: dict = {}
    current_section: list[str] = []
    in_deps = False
    deps_buffer: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()

        # Skip comments
        if line.startswith("#"):
            continue

        # Section headers — match lines like [section] or [section.subsection]
        # but NOT lines that are just ']' (closing an array)
        m = re.match(r"^\[([A-Za-z0-9_\-. \"']+)\]$", line)
        if m:
            current_section = [s.strip() for s in m.group(1).split(".")]
            in_deps = False
            continue

        # Start of dependencies array in [project]
        if current_section == ["project"] and re.match(r"^dependencies\s*=\s*\[", line):
            in_deps = True
            # might have items on same line
            inner = re.sub(r"^dependencies\s*=\s*\[", "", line).strip()
            if inner.rstrip(",").endswith("]") and not re.search(r'"\s*]', inner):
                # single-line array
                in_deps = False
                inner = inner[:inner.rfind("]")]
            for item in _split_deps(inner):
                deps_buffer.append(item)
            continue

        if in_deps:
            # Detect closing bracket that is NOT inside a quoted string.
            # A line is the array terminator when it is just "]" (possibly with trailing comma)
            # after stripping quoted content.
            stripped_quotes = re.sub(r'"[^"]*"', "", line)
            if "]" in stripped_quotes:
                in_deps = False
                # Collect any quoted items before the bracket
                for item in _split_deps(line):
                    deps_buffer.append(item)
            else:
                for item in _split_deps(line):
                    deps_buffer.append(item)
            continue

        # Key = "value" pairs in [project]
        if current_section == ["project"]:
            m2 = re.match(r'^(\w+)\s*=\s*"([^"]*)"', line)
            if m2:
                result[m2.group(1)] = m2.group(2)

    result["dependencies"] = deps_buffer
    return result


def _split_deps(chunk: str) -> list[str]:
    """Extract quoted strings from a fragment like '"foo>=1.0", "bar"'."""
    return re.findall(r'"([^"]+)"', chunk)


def _parse_dep(dep_str: str) -> tuple[str, str]:
    """
    Split 'fastmcp>=0.4.0' into ('fastmcp', '>=0.4.0').
    Handles extras like 'mcp[cli]>=1.3.0'.
    """
    # Strip extras
    cleaned = re.sub(r"\[.*?\]", "", dep_str)
    m = re.match(r"^([A-Za-z0-9_.\-]+)(.*)", cleaned)
    if not m:
        return dep_str, ""
    name = m.group(1).lower().replace("_", "-")
    version_spec = m.group(2).strip()
    # Best-effort: extract version number
    vm = re.search(r"[\d][^\s,;]*", version_spec)
    version = vm.group(0) if vm else ""
    return name, version


def _purl(name: str, version: str) -> str:
    if version:
        return f"pkg:pypi/{name}@{version}"
    return f"pkg:pypi/{name}"


def build_sbom(meta: dict) -> dict:
    app_name = meta.get("name", "finops-mcp")
    app_version = meta.get("version", "0.0.0")

    components = []
    for dep_str in meta.get("dependencies", []):
        dep_str = dep_str.strip()
        if not dep_str or dep_str.startswith("#"):
            continue
        name, version = _parse_dep(dep_str)
        components.append({
            "type": "library",
            "name": name,
            "version": version,
            "purl": _purl(name, version),
        })

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.4",
        "version": 1,
        "serialNumber": f"urn:uuid:finops-mcp-sbom-{date.today().isoformat()}",
        "metadata": {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "component": {
                "type": "application",
                "name": app_name,
                "version": app_version,
            },
        },
        "components": components,
    }


def main() -> None:
    if not PYPROJECT.exists():
        print(f"error: {PYPROJECT} not found", file=sys.stderr)
        sys.exit(1)

    text = PYPROJECT.read_text(encoding="utf-8")
    meta = _parse_toml_simple(text)
    sbom = build_sbom(meta)

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(sbom, indent=2) + "\n", encoding="utf-8")

    comp_count = len(sbom["components"])
    print(f"SBOM written to {OUT_FILE}")
    print(f"  app: {sbom['metadata']['component']['name']} {sbom['metadata']['component']['version']}")
    print(f"  components: {comp_count}")
    print(f"  format: CycloneDX {sbom['specVersion']}")


if __name__ == "__main__":
    main()
