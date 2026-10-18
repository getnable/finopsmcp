# SPDX-License-Identifier: Apache-2.0
"""`nable cloud connect` and `nable cloud status`: bring this machine's org model
and guard ledger into a paid workspace, and see what the workspace holds.

    nable cloud connect --box https://HOST [--org-dir DIR] [--out PATH] [--json]
                        [--send --confirm SHA256 [--key-file PATH]]
    nable cloud status  --box https://HOST [--key-file PATH] [--json]

`connect` is a dry run unless --send: it builds the payload, writes the exact
bytes to a file, and prints where they would go, what is in them, their size
and their sha256 (finops.cloud_connect says what the payload holds and why).
`--send --confirm SHA256` sends those bytes, and only while they are still the
bytes that sha256 names.

The workspace key is a bound analyst key made on the workspace's /workspace
page, read from NABLE_WORKSPACE_KEY, a --key-file with mode 0600, or a hidden
prompt at a terminal. It is never an argument: a --key given anyway is refused
without being echoed or used, and so is any word left over, which could be one.

Exit codes:
    0  the dry run was built, the send finished, or the status was read
    1  refused or not reached: the payload changed since the dry run, the box
       said no or could not be reached, or FINOPS_AIRGAP is set
    2  a usage error: a box that is not https://host, --send without
       --confirm, no workspace key, a key file that is not 0600, or a key on
       the command line
    3  nothing to send: no org model and no ledger records here
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import cloud_connect as cc

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_NOTHING = 3

_ARGV_KEY = ("the workspace key is never taken from the command line, so it was not used "
             "(and is not repeated here). Set NABLE_WORKSPACE_KEY, pass --key-file with a "
             "0600 file, or run at a terminal to be asked. A key typed on a command line "
             "is in your shell history: revoke it on /workspace and make another.")


def add_parser(sub) -> None:
    """Register `cloud connect` and `cloud status` on the wizard's argparse tree."""
    p = sub.add_parser(
        "cloud",
        help="Bring your org model and guard ledger into a paid workspace (a dry run first)",
    )
    actions = p.add_subparsers(dest="cloud_action", metavar="<action>")
    c = actions.add_parser(
        "connect",
        help="show, then send, your org model and guard ledger to a paid workspace",
        description=("Builds the payload from the org model files in your org dir and the "
                     "guard ledger's lines as stored, saves the exact bytes, and prints "
                     "their destination, contents, size and sha256. Nothing is sent "
                     "without --send --confirm SHA256."),
        epilog="exit codes:\n  0  done\n  1  refused or not reached\n  2  usage error\n"
               "  3  nothing to send",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    s = actions.add_parser(
        "status", help="what the workspace holds from the free tool",
        description="What the workspace holds: org model versions and ledger chains.")
    for q in (c, s):
        q.add_argument("--box", required=True, metavar="https://HOST",
                       help="the paid workspace's address (https only)")
        q.add_argument("--key-file", metavar="PATH",
                       help=f"read the workspace key from this 0600 file (else {cc.KEY_ENV}, "
                            "else a hidden prompt)")
        q.add_argument("--json", action="store_true", help="one JSON document on stdout")
        # Traps: a key on the command line is refused, never echoed or used.
        q.add_argument("--key", "--workspace-key", "--api-key", dest="argv_key", nargs="?",
                       const="", default=None, help=argparse.SUPPRESS)
        q.add_argument("leftover", nargs="*", help=argparse.SUPPRESS)
    c.add_argument("--org-dir", metavar="DIR",
                   help="the org model dir (default: FINOPS_ORG_DIR, a trusted repo's "
                        "nable.org/, else the data dir's)")
    c.add_argument("--out", metavar="PATH",
                   help=f"where to save the payload (default: {cc.PAYLOAD_NAME} beside the "
                        "guard ledger)")
    c.add_argument("--send", action="store_true",
                   help="send the payload; needs --confirm with the sha256 the dry run printed")
    c.add_argument("--confirm", metavar="SHA256",
                   help="the payload's sha256, as the dry run printed it")


def _err_doc(klass: str, code: int, message: str) -> str:
    return json.dumps({"error": {"class": klass, "exit_code": code, "message": message}})


def _fail(klass: str, code: int, message: str, *, as_json: bool, out: Any, err: Any) -> int:
    print(f"nable cloud: {message}", file=err)
    if as_json:
        print(_err_doc(klass, code, message), file=out)
    return code


def _terminal_prompt() -> Callable[[str], str] | None:
    try:
        if sys.stdin.isatty():
            import getpass
            return getpass.getpass
    except (AttributeError, ValueError):
        pass
    return None


def _status_text(box: str, st: Mapping[str, Any]) -> str:
    lines = [f"nable cloud status: {box}"]
    om = st.get("org_model") if isinstance(st.get("org_model"), dict) else None
    latest = (om or {}).get("latest") if isinstance((om or {}).get("latest"), dict) else None
    if latest:
        files = latest.get("files") or {}
        names = ", ".join(sorted(files)) if isinstance(files, dict) else ""
        lines.append(f"Org model: {om.get('versions', 1)} version(s) kept; the latest, "
                     f"uploaded {latest.get('uploaded_at') or 'at an unknown time'}"
                     + (f": {names}" if names else ""))
    else:
        lines.append("Org model: none uploaded")
    chains = [c for c in st.get("chains") or [] if isinstance(c, dict)]
    lines.append(f"Ledger chains: {len(chains)}")
    for c in chains:
        lines.append(f"  chain {str(c.get('chain_id'))[:16]}: {c.get('lines', 0):,} records, "
                     f"head {str(c.get('head'))[:16]}"
                     + (f", last upload {c['last_at']}" if c.get("last_at") else ""))
    totals = st.get("totals") if isinstance(st.get("totals"), dict) else {}
    limits = st.get("limits") if isinstance(st.get("limits"), dict) else {}
    if totals:
        held = f"The workspace holds {totals.get('lines', 0):,} ledger lines"
        if "bytes" in totals:
            held += f" and {totals['bytes']:,} bytes"
        if limits.get("lines") and limits.get("bytes"):
            held += f", of {limits['lines']:,} lines and {limits['bytes']:,} bytes it takes"
        lines.append(held + ".")
    return "\n".join(lines)


def run(args: argparse.Namespace, *, opener: cc.Opener | None = None,
        environ: Mapping[str, str] | None = None,
        prompt: Callable[[str], str] | None | bool = True,
        stdout: Any = None, stderr: Any = None) -> int:
    """`prompt` True means a hidden prompt when stdin is a terminal; None
    means never ask."""
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    as_json = bool(getattr(args, "json", False))
    action = getattr(args, "cloud_action", None)
    if action not in ("connect", "status"):
        print("usage: nable cloud connect --box https://HOST [--send --confirm SHA256]\n"
              "       nable cloud status --box https://HOST", file=err)
        return EXIT_USAGE
    if getattr(args, "argv_key", None) is not None or getattr(args, "leftover", None):
        return _fail("key_in_argv", EXIT_USAGE, _ARGV_KEY, as_json=as_json, out=out, err=err)
    env = os.environ if environ is None else environ
    ask = _terminal_prompt() if prompt is True else prompt
    try:
        box = cc.parse_box(args.box)
    except cc.ConnectError as exc:
        return _fail("usage", EXIT_USAGE, str(exc), as_json=as_json, out=out, err=err)

    def key() -> str:
        return cc.read_key(key_file=getattr(args, "key_file", None), environ=env, prompt=ask)

    if action == "status":
        try:
            k = key()
        except cc.ConnectError as exc:
            return _fail("no_key", EXIT_USAGE, str(exc), as_json=as_json, out=out, err=err)
        try:
            st = cc.status(box, k, opener=opener)
        except cc.ConnectError as exc:
            return _fail("refused", EXIT_REFUSED, str(exc), as_json=as_json, out=out, err=err)
        print(json.dumps(st, indent=2, default=str) if as_json else _status_text(box, st),
              file=out)
        return EXIT_OK

    sending = bool(getattr(args, "send", False))
    confirm = str(getattr(args, "confirm", None) or "").strip().lower()
    if sending and not confirm:
        return _fail("usage", EXIT_USAGE, "--send needs --confirm SHA256, the sha256 the dry "
                     "run printed for the bytes it saved", as_json=as_json, out=out, err=err)
    if confirm and not sending:
        return _fail("usage", EXIT_USAGE, "--confirm goes with --send; without --send this "
                     "is a dry run", as_json=as_json, out=out, err=err)
    payload = cc.build(org_dir=getattr(args, "org_dir", None))
    if not sending:
        if not payload.data:
            if as_json:
                print(json.dumps(cc.summary(payload, box, None), indent=2), file=out)
            else:
                print(cc.describe(payload, box, None), file=out)
            return EXIT_NOTHING
        saved = cc.save(payload, Path(args.out) if getattr(args, "out", None) else None)
        if as_json:
            doc = cc.summary(payload, box, saved)
            doc["airgap"] = cc._airgap()
            print(json.dumps(doc, indent=2), file=out)
        else:
            print(cc.describe(payload, box, saved), file=out)
            if cc._airgap():
                print("\nFINOPS_AIRGAP is set here, so a send is refused on this machine.",
                      file=out)
        return EXIT_OK

    if payload.sha256 != confirm:
        return _fail("payload_changed", EXIT_REFUSED,
                     "the payload changed since you looked: its sha256 is now "
                     f"{payload.sha256}, not the one confirmed. Run the dry run again, read "
                     "what it saved, and confirm the new sha256.",
                     as_json=as_json, out=out, err=err)
    if not payload.data:
        return _fail("nothing", EXIT_NOTHING, "nothing to send", as_json=as_json, out=out,
                     err=err)
    try:
        k = key()
    except cc.ConnectError as exc:
        return _fail("no_key", EXIT_USAGE, str(exc), as_json=as_json, out=out, err=err)
    chrome = err if as_json else out
    print(f"nable cloud connect: sending to {box} (host {cc.host_of(box)})", file=chrome)
    print(f"  payload sha256 {payload.sha256} confirmed, {len(payload.data):,} bytes",
          file=chrome)
    try:
        result = cc.send(payload, box, k, opener=opener,
                         say=lambda line: print(line, file=chrome))
    except cc.ConnectError as exc:
        return _fail("refused", EXIT_REFUSED, str(exc), as_json=as_json, out=out, err=err)
    if as_json:
        print(json.dumps({"sent": True, "sha256": payload.sha256, **result}, indent=2,
                         default=str), file=out)
    else:
        held = result["bytes_already_held"]
        print(f"Sent {result['bytes_sent']:,} bytes in {result['batches']} batch(es)"
              + (f"; the workspace already held the other {held:,}" if held else "") + ".",
              file=out)
        print(_status_text(box, result.get("status") or {}), file=out)
    return EXIT_OK


def main(args: argparse.Namespace) -> int:
    """run(), with Ctrl-C as 130 and an unexpected error said in one line."""
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\ncancelled", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - a bug is reported, never a traceback
        print(f"nable cloud stopped on an unexpected error ({type(exc).__name__}); please "
              "report it: https://github.com/getnable/finopsmcp/issues/new", file=sys.stderr)
        return 1
