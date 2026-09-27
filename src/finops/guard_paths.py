# SPDX-License-Identifier: Apache-2.0
"""The files that decide what the guard allows, which an agent must not edit.

The org model, the policy file, the installed packs and the guard's own
switches and records are how a person tells the guard what is allowed: a
confirmed per-team threshold, a trusted pack key, the off flag. An agent
that edits them directly can loosen its own guard (append a confirmed
threshold to nable.org/policy.yaml, add a key to packs.trusted_keys, delete
a guard-rule pack, touch ~/.finops/guard-off), so every write to one asks:

  a shell command       guard.gate_command (the protected-write self rule)
  an MCP tool call      guard.gate_mcp_call (a path or a command argument)
  Claude Code's editor  Write, Edit, MultiEdit and NotebookEdit, answered by
                        guard_plugin before the guard is imported

protected() lists them; match() says whether a path is one of them. Both
run on every editor tool call Claude Code sends the hook, before the guard
is imported, so this module is standard library only, creates nothing (the
data directory rule is copied from guard_ledger._data_dir without its
mkdir), reads no file and runs no subprocess: a few environment variables,
a walk up to the git root and a realpath per entry. Where a module that
knows a path better is already imported (a test's override of the ledger
or the packs root), its answer is used instead.

Paths are compared after realpath on both sides, so `~`, `$HOME`, a
relative path from inside the repo, `..` and a symlinked directory name all
reach the same file. On macOS and Windows, whose default filesystems ignore
case, the comparison does too.
"""
from __future__ import annotations

import fnmatch
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

# The names nable gives the files beside the decision ledger (guard_ledger,
# budget/summary.py, guard_org.py). Copied, not imported: this module must
# not pull in the ledger's redaction patterns.
_LEDGER_FILES = (
    ("guard-ledger.jsonl", "the guard's decision ledger"),
    ("guard-ledger.anchor.json", "the decision ledger's anchor"),
    ("guard-ledger.unrecorded.jsonl", "the record of verdicts the ledger missed"),
    ("budget-summary.json", "the budget summary the guard checks spend against"),
    ("org-model-cache.json", "the guard's cache of the org model"),
    ("packs-guard-cache.json", "the guard's cache of pack rules and price books"),
)
POLICY_FILE_NAME = "nable.policy.yaml"
ORG_DIR_NAME = "nable.org"
# Any file with one of these names: `nable budget ci-gate --budget-file` and
# sync_budgets_from_yaml read budgets from it, wherever it is kept.
PROTECTED_NAMES = {"budget.yml": "a budget file the guard's budgets come from",
                   "budget.yaml": "a budget file the guard's budgets come from"}
_FOLD = sys.platform in ("darwin", "win32")


class Protected(NamedTuple):
    """A NamedTuple, not a dataclass: importing dataclasses costs the
    file-edit fast path more than everything else it does."""

    path: str       # absolute and resolved (realpath), the form compared
    shown: str      # as a person would write it: ~ for the home directory
    what: str       # what it is, for the reason a human reads
    tree: bool = False      # a directory: everything under it is protected too

    def as_dict(self) -> dict:
        return {"path": self.shown, "what": self.what, "tree": self.tree}


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _home() -> Path:
    return Path.home()


# What asking an already-imported module for a path may raise (its data dir
# could not be created); the copied rule answers instead.
_LOOKUP_ERRORS = (OSError, AttributeError, TypeError, ValueError)


def data_dir() -> Path:
    """guard_ledger._data_dir's rule (FINOPS_PROFILE, FINOPS_DATA_DIR or
    ~/.finops), without creating anything. A test pins that the two agree."""
    db = sys.modules.get("finops.storage.db")
    try:
        if db is not None:
            return Path(db.data_dir())
    except _LOOKUP_ERRORS:
        db = None
    profile = _env("FINOPS_PROFILE")
    if profile:
        return _home() / ".finops" / "profiles" / profile
    raw = os.environ.get("FINOPS_DATA_DIR", "")
    return Path(raw).expanduser() if raw else _home() / ".finops"


def _ledger() -> Path:
    led = sys.modules.get("finops.guard_ledger")
    try:
        if led is not None:
            return Path(led.ledger_path())
    except _LOOKUP_ERRORS:
        led = None
    return data_dir() / "guard-ledger.jsonl"


def _packs_root() -> Path:
    store = sys.modules.get("finops.packs.store")
    try:
        if store is not None:
            return Path(store.packs_root())
    except _LOOKUP_ERRORS:
        store = None
    return data_dir() / "packs"


def _policy_files() -> list[Path]:
    """FINOPS_POLICY_FILE and the data directory's nable.policy.yaml: both,
    since the agent's shell may not carry the variable the hook sees."""
    out = []
    raw = _env("FINOPS_POLICY_FILE")
    if raw:
        out.append(Path(raw).expanduser())
    out.append(data_dir() / POLICY_FILE_NAME)
    return out


def git_root(start: str | os.PathLike | None = None) -> Path | None:
    """finops.org.store.git_root, copied: the org package is not light."""
    try:
        p = Path(start or os.getcwd()).resolve()
    except (OSError, RuntimeError):
        return None
    for d in (p, *p.parents):
        if (d / ".git").exists():
            return d
    return None


def _claude_user_dir() -> Path:
    from . import guard_plugin
    return guard_plugin.claude_user_dir()


def _project_dirs(cwd: str | None) -> list[Path]:
    """Where the agent's project settings live: CLAUDE_PROJECT_DIR, the
    working directory and its git root."""
    dirs: list[Path] = []
    for raw in (_env("CLAUDE_PROJECT_DIR"), cwd or ""):
        if raw:
            dirs.append(Path(raw).expanduser())
    try:
        dirs.append(Path(cwd) if cwd else Path.cwd())
    except OSError:
        pass
    root = git_root(cwd)
    if root is not None:
        dirs.append(root)
    return list(dict.fromkeys(dirs))


def _managed_settings() -> list[Path]:
    if sys.platform == "darwin":
        return [Path("/Library/Application Support/ClaudeCode/managed-settings.json")]
    if sys.platform == "win32":
        return [Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
                / "ClaudeCode" / "managed-settings.json"]
    return [Path("/etc/claude-code/managed-settings.json")]


def _hook_files(projects: list[Path]) -> list[tuple[Path, str]]:
    """Every settings or hooks file a harness reads the guard's hook from
    (guard_adapters.hooks_path, per harness and scope; a test pins that the
    two agree), plus Codex's config.toml, whose [features] table can turn
    its hooks off."""
    home = _home()
    codex = Path(_env("CODEX_HOME")) if _env("CODEX_HOME") else home / ".codex"
    copilot = Path(_env("COPILOT_HOME")) if _env("COPILOT_HOME") else home / ".copilot"
    gemini = (Path(_env("GEMINI_CLI_HOME")) if _env("GEMINI_CLI_HOME") else home) / ".gemini"
    user = _claude_user_dir()
    out = [(user / "settings.json", "Claude Code's user settings, which carry the guard hook"),
           (user / "settings.local.json", "Claude Code's user settings"),
           (home / ".cursor" / "hooks.json", "Cursor's hooks file, which carries the guard hook"),
           (codex / "hooks.json", "Codex's hooks file, which carries the guard hook"),
           (codex / "config.toml", "Codex's config, which can turn its hooks off"),
           (copilot / "hooks" / "nable-guard.json", "Copilot's guard hook file"),
           (gemini / "settings.json", "Gemini CLI's settings, which carry the guard hook"),
           (home / "Documents" / "Cline" / "Hooks" / "PreToolUse", "Cline's guard hook")]
    out += [(p, "Claude Code's managed settings") for p in _managed_settings()]
    for d in projects:
        out += [(d / ".claude" / "settings.json", "this project's Claude Code settings"),
                (d / ".claude" / "settings.local.json", "this project's Claude Code settings"),
                (d / ".cursor" / "hooks.json", "this project's Cursor hooks"),
                (d / ".codex" / "hooks.json", "this project's Codex hooks"),
                (d / ".codex" / "config.toml", "this project's Codex config"),
                (d / ".github" / "hooks" / "nable-guard.json", "this project's Copilot guard hook"),
                (d / ".gemini" / "settings.json", "this project's Gemini CLI settings"),
                (d / ".clinerules" / "hooks" / "PreToolUse", "this project's Cline guard hook")]
    return out


def _real(p: Path | str) -> str:
    s = os.path.realpath(os.fspath(p))
    return s.casefold() if _FOLD else s


def _shown(p: Path) -> str:
    s = os.fspath(p)
    home = os.fspath(_home())
    if home and home != os.sep and (s == home or s.startswith(home + os.sep)):
        return "~" + s[len(home):]
    return s


def protected(cwd: str | None = None) -> list[Protected]:
    """Every protected path for an agent working in `cwd` (default: this
    process's), most important first, each once."""
    from . import guard_plugin

    ledger = _ledger()
    data = data_dir()
    entries: list[tuple[Path, str, bool]] = []
    org_env = _env("FINOPS_ORG_DIR")
    if org_env:
        entries.append((Path(org_env).expanduser(), "the org model (FINOPS_ORG_DIR)", True))
    root = git_root(cwd)
    if root is not None:
        # Whether it exists or not: creating it is what makes it the org
        # model this repo's agents are judged by.
        entries.append((root / ORG_DIR_NAME, "this repo's org model", True))
    entries.append((data / "org", "the org model in nable's data directory", True))
    entries += [(p, "the guard's policy file", False) for p in _policy_files()]
    packs = _packs_root()
    entries.append((packs, "the installed packs and their index", True))
    entries.append((guard_plugin.off_flag_path(), "the guard's off switch", False))
    for dirpath in dict.fromkeys((ledger.parent, data)):
        entries += [(dirpath / name, what, False) for name, what in _LEDGER_FILES]
    ai = Path(os.environ["FINOPS_DATA_DIR"]).expanduser() if os.environ.get(
        "FINOPS_DATA_DIR") else _home() / ".nable"
    entries.append((ai / "ai-budget.json", "the agent's AI budget", False))
    tag_rules = _env("FINOPS_TAG_RULES")
    accounts = _env("FINOPS_ACCOUNTS_FILE")
    entries.append((Path(tag_rules).expanduser() if tag_rules
                    else _home() / ".finops" / "tag_rules.yaml",
                    "tag rules the org model reads as confirmed facts", False))
    entries.append((Path(accounts).expanduser() if accounts
                    else _home() / ".finops-mcp" / "accounts.yaml",
                    "accounts the org model reads as confirmed facts", False))
    entries += [(p, what, False) for p, what in _hook_files(_project_dirs(cwd))]
    entries.append((_claude_user_dir() / "plugins",
                    ("Claude Code's installed plugins, the nable plugin's guard hook "
                     "among them"), True))
    out: list[Protected] = []
    seen: set[str] = set()
    for p, what, tree in entries:
        try:
            key = _real(p)
        except (OSError, ValueError):
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(Protected(key, _shown(p), what, tree))
    return out


_VAR_RE = re.compile(r"\$(?:\{(\w+)\}|(\w+))")
_GLOB_CHARS = frozenset("*?[")
_BRACE_RE = re.compile(r"\{([^{}]*,[^{}]*)\}")
_BRACE_MAX = 16


def _expand(path: str, env: Mapping[str, str] | None) -> str | None:
    """`$VAR` and `${VAR}` from `env` (assignments earlier on the command
    line), then the environment; `~` and `~user`. None when a variable is
    unset, or written in a form this does not read (`${X:-y}`)."""
    def one(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        val = (env or {}).get(name)
        if val is None:
            val = os.environ.get(name)
        if val is None:
            return m.group(0)
        # `D=~/.finops` assigns the expanded home, as the shell does.
        return os.path.expanduser(val) if val.startswith("~") else val
    for _ in range(4):                  # D=$HOME/x; E=$D/y; rm $E/z
        if "$" not in path:
            break
        path = _VAR_RE.sub(one, path)
    if "$" in path:
        return None
    return os.path.expanduser(path)


def _braces(path: str) -> list[str]:
    """One level of brace expansion: `~/.finops/{a,guard-off}` is two paths."""
    m = _BRACE_RE.search(path)
    if m is None:
        return [path]
    alts = m.group(1).split(",")[:_BRACE_MAX]
    return [path[:m.start()] + a + path[m.end():] for a in alts]


def resolve(path: str, cwd: str | None = None, *,
            env: Mapping[str, str] | None = None) -> str | None:
    """`path` as the shell or an editor would open it from `cwd`: variables
    and `~` expanded, relative to `cwd`, symlinks resolved. None when it
    cannot be resolved (an unset variable, a NUL byte)."""
    if not isinstance(path, str) or not path or "\0" in path:
        return None
    p = _expand(path, env)
    if p is None:
        return None
    if not os.path.isabs(p):
        base = cwd or os.getcwd()
        p = os.path.join(os.path.expanduser(base), p)
    try:
        return _real(p)
    except (OSError, ValueError):
        return None


def _under(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def is_under(child: str, parent: str) -> bool:
    """Both resolved: `child` is `parent` or inside it."""
    return _under(child, parent)


def _match_glob(pattern: str, cwd: str | None, env: Mapping[str, str] | None,
                entries: list[Protected], ancestors: bool) -> Protected | None:
    """A glob (`nable.org/*.yaml`, `~/.fin*/guard-off`) that could name a
    protected path. The fixed directory in front of the first wildcard is
    resolved; the rest is matched as fnmatch does, where `*` also crosses a
    `/`, which can only over-match."""
    p = _expand(pattern, env)
    if p is None:
        return None
    if not os.path.isabs(p):
        p = os.path.join(os.path.expanduser(cwd or os.getcwd()), p)
    parts = p.split(os.sep)
    i = next(n for n, part in enumerate(parts) if _GLOB_CHARS & set(part))
    fixed = os.sep.join(parts[:i]) or os.sep
    try:
        fixed = _real(fixed)
    except (OSError, ValueError):
        return None
    full = os.path.join(fixed, *parts[i:])
    if _FOLD:
        full = full.casefold()
    for e in entries:
        if e.tree and _under(fixed, e.path):
            return e                    # whatever it matches is inside a protected tree
        if fnmatch.fnmatchcase(e.path, full):
            return e
        if ancestors and _under(e.path, fixed):
            d = e.path
            while _under(d, fixed) and d != fixed:
                if fnmatch.fnmatchcase(d, full):
                    return e
                d = os.path.dirname(d)
    name = os.path.basename(full)
    for n, what in PROTECTED_NAMES.items():
        if fnmatch.fnmatchcase(n, name):
            return Protected(full, _shown(Path(full)), what)
    return None


def match(path: str, cwd: str | None = None, *, entries: list[Protected] | None = None,
          ancestors: bool = False, env: Mapping[str, str] | None = None) -> Protected | None:
    """The protected entry `path` is, or is inside; with `ancestors`, also
    one it contains (`rm -rf ~/.finops` takes the ledger with it). `env`
    holds variables assigned earlier on a command line. Braces and globs are
    read as the shell would expand them. None when it is none of them."""
    entries = entries if entries is not None else protected(cwd)
    for one in _braces(path) if "{" in path else (path,):
        if _GLOB_CHARS & set(one):
            hit = _match_glob(one, cwd, env, entries, ancestors)
        else:
            hit = _match_one(one, cwd, env, entries, ancestors)
        if hit is not None:
            return hit
    return None


def _match_one(path: str, cwd: str | None, env: Mapping[str, str] | None,
               entries: list[Protected], ancestors: bool) -> Protected | None:
    real = resolve(path, cwd, env=env)
    if real is None:
        return None
    for e in entries:
        if real == e.path or (e.tree and _under(real, e.path)):
            return e
        if ancestors and _under(e.path, real):
            return e
    name = os.path.basename(real)
    what = PROTECTED_NAMES.get(name.casefold() if _FOLD else name)
    if what:
        return Protected(real, _shown(Path(real)), what)
    return None


# File names specific enough to nable that code naming one is taken to mean
# that file: `Path.home() / ".finops" / "guard-off"` in a one-liner has no
# path in it to resolve, but it has the name.
DISTINCTIVE_NAMES = frozenset({"guard-off", POLICY_FILE_NAME, ORG_DIR_NAME, "ai-budget.json",
                               "tag_rules.yaml", "nable-guard.json",
                               *PROTECTED_NAMES, *(n for n, _ in _LEDGER_FILES)})
