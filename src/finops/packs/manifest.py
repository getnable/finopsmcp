# SPDX-License-Identifier: Apache-2.0
"""`nable-pack.toml`: parse it, validate it strictly, return a typed Manifest.

The tables are the design's, exactly: [pack], [capabilities], [provides],
[compat], [integrity]. Validation is strict in both directions: a missing
required field is an error, and so is a field nobody defined, because a typo
in a capability table ("netwrok") would otherwise read as "asks for nothing"
and install. Every problem is collected, with its field and reason.

    [pack]          name, namespace (reverse-DNS), version (semver),
                    description, license, nable_api (a range that must
                    include finops.packs.API_VERSION), maintainers, support
                    (first-party | verified | community | private),
                    repository, source_commit, status
    [capabilities]  see capabilities.py
    [provides]      data content (policies, guard_rules, playbooks,
                    price_books, reports, skills) as lists of relative globs;
                    code (connectors, adapters, sinks) as lists of
                    {id, entry, output}. Code is declared and validated here,
                    never imported by the core, and run only out of process
                    by broker.py.
    [compat]        clouds, harnesses
    [integrity]     files = {"relative/path" = "sha256 hex"}, attestation
                    (a PEP 740 / Sigstore bundle reference: parsed, recorded
                    and shown, and NOT verified yet; the detached Ed25519
                    signature in signing.py is what nable checks today)

"first-party" is bound to nable's own namespaces here, and that alone is a
claim. It is honoured only when the pack carries a signature from nable's
first-party key (signing.py); install.py refuses the claim otherwise.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from . import capabilities as caps_mod
from .errors import Problem, ValidationError
from .versions import in_range, is_semver, parse_range

MANIFEST_NAME = "nable-pack.toml"
SIG_NAME = "nable-pack.sig"  # store.SIG_NAME, repeated so this module stays light
MAX_MANIFEST_BYTES = 64 * 1024

TIERS: tuple[str, ...] = ("first-party", "verified", "community", "private")
STATUSES: tuple[str, ...] = ("active", "deprecated", "archived")
FIRST_PARTY_NAMESPACES: frozenset[str] = frozenset({"io.github.getnable", "com.getnable",
                                                    "sh.nable"})

DATA_KINDS: tuple[str, ...] = ("policies", "guard_rules", "playbooks", "price_books",
                               "reports", "skills")
CODE_KINDS: tuple[str, ...] = ("connectors", "adapters", "sinks")
CONNECTOR_OUTPUTS: tuple[str, ...] = ("focus-1.3",)
CLOUDS: tuple[str, ...] = ("aws", "gcp", "azure", "k8s", "oci", "any")
HARNESSES: tuple[str, ...] = ("claude-code", "cursor", "codex", "copilot", "gemini", "cline")

_PACK_KEYS = {"name", "namespace", "version", "description", "license", "nable_api",
              "maintainers", "support", "repository", "source_commit", "status"}
_REQUIRED = ("name", "namespace", "version", "description", "nable_api", "maintainers",
             "support")
_TABLES = {"pack", "capabilities", "provides", "compat", "integrity"}

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}[a-z0-9]$")
_NS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_TLD = re.compile(r"^[a-z]{2,63}$")
_ENTRY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
                    r":[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LICENSE = re.compile(r"^[A-Za-z0-9.+() -]{1,80}$")
_MAINTAINER = re.compile(r"^\S.{0,98}\S$|^\S$")


def check_namespace(ns: object) -> str | None:
    """Why a namespace is not reverse-DNS, or None. At least two labels, the
    first a top-level domain (letters only), all lowercase."""
    if not isinstance(ns, str) or not ns:
        return "must be a reverse-DNS name such as io.github.your-org"
    labels = ns.split(".")
    if len(labels) < 2:
        return f"{ns!r} is not reverse-DNS: it needs at least two labels (io.github.your-org)"
    if len(ns) > 253:
        return "is longer than 253 characters"
    if not _TLD.match(labels[0]):
        return (f"{ns!r} is not reverse-DNS: it must start with a top-level domain "
                f"(io, com, org, ...), not {labels[0]!r}")
    for label in labels[1:]:
        if not _NS_LABEL.match(label):
            return (f"label {label!r} must be lowercase letters, digits and inner hyphens, "
                    "1 to 63 characters")
    return None


def check_name(name: object) -> str | None:
    if not isinstance(name, str) or not _NAME.match(name):
        return ("must be 2 to 64 lowercase letters, digits and hyphens, starting with a "
                "letter (e.g. k8s-allocation)")
    return None


def check_glob(pattern: object) -> str | None:
    """Why a [provides] path glob is unsafe, or None. Relative, POSIX, inside
    the pack: no leading slash, no drive, no backslash, no `..`."""
    if not isinstance(pattern, str) or not pattern.strip():
        return "must be a non-empty relative path or glob"
    if pattern.startswith("/") or "\\" in pattern or re.match(r"^[A-Za-z]:", pattern):
        return f"{pattern!r} must be relative to the pack, not absolute"
    if ".." in PurePosixPath(pattern).parts:
        return f"{pattern!r} may not leave the pack with .."
    return None


@dataclass(frozen=True)
class CodeEntry:
    """A connector, adapter or sink the pack provides. The core never imports
    it: broker.py runs it in a subprocess (finops.packs.host)."""

    kind: str
    id: str
    entry: str
    output: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {"kind": self.kind, "id": self.id, "entry": self.entry}
        if self.output:
            d["output"] = self.output
        return d


@dataclass(frozen=True)
class Manifest:
    namespace: str
    name: str
    version: str
    description: str
    nable_api: str
    maintainers: tuple[str, ...]
    support: str
    license: str | None = None
    repository: str | None = None
    source_commit: str | None = None
    status: str = "active"
    capabilities: dict[str, Any] = field(default_factory=dict)
    provides: dict[str, tuple[str, ...]] = field(default_factory=dict)
    code: tuple[CodeEntry, ...] = ()
    compat: dict[str, tuple[str, ...]] = field(default_factory=dict)
    integrity_files: dict[str, str] | None = None
    attestation: str | None = None

    @property
    def id(self) -> str:
        return f"{self.namespace}/{self.name}"

    @property
    def tier(self) -> str:
        return self.support

    @property
    def first_party(self) -> bool:
        return self.support == "first-party"

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace, "name": self.name, "version": self.version,
            "description": self.description, "nable_api": self.nable_api,
            "maintainers": list(self.maintainers), "support": self.support,
            "license": self.license, "repository": self.repository,
            "source_commit": self.source_commit, "status": self.status,
            "capabilities": {k: (list(v) if isinstance(v, tuple) else v)
                             for k, v in self.capabilities.items()},
            "provides": {k: list(v) for k, v in self.provides.items()},
            "code": [c.to_dict() for c in self.code],
            "compat": {k: list(v) for k, v in self.compat.items()},
        }


def _str_list(raw: Any, where: str, problems: list[Problem], *, allowed=None,
              nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        problems.append(Problem(where, "must be a list of strings"))
        return ()
    if nonempty and not raw:
        problems.append(Problem(where, "must list at least one"))
    if allowed is not None:
        for x in raw:
            if x not in allowed:
                problems.append(Problem(where, f"{x!r} is not one of {', '.join(allowed)}"))
    return tuple(raw)


def _parse_pack(raw: Any, problems: list[Problem], api_version: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not isinstance(raw, dict):
        problems.append(Problem("pack", "the [pack] table is required"))
        return out
    for key in raw:
        if key not in _PACK_KEYS:
            problems.append(Problem(f"pack.{key}", "is not a [pack] field; known: "
                                    + ", ".join(sorted(_PACK_KEYS))))
    for key in _REQUIRED:
        if key not in raw:
            problems.append(Problem(f"pack.{key}", "is required"))
    if "name" in raw:
        why = check_name(raw["name"])
        if why:
            problems.append(Problem("pack.name", why))
        else:
            out["name"] = raw["name"]
    if "namespace" in raw:
        why = check_namespace(raw["namespace"])
        if why:
            problems.append(Problem("pack.namespace", why))
        else:
            out["namespace"] = raw["namespace"]
    if "version" in raw:
        if not is_semver(raw["version"]):
            problems.append(Problem("pack.version",
                                    f"{raw['version']!r} is not a semver version like 1.2.0"))
        else:
            out["version"] = raw["version"]
    if "description" in raw:
        d = raw["description"]
        if not isinstance(d, str) or not d.strip():
            problems.append(Problem("pack.description", "must be a non-empty string"))
        elif len(d) > 300:
            problems.append(Problem("pack.description", "must be 300 characters or fewer"))
        else:
            out["description"] = d.strip()
    if "nable_api" in raw:
        spec = raw["nable_api"]
        try:
            parse_range(spec)
        except ValueError as e:
            problems.append(Problem("pack.nable_api", str(e)))
        else:
            if not in_range(api_version, spec):
                problems.append(Problem("pack.nable_api",
                                        f"{spec!r} does not include this nable's pack API "
                                        f"{api_version}"))
            else:
                out["nable_api"] = spec
    if "maintainers" in raw:
        m = _str_list(raw["maintainers"], "pack.maintainers", problems, nonempty=True)
        bad = [x for x in m if not _MAINTAINER.match(x)]
        if bad:
            problems.append(Problem("pack.maintainers",
                                    f"{bad[0]!r} must be a handle or a name, 1 to 100 characters"))
        out["maintainers"] = m
    if "support" in raw:
        s = raw["support"]
        if s not in TIERS:
            problems.append(Problem("pack.support", f"{s!r} is not one of {', '.join(TIERS)}"))
        else:
            out["support"] = s
    if "license" in raw:
        lic = raw["license"]
        if not isinstance(lic, str) or not _LICENSE.match(lic):
            problems.append(Problem("pack.license", "must be an SPDX expression such as Apache-2.0"))
        else:
            out["license"] = lic
    if "repository" in raw:
        r = raw["repository"]
        if not isinstance(r, str) or not re.match(r"^https://[^\s/]+(/\S*)?$", r):
            problems.append(Problem("pack.repository", "must be an https:// URL"))
        else:
            out["repository"] = r
    if "source_commit" in raw:
        c = raw["source_commit"]
        if not isinstance(c, str) or not _COMMIT.match(c):
            problems.append(Problem("pack.source_commit",
                                    "must be a lowercase hex commit id (7 to 64 characters)"))
        else:
            out["source_commit"] = c
    if "status" in raw:
        st = raw["status"]
        if st not in STATUSES:
            problems.append(Problem("pack.status", f"{st!r} is not one of {', '.join(STATUSES)}"))
        else:
            out["status"] = st
    if out.get("support") == "first-party" and "namespace" in out \
            and out["namespace"] not in FIRST_PARTY_NAMESPACES:
        problems.append(Problem("pack.support",
                                f"\"first-party\" is reserved for nable's namespaces "
                                f"({', '.join(sorted(FIRST_PARTY_NAMESPACES))}), not "
                                f"{out['namespace']}"))
    return out


def _parse_provides(raw: Any, problems: list[Problem]
                    ) -> tuple[dict[str, tuple[str, ...]], tuple[CodeEntry, ...]]:
    provides: dict[str, tuple[str, ...]] = {}
    code: list[CodeEntry] = []
    if raw is None:
        return provides, ()
    if not isinstance(raw, dict):
        problems.append(Problem("provides", "must be a table"))
        return provides, ()
    for key in raw:
        if key not in DATA_KINDS and key not in CODE_KINDS:
            problems.append(Problem(f"provides.{key}", "is not a content type; known: "
                                    + ", ".join(DATA_KINDS + CODE_KINDS)))
    for kind in DATA_KINDS:
        if kind not in raw:
            continue
        globs = _str_list(raw[kind], f"provides.{kind}", problems)
        for g in globs:
            why = check_glob(g)
            if why:
                problems.append(Problem(f"provides.{kind}", why))
        provides[kind] = globs
    seen: set[tuple[str, str]] = set()
    for kind in CODE_KINDS:
        if kind not in raw:
            continue
        items = raw[kind]
        if not isinstance(items, list):
            problems.append(Problem(f"provides.{kind}", "must be a list of tables"))
            continue
        for i, item in enumerate(items):
            where = f"provides.{kind}[{i}]"
            if not isinstance(item, dict):
                problems.append(Problem(where, "must be a table {id, entry}"))
                continue
            allowed = {"id", "entry", "output"} if kind == "connectors" else {"id", "entry"}
            for k in item:
                if k not in allowed:
                    problems.append(Problem(f"{where}.{k}", "is not a field; known: "
                                            + ", ".join(sorted(allowed))))
            cid, entry, output = item.get("id"), item.get("entry"), item.get("output")
            ok = True
            if not isinstance(cid, str) or not _ID.match(cid):
                problems.append(Problem(f"{where}.id", "must be a short lowercase id"))
                ok = False
            elif (kind, cid) in seen:
                problems.append(Problem(f"{where}.id", f"{cid!r} is declared twice"))
                ok = False
            if not isinstance(entry, str) or not _ENTRY.match(entry):
                problems.append(Problem(f"{where}.entry",
                                        "must be an entry point like package.module:function"))
                ok = False
            if kind == "connectors" and output not in CONNECTOR_OUTPUTS:
                problems.append(Problem(f"{where}.output",
                                        f"must be one of {', '.join(CONNECTOR_OUTPUTS)}"))
                ok = False
            if ok:
                seen.add((kind, cid))
                code.append(CodeEntry(kind, cid, entry, output if kind == "connectors" else None))
    return provides, tuple(code)


def _parse_compat(raw: Any, problems: list[Problem]) -> dict[str, tuple[str, ...]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        problems.append(Problem("compat", "must be a table"))
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for key in raw:
        if key not in ("clouds", "harnesses"):
            problems.append(Problem(f"compat.{key}", "is not a field; known: clouds, harnesses"))
    if "clouds" in raw:
        out["clouds"] = _str_list(raw["clouds"], "compat.clouds", problems, allowed=CLOUDS)
    if "harnesses" in raw:
        out["harnesses"] = _str_list(raw["harnesses"], "compat.harnesses", problems,
                                     allowed=HARNESSES)
    return out


def _parse_integrity(raw: Any, problems: list[Problem]) -> tuple[dict[str, str] | None,
                                                                 str | None]:
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        problems.append(Problem("integrity", "must be a table"))
        return None, None
    for key in raw:
        if key not in ("files", "attestation"):
            problems.append(Problem(f"integrity.{key}",
                                    "is not a field; known: files, attestation"))
    files: dict[str, str] | None = None
    if "files" in raw:
        f = raw["files"]
        if not isinstance(f, dict):
            problems.append(Problem("integrity.files",
                                    "must be a table of \"relative/path\" = \"sha256 hex\""))
        else:
            files = {}
            for path, digest in f.items():
                why = check_glob(path)
                if why or any(c in path for c in "*?["):
                    problems.append(Problem(f"integrity.files.{path}",
                                            why or "must be a file path, not a glob"))
                elif path == MANIFEST_NAME:
                    problems.append(Problem(f"integrity.files.{path}",
                                            "the manifest cannot pin its own hash"))
                elif path == SIG_NAME:
                    problems.append(Problem(f"integrity.files.{path}",
                                            "the signature signs the pack's digest, so the "
                                            "manifest cannot pin it"))
                elif not isinstance(digest, str) or not _SHA256.match(digest):
                    problems.append(Problem(f"integrity.files.{path}",
                                            "must be a lowercase sha256 hex digest"))
                else:
                    files[path] = digest
    att = raw.get("attestation")
    if att is not None and (not isinstance(att, str) or not att.strip()):
        problems.append(Problem("integrity.attestation", "must be a non-empty string"))
        att = None
    return files, att


def parse_manifest(text: str, *, api_version: str | None = None) -> Manifest:
    """Validate a manifest's text. Raises ValidationError listing every problem."""
    if api_version is None:
        from . import API_VERSION
        api_version = API_VERSION
    if len(text.encode("utf-8", "replace")) > MAX_MANIFEST_BYTES:
        raise ValidationError(f"{MANIFEST_NAME} is larger than {MAX_MANIFEST_BYTES} bytes")
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValidationError(f"{MANIFEST_NAME} is not valid TOML",
                              [Problem(MANIFEST_NAME, str(e))]) from None
    problems: list[Problem] = []
    for key in doc:
        if key not in _TABLES:
            problems.append(Problem(key, "is not a manifest table; known: "
                                    + ", ".join(sorted(_TABLES))))
    pack = _parse_pack(doc.get("pack"), problems, api_version)
    first_party = pack.get("support") == "first-party"
    capabilities, cproblems = caps_mod.validate(doc.get("capabilities"), first_party=first_party)
    problems.extend(cproblems)
    provides, code = _parse_provides(doc.get("provides"), problems)
    compat = _parse_compat(doc.get("compat"), problems)
    files, attestation = _parse_integrity(doc.get("integrity"), problems)
    if problems:
        raise ValidationError(f"{MANIFEST_NAME} is not valid", problems)
    return Manifest(
        namespace=pack["namespace"], name=pack["name"], version=pack["version"],
        description=pack["description"], nable_api=pack["nable_api"],
        maintainers=pack["maintainers"], support=pack["support"],
        license=pack.get("license"), repository=pack.get("repository"),
        source_commit=pack.get("source_commit"), status=pack.get("status", "active"),
        capabilities=capabilities, provides=provides, code=code, compat=compat,
        integrity_files=files, attestation=attestation,
    )


def load_manifest(root: Path) -> Manifest:
    """Read and validate `<root>/nable-pack.toml`."""
    path = Path(root) / MANIFEST_NAME
    try:
        if path.is_symlink() or not path.is_file():
            raise ValidationError(f"no {MANIFEST_NAME} in {root}",
                                  [Problem(MANIFEST_NAME, "is missing (or not a regular file)")])
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValidationError(f"{MANIFEST_NAME} is larger than {MAX_MANIFEST_BYTES} bytes")
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ValidationError(f"{MANIFEST_NAME} is not UTF-8 text") from None
    except OSError as e:
        raise ValidationError(f"{MANIFEST_NAME} could not be read ({e.strerror or e})") from None
    return parse_manifest(text)
