# SPDX-License-Identifier: Apache-2.0
"""The guard read ratchet (D171): every file the hook reads is opened without waiting.

The guard's hook runs on every tool call an agent makes, and a hook that
outlives the harness's timeout fails open: the call runs unexamined. An agent
can put a FIFO where any file the hook reads should be (`mkfifo
~/.finops/cloud-rates.json`), and an ordinary open() of a FIFO waits for a
writer that never comes. So, on the hook path, every read goes through
guard_paths.open_regular or guard_paths.read_regular (or os.open with
O_NONBLOCK and an S_ISREG check on what it opened): a FIFO, a socket or a
device in a file's place is "not read", never a wait.

What has to stay true:
  - the hook path is the static import closure of finops.guard and
    finops.guard_outcome, function-level imports included, computed here: a
    module that joins it joins the ratchet. Imports inside the functions in
    NOT_ON_HOOK_PATH are not followed, each with why the hook never runs
    them, and a runtime trace of the hook over representative calls checks
    that the hook imports nothing the closure left out. A module that is not
    installed (another workstream's, not merged yet) is skipped.
  - every read in that closure is non-waiting and reads only a regular file,
    package data included; the reads still in files this workstream does
    not own are listed in NOT_YET with why and whose, and that list can only
    shrink (an entry with no read left fails)
  - every write in that closure is on WRITES with its reason, and that list
    can only shrink too
  - the check itself fails on a synthetic module that calls Path.read_text,
    open() for reading or os.open without O_NONBLOCK
  - a FIFO in place of each file the guard reads (authority.yaml,
    owners.yaml, budgets.yaml, a pack index, the ledger, cloud-rates.json,
    the cloud standing, the budget summary, nable.policy.yaml and an
    approval file) returns the hook inside its budget, run in a subprocess
    with a timeout, and where the guard reports what it read, the file is
    reported as not read, never as zero
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
ROOTS = ("finops.guard", "finops.guard_outcome")
# The seams the guard and cloud connect import by name at run time
# (importlib), which a static walk cannot see: walked too when installed, so
# workstream B's modules are held to the same reads the moment they land.
SEAMS = ("finops.guard_authority", "finops.authority_standing")
# How long a hook may take with a FIFO in a file's place before the test
# calls it a wait (the harness's own hook timeout is 60 s; an unhung hook
# answers in well under one).
HOOK_BUDGET_S = 20

# Imports inside these functions are not followed: the hook never runs them.
NOT_ON_HOOK_PATH: dict[tuple[str, str], str] = {
    ("finops.guard_adapters", "cli"):
        "`nable guard install|uninstall --harness`, run by a person at a terminal",
    ("finops.guard_adapters", "status_lines"):
        "`nable guard status`, a person's command; it colours its lines with welcome's helpers",
    ("finops.guard", "doctor"):
        "`nable guard doctor`, a person's command",
    ("finops.connectors.kubernetes", "_spot_factor"):
        "prices a running cluster's spot nodes; the hook reads only the node price tables",
    ("finops.connectors.kubernetes", "KubernetesConnector"):
        "reads a live cluster for `nable scan`; the hook reads only the node price tables",
    ("finops.background_refresh", "_refresh_budget"):
        "runs in the detached process background_refresh.maybe_start spawns, never in the hook",
    ("finops.connectors.terraform_estimate", "estimate_from_dir"):
        "`nable estimate --dir`, which runs terraform plan for a person; the hook reads a "
        "saved plan through guard._plan_read instead",
    ("finops.org.coverage", "read_spend"):
        "`nable org status`'s coverage of the spend figures; the guard reads no spend",
    ("finops.org.coverage", "_db_path"):
        "the spend database read_spend opens, for `nable org status` only",
    ("finops.org.adapters", "AdapterContext"):
        "the context `nable org init` hands its adapters; the guard runs none",
    ("finops.security.vault", "_keyring_disabled"):
        "the OS keychain gate of Vault's own reads; the hook calls child_env alone, which "
        "reads no file",
    ("finops.packs", "org_adapters"):
        "pack adapters for `nable org init`; the guard loads only guard rules and price books",
}

# Reads that are not yet non-waiting, in files this workstream does not own,
# each with why it is still here and what would remove it. The ratchet fails
# on any read not listed, and on an entry whose read is gone.
NOT_YET: dict[tuple[str, str], str] = {
    ("finops.harness_usage", "_meta"):
        "Codex session metadata, read by the AI budget gate; harness_usage.py is not this "
        "workstream's: proposed, open it with guard_paths.open_regular",
    ("finops.harness_usage", "_read_totals"):
        "Codex token totals, read by the AI budget gate; proposed as above",
    ("finops.harness_usage", "_codex_file"):
        "a Codex rollout file, read by the AI budget gate; proposed as above",
    ("finops.harness_usage", "_cursor_read_cache"):
        "the Cursor usage cache, read by the AI budget gate; proposed: read_regular",
    ("finops.background_refresh", "read_state"):
        "the refresh's state file, read only under FINOPS_GUARD_AUTO_REFRESH_BUDGET=1; "
        "background_refresh.py is not this workstream's: proposed, read_regular",
    ("finops.security.vault", "_machine_id"):
        "/etc/machine-id for the vault's key; in the closure because the hook imports "
        "child_env, which reads no file; proposed, read_regular",
    ("finops.security.vault", "Vault._read_key_file"):
        "the vault's key file, never read by child_env; proposed, read_regular",
}

# Every write in the closure, by function, with why it writes. Each of the
# guard's own files is written 0600 beside the ledger, atomically where a
# reader could see half of it; none is a file an agent's call can name.
WRITES: dict[tuple[str, str], str] = {
    ("finops.ai_budget", "set_budget"): "ai-budget.json, a person's `nable ai-budget` choice",
    ("finops.ai_budget", "reset_budget"): "removes ai-budget.json on `nable ai-budget --reset`",
    ("finops.ai_budget", "_data_dir"): "the data dir the AI budget files live in",
    ("finops.ai_budget", "_TallyCache.at_hand"): "the tally cache's directory beside the ledger",
    ("finops.ai_budget", "_TallyCache.prune"): "drops tallies of transcripts that are gone",
    ("finops.ai_budget", "_write_person_cache"): "guard-person.json: whose budgets apply",
    ("finops.ai_budget", "_MonthTally._write"): "the month cache of priced responses",
    ("finops.ai_budget", "_MonthTally._prune"): "drops month cache shards of transcripts gone",
    ("finops.background_refresh", "_take_lock"): "the refresh's lock, so one refresh runs",
    ("finops.background_refresh", "maybe_start"): "removes the lock when no refresh started",
    ("finops.background_refresh", "run"): "removes the lock when the refresh ends",
    ("finops.budget.summary", "write_summary"): "budget-summary.json, written atomically",
    ("finops.cloud_rates", "_write"): "cloud-rates.json, 0600, atomically",
    ("finops.cloud_rates", "keep"): "removes cloud-rates.json when its workspace serves none",
    ("finops.cloud_standing", "_write"): "cloud-standing.json and cloud-box.json, 0600, "
                                         "atomically",
    ("finops.connectors.terraform_estimate", "estimate_from_dir"):
        "a temp dir for `nable estimate --dir`'s plan file, outside the person's repo",
    ("finops.guard", "_budget_note_due"): "when the AI budget note last showed, per session",
    ("finops.guard", "_pack_trouble_due"): "when a pack load problem was last recorded",
    ("finops.guard", "install"): "`nable guard install`: the harness's settings file",
    ("finops.guard", "uninstall"): "`nable guard uninstall`: the harness's settings file",
    ("finops.guard_adapters", "_backup"): "a backup of a harness file before install edits it",
    ("finops.guard_adapters", "_cline_uninstall"): "removes the guard's Cline hook script",
    ("finops.guard_adapters", "_write_text"): "a harness's hook file, written atomically",
    ("finops.guard_adapters", "uninstall"): "removes a harness hook file the guard wrote",
    ("finops.guard_approvals", "_locked"): "the approvals store's directory and its lock",
    ("finops.guard_approvals", "_write"): "guard-approvals.json, written atomically",
    ("finops.guard_ledger", "_data_dir"): "the data dir, 0700",
    ("finops.guard_ledger", "_lock"): "the ledger's lock while a record is appended",
    ("finops.guard_ledger", "_note_unrecorded"): "counts a verdict the ledger could not take",
    ("finops.guard_ledger", "append"): "the decision ledger, one chained record a verdict",
    ("finops.guard_ledger", "save_anchor"): "the ledger's anchor after a clean check",
    ("finops.guard_org", "_cached"): "the org model's parse cache, written atomically",
    ("finops.guard_packs", "_write_cache"): "the installed packs' guard cache, atomically",
    ("finops.guard_plugin", "set_off"): "the guard-off flag, a person's `nable guard off`",
    ("finops.harness_usage", "_cursor_cache_path"): "the Cursor usage cache's directory",
    ("finops.harness_usage", "write_json_atomic"): "a usage cache, written atomically",
    ("finops.org.store", "_atomic_write"): "an org model file, a person's `nable org` command",
    ("finops.org.store", "_cache_put"): "the org model's per-file parse cache, atomically",
    ("finops.org.store", "_locked"): "the org dir's lock while a person's command writes it",
    ("finops.org.store", "ensure_dir"): "the org dir, made by `nable org init`",
    ("finops.packs.install", "_commit"): "`nable packs install` moving a checked pack in",
    ("finops.packs.install", "_copy_dir"): "`nable packs install` staging a pack's files",
    ("finops.packs.install", "_copy_regular"): "one staged pack file",
    ("finops.packs.install", "_locked"): "the packs store's lock during an install",
    ("finops.packs.install", "_stage"): "the staging dir of an install",
    ("finops.packs.install", "extract_tarball"): "an archive's checked members, staged",
    ("finops.packs.install", "fetch_git"): "removes a failed git fetch's staging dir",
    ("finops.packs.install", "install"): "`nable packs install`'s work dir",
    ("finops.packs.install", "new_pack"): "`nable packs new`: a pack skeleton",
    ("finops.packs.install", "remove"): "`nable packs remove`",
    ("finops.packs.install", "sign"): "`nable packs sign`'s temp dir",
    ("finops.packs.signing", "keygen"): "`nable packs keygen`: a signing key, 0600",
    ("finops.packs.signing", "write_sig"): "`nable packs sign`: the signature file",
    ("finops.packs.store", "write_index"): "the packs index, written atomically",
    ("finops.security.vault", "Vault._audit"): "the vault's own audit table; never run by "
                                               "child_env, the hook's only use of the module",
    ("finops.security.vault", "Vault._claim_key_file"): "the vault's key file, made once",
    ("finops.security.vault", "Vault._init_db"): "the vault's database",
    ("finops.security.vault", "Vault._key_temp"): "a temp file for the vault's key",
    ("finops.security.vault", "Vault._secure_file"): "the vault database's mode, 0600",
    ("finops.security.vault", "Vault._write_key_file"): "the vault's key file, atomically",
    ("finops.security.vault", "Vault.delete"): "`nable vault` removing a stored secret",
    ("finops.security.vault", "Vault.get"): "the vault's database, opened to read a secret",
    ("finops.security.vault", "Vault.list_keys"): "the vault's database, opened to list",
    ("finops.security.vault", "Vault.load_to_env"): "the vault's database, opened at startup "
                                                    "of a command, never the hook",
    ("finops.security.vault", "Vault.rotate_key"): "`nable vault rotate`",
    ("finops.security.vault", "Vault.store"): "`nable vault set`",
    ("finops.security.vault", "_data_dir"): "the data dir, 0700",
    ("finops.security.vault", "_vault_dir"): "the vault's dir, 0700",
    # Workstream B's seam module (S2), walked when installed: it keeps the box's
    # authority block for `nable cloud status` and `connect --send`, never the hook.
    ("finops.authority_standing", "_keep"):
        "keeps the box's authority block, run by nable cloud status and connect --send",
    ("finops.authority_standing", "_write"):
        "writes the kept authority block beside the ledger, from keep only",
}


# ── the closure ──────────────────────────────────────────────────────────────

def _module_path(name: str) -> Path | None:
    p = SRC.joinpath(*name.split("."))
    if (p / "__init__.py").is_file():
        return p / "__init__.py"
    return p.with_suffix(".py") if p.with_suffix(".py").is_file() else None


def _scopes(tree: ast.AST) -> Iterator[tuple[ast.AST, str]]:
    """(node, the qualified name of the function or class it is in, or
    "<module>") for every node."""
    def walk(node: ast.AST, scope: list[str]) -> Iterator[tuple[ast.AST, str]]:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                inner = [*scope, child.name]
            yield child, ".".join(inner) or "<module>"
            yield from walk(child, inner)
    yield from walk(tree, [])


def _imports(name: str, path: Path) -> Iterator[tuple[str, str]]:
    """(module imported, the top-level function or class it is imported in)."""
    is_pkg = path.name == "__init__.py"
    for node, scope in _scopes(ast.parse(path.read_text(encoding="utf-8"))):
        top = scope.split(".", 1)[0]
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name, top
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = name.split(".") if is_pkg else name.split(".")[:-1]
                base = base[:len(base) - (node.level - 1)]
                mod = ".".join([*base, *([node.module] if node.module else [])])
            else:
                mod = node.module or ""
            yield mod, top
            for a in node.names:
                yield f"{mod}.{a.name}", top


def closure() -> dict[str, Path]:
    """{module: its file} for the hook path: ROOTS, the SEAMS installed, and
    every finops module they import, at any depth, function-level imports
    included, but not those inside NOT_ON_HOOK_PATH. A module not installed
    is skipped."""
    seen: dict[str, Path] = {}
    todo = [*ROOTS, *SEAMS]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        path = _module_path(name)
        if path is None:
            continue
        seen[name] = path
        parts = name.split(".")
        todo += [".".join(parts[:i]) for i in range(1, len(parts))]
        for mod, top in _imports(name, path):
            if (name, top) in NOT_ON_HOOK_PATH or not mod.startswith("finops"):
                continue
            todo.append(mod)
    return seen


# ── reads and writes ─────────────────────────────────────────────────────────

class Site(NamedTuple):
    module: str
    function: str
    line: int
    what: str
    ok: bool


_WRITE_FLAGS = ("O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC")
_WRITE_MODE = set("wax+")
_WRITE_ATTRS = {"write_text", "write_bytes", "mkdir", "touch", "unlink", "rmdir",
                "symlink_to", "hardlink_to"}
_OS_WRITES = {"makedirs", "mkdir", "remove", "unlink", "rename", "replace", "rmdir",
              "symlink", "link", "chmod", "fchmod", "write", "ftruncate", "truncate", "utime",
              "mkfifo", "removedirs", "renames"}
_SHUTIL_WRITES = {"move", "rmtree", "copyfileobj"}
_SHUTIL_READS = {"copy", "copy2", "copyfile"}
_TEMPFILE_WRITES = {"mkstemp", "mkdtemp", "NamedTemporaryFile", "TemporaryDirectory",
                    "TemporaryFile", "SpooledTemporaryFile"}
_OPENERS = {"io", "codecs", "gzip", "bz2", "lzma"}


def _mode(call: ast.Call, at: int) -> str | None:
    """The constant mode an open() call passes (positional `at` or mode=),
    "r" when none, or None when it is not a constant."""
    node = call.args[at] if len(call.args) > at else next(
        (k.value for k in call.keywords if k.arg == "mode"), None)
    if node is None:
        return "r"
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) \
        else None


def _flags_text(call: ast.Call, fn: ast.AST | None) -> str:
    """The source of an os.open call's flags, a local name followed to what
    the enclosing function assigns it."""
    node = call.args[1] if len(call.args) > 1 else next(
        (k.value for k in call.keywords if k.arg == "flags"), None)
    if node is None:
        return ""
    text = ast.unparse(node)
    if isinstance(node, ast.Name) and fn is not None:
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == node.id for t in sub.targets):
                text += " " + ast.unparse(sub.value)
    return text


def sites(module: str, source: str) -> tuple[list[Site], list[Site]]:
    """(reads, writes) of one module's source. A read is ok when it goes
    through guard_paths.open_regular or read_regular, or is os.open with
    O_NONBLOCK and an S_ISREG check in the same function (or O_DIRECTORY)."""
    tree = ast.parse(source)
    funcs: dict[str, ast.AST] = {}
    for node, scope in _scopes(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            funcs.setdefault(scope, node)
    reads: list[Site] = []
    writes: list[Site] = []
    for node, scope in _scopes(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = funcs.get(scope)
        f = node.func
        base = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
            else None
        attr = f.attr if isinstance(f, ast.Attribute) else None

        def add(kind: list[Site], what: str, ok: bool = True) -> None:
            kind.append(Site(module, scope, node.lineno, what, ok))

        if isinstance(f, ast.Name) and f.id == "open":
            mode = _mode(node, 1)
            if mode is not None and _WRITE_MODE & set(mode):
                add(writes, f"open({mode!r})")
            else:
                add(reads, f"open({mode!r})", ok=False)
        elif attr in ("read_text", "read_bytes"):
            add(reads, f".{attr}()", ok=False)
        elif attr == "open" and base == "os":
            flags = _flags_text(node, fn)
            if any(w in flags for w in _WRITE_FLAGS):
                add(writes, "os.open(write)")
            else:
                body = ast.unparse(fn) if fn is not None else ""
                ok = "O_NONBLOCK" in flags and ("S_ISREG" in body or "O_DIRECTORY" in flags)
                add(reads, "os.open(read)", ok=ok)
        elif attr == "open" and base == "tarfile":
            if not node.args or any(k.arg == "fileobj" for k in node.keywords):
                continue
            add(reads, "tarfile.open(path)", ok=False)
        elif attr == "open" and base in _OPENERS:
            mode = _mode(node, 1)
            if mode is not None and _WRITE_MODE & set(mode):
                add(writes, f"{base}.open({mode!r})")
            else:
                add(reads, f"{base}.open", ok=False)
        elif attr == "open":
            mode = _mode(node, 0)
            if mode is not None and _WRITE_MODE & set(mode):
                add(writes, f".open({mode!r})")
            else:
                add(reads, ".open()", ok=False)
        elif base == "zipfile" and attr == "ZipFile":
            add(reads, "zipfile.ZipFile", ok=False)
        elif base == "shutil" and attr in _SHUTIL_READS:
            add(reads, f"shutil.{attr}", ok=False)
            add(writes, f"shutil.{attr}")
        elif base == "shutil" and attr in _SHUTIL_WRITES:
            add(writes, f"shutil.{attr}")
        elif base == "os" and attr in _OS_WRITES:
            add(writes, f"os.{attr}")
        elif base == "tempfile" and attr in _TEMPFILE_WRITES:
            add(writes, f"tempfile.{attr}")
        elif base == "fcntl" and attr in ("flock", "lockf"):
            add(writes, f"fcntl.{attr}")
        elif base == "sqlite3" and attr == "connect":
            add(writes, "sqlite3.connect")
        elif attr in _WRITE_ATTRS:
            add(writes, f".{attr}()")
        elif attr in ("replace", "rename", "chmod") and base != "os" and \
                len(node.args) == 1 and not node.keywords:
            add(writes, f".{attr}(target)")
        elif isinstance(f, ast.Name) and f.id in ("read_regular", "open_regular"):
            add(reads, f.id)
        elif attr in ("read_regular", "open_regular"):
            add(reads, attr)
    return reads, writes


def every_site() -> tuple[list[Site], list[Site]]:
    reads: list[Site] = []
    writes: list[Site] = []
    for name, path in sorted(closure().items()):
        r, w = sites(name, path.read_text(encoding="utf-8"))
        reads += r
        writes += w
    return reads, writes


# ── the ratchet ──────────────────────────────────────────────────────────────

def test_the_closure_holds_the_hook_path_and_follows_function_level_imports():
    mods = closure()
    for name in ("finops.guard", "finops.guard_outcome", "finops.guard_ledger",
                 "finops.guard_paths", "finops.guard_org", "finops.guard_packs",
                 "finops.guard_approvals", "finops.guard_git", "finops.org.store",
                 "finops.org.legacy", "finops.packs.store", "finops.policy",
                 "finops.cloud_rates", "finops.cloud_standing", "finops.ai_budget",
                 "finops.budget.summary", "finops.aws_prices",
                 "finops.connectors.terraform_estimate"):
        assert name in mods, f"{name} is not in the hook's closure"
    # cloud_rates is imported inside a function only (guard._with_effective).
    assert "finops.cloud_rates" in mods
    for cli in ("finops.welcome", "finops.setup_wizard", "finops.server", "finops.doctor"):
        assert cli not in mods, f"{cli} joined the hook's closure"


def test_every_function_the_closure_does_not_follow_exists_and_imports_something():
    mods = closure()
    for (module, function), why in NOT_ON_HOOK_PATH.items():
        assert why and len(why) > 20, (module, function)
        path = _module_path(module)
        assert path is not None, f"{module} no longer exists: drop its entry"
        tops = {top for _mod, top in _imports(module, path)}
        assert function in tops, f"{module}.{function} imports nothing now: drop its entry"
        assert module in mods, f"{module} is not in the closure: drop its entry"


def test_every_read_in_the_closure_is_non_waiting():
    reads, _ = every_site()
    bad = [s for s in reads if not s.ok and (s.module, s.function) not in NOT_YET]
    assert not bad, ("reads on the hook path that can wait on a FIFO (use guard_paths."
                     "open_regular or read_regular):\n" + "\n".join(
                         f"  {s.module}:{s.line} {s.function} {s.what}" for s in bad))


def test_the_reads_not_yet_converted_only_shrink():
    reads, _ = every_site()
    still = {(s.module, s.function) for s in reads if not s.ok}
    for key, why in NOT_YET.items():
        if _module_path(key[0]) is None:
            continue                    # a seam module not installed on this branch
        assert key in still, f"{key} reads without waiting now: drop it from NOT_YET"
        assert why and "proposed" in why, key
    owned = ("finops.guard", "finops.ai_budget", "finops.cloud_", "finops.policy",
             "finops.token_budget", "finops.budget", "finops.org.store", "finops.org.legacy",
             "finops.packs", "finops.aws_prices", "finops.connectors.terraform_estimate")
    assert not [k for k in NOT_YET if k[0].startswith(owned)], \
        "a read in a file this workstream owns is never deferred"


def test_every_write_in_the_closure_is_on_the_allow_list():
    _, writes = every_site()
    missing = sorted({(s.module, s.function) for s in writes} - set(WRITES))
    assert not missing, ("writes on the hook path with no reason on WRITES:\n"
                         + "\n".join(f"  {m} {f}" for m, f in missing))


def test_the_write_allow_list_only_shrinks():
    _, writes = every_site()
    have = {(s.module, s.function) for s in writes}
    # A seam module not installed on this branch has no write to find.
    stale = sorted(k for k in set(WRITES) - have if _module_path(k[0]) is not None)
    assert not stale, f"entries on WRITES with no write left: {stale}"
    for key, why in WRITES.items():
        assert why and len(why) > 10, key


SYNTHETIC = '''
import os
from pathlib import Path

def reads_text(p):
    return Path(p).read_text()

def opens_for_reading(p):
    with open(p) as fh:
        return fh.read()

def opens_blocking(p):
    return os.open(p, os.O_RDONLY)

def opens_path(p):
    with Path(p).open("rb") as fh:
        return fh.read()
'''

COMPLIANT = '''
import os
import stat
from finops.guard_paths import read_regular, open_regular

def through_the_guard(p):
    return read_regular(p, 1024)

def through_the_fd(p):
    return os.fdopen(open_regular(p), "rb")

def by_hand(p):
    fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise OSError("not a regular file")
    return fd

def writes(p):
    with open(p, "w") as fh:
        fh.write("x")
'''


def test_the_check_fails_on_a_synthetic_module_and_passes_a_compliant_one():
    reads, _ = sites("synthetic", SYNTHETIC)
    assert {(s.function, s.ok) for s in reads} == {
        ("reads_text", False), ("opens_for_reading", False), ("opens_blocking", False),
        ("opens_path", False)}
    reads, writes = sites("compliant", COMPLIANT)
    assert reads and all(s.ok for s in reads)
    assert [s.function for s in writes] == ["writes"]


def test_a_module_that_joins_the_closure_joins_the_ratchet(monkeypatch):
    """A synthetic module imported inside a guard function is in the
    closure, and its read is found."""
    import finops.guard_ledger as gl
    real = _imports

    def with_synthetic(name, path):
        yield from real(name, path)
        if name == "finops.guard_ledger":
            yield "finops._synthetic_ratchet_probe", "append"

    probe = SRC / "finops" / "_synthetic_ratchet_probe.py"
    monkeypatch.setattr(sys.modules[__name__], "_imports", with_synthetic)
    real_path = _module_path

    def path_of(name):
        return probe if name == "finops._synthetic_ratchet_probe" else real_path(name)
    monkeypatch.setattr(sys.modules[__name__], "_module_path", path_of)
    monkeypatch.setattr(Path, "read_text", lambda self, encoding=None: (
        SYNTHETIC if self == probe else Path.read_bytes(self).decode(encoding or "utf-8")))
    assert "finops._synthetic_ratchet_probe" in closure()
    reads, _ = every_site()
    assert any(s.module == "finops._synthetic_ratchet_probe" and not s.ok for s in reads)
    assert gl is not None


# ── the runtime trace: the static closure misses nothing the hook imports ────

TRACE = r'''
import io, json, os, sys
os.environ["FINOPS_DATA_DIR"] = sys.argv[1]
os.environ["HOME"] = sys.argv[2]
from finops import guard, guard_outcome
calls = [
    {"tool_name": "Bash", "tool_input": {"command": "aws ec2 run-instances --region eu-west-1 --instance-type m5.4xlarge --count 3"}, "tool_use_id": "toolu_1", "session_id": "s", "cwd": sys.argv[2]},
    {"tool_name": "Bash", "tool_input": {"command": "aws elasticache create-cache-cluster --region eu-west-1 --cache-cluster-id c --engine redis --cache-node-type cache.r6g.large"}, "session_id": "s", "cwd": sys.argv[2]},
    {"tool_name": "Bash", "tool_input": {"command": "terraform apply plan.out"}, "session_id": "s", "cwd": sys.argv[2]},
    {"tool_name": "Bash", "tool_input": {"command": "gcloud compute instances create a --machine-type n2-standard-8"}, "session_id": "s", "cwd": sys.argv[2]},
    {"tool_name": "Bash", "tool_input": {"command": "git checkout main"}, "session_id": "s", "cwd": sys.argv[2]},
    {"tool_name": "mcp__aws__call_aws", "tool_input": {"cli_command": "aws ec2 terminate-instances --instance-ids i-1"}, "session_id": "s", "cwd": sys.argv[2]},
    {"tool_name": "Write", "tool_input": {"file_path": os.path.join(sys.argv[1], "guard-off"), "content": "x"}, "session_id": "s", "cwd": sys.argv[2]},
]
for c in calls:
    guard.run_hook(io.StringIO(json.dumps(c)), io.StringIO())
guard_outcome.run_post("claude-code", io.StringIO(json.dumps({**calls[0], "tool_response": {"stdout": "i-0123456789abcdef0"}})), io.StringIO())
print(json.dumps(sorted(m for m in sys.modules if m == "finops" or m.startswith("finops."))))
'''


def _env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AWS_", "FINOPS_", "NABLE_", "XDG_", "GIT_CONFIG"))}
    env.update(PYTHONPATH=str(SRC), HOME=str(tmp_path / "home"),
               FINOPS_DATA_DIR=str(tmp_path / "data"), NABLE_NO_TELEMETRY="1",
               FINOPS_NO_KEYRING="1", AWS_CONFIG_FILE=str(tmp_path / "no-aws-config"),
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(tmp_path / "no-gitconfig"))
    return env


def test_the_hook_imports_nothing_the_static_closure_left_out(tmp_path):
    (tmp_path / "home").mkdir()
    (tmp_path / "data").mkdir()
    r = subprocess.run([sys.executable, "-c", TRACE, str(tmp_path / "data"),
                        str(tmp_path / "home")], env=_env(tmp_path), capture_output=True,
                       text=True, stdin=subprocess.DEVNULL, timeout=120, check=False,
                       cwd=str(tmp_path / "home"))
    assert r.returncode == 0, r.stderr
    loaded = set(json.loads(r.stdout.strip().splitlines()[-1]))
    outside = sorted(loaded - set(closure()))
    assert not outside, f"the hook imported modules the static closure left out: {outside}"


# ── a FIFO in place of each file the guard reads ─────────────────────────────

FIFO_HOOK = r"""
import io, json, os, sys, time
cmd, cwd, harness = sys.argv[1], sys.argv[2], sys.argv[3]
from finops import guard
t = time.monotonic()
if harness == "claude-code":
    out = io.StringIO()
    guard.run_hook(io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd},
                                           "session_id": "s", "cwd": cwd})), out)
    said = out.getvalue()
else:
    said = json.dumps(guard.gate_command(cmd, harness=harness, cwd=cwd))
print(json.dumps({"s": time.monotonic() - t, "out": said}))
"""

PRICED = "aws ec2 run-instances --region us-east-1 --instance-type m5.4xlarge --count 3"
# Under the threshold, so the guard reads the ledger's recent end for its
# velocity cap and loop checks.
SMALL = "aws ec2 run-instances --region us-east-1 --instance-type t3.micro"
WS = "ws_" + "a" * 32
BOX = "https://box.example.com"


def _rates_doc() -> dict:
    """cloud-rates.json as cloud_rates.keep writes it, holding a factor for
    the priced command's usage type."""
    block = {"v": 1, "read": True, "workspace_ref": WS, "as_of": "2099-01-01",
             "window": {"start": "2098-12-03", "end": "2099-01-01"},
             "source": "the AWS cost export at s3://acme-bills/cur/", "columns": [],
             "rows": [{"product_code": "AmazonEC2", "region": "us-east-1",
                       "usage_type": "BoxUsage:m5.4xlarge", "unit": "Hrs",
                       "on_demand_factor": "0.8000", "blended_factor": "0.8000",
                       "covered_share": "0.5000", "days": 30, "line_items": 9}],
             "more": 0, "not_served": {}}
    return {"v": 1, "box": BOX, "host": "box.example.com",
            "fetched_at": "2099-01-01T00:00:00+00:00", "block": block}


class Case(NamedTuple):
    path: str            # relative to "data" or "home"
    under: str
    said: str | None     # what the guard says of it, where it reports the read
    harness: str = "claude-code"
    replaced: bool = False
    cmd: str = PRICED
    asks: bool = True


KINDS = {
    "authority.yaml": Case("repo/nable.org/authority.yaml", "home", None),
    "owners.yaml": Case("repo/nable.org/owners.yaml", "home", None),
    "budgets.yaml": Case("repo/nable.org/budgets.yaml", "home", None),
    "pack index": Case("packs/index.json", "data", None),
    "ledger": Case("guard-ledger.jsonl", "data", None, cmd=SMALL, asks=False),
    "cloud-rates.json": Case("cloud-rates.json", "data", "cloud-rates.json is not a regular file"),
    "cloud standing": Case("cloud-box.json", "data",
                           "cloud-box.json was not read: it is not a regular file"),
    "budget summary": Case("budget-summary.json", "data", None),
    "nable.policy.yaml": Case(".finops/nable.policy.yaml", "home", None),
    "approval file": Case("guard-approvals.json", "data", None, harness="codex",
                          replaced=True),
}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs here")
@pytest.mark.parametrize("kind", sorted(KINDS))
def test_a_fifo_in_place_of_a_guard_file_returns_the_hook_in_its_budget(tmp_path, kind):
    case = KINDS[kind]
    home, data = tmp_path / "home", tmp_path / "data"
    repo = home / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "nable.org").mkdir()
    data.mkdir()
    fifo = (data if case.under == "data" else home) / case.path
    fifo.parent.mkdir(parents=True, exist_ok=True)
    if kind == "cloud standing":
        (data / "cloud-rates.json").write_text(json.dumps(_rates_doc()))
    os.mkfifo(fifo)
    env = _env(tmp_path)
    env["FINOPS_POLICY_FILE"] = str(home / ".finops" / "nable.policy.yaml")
    r = subprocess.run([sys.executable, "-c", FIFO_HOOK, case.cmd, str(repo), case.harness],
                       env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       timeout=HOOK_BUDGET_S * 3, check=False, cwd=str(repo))
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert got["s"] < HOOK_BUDGET_S, f"the hook took {got['s']:.1f} s with a FIFO for {kind}"
    if case.asks:
        assert '"ask"' in got["out"], f"the priced launch was not asked about: {got['out']}"
    if case.said is not None:
        assert case.said in got["out"], got["out"]
    if not case.replaced:
        assert fifo.exists() and not fifo.is_file(), "the FIFO was written over, not read around"
    if kind == "ledger":
        # The history checks could not read it: said as a fail-open on the
        # count of what the ledger could not take, never as no history.
        lost = (data / "guard-ledger.unrecorded.jsonl").read_text()
        assert '"not_a_file"' in lost, "the verdict the ledger could not take was not counted"
        assert '"fail_open"' in lost, "the history checks' failed read was not said"
    if kind == "budget summary":
        assert '"within"' not in got["out"], "an unread summary was read as room in the budget"
