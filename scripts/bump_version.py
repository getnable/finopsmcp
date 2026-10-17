#!/usr/bin/env python3
"""Move every carrier of the release version, and nothing else.

Why this exists. The 0.11.0 bump was a find-and-replace of "0.10.0", and it
also turned the `pdfplumber>=0.10.0` dependency floor into `>=0.11.0`. That
floor happened to exist on PyPI. The next blind replace would have asked for a
pdfplumber that may not, and the package would not install.

This script edits only the fields scripts/check_release.py reads as carriers
(pyproject's [project] version, __version__, server.json, the plugin, the
marketplace entry, the MCPB manifest and the plugin's hook pins), regenerates
docs/sbom.json with scripts/generate_sbom.py, then checks that every carrier
says the new version and that no dependency line in pyproject.toml changed.

    python scripts/bump_version.py 0.12.0

It never tags, commits or publishes. docs/RELEASING.md has the rest.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sub_exact(staged: dict[Path, str], path: Path, pattern: str, repl: str,
               count: int) -> None:
    """Stage `pattern` -> `repl` in `path`; it must match exactly `count` times."""
    text = staged.get(path) or path.read_text(encoding="utf-8")
    new, n = re.subn(pattern, repl, text, flags=re.MULTILINE)
    if n != count:
        raise SystemExit(
            f"{path.name} ({path}): expected {count} match(es) of {pattern!r}, found {n}")
    staged[path] = new


def _dependency_lines(pyproject_text: str) -> list[str]:
    """Every line of pyproject.toml except the [project] version line."""
    return [line for line in pyproject_text.splitlines()
            if not re.match(r'^version = "', line)]


def bump(old: str, new: str, root: Path = ROOT) -> None:
    """Rewrite every carrier from `old` to `new`; write nothing unless all match."""
    o, n = re.escape(old), new
    staged: dict[Path, str] = {}
    edits = [
        ("pyproject.toml", rf'^version = "{o}"$', f'version = "{n}"', 1),
        ("src/finops/__init__.py", rf'^__version__ = "{o}"$', f'__version__ = "{n}"', 1),
        ("server.json", rf'"version": "{o}"', f'"version": "{n}"', 2),
        ("plugins/nable/.claude-plugin/plugin.json", rf'"version": "{o}"',
         f'"version": "{n}"', 1),
        ("plugins/nable/.claude-plugin/plugin.json", rf'"finops-mcp=={o}"',
         f'"finops-mcp=={n}"', 1),
        (".claude-plugin/marketplace.json", rf'"version": "{o}"', f'"version": "{n}"', 1),
        ("packaging/mcpb/manifest.json", rf'^  "version": "{o}"', f'  "version": "{n}"', 1),
        ("plugins/nable/hooks/hooks.json", rf'--from finops-mcp=={o} ',
         f'--from finops-mcp=={n} ', 2),
    ]
    for rel, pattern, repl, count in edits:
        _sub_exact(staged, root / rel, pattern, repl, count)
    pyproject = root / "pyproject.toml"
    if _dependency_lines(staged[pyproject]) != _dependency_lines(
            pyproject.read_text(encoding="utf-8")):
        raise SystemExit("pyproject.toml would change outside its version line; not bumping")
    for path, text in staged.items():
        path.write_text(text, encoding="utf-8")


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not _SEMVER.match(argv[1]):
        print("usage: python scripts/bump_version.py X.Y.Z", file=sys.stderr)
        return 2
    new = argv[1]
    check_release = _load("check_release")
    found = set(check_release.carriers(ROOT).values())
    if len(found) != 1:
        print(f"the carriers disagree before the bump: {sorted(found)}", file=sys.stderr)
        return 1
    old = found.pop()
    if old == new:
        print(f"already {new}")
        return 0
    bump(old, new)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_sbom.py")], check=True)
    after = check_release.carriers(ROOT)
    wrong = {k: v for k, v in after.items() if v != new}
    if wrong:
        print(f"carriers not at {new}: {wrong}", file=sys.stderr)
        return 1
    print(f"{old} -> {new} in {len(after)} carriers")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
