# SPDX-License-Identifier: Apache-2.0
"""`nable pack`: install, audit and author packs.

    nable pack validate <dir>            check a pack as install would (authors)
    nable pack new <name>                scaffold a minimal valid data pack
    nable pack install <source> [--yes]  a directory, a tarball, git+URL@commit,
                                         or ns/name from the registry
    nable pack update <ns/name|source>   a newer version; new capabilities need
                                         a person to approve them again
    nable pack remove <ns/name> [--yes]
    nable pack list [--code]             installed packs; --code adds the
                                         code plugins found (never loaded)
    nable pack audit                     re-hash every installed file, re-check
                                         policy; exit 1 on anything wrong
    nable pack search <term>             the registry

Exit codes: 0 done, 1 refused or failed (nothing changed), 2 usage.
"""
from __future__ import annotations

import json
import sys
from typing import Any

EXIT_OK, EXIT_FAIL = 0, 1


def add_parser(sub) -> None:
    p = sub.add_parser(
        "pack",
        help="Packs: install, audit and author nable extensions (policies, guard rules, ...)",
        description="Packs extend nable with policies, guard rules, playbooks, price books, "
                    "report templates and skills. They are data: nothing in them runs. "
                    "Each declares its capabilities, which you approve at install and again "
                    "whenever an update adds one.",
    )
    ps = p.add_subparsers(dest="pack_action", metavar="<action>")
    v = ps.add_parser("validate", help="Validate a pack directory as install would")
    v.add_argument("path")
    n = ps.add_parser("new", help="Scaffold a minimal valid data pack")
    n.add_argument("name")
    n.add_argument("--dir", dest="pack_dir", default=None, metavar="PATH",
                   help="where to create it (default ./<name>)")
    n.add_argument("--namespace", dest="pack_namespace", default="io.github.your-org",
                   help="reverse-DNS namespace (default io.github.your-org)")
    i = ps.add_parser("install", help="Install from a directory, tarball, git URL or registry")
    i.add_argument("source")
    u = ps.add_parser("update", help="Update an installed pack")
    u.add_argument("source")
    r = ps.add_parser("remove", help="Remove an installed pack")
    r.add_argument("pack_id")
    ls = ps.add_parser("list", help="Installed packs")
    ls.add_argument("--code", dest="pack_code", action="store_true",
                    help="also list code plugins (nable.connectors, nable.adapters, nable.sinks)")
    ps.add_parser("audit", help="Re-hash installed packs and re-check them against policy")
    s = ps.add_parser("search", help="Search the pack registry")
    s.add_argument("term", nargs="?", default="")
    for sp in (v, n, i, u, r, ls, s, ps.choices["audit"]):
        sp.add_argument("--json", dest="pack_json", action="store_true",
                        help="machine-readable output on stdout")
    for sp in (i, u, r):
        sp.add_argument("--yes", "-y", dest="pack_yes", action="store_true",
                        help="approve without a prompt (refused when the org policy "
                             "requires signed packs)")
    for sp in (i, u, s):
        sp.add_argument("--registry", dest="pack_registry", default=None, metavar="URL|PATH",
                        help="read this registry index instead of the configured one")
    p.set_defaults(cmd="pack")


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def describe_plan(plan) -> str:
    """What a person approving the pack reads before saying yes."""
    from . import capabilities as caps_mod
    m = plan.manifest
    if not plan.is_update:
        verb = f"Install {m.id} {m.version}"
    elif plan.previous["version"] == m.version:
        verb = f"Reinstall {m.id} {m.version} (its installed files changed since approval)"
    else:
        verb = f"Update {m.id} {plan.previous['version']} -> {m.version}"
    lines = [f"{verb} ({m.tier})", f"  {m.description}",
             f"  source: {plan.source.spec()}", f"  digest: {plan.digest}",
             f"  maintainers: {', '.join(m.maintainers)}", "", "  It may:"]
    caps = m.capabilities
    if not any(caps.get(k) for k in caps_mod.KEYS):
        lines.append("    nothing beyond loading its data: no data scopes, no cloud APIs, "
                     "no secrets, no network")
    for k in caps_mod.KEYS:
        vals = caps.get(k)
        if not vals:
            continue
        for val in (vals if isinstance(vals, tuple) else (vals,)):
            lines.append(f"    {k:<12} {caps_mod.describe(k, val)}")
    if plan.is_update:
        if plan.added:
            lines += ["", "  NEW since the installed version (needs your approval again):"]
            lines += [f"    + {k}: {', '.join(v)}" for k, v in plan.added.items()]
        if plan.removed:
            lines += [f"    - {k}: {', '.join(v)}" for k, v in plan.removed.items()]
    counts = plan.content.counts()
    if counts:
        lines += ["", "  Provides: " + ", ".join(f"{n} {k.replace('_', ' ')}"
                                                 for k, n in counts.items())]
    if m.code:
        lines.append("  Declares code (not loaded or run in this version): "
                     + ", ".join(f"{c.kind[:-1]} {c.id}" for c in m.code))
    for skill in plan.content.items.get("skills", []):
        lines += ["", f"  Skill {skill.path}, shown in full because it steers your agents:",
                  *("    | " + ln for ln in skill.text.splitlines())]
    return "\n".join(lines)


def _prompt(plan) -> bool:
    print(describe_plan(plan))
    try:
        answer = input("\n  Approve? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _out(obj: dict[str, Any], as_json: bool, text: str) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, default=str))
    else:
        print(text)


def _fail(err: Exception, as_json: bool) -> int:
    from .errors import PackError
    if as_json:
        body: dict[str, Any] = {"ok": False, "error": getattr(err, "message", str(err))}
        if isinstance(err, PackError):
            body["problems"] = [str(p) for p in err.problems]
            body["kind"] = type(err).__name__
        print(json.dumps(body, indent=2))
    else:
        _err(str(err))
    return EXIT_FAIL


def _caps_line(caps: dict[str, Any]) -> str:
    parts = [f"{k}={','.join(v) if isinstance(v, list) else v}" for k, v in caps.items() if v]
    return "; ".join(parts) or "no capabilities"


def run(parsed) -> int:
    from . import install as inst
    from .errors import PackError
    action = getattr(parsed, "pack_action", None)
    as_json = bool(getattr(parsed, "pack_json", False))
    yes = bool(getattr(parsed, "pack_yes", False))
    registry = getattr(parsed, "pack_registry", None)
    approve = _prompt if _interactive() and not as_json else None
    try:
        if action == "validate":
            r = inst.validate_dir(parsed.path)
            if r["ok"]:
                text = (f"OK {r['id']} {r['version']} ({r['tier']}): {r['files']} files, "
                        + (", ".join(f"{n} {k}" for k, n in r["provides"].items()) or "no content")
                        + f"\n  digest {r['digest']}")
            else:
                text = f"{parsed.path} is not a valid pack:\n" + "\n".join(
                    f"  {p}" for p in r["problems"])
            _out(r, as_json, text)
            return EXIT_OK if r["ok"] else EXIT_FAIL
        if action == "new":
            root = inst.new_pack(parsed.name, parsed.pack_dir, namespace=parsed.pack_namespace)
            _out({"ok": True, "path": str(root)}, as_json,
                 f"Created {root}\n  Next: edit nable-pack.toml, then "
                 f"`nable pack validate {root}`")
            return EXIT_OK
        if action in ("install", "update"):
            fn = inst.install if action == "install" else inst.update
            r = fn(parsed.source, yes=yes, approve=approve, registry=registry)
            pk = r["pack"]
            _out({"ok": True, **r}, as_json,
                 f"{r['status'].capitalize()}: {pk['id']} {pk['version']} ({pk.get('tier')})"
                 + (f", approved by {pk.get('approved_by')} ({pk.get('approval')})"
                    if r["status"] in ("installed", "updated", "repaired") else ""))
            return EXIT_OK
        if action == "remove":
            if not yes:
                if not _interactive():
                    raise PackError(f"Removing {parsed.pack_id} drops the policies and guard "
                                    "rules it adds. Confirm at a terminal, or pass --yes")
                ans = input(f"  Remove {parsed.pack_id}? [y/N] ").strip().lower()
                if ans not in ("y", "yes"):
                    raise PackError("Not removed")
            r = inst.remove(parsed.pack_id)
            _out({"ok": True, **r}, as_json, f"Removed {r['pack']['id']} {r['pack']['version']}")
            return EXIT_OK
        if action == "list":
            packs = inst.list_installed()
            obj: dict[str, Any] = {"ok": True, "packs": packs}
            lines = [f"  {p['id']} {p['version']} ({p.get('tier')}): {p.get('description')}\n"
                     f"    {_caps_line(p.get('capabilities') or {})}" for p in packs] \
                or ["  No packs installed. `nable pack search` lists the registry."]
            if getattr(parsed, "pack_code", False):
                from .discovery import code_plugins
                code = code_plugins()
                obj["code"] = code
                lines += ["", "  Code plugins (found, not loaded):"]
                lines += [f"    {c['group']} {c['name']} = {c['entry']} "
                          f"({c['distribution'] or 'unknown distribution'}"
                          f"{', declared by ' + c['declared_by'] if c['declared_by'] else ', no pack manifest'})"
                          for c in code] or ["    none"]
            _out(obj, as_json, "\n".join(lines))
            return EXIT_OK
        if action == "audit":
            r = inst.audit()
            lines = []
            for p in r["packs"]:
                lines.append(f"  [{p['status']}] {p['id']} {p['version']} ({p.get('tier')}): "
                             f"{_caps_line(p.get('capabilities') or {})}")
                lines += [f"      {x}" for x in p["problems"]]
            if not r["packs"]:
                lines.append("  No packs installed.")
            _out(r, as_json, "\n".join(lines))
            return EXIT_OK if r["ok"] else EXIT_FAIL
        if action == "search":
            from .registry import search
            hits = search(parsed.term, registry)
            _out({"ok": True, "packs": [h.to_dict() for h in hits]}, as_json,
                 "\n".join(f"  {h.id} {h.version} ({h.tier}): {h.description}" for h in hits)
                 or f"  Nothing in the registry matches {parsed.term!r}.")
            return EXIT_OK
    except PackError as e:
        return _fail(e, as_json)
    except KeyboardInterrupt:
        _err("Stopped; nothing was changed.")
        return EXIT_FAIL
    _err("usage: nable pack {validate,new,install,update,remove,list,audit,search} ...")
    return 2

