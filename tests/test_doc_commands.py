"""Every command the docs teach exists and parses.

Found by extracting each `nable ...` / `finops ...` command from the README,
CAPABILITIES.md, docs/*.md, the plugin's files and packs/*/README.md and
parsing it against the CLI's real argument parser:

  - docs/COST-ATTRIBUTION.md told people to run `nable tags`, which is not a
    command ("nable: unknown command 'tags'")
  - docs/SLACK.md taught `pip install "finops-mcp[slack]"` and `finops-slack`;
    the bot, the extra and the console script left this package in 9c08084
  - four places said Python 3.10 or newer; the package requires 3.11

Placeholders (`<id>`, `[--json]`, `a|b`) are filled in before parsing. A
command a doc shows inside a sentence without its required arguments (`nable
pack run`) counts when the subcommand exists.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import re
import shlex
import tomllib
from pathlib import Path

import pytest

from finops import setup_wizard
from finops.guard_plugin import parse_hook_args

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "README.md", ROOT / "CAPABILITIES.md", ROOT / "plugins/nable/README.md",
        *sorted((ROOT / "docs").glob("*.md")), *sorted((ROOT / "packs").glob("*/README.md")),
        *sorted((ROOT / "plugins/nable/commands").glob("*.md")),
        ROOT / "plugins/nable/skills/cloud-costs/SKILL.md"]
PROGRAMS = (r"uvx --python 3\.12 --from finops-mcp(?:==\S+)? finops",
            r"uvx --from finops-mcp(?:==\S+)? finops", r"uvx --python 3\.12 nable",
            r"uvx nable", r"nable", r"finops")
_PROGRAM = re.compile(r"(?:^|(?<=[\s`$(]))(" + "|".join(PROGRAMS) + r")(?=\s|$|`)")
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())


class _Captured(Exception):
    pass


@pytest.fixture(scope="module")
def parser(tmp_path_factory):
    """The parser setup_wizard.main builds, taken at the moment it parses."""
    real = argparse.ArgumentParser.parse_known_args
    held: dict = {}

    def grab(self, args=None, namespace=None):
        if self.prog == "nable" and "p" not in held:
            held["p"] = self
            raise _Captured
        return real(self, args, namespace)

    mp = pytest.MonkeyPatch()
    mp.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    mp.setenv("NABLE_NO_TELEMETRY", "1")
    mp.setattr(argparse.ArgumentParser, "parse_known_args", grab)
    try:
        with (contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()),
              contextlib.suppress(_Captured)):
            setup_wizard.main(["doctor"])
    finally:
        mp.undo()
    return held["p"]


def _snippets(path: Path):
    in_block, lang, pending = False, "", ""
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip().startswith("```"):
            in_block = not in_block
            lang = line.strip()[3:].strip() if in_block else ""
            continue
        if in_block:
            text = line.strip()
            if lang in ("text", "json", "yaml", "toml") and not text.startswith("$ "):
                continue                    # output and config, not commands
            text = re.sub(r"^\$\s+", "", text)
            if text.endswith("\\"):
                pending += text[:-1] + " "
                continue
            yield n, pending + text, True
            pending = ""
        else:
            for m in re.finditer(r"`([^`]+)`", line):
                yield n, m.group(1), False


def _commands():
    for doc in DOCS:
        for n, text, block in _snippets(doc):
            for m in _PROGRAM.finditer(text):
                if not block and m.start(1) != 0:
                    continue
                before = text[:m.start(1)]
                if block and "#" in before:
                    continue                # inside a comment
                rest = re.sub(r"\[([^\]|]*)\|[^\]]*\]", r"\1", text[m.end(1):])
                rest = re.split(r"\s+#\s|\s*\|\s|\s*;\s|\s*&&\s", rest)[0].strip()
                if rest and not re.match(r"-|[a-z][a-z0-9-]*(\s|$)", rest):
                    continue                # prose or output that names nable
                yield f"{doc.relative_to(ROOT)}:{n}", rest, block


def _argv(rest: str) -> list[str]:
    rest = re.sub(r"\[([^\]|]*)\|[^\]]*\]", r"\1", rest)          # [a | b] -> a
    rest = re.sub(r"\[([^\]]*)\]", r"\1", rest)                     # [x] -> x
    rest = re.sub(r"<[^>]+>", "PLACEHOLDER", rest)
    rest = re.sub(r"\S*\|\S*", lambda m: m.group(0).split("|")[0], rest)
    return shlex.split(rest.replace("...", ""))


def _problem(parser, rest: str, block: bool) -> str | None:
    argv = _argv(rest)
    if argv[:1] == ["setup"]:
        argv = argv[1:]
    if not argv or argv[:1] in (["--help"], ["--version"]):
        return None
    if argv[:2] == ["guard", "hook"]:
        return None if parse_hook_args(argv[2:]) is not None else "bad hook arguments"
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            _ns, extras = parser.parse_known_args(argv)
    except SystemExit as e:
        msg = (err.getvalue().strip().splitlines() or [f"exit {e.code}"])[-1]
        if e.code in (0, None) or (not block and "arguments are required" in msg):
            return None
        return msg
    return f"unrecognized: {extras}" if extras else None


def test_every_documented_command_parses(parser):
    seen = list(_commands())
    assert len(seen) > 100, "the extractor found almost nothing; it is broken"
    bad = [f"{where}: {rest}  ({why})" for where, rest, block in seen
           if (why := _problem(parser, rest, block))]
    assert not bad, "\n".join(bad)


def test_every_documented_console_script_is_installed():
    scripts = set(PYPROJECT["project"]["scripts"])
    named = {m.group(0) for doc in DOCS
             for m in re.finditer(r"\bfinops-(?!mcp\b)[a-z][a-z-]*\b",
                                  doc.read_text(encoding="utf-8"))}
    assert named, "no console script found in the docs; the pattern is broken"
    assert named <= scripts, sorted(named - scripts)


def test_every_documented_extra_exists():
    extras = set(PYPROJECT["project"]["optional-dependencies"])
    named = {m.group(1) for doc in DOCS
             for m in re.finditer(r"finops-mcp\[([a-z-]+)\]", doc.read_text(encoding="utf-8"))}
    assert named <= extras, sorted(named - extras)


def test_the_documented_python_floor_is_the_packages():
    floor = re.fullmatch(r">=(3\.\d+)", PYPROJECT["project"]["requires-python"]).group(1)
    for doc in DOCS:
        for m in re.finditer(r"Python (3\.\d+)(?:\+| or newer)", doc.read_text(encoding="utf-8")):
            assert m.group(1) == floor, f"{doc.relative_to(ROOT)}: Python {m.group(1)}"
