#!/usr/bin/env python3
"""Report whether the version this checkout carries has actually been released.

Why this exists. Claude Code installs the nable plugin from this repository's
default branch, so the plugin's pin (`finops-mcp==X` in
plugins/nable/.claude-plugin/plugin.json and plugins/nable/hooks/hooks.json)
reaches users the moment a version bump is merged, not when it is tagged. If X
is not on PyPI, `uvx` cannot resolve it: the plugin's MCP server does not
start, and its guard hook (which ends in `; exit 0` so a broken hook never
blocks the agent) objects to nothing, without a word. That happened:
main carried 0.8.217 and then 0.9.0, 0.10.0 and 0.11.0 for weeks while PyPI's
newest was 0.8.216, because no tag was pushed.

tests/test_upgrade_pin.py already makes every carrier agree with pyproject.
That test cannot see PyPI, and should not: a merge is always ahead of its tag
for a few minutes. This script is the other half. It reads the carriers, asks
PyPI whether that exact release exists and looks for the `vX.Y.Z` tag, then:

    exit 0  released; or unreleased for fewer than --grace-days days since the
            commit that set the version (a merge waiting for its tag)
    exit 1  unreleased for longer than that, or the carriers disagree
    exit 2  the check could not run (PyPI unreachable, no version found)

It never publishes, tags or changes anything. Run it by hand after a release
(`python scripts/check_release.py`), or let .github/workflows/release-gap.yml
run it on a schedule. Standard library only, so it runs before any install.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess  # nosec B404 - fixed git argv, no shell
import sys
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "finops-mcp"
_PIN = re.compile(r"finops-mcp==([0-9][0-9A-Za-z.+-]*)")

# The files that carry the release number, as tests/test_upgrade_pin.py lists
# them (test_every_version_carrier_agrees_with_the_package). Each reader takes
# the repo root and returns the version that file names.


def _json(root: Path, rel: str) -> dict:
    return json.loads((root / rel).read_text(encoding="utf-8"))


def _hook_pins(root: Path) -> list[str]:
    hooks = _json(root, "plugins/nable/hooks/hooks.json")["hooks"]
    pins = []
    for event in ("PreToolUse", "PostToolUse"):
        for entry in hooks.get(event, []):
            for h in entry.get("hooks", []):
                m = _PIN.search(h.get("command", ""))
                pins.append(m.group(1) if m else "")
    return pins


def package_version(root: Path = ROOT) -> str:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def carriers(root: Path = ROOT) -> dict[str, str]:
    """Every place that states the release, and what each one says."""
    init = (root / "src/finops/__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__ = "([^"]+)"', init, re.MULTILINE)
    plugin = _json(root, "plugins/nable/.claude-plugin/plugin.json")
    plugin_pin = _PIN.search(" ".join(plugin["mcpServers"]["nable"]["args"]))
    server = _json(root, "server.json")
    out = {
        "pyproject.toml": package_version(root),
        "src/finops/__init__.py": m.group(1) if m else "",
        "server.json": server["version"],
        "server.json packages[0]": server["packages"][0]["version"],
        "plugins/nable/.claude-plugin/plugin.json": plugin["version"],
        "plugins/nable/.claude-plugin/plugin.json (MCP server pin)":
            plugin_pin.group(1) if plugin_pin else "",
        ".claude-plugin/marketplace.json":
            _json(root, ".claude-plugin/marketplace.json")["plugins"][0]["version"],
        "packaging/mcpb/manifest.json": _json(root, "packaging/mcpb/manifest.json")["version"],
        "docs/sbom.json": _json(root, "docs/sbom.json")["metadata"]["component"]["version"],
    }
    for i, pin in enumerate(_hook_pins(root)):
        out[f"plugins/nable/hooks/hooks.json (hook {i + 1})"] = pin
    return out


def on_pypi(version: str, timeout: float = 15.0) -> bool | None:
    """True when PyPI serves finops-mcp==version, False on a 404, None when
    the answer is unknown (network, rate limit, an unexpected status)."""
    url = f"https://pypi.org/pypi/{PACKAGE}/{version}/json"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310 - fixed https URL
            return r.status == 200
    except urllib.error.HTTPError as e:
        return False if e.code == 404 else None
    except (urllib.error.URLError, OSError, TimeoutError):
        return None


def _git(root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def tag_exists(version: str, root: Path = ROOT) -> bool | None:
    """Whether the vX.Y.Z tag is in this checkout (None when git is missing).
    CI checks out with fetch-depth: 0 so tags are present."""
    out = _git(root, "tag", "--list", f"v{version}")
    return None if out is None else out == f"v{version}"


def bump_age_days(version: str, root: Path = ROOT, now: float | None = None) -> float | None:
    """Days since the oldest commit that set pyproject's version to `version`."""
    out = _git(root, "log", "--format=%ct", f"-Sversion = \"{version}\"", "--", "pyproject.toml")
    stamps = [int(s) for s in (out or "").split() if s.isdigit()]
    if not stamps:
        return None
    return ((now if now is not None else time.time()) - min(stamps)) / 86400.0


def verdict(version: str, disagree: dict[str, str], released: bool | None,
            age_days: float | None, grace_days: float) -> tuple[int, str]:
    """The exit code and the one line that explains it."""
    if disagree:
        listed = ", ".join(f"{k}={v or '(none)'}" for k, v in sorted(disagree.items()))
        return 1, f"the version carriers disagree with pyproject ({version}): {listed}"
    if released is None:
        return 2, f"could not ask PyPI whether {PACKAGE}=={version} exists; nothing is known"
    if released:
        return 0, f"{PACKAGE}=={version} is on PyPI"
    if age_days is not None and age_days < grace_days:
        return 0, (f"{PACKAGE}=={version} is not on PyPI yet; the bump is {age_days:.1f} days "
                   f"old, inside the {grace_days:g}-day grace. Tag v{version} to publish it")
    age = "an unknown time" if age_days is None else f"{age_days:.1f} days"
    return 1, (f"{PACKAGE}=={version} is not on PyPI and this checkout has carried it for "
               f"{age}. Claude Code plugin users run that pin: its MCP server cannot "
               f"start and its guard hook checks nothing. Tag v{version} "
               f"(docs/RELEASING.md)")


def run(root: Path = ROOT, grace_days: float = 2.0,
        pypi: Callable[[str], bool | None] = on_pypi,
        out=sys.stdout) -> int:
    try:
        found = carriers(root)
    except (OSError, KeyError, IndexError, ValueError) as e:
        print(f"release-gap: could not read the version carriers: {e}", file=out)
        return 2
    version = found["pyproject.toml"]
    disagree = {k: v for k, v in found.items() if v != version}
    released = pypi(version) if not disagree else None
    age = bump_age_days(version, root) if released is False else None
    tag = tag_exists(version, root)
    code, line = verdict(version, disagree, released, age, grace_days)

    print(f"version in this checkout: {version}", file=out)
    print(f"on PyPI:                  {_yn(released)}", file=out)
    print(f"tag v{version}:{' ' * max(1, 20 - len(version))}{_yn(tag)}", file=out)
    print(f"result: {line}", file=out)

    if os.environ.get("GITHUB_ACTIONS") == "true":
        level = {0: "notice", 1: "error", 2: "warning"}[code]
        if code == 0 and released is False:
            level = "warning"
        print(f"::{level} title=release gap::{line}", file=out)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(f"### Release gap\n\n- version: `{version}`\n- on PyPI: {_yn(released)}"
                         f"\n- tag `v{version}`: {_yn(tag)}\n\n{line}\n")
    return code


def _yn(v: bool | None) -> str:
    return "unknown" if v is None else ("yes" if v else "no")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("--grace-days", type=float, default=2.0,
                   help="how long an unreleased bump may sit before this fails (default 2)")
    args = p.parse_args(argv)
    return run(grace_days=args.grace_days)


if __name__ == "__main__":
    sys.exit(main())
