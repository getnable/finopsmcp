# SPDX-License-Identifier: Apache-2.0
"""Where installed packs live, and the index that says what was approved.

    <data dir>/packs/<namespace>/<name>/<version>/   the pack's files
    <data dir>/packs/index.json                      what is installed

The data dir is nable's (guard_ledger._data_dir: FINOPS_DATA_DIR, a profile,
or ~/.finops). The index records, per pack: the source (a path, a git URL and
commit, or a tarball and its sha256), the sha256 of every file, the approved
capabilities, who approved them and when. It is written atomically (a temp
file in the same directory, fsync, os.replace), so a crash mid-write leaves the
old index, never half of a new one.

Nothing in a pack tree may be a symlink or a special file. walk() refuses
them rather than skipping them: a link in a pack is either a mistake or an
attempt to make the core read something outside it. Nor may it hold compiled
Python (a __pycache__ directory, .pyc or .pyo files), whatever the source:
Python would run that bytecode instead of the source a person reviewed, and
nothing shows the difference. Native libraries (.so, .pyd, .dylib) are
refused for every pack that is not first-party (native_code_problems).

The index is only as trustworthy as the file it is kept in. read_index()
checks the shape of every entry (namespace, name, version, and that the key
is namespace/name), so a hand-edited or tampered entry cannot point
install_dir() outside the packs root; the loaders re-read capabilities and
tier from the installed manifest, which the index's hashes pin. An entry's
`source` is shown and matched against packs.allowed_sources as recorded: it
is only as trustworthy as the index.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import PackError, Problem
from .versions import is_semver

INDEX_NAME = "index.json"
# A pack's detached signature (signing.py). It signs content_digest(), so the
# digest leaves it out; the file is still hashed, installed and audited.
SIG_NAME = "nable-pack.sig"
INDEX_SCHEMA = 1
MAX_FILES = 2000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
# Skipped when a pack is copied from a working directory or a git checkout.
IGNORED_DIRS = frozenset({".git"})
# Refused anywhere in a pack: compiled Python runs instead of its source.
BYTECODE_DIRS = frozenset({"__pycache__"})
BYTECODE_SUFFIXES = (".pyc", ".pyo")
# Refused in a pack that is not first-party: native code the reviewer cannot read.
NATIVE_SUFFIXES = (".so", ".pyd", ".dylib")

# Tests point this at a throwaway directory; nothing else sets it.
_root_override: Path | None = None


def packs_root() -> Path:
    if _root_override is not None:
        return _root_override
    from ..guard_ledger import _data_dir  # storage.db's rule, without SQLAlchemy
    return _data_dir() / "packs"


def index_path() -> Path:
    return packs_root() / INDEX_NAME


def install_dir(namespace: str, name: str, version: str) -> Path:
    """<packs root>/<namespace>/<name>/<version>. Raises PackError unless all
    three are valid (read_index checks every entry) and the path resolves
    inside the packs root."""
    why = entry_problem(f"{namespace}/{name}",
                        {"namespace": namespace, "name": name, "version": version})
    if why:
        raise PackError(f"not an installed pack location: {why}")
    root = packs_root()
    path = root / namespace / name / version
    try:
        inside = path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        inside = False
    if not inside:
        raise PackError(f"{namespace}/{name} {version} resolves outside {root}")
    return path


def entry_problem(key: Any, entry: Any) -> str | None:
    """Why an index entry is not the shape install writes, or None."""
    from .manifest import check_name, check_namespace
    if not isinstance(entry, dict):
        return f"{key!r}: the entry is not an object"
    ns, name, version = entry.get("namespace"), entry.get("name"), entry.get("version")
    if check_namespace(ns) or check_name(name):
        return f"{key!r}: namespace or name is not valid"
    if not is_semver(version):
        return f"{key!r}: version {version!r} is not semver"
    if key != f"{ns}/{name}":
        return f"{key!r}: the key is not its namespace/name ({ns}/{name})"
    files = entry.get("files", {})
    if not isinstance(files, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                              for k, v in files.items()):
        return f"{key!r}: files is not a table of paths and hashes"
    return None


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def walk(root: Path, *, skip_ignored: bool = False) -> list[str]:
    """Every regular file under `root`, as sorted POSIX relative paths.

    Raises PackError on a symlink, a special file, compiled Python (a
    __pycache__ directory, a .pyc or .pyo file), or a tree over the size
    limits. With skip_ignored, .git directories are passed over (a working
    copy has them; an installed pack never does)."""
    root = Path(root)
    problems: list[Problem] = []
    files: list[str] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        keep = []
        for d in sorted(dirnames):
            p = base / d
            rel = p.relative_to(root).as_posix()
            if skip_ignored and d in IGNORED_DIRS:
                continue
            if d in BYTECODE_DIRS:
                problems.append(Problem(rel, "is compiled Python, which would run instead of the "
                                        "source; delete it (packs ship source only)"))
                continue
            if p.is_symlink():
                problems.append(Problem(rel, "is a symlink; packs may not contain links"))
                continue
            keep.append(d)
        dirnames[:] = keep
        for fn in sorted(filenames):
            p = base / fn
            rel = p.relative_to(root).as_posix()
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                problems.append(Problem(rel, "is a symlink; packs may not contain links"))
                continue
            if not stat.S_ISREG(st.st_mode):
                problems.append(Problem(rel, "is not a regular file"))
                continue
            if fn.lower().endswith(BYTECODE_SUFFIXES):
                problems.append(Problem(rel, "is compiled Python, which would run instead of the "
                                        "source; delete it (packs ship source only)"))
                continue
            if st.st_size > MAX_FILE_BYTES:
                problems.append(Problem(rel, f"is larger than {MAX_FILE_BYTES} bytes"))
                continue
            total += st.st_size
            files.append(rel)
    if len(files) > MAX_FILES:
        problems.append(Problem(".", f"has {len(files)} files; a pack may have at most {MAX_FILES}"))
    if total > MAX_TOTAL_BYTES:
        problems.append(Problem(".", f"is {total} bytes; a pack may be at most {MAX_TOTAL_BYTES}"))
    if problems:
        raise PackError(f"{root} cannot be a pack", problems)
    return sorted(files)


def native_code_problems(files: Any, *, first_party: bool) -> list[Problem]:
    """Native libraries in a pack that is not first-party: refused, since
    importing one runs machine code nobody reviewed and the host's audit hook
    does not see what it does."""
    if first_party:
        return []
    return [Problem(rel, "is a native library; only a first-party pack may ship one")
            for rel in sorted(files) if rel.lower().endswith(NATIVE_SUFFIXES)]


def hash_tree(root: Path, *, skip_ignored: bool = False) -> dict[str, str]:
    """{relative path: sha256} for every file in the pack."""
    return {rel: sha256_file(Path(root) / rel) for rel in walk(root, skip_ignored=skip_ignored)}


def content_digest(files: dict[str, str]) -> str:
    """One sha256 over the pack's file list and hashes, independent of how
    it was transported. This is what a registry entry's `sha256` pins and
    what a signature signs, so the signature file itself is left out."""
    h = hashlib.sha256()
    for rel in sorted(files):
        if rel == SIG_NAME:
            continue
        h.update(f"{rel}\0{files[rel]}\n".encode())
    return h.hexdigest()


def compare_files(expected: dict[str, str], actual: dict[str, str]) -> dict[str, list[str]]:
    """{"modified", "missing", "added"}: how `actual` differs from `expected`."""
    return {
        "modified": sorted(p for p in expected if p in actual and actual[p] != expected[p]),
        "missing": sorted(p for p in expected if p not in actual),
        "added": sorted(p for p in actual if p not in expected),
    }


def empty_index() -> dict[str, Any]:
    return {"schema": INDEX_SCHEMA, "packs": {}}


def read_index() -> dict[str, Any]:
    """The index, or an empty one when there is none. Raises PackError when a
    file exists but cannot be read as an index: installing over it would drop
    the record of what was approved."""
    path = index_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return empty_index()
    except OSError as e:
        raise PackError(f"{path} could not be read ({e.strerror or e})") from None
    try:
        doc = json.loads(raw)
    except ValueError:
        raise PackError(f"{path} is not valid JSON. Move it aside to start over; "
                        "the packs under it stay on disk.") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("packs"), dict):
        raise PackError(f"{path} is not a pack index (no packs table)")
    bad = [why for key, e in doc["packs"].items() if (why := entry_problem(key, e))]
    if bad:
        raise PackError(f"{path} has entries install did not write, so nothing in it is "
                        "trusted until it is fixed (move it aside to start over)",
                        [Problem("index", why) for why in bad[:10]])
    return doc


def write_index(doc: dict[str, Any]) -> None:
    """Atomic: a crash leaves the previous index intact."""
    path = index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def index_stamp() -> tuple[int, int] | None:
    """(mtime_ns, size) of the index, for caches; None when there is none."""
    try:
        st = index_path().stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)
