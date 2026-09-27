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
attempt to make the core read something outside it.
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

INDEX_NAME = "index.json"
# A pack's detached signature (signing.py). It signs content_digest(), so the
# digest leaves it out; the file is still hashed, installed and audited.
SIG_NAME = "nable-pack.sig"
INDEX_SCHEMA = 1
MAX_FILES = 2000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
# Skipped when a pack is copied from a working directory or a git checkout.
IGNORED_DIRS = frozenset({".git", "__pycache__"})

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
    """Only ever called with a validated namespace, name and version, which
    cannot contain a separator or `..`."""
    return packs_root() / namespace / name / version


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

    Raises PackError on a symlink, a special file, or a tree over the size
    limits. With skip_ignored, .git and __pycache__ directories are passed over
    (a working copy has them; an installed pack never does)."""
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
