# SPDX-License-Identifier: Apache-2.0
"""adapter backstage: owners and teams from a Backstage software catalog.

Two sources, both optional:

  local  every catalog-info.yaml (or .yml) in the repos `nable org init`
         reads, which nable hands over through the repo.files data scope.
         No network: the files are read on this machine by nable.
  api    a Backstage instance's catalog API, when BACKSTAGE_URL is set (with
         BACKSTAGE_TOKEN when the instance needs one) and its host is in the
         pack's declared network. The pack ships declaring no Backstage host;
         an org adds its own, and the changed pack is approved again.

What it proposes (every one a proposal a person confirms):

  Component, System, API    owner of service:<name>, from spec.owner
  Component (local file)    owner of the repo path holding the file, when
                            every component in the file has the same owner
  Group                     team:<name>, with its display name, parent and
                            members

Owner references are Backstage entity refs: "team-a", "group:team-a" and
"group:default/team-a" name the group team-a; "user:default/alice" names a
person, who stands in as the team at lower confidence (as a CODEOWNERS rule
naming only people does). Entities outside the default namespace keep it:
service:<namespace>/<name>.

Confidence: 0.8 for a service's owner (the catalog is a declaration of
ownership), 0.7 for a repo path and for a team, times 0.6 when the owner is
a person.
"""
from __future__ import annotations

import posixpath
import re
import urllib.parse
from typing import Any

from . import web

FILE_NAMES = ["catalog-info.yaml", "catalog-info.yml"]
OWNED_KINDS = {"component": "Component", "system": "System", "api": "API"}
MAX_FACTS = 5000
MAX_PAGES = 20
PAGE_LIMIT = 500
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def parse_ref(ref: Any, default_kind: str = "group") -> tuple[str, str, str] | None:
    """(kind, namespace, name) for an entity ref, or None."""
    if not isinstance(ref, str) or not ref.strip():
        return None
    text = ref.strip()
    kind, sep, rest = text.partition(":")
    if not sep:
        kind, rest = default_kind, text
    ns, sep, name = rest.partition("/")
    if not sep:
        ns, name = "default", rest
    kind, ns, name = kind.strip().lower(), ns.strip() or "default", name.strip()
    if not (_NAME.match(name) and _NAME.match(ns)):
        return None
    return kind, ns, name


def _entity_name(entity: dict[str, Any]) -> tuple[str, str] | None:
    meta = entity.get("metadata")
    if not isinstance(meta, dict):
        return None
    name, ns = meta.get("name"), meta.get("namespace") or "default"
    if not isinstance(name, str) or not _NAME.match(name) or not isinstance(ns, str) \
            or not _NAME.match(ns):
        return None
    return ns, name


def _qualified(ns: str, name: str) -> str:
    return name if ns == "default" else f"{ns}/{name}"


def owner_value(ref: Any) -> tuple[dict[str, Any], float] | None:
    """(owner fact value, confidence factor) for a spec.owner ref."""
    parsed = parse_ref(ref)
    if parsed is None:
        return None
    kind, ns, name = parsed
    if kind == "group":
        return {"team": _qualified(ns, name)}, 1.0
    if kind == "user":
        return {"team": name, "people": [name]}, 0.6
    return None


def entity_facts(entity: Any, source: str) -> list[dict[str, Any]]:
    """Facts one catalog entity supports."""
    if not isinstance(entity, dict):
        return []
    # apiVersion is "<group>/<version>", e.g. backstage.io/v1alpha1: the group
    # must be Backstage's own, compared whole.
    group, _, version = str(entity.get("apiVersion") or "").partition("/")
    if not (group == "backstage.io" and version):
        return []
    kind = str(entity.get("kind") or "").strip().lower()
    named = _entity_name(entity)
    spec = entity.get("spec") if isinstance(entity.get("spec"), dict) else {}
    if named is None:
        return []
    ns, name = named
    if kind in OWNED_KINDS:
        owned = owner_value(spec.get("owner"))
        if owned is None:
            return []
        value, factor = owned
        return [{"fact": "owner", "subject": {"kind": "service", "id": _qualified(ns, name)},
                 "value": value, "source": source, "confidence": round(0.8 * factor, 2)}]
    if kind == "group":
        meta = entity.get("metadata") or {}
        profile = spec.get("profile") if isinstance(spec.get("profile"), dict) else {}
        value: dict[str, Any] = {}
        title = profile.get("displayName") or meta.get("title")
        if isinstance(title, str) and title.strip():
            value["name"] = title.strip()[:200]
        parent = parse_ref(spec.get("parent"))
        if parent is not None and parent[0] == "group":
            value["parent"] = _qualified(parent[1], parent[2])
        members = spec.get("members")
        if isinstance(members, list):
            people = sorted({p[2] for m in members if (p := parse_ref(m, "user")) is not None
                             and p[0] == "user"})
            if people:
                value["people"] = people
        return [{"fact": "team", "subject": {"kind": "team", "id": _qualified(ns, name)},
                 "value": value, "source": source, "confidence": 0.7}]
    return []


def _load_docs(text: str) -> list[Any]:
    import yaml
    return list(yaml.safe_load_all(text))


def file_facts(repo: dict[str, Any], path: str, text: str, log=None) -> list[dict[str, Any]]:
    """Facts from one catalog-info.yaml in a repo nable named."""
    label = str(repo.get("label") or "")
    try:
        docs = _load_docs(text)
    except Exception as e:  # noqa: BLE001 - one bad file never stops the others
        if log:
            log(f"backstage: {label}{path} is not valid YAML ({type(e).__name__}); skipped")
        return []
    out: list[dict[str, Any]] = []
    owners: list[dict[str, Any]] = []
    for i, doc in enumerate(docs):
        facts = entity_facts(doc, f"backstage:{label}{path}#{i}")
        out.extend(facts)
        if isinstance(doc, dict) and str(doc.get("kind") or "").lower() == "component":
            owners.extend(f["value"] for f in facts if f["fact"] == "owner")
    prefix = str(repo.get("subject_prefix") or "")
    if owners and prefix.startswith("repo_path:") and all(o == owners[0] for o in owners):
        where = posixpath.dirname(path) or "."
        conf = 0.7 * (0.6 if "people" in owners[0] else 1.0)
        out.append({"fact": "owner", "subject": f"{prefix}{where}", "value": dict(owners[0]),
                    "source": f"backstage:{label}{path}", "confidence": round(conf, 2)})
    return out


def local_facts(ctx: Any, context: dict[str, Any]) -> list[dict[str, Any]]:
    repos = {r.get("id"): r for r in context.get("repos") or [] if isinstance(r, dict)}
    if not repos:
        ctx.log("backstage: no repositories were named for this run, so no local "
                "catalog-info.yaml was read")
        return []
    got = ctx.read_data("repo.files", {"names": FILE_NAMES}) or {}
    if got.get("truncated"):
        ctx.log("backstage: nable stopped reading catalog files at its limit; some were "
                "left out")
    out: list[dict[str, Any]] = []
    for f in got.get("files") or []:
        repo = repos.get(f.get("repo"))
        if repo is not None and isinstance(f.get("path"), str) and isinstance(f.get("text"), str):
            out.extend(file_facts(repo, f["path"], f["text"], ctx.log))
    return out


def api_facts(ctx: Any, base_url: str) -> list[dict[str, Any]]:
    """Facts from a Backstage catalog API (GET /api/catalog/entities/by-query)."""
    base = base_url.strip().rstrip("/")
    why = web.url_problem(ctx, base)
    if why:
        ctx.log(f"backstage: BACKSTAGE_URL {why}; the catalog API was not read")
        return []
    token = ctx.secret("BACKSTAGE_TOKEN")
    host = urllib.parse.urlsplit(base).hostname or "backstage"
    query = "&".join(f"filter=kind={k}" for k in ("component", "system", "api", "group"))
    out: list[dict[str, Any]] = []
    cursor = None
    for _ in range(MAX_PAGES):
        url = f"{base}/api/catalog/entities/by-query?{query}&limit={PAGE_LIMIT}"
        if cursor:
            url += "&cursor=" + urllib.parse.quote(str(cursor), safe="")
        data, _headers = web.get_json(url, token)
        items = data.get("items") if isinstance(data, dict) else None
        for entity in items or []:
            named = _entity_name(entity) if isinstance(entity, dict) else None
            if named is None:
                continue
            kind = str(entity.get("kind") or "").lower()
            out.extend(entity_facts(entity, f"backstage-api:{host}:{kind}:{named[0]}/{named[1]}"))
        page = data.get("pageInfo") if isinstance(data, dict) else None
        cursor = page.get("nextCursor") if isinstance(page, dict) else None
        if not cursor or len(out) >= MAX_FACTS:
            break
    return out


def propose(ctx: Any, context: dict[str, Any]) -> list[dict[str, Any]]:
    """Owner and team proposals from the local catalog files, then the API."""
    facts = local_facts(ctx, context)
    url = ctx.setting("BACKSTAGE_URL")
    if url:
        facts.extend(api_facts(ctx, url))
    return facts[:MAX_FACTS]
