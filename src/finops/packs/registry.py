# SPDX-License-Identifier: Apache-2.0
"""The pack registry: a JSON index, like a Claude Code marketplace.json.

A registry is a file, served from a git repo or anywhere else:

    {"schema": 1,
     "packs": [{"namespace": "io.github.getnable", "name": "startup-credits-runway",
                "version": "1.0.0",
                "source": "git+https://github.com/getnable/packs@<40-hex commit>#subdir=startup-credits-runway",
                "sha256": "<content digest of the pack's files>",
                "tier": "first-party",
                "description": "Months of credits left at current burn"}]}

`sha256` pins the pack's content, not its transport: it is
store.content_digest() over every file's hash (what `nable pack validate`
prints as "digest"), so the same pin holds whether the files came from git or
a tarball. install.py refuses a pack whose files do not match it, and one
whose manifest claims a different tier than the entry.

Where the index is read from, first match wins: `packs.registry` in the org
policy file, then `--registry`, then NABLE_PACK_REGISTRY, then
DEFAULT_REGISTRY_URL. The org policy wins over both (a `--registry` that
disagrees with it is refused) so an agent cannot point a managed machine at a
registry of its choosing. A local path (or file:// URL) works too, and a
relative `source` in a local registry resolves against the registry's folder.
A remote (https) registry may only list pinned git+ sources: a path in a
remote index would mean whatever that path is on the reader's machine.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .errors import RegistryError
from .manifest import TIERS, check_name, check_namespace
from .versions import is_semver, version_key

MAX_INDEX_BYTES = 5 * 1024 * 1024
FETCH_TIMEOUT_S = 10.0
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REF = re.compile(r"^(?P<ns>[a-z][a-z0-9.-]*\.[a-z0-9.-]+)/(?P<name>[a-z][a-z0-9-]*)"
                  r"(?:@(?P<version>[0-9][0-9A-Za-z.-]*))?$")


@dataclass(frozen=True)
class Entry:
    namespace: str
    name: str
    version: str
    source: str
    sha256: str
    tier: str
    description: str = ""
    registry: str = ""

    @property
    def id(self) -> str:
        return f"{self.namespace}/{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return {"namespace": self.namespace, "name": self.name, "version": self.version,
                "source": self.source, "sha256": self.sha256, "tier": self.tier,
                "description": self.description}


def parse_ref(text: str) -> tuple[str, str, str | None] | None:
    """"ns/name" or "ns/name@1.2.0" -> (ns, name, version); None otherwise."""
    m = _REF.match(text.strip())
    if not m:
        return None
    return m.group("ns"), m.group("name"), m.group("version")


def location(explicit: str | None = None) -> str:
    """Where the registry index is read from (see the module docstring).
    `explicit` is a caller's choice (`--registry`); it is refused when the org
    policy pins a different registry."""
    from ..policy import pack_policy
    from . import DEFAULT_REGISTRY_URL
    configured = pack_policy().get("registry")
    if configured:
        if explicit and explicit != configured:
            raise RegistryError(f"The org policy pins the pack registry to {configured}, so "
                                f"{explicit} cannot be used")
        return configured
    if explicit:
        return explicit
    env = os.getenv("NABLE_PACK_REGISTRY", "").strip()
    return env or DEFAULT_REGISTRY_URL


def _local_path(loc: str) -> Path | None:
    if loc.startswith("file://"):
        return Path(unquote(urlparse(loc).path))
    if "://" in loc:
        return None
    return Path(loc).expanduser()


def _fetch(loc: str) -> tuple[bytes, Path | None]:
    unreachable = ("The pack registry at {loc} could not be read ({why}). Nothing was "
                   "installed. Set NABLE_PACK_REGISTRY (or packs.registry in the org policy) "
                   "to a reachable index or a local file.")
    local = _local_path(loc)
    if local is not None:
        try:
            if local.stat().st_size > MAX_INDEX_BYTES:
                raise RegistryError(f"The pack registry at {loc} is larger than "
                                    f"{MAX_INDEX_BYTES} bytes")
            return local.read_bytes(), local.parent
        except OSError as e:
            raise RegistryError(unreachable.format(loc=loc, why=e.strerror or e)) from None
    if not loc.startswith("https://"):
        raise RegistryError(f"The pack registry must be an https:// URL or a local file, "
                            f"not {loc!r}")
    try:
        import httpx
        with httpx.Client(timeout=FETCH_TIMEOUT_S, follow_redirects=True) as client, \
                client.stream("GET", loc) as resp:
            if not str(resp.url).startswith("https://"):
                raise RegistryError(f"The pack registry at {loc} redirected off https")
            if resp.status_code != 200:
                raise RegistryError(unreachable.format(loc=loc, why=f"HTTP {resp.status_code}"))
            body = b""
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) > MAX_INDEX_BYTES:
                    raise RegistryError(f"The pack registry at {loc} is larger than "
                                        f"{MAX_INDEX_BYTES} bytes")
        return body, None
    except RegistryError:
        raise
    except Exception as e:  # noqa: BLE001 - any transport failure reads as "unreachable"
        raise RegistryError(unreachable.format(loc=loc, why=type(e).__name__)) from None


def _entry(raw: Any, base: Path | None, loc: str) -> tuple[Entry | None, str | None]:
    if not isinstance(raw, dict):
        return None, "an entry is not an object"
    ns, name, ver = raw.get("namespace"), raw.get("name"), raw.get("version")
    label = f"{ns}/{name}@{ver}"
    if check_namespace(ns) or check_name(name):
        return None, f"{label}: namespace or name is not valid"
    if not is_semver(ver):
        return None, f"{label}: version is not semver"
    src, sha, tier = raw.get("source"), raw.get("sha256"), raw.get("tier")
    if not isinstance(src, str) or not src.strip() or parse_ref(src):
        return None, f"{label}: source must be a git URL or a path, not another registry entry"
    if not isinstance(sha, str) or not _SHA256.match(sha):
        return None, f"{label}: sha256 must be the pack's 64-hex content digest"
    if tier not in TIERS:
        return None, f"{label}: tier {tier!r} is not one of {', '.join(TIERS)}"
    desc = raw.get("description") or ""
    src = src.strip()
    if base is None and not src.startswith("git+"):
        # A remote index naming a path would install from whatever that path
        # is on the reader's machine (their working directory, even).
        return None, f"{label}: a remote registry may only list pinned git+ sources"
    if base is not None and not src.startswith("git+") \
            and not Path(src).expanduser().is_absolute():
        src = str((base / src).resolve())
    return Entry(ns, name, ver, src, sha, tier, str(desc)[:300], loc), None


def fetch_index(loc: str | None = None) -> tuple[list[Entry], list[str]]:
    """(valid entries, why each invalid entry was skipped). Raises
    RegistryError when the index cannot be read or is not an index."""
    loc = location(loc)
    body, base = _fetch(loc)
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise RegistryError(f"The pack registry at {loc} is not valid JSON") from None
    items = doc.get("packs") if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        raise RegistryError(f"The pack registry at {loc} has no packs list")
    entries, skipped = [], []
    for raw in items:
        e, why = _entry(raw, base, loc)
        if e:
            entries.append(e)
        else:
            skipped.append(why)
    return entries, skipped


def search(term: str, loc: str | None = None) -> list[Entry]:
    """Newest version of each pack whose id or description contains `term`."""
    entries, _ = fetch_index(loc)
    t = (term or "").strip().lower()
    newest: dict[str, Entry] = {}
    for e in entries:
        if t and t not in e.id.lower() and t not in e.description.lower():
            continue
        cur = newest.get(e.id)
        if cur is None or version_key(e.version) > version_key(cur.version):
            newest[e.id] = e
    return sorted(newest.values(), key=lambda e: e.id)


def resolve(ref: str, loc: str | None = None) -> Entry:
    """"ns/name" (newest) or "ns/name@version" -> its registry entry."""
    parsed = parse_ref(ref)
    if parsed is None:
        raise RegistryError(f"{ref!r} is not a pack reference like io.github.org/pack-name")
    ns, name, ver = parsed
    entries, _ = fetch_index(loc)
    hits = [e for e in entries if e.namespace == ns and e.name == name
            and (ver is None or e.version == ver)]
    if not hits:
        where = location(loc)
        raise RegistryError(f"{ref} is not in the pack registry at {where}")
    return max(hits, key=lambda e: version_key(e.version))
