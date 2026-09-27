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
    nable pack sign <dir|tarball> --key <pem>
                                         sign a pack's content digest (publishers,
                                         and orgs with a private registry key)
    nable pack keygen --out <path>       a new Ed25519 signing key (PEM, 0600)
                                         and its public half for packs.trusted_keys
    nable pack run <ns/name> <entry-id> [--start D --end D]
                                         run a connector (or adapter) through the
                                         broker and summarize what it returned

Exit codes: 0 done, 1 refused or failed (nothing changed), 2 usage.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

EXIT_OK, EXIT_FAIL = 0, 1

SANDBOX_NOTE = ("Code packs run out of process with a scrubbed environment and only what "
                "they declare. On a laptop that is a seatbelt, not a security boundary: "
                "egress is audited in process (or cut off entirely on Linux when the pack "
                "declares no network and namespaces are available), and the filesystem is "
                "not sandboxed.")


def add_parser(sub) -> None:
    p = sub.add_parser(
        "pack",
        help="Packs: install, audit and author nable extensions (policies, guard rules, ...)",
        description="Packs extend nable with policies, guard rules, playbooks, price books, "
                    "report templates and skills, which are data: nothing in them runs. "
                    "Connectors, adapters and sinks carry code, which runs only out of "
                    "process through the broker, and only when signed by a trusted key or "
                    "allowlisted by the org. Each pack declares its capabilities, which you "
                    "approve at install and again whenever an update adds one.",
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
    sg = ps.add_parser("sign", help="Sign a pack directory or tarball with an Ed25519 key")
    sg.add_argument("path")
    sg.add_argument("--key", dest="pack_key", required=True, metavar="PEM",
                    help="the private key file (from `nable pack keygen`); never printed")
    kg = ps.add_parser("keygen", help="Make an Ed25519 signing key for an org or publisher")
    kg.add_argument("--out", dest="pack_out", required=True, metavar="PATH",
                    help="where to write the private key (PEM, mode 0600); the public key "
                         "goes to PATH.pub")
    kg.add_argument("--name", dest="pack_key_name", default="org-packs",
                    help="the name to show in packs.trusted_keys (default org-packs)")
    kg.add_argument("--encrypt", dest="pack_encrypt", action="store_true",
                    help="encrypt the private key with a passphrase (asked for, or read from "
                         "NABLE_PACK_KEY_PASSPHRASE)")
    rn = ps.add_parser("run", help="Run an installed pack's connector (or adapter) once")
    rn.add_argument("pack_id")
    rn.add_argument("entry_id")
    rn.add_argument("--start", dest="pack_start", default=None, metavar="YYYY-MM-DD",
                    help="window start for a connector (default 30 days before --end)")
    rn.add_argument("--end", dest="pack_end", default=None, metavar="YYYY-MM-DD",
                    help="window end, exclusive (default today)")
    rn.add_argument("--timeout", dest="pack_timeout", type=float, default=None,
                    metavar="SECONDS", help="stop the pack after this long")
    for sp in (v, n, i, u, r, ls, s, sg, kg, rn, ps.choices["audit"]):
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
    sig = plan.signature
    lines = [f"{verb} ({m.tier})", f"  {m.description}",
             f"  source: {plan.source.spec()}", f"  digest: {plan.digest}",
             f"  signature: {sig.status}, {sig.reason}",
             f"  maintainers: {', '.join(m.maintainers)}"]
    if m.attestation:
        lines.append(f"  attestation: {m.attestation} (recorded, not verified)")
    lines += ["", "  It may:"]
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
        lines.append("  Carries code: " + ", ".join(f"{c.kind[:-1]} {c.id}" for c in m.code))
        lines.append("    It runs out of process through the broker, only when the pack is "
                     "signed by a trusted key or allowlisted by the org.")
        lines.append(f"    {SANDBOX_NOTE}")
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


def _passphrase(key_path: str) -> bytes | None:
    """The passphrase for an encrypted key: NABLE_PACK_KEY_PASSPHRASE, else a
    prompt at a terminal; None for an unencrypted key. Never echoed or logged."""
    from .signing import PASSPHRASE_ENV
    try:
        with open(os.path.expanduser(key_path), "rb") as f:
            head = f.read(64)
    except OSError:
        return None  # load_private_key says what is wrong with the file
    if b"ENCRYPTED" not in head:
        return None
    env = os.environ.get(PASSPHRASE_ENV)
    if env:
        return env.encode("utf-8")
    if sys.stdin.isatty():
        import getpass
        return getpass.getpass("  Key passphrase: ").encode("utf-8")
    return None


def _keygen(parsed, as_json: bool) -> int:
    from .signing import PASSPHRASE_ENV, keygen
    passphrase = None
    if parsed.pack_encrypt:
        env = os.environ.get(PASSPHRASE_ENV)
        if env:
            passphrase = env.encode("utf-8")
        elif sys.stdin.isatty():
            import getpass
            a = getpass.getpass("  New key passphrase: ")
            if a != getpass.getpass("  Again: ") or not a:
                _err("The passphrases did not match (or were empty); no key was written.")
                return EXIT_FAIL
            passphrase = a.encode("utf-8")
        else:
            _err(f"--encrypt needs a terminal or {PASSPHRASE_ENV}; no key was written.")
            return EXIT_FAIL
    r = keygen(parsed.pack_out, passphrase=passphrase)
    r["name"] = parsed.pack_key_name
    snippet = (f"packs:\n  trusted_keys:\n    - name: {parsed.pack_key_name}\n"
               f"      key: {r['public_key']}\n")
    r["policy_snippet"] = snippet
    _out({"ok": True, **r}, as_json,
         f"Wrote the private key to {r['private_key_path']} (mode 0600). Keep it offline or in "
         f"a secrets manager; anyone holding it can sign packs your machines trust.\n"
         f"Public key {r['key_id']} in {r['public_key_path']}. To trust it, add to the org "
         f"policy file (nable.policy.yaml):\n\n{snippet}")
    return EXIT_OK


def _run_code(parsed, as_json: bool) -> int:
    from datetime import date, timedelta

    from . import broker
    from .errors import PackError
    prep = broker.prepare(parsed.pack_id, parsed.entry_id)
    if prep.kind == "sinks":
        raise PackError(f"{parsed.pack_id} {parsed.entry_id} is a sink; sinks deliver "
                        "proposals from nable's own flows, not from `nable pack run`")
    if prep.kind == "connectors":
        try:
            end = (date.fromisoformat(parsed.pack_end) if parsed.pack_end
                   else broker.local_today())
            start = (date.fromisoformat(parsed.pack_start) if parsed.pack_start
                     else end - timedelta(days=30))
        except ValueError:
            raise PackError("--start and --end are dates like 2026-09-01") from None
        if start >= end:
            raise PackError("--start must be before --end")
        r = broker.fetch_costs(parsed.pack_id, parsed.entry_id, start, end,
                               timeout=parsed.pack_timeout)
        rows = r.output
        by_service: dict[str, float] = {}
        for row in rows:
            by_service[row["ServiceName"]] = by_service.get(row["ServiceName"], 0.0) + \
                row["BilledCost"]
        total = sum(by_service.values())
        lines = [(f"{r.pack} connector {r.entry}: {len(rows)} FOCUS rows for {start} to "
                  f"{end}, {total:,.2f} billed")]
        lines += [f"  {svc}: {amt:,.2f}" for svc, amt in
                  sorted(by_service.items(), key=lambda kv: -kv[1])[:5]]
        summary = {"rows": len(rows), "billed_total": round(total, 6),
                   "by_service": by_service, "start": str(start), "end": str(end)}
    else:
        r = broker.propose_facts(parsed.pack_id, parsed.entry_id,
                                 {"today": broker.local_today().isoformat()},
                                 timeout=parsed.pack_timeout)
        lines = [(f"{r.pack} adapter {r.entry}: {len(r.output)} proposed facts "
                  "(shown, not written; `nable org init` proposes them)")]
        lines += [f"  {f.fact} {f.subject}: {json.dumps(f.value, sort_keys=True)} "
                  f"[{f.source}]" for f in r.output[:20]]
        summary = {"facts": len(r.output)}
    if r.dropped:
        lines.append(f"  {r.dropped} dropped as invalid:")
        lines += [f"    {p}" for p in r.problems]
    elif r.problems:
        lines += [f"  note: {p}" for p in r.problems]
    net = r.network
    observed = net.get("observed") or []
    refused = [o for o in observed if not o.get("allowed")]
    declared = ", ".join(net.get("declared") or []) or "nothing"
    lines.append(f"  network: {net.get('mode')}; declared {declared}; "
                 f"{len(observed) - len(refused)} connections allowed, {len(refused)} refused")
    lines += [f"    refused {o['host']}:{o['port']}" for o in refused[:10]]
    lines.append(f"  log: {r.log}")
    lines.append(f"  {SANDBOX_NOTE}")
    body = {"ok": True, **r.to_dict(), "summary": summary, "sandbox": SANDBOX_NOTE}
    if prep.kind == "connectors":
        body["output"] = body["output"][:50]
        body["output_truncated"] = len(r.output) > 50
    _out(body, as_json, "\n".join(lines))
    return EXIT_OK


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
                sig = r.get("signature") or {}
                what = [f"{n} {k}" for k, n in r["provides"].items()]
                what += [f"{c['kind'][:-1]} {c['id']}" for c in r.get("code") or []]
                text = (f"OK {r['id']} {r['version']} ({r['tier']}): {r['files']} files, "
                        + (", ".join(what) or "no content")
                        + f"\n  digest {r['digest']}"
                        + f"\n  signature {sig.get('status')}: {sig.get('reason')}")
            else:
                text = f"{parsed.path} is not a valid pack:\n" + "\n".join(
                    f"  {p}" for p in r["problems"])
            text += "".join(f"\n  warning: {w}" for w in r.get("warnings") or [])
            _out(r, as_json, text)
            return EXIT_OK if r["ok"] else EXIT_FAIL
        if action == "sign":
            r = inst.sign(parsed.path, parsed.pack_key, passphrase=_passphrase(parsed.pack_key))
            _out(r, as_json, f"Signed {r['id']} {r['version']} ({r['tier']})\n"
                             f"  digest {r['digest']}\n  key {r['key_id']}\n"
                             f"  wrote {r['signature_path']}")
            return EXIT_OK
        if action == "keygen":
            return _keygen(parsed, as_json)
        if action == "run":
            return _run_code(parsed, as_json)
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
    _err("usage: nable pack {validate,new,install,update,remove,list,audit,search,sign,"
         "keygen,run} ...")
    return 2

