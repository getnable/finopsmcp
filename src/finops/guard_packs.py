# SPDX-License-Identifier: Apache-2.0
"""Installed packs in the guard: their guard rules and price books, cheaply.

finops.packs.active() re-hashes every installed file, re-reads the manifests
and the content and re-checks signatures against the org policy, then caches
the result for the life of the process. The guard's hook is a new process on
every agent tool call, and with one small pack installed that load is about
95 ms (hashing, tomllib, PyYAML, the signature check) before any verdict. So
the guard reads packs through a small cache beside the decision ledger,
keyed on the stat (mtime, ctime, size, inode) of the pack index, of the
policy file and of every file and directory in every installed pack, on the
date and on nable's version. An edit, an added or removed file, an install,
an update or a policy change misses, and a miss is exactly what
finops.packs.guard_rules() and finops.packs.active("price_books") return.
ctime is in the key because nothing but the clock sets it: an edit that puts
a file's mtime back still moves its ctime.

Derived data only: a cache that cannot be read, or whose key does not match,
is a miss. The cache file, like the packs, is a protected path (guard_paths),
so an agent writing to it asks. With no pack index there is nothing to read:
that costs one stat() and imports nothing from finops.packs but its store.

Guard rules may only tighten (finops.packs.content.tighten, which is
finops.packs.rules.tighten). A pack the loader refuses (files changed since
approval, a signature that no longer verifies, a policy that now forbids it)
contributes nothing, and the guard records that as a fail-open (check
"packs") rather than judging without it in silence.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__

CACHE_NAME = "packs-guard-cache.json"
# 2: packs validated with the regex, pricing and control-character checks.
_CACHE_VERSION = 2
# The content types the guard reads. A pack that provides one of them and is
# not loaded is a pack the guard is judging without.
GUARD_KINDS = ("guard_rules", "price_books")
_PER = ("hour", "month")


class PackLoadProblem(Exception):
    """An installed pack with guard rules or a price book the guard could
    not load. Raised by nothing; recorded (guard._record_fail_open) by name."""


_EMPTY: dict[str, Any] = {"rules": [], "rates": [], "problems": [], "loaded": [],
                          "guard_problems": []}
_STATE: dict[str, Any] = {}


def _stamp(p: Path) -> list[int] | None:
    try:
        st = os.lstat(p)
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino]


def _tree(root: Path) -> list[Any]:
    """Every directory and file under an installed pack, stat-stamped."""
    out: list[Any] = [[".", _stamp(root)]]
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        base = Path(dirpath)
        for name in (*dirnames, *sorted(filenames)):
            p = base / name
            out.append([p.relative_to(root).as_posix(), _stamp(p)])
    return out


def _cache_path() -> Path:
    from . import guard_ledger
    return guard_ledger.ledger_path().with_name(CACHE_NAME)


def _key() -> tuple[str | None, dict[str, Any]]:
    """(cache key, the index) for what is installed now; (None, {}) when
    nothing is (no index file)."""
    store = sys.modules.get("finops.packs.store")
    if store is not None:
        index = store.index_path()
    else:
        # store.packs_root()'s rule, without importing the store (hashlib)
        # on a machine with no packs: that is most of them, on every call.
        from .guard_ledger import _data_dir
        index = _data_dir() / "packs" / "index.json"
    stamp = _stamp(index)
    if stamp is None:
        return None, {}
    from .packs import store
    from .policy import policy_file_path
    try:
        doc = json.loads(index.read_text(encoding="utf-8"))
        packs = doc.get("packs") if isinstance(doc, dict) else None
    except (OSError, ValueError):
        packs = None
    policy = policy_file_path()
    parts: dict[str, Any] = {
        "v": _CACHE_VERSION, "nable": __version__, "root": str(store.packs_root()),
        "index": stamp, "policy": [str(policy), _stamp(policy)],
        "day": datetime.now().astimezone().date().isoformat(), "packs": {}}
    if not isinstance(packs, dict):
        parts["packs"] = "unreadable"      # the loader says why; keyed so it is re-read
    else:
        for pid, e in sorted(packs.items()):
            try:
                root = store.install_dir(e["namespace"], e["name"], e["version"])
                parts["packs"][pid] = _tree(root)
            except (KeyError, TypeError, OSError, ValueError, store.PackError):
                parts["packs"][pid] = None
    blob = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest(), packs if isinstance(packs, dict) else {}


def _build(index: dict[str, Any]) -> dict[str, Any]:
    """What finops.packs loads right now, as plain data."""
    from .packs import runtime
    runtime.invalidate()                 # a miss means something changed on disk
    rules = [r.to_dict() for r in runtime.guard_rules()]
    rates = [r.to_dict() for r in runtime.active("price_books")]
    problems = runtime.load_problems()
    loaded = runtime.loaded_packs()
    guard_problems = []
    for p in problems:
        pid = p.split(" is not loaded", 1)[0]
        e = index.get(pid) if isinstance(index, dict) else None
        provides = (e or {}).get("provides") if isinstance(e, dict) else None
        # An unreadable index, or a pack whose entry does not say, counts.
        if not isinstance(provides, dict) or any(k in provides for k in GUARD_KINDS):
            guard_problems.append(p)
    return {"rules": rules, "rates": rates, "problems": problems, "loaded": loaded,
            "guard_problems": guard_problems}


def _read_cache(key: str) -> dict[str, Any] | None:
    try:
        doc = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("key") != key or not isinstance(doc.get("state"),
                                                                             dict):
        return None
    return doc["state"]


def _write_cache(key: str, state: dict[str, Any]) -> None:
    import tempfile
    path = _cache_path()
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{CACHE_NAME}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"key": key, "state": state}))
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        # No cache is a slower next call, nothing more.
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def state() -> dict[str, Any]:
    """{rules (GuardRule), rates (dicts, as PriceRate.to_dict), problems,
    loaded, guard_problems}. May raise: the guard records that as a
    fail-open and judges without packs."""
    key, index = _key()
    if key is None:
        return _EMPTY
    if _STATE.get("key") == key:
        return _STATE["state"]
    raw = _read_cache(key)
    if raw is None:
        raw = _build(index)
        _write_cache(key, raw)
    from .packs.rules import GuardRule
    out = {"rules": [GuardRule.from_dict(d) for d in raw.get("rules") or []],
           "rates": [r for r in raw.get("rates") or [] if isinstance(r, dict)],
           "problems": list(raw.get("problems") or []),
           "loaded": list(raw.get("loaded") or []),
           "guard_problems": list(raw.get("guard_problems") or [])}
    _STATE.update(key=key, state=out)
    return out


def invalidate() -> None:
    _STATE.clear()


def guard_rules() -> list[Any]:
    """finops.packs.guard_rules(), through the cache."""
    return list(state()["rules"])


def price_override(provider: str, sku: str) -> dict[str, Any] | None:
    """finops.packs.price_override(provider, sku) for today, through the
    cache: the rate in effect that took effect most recently, a tie going
    to the first pack by id (the order active() loads them in)."""
    day = datetime.now().astimezone().date().isoformat()
    p, s = (provider or "").strip().lower(), (sku or "").strip().casefold()
    best: dict[str, Any] | None = None
    for r in state()["rates"]:
        try:
            if r["provider"] != p or str(r["sku"]).casefold() != s:
                continue
            if not (r["effective_from"] <= day and (not r.get("effective_to")
                                                    or day <= r["effective_to"])):
                continue
        except (KeyError, TypeError):
            continue
        if best is None or r["effective_from"] > best["effective_from"]:
            best = r
    return dict(best) if best else None


def rate(provider: str, sku: str, *, per: str = "hour") -> dict[str, Any] | None:
    """An installed price book's USD rate for `sku`, per hour or per month
    (730 hours to a month, as aws_prices), with the rate it came from:
    {"usd": float, "pack": id, "rate": ..., "unit": ..., "sku": ...}.

    None when no price book has the SKU, when its unit is not a time unit,
    when its currency is not USD (the guard's thresholds and budgets are in
    dollars, and converting would be a guess), or on any error: then the
    caller prices at list, exactly as before any pack was installed."""
    if per not in _PER:
        raise ValueError(per)
    try:
        r = price_override(provider, sku)
    except Exception:
        return None
    if not r or r.get("currency", "USD") != "USD" or r.get("unit") not in _PER:
        return None
    from .aws_prices import HOURS_PER_MONTH
    value = float(r["rate"])
    if r["unit"] != per:
        value = value * HOURS_PER_MONTH if per == "month" else value / HOURS_PER_MONTH
    return {"usd": value, "pack": r.get("pack") or "", "rate": r["rate"], "unit": r["unit"],
            "sku": r["sku"]}


def status() -> dict[str, Any]:
    """What the guard reads from packs, for `nable guard doctor`. Never raises."""
    try:
        st = state()
    except Exception as exc:
        why = f"{type(exc).__name__}: {exc}"
        return {"guard_rules": [], "price_books": [], "problems": [why], "guard_problems": [why],
                "loaded": [], "error": type(exc).__name__}
    return {"guard_rules": [r.to_dict() for r in st["rules"]],
            "price_books": [dict(r) for r in st["rates"]],
            "problems": list(st["problems"]), "guard_problems": list(st["guard_problems"]),
            "loaded": list(st["loaded"])}
