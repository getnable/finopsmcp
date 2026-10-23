# SPDX-License-Identifier: Apache-2.0
"""`nable cloud connect` and `nable cloud status`: bring this machine's org model,
guard ledger and coding-agent usage into a paid workspace, and see what the
workspace holds.

    nable cloud connect --box https://HOST [--org-dir DIR] [--out PATH] [--json]
                        [--as WHO] [--no-agent-usage] [--delete-org-file NAME ...]
                        [--without-org-file NAME ...]
                        [--send --confirm CODE [--key-file PATH]]
    nable cloud status  --box https://HOST [--key-file PATH] [--as WHO] [--json]

`connect` is a dry run unless --send: it builds the payload, writes the exact
bytes to a file, and prints where they would go, what is in them, their size,
their sha256 and the confirm code, the sha256 of the box, a newline and the
bytes (finops.cloud_connect says what the payload holds and why). `--send
--confirm CODE` sends those bytes to that box, and only while they are still
the bytes, and it the box, that the code names.

`--delete-org-file NAME` (repeatable, NAME one of the org model's file names)
adds a line that removes that file from the workspace, ahead of the org model,
so the dry run shows it and the confirm code covers it. A file this machine
also carries is applied after its delete, even when the workspace held that
text before and replaced it: how a team deliberately goes back (D97). A box
that does not list org_file_delete among what it accepts is told nothing.

`--without-org-file NAME` (repeatable, NAME one of the org model's file names)
leaves that file out of the org model the payload carries (D138): the dry run
names it, and the confirm code covers the bytes without it, so a send needs
the flag again. authority.yaml goes only to a box whose status lists
org_file:authority.yaml among what it accepts; to any other box the send is
refused before anything leaves, and the refusal names --without-org-file
authority.yaml.

`status` and a send keep the workspace's effective rates the status answer
carries (its `rates` block, D147) as cloud-rates.json for the guard, only
when they come from the box and the workspace this machine last sent to
(finops.cloud_rates); otherwise they are shown and not kept, with why.

`status` also reads the box's month to date for one person, when the box
lists "standing" among what it reads (POST /api/free-tool/standing with
exactly {"person": P}): P is --as WHO, else FINOPS_GUARD_PERSON, else git's
user.email, as the guard resolves whose AI budget it holds this machine's
agents to (ai_budget.developer). P is sent only when it is an email or
github:LOGIN of at most 320 characters, and every answer says who was asked
and where that name came from. The answer is kept as cloud-standing.json
beside the guard ledger (finops.cloud_standing), without the key, only when
the guard would use it: it is about the guard's own person, and from the box
and the workspace this machine last sent its usage to (or it has sent to
none). Otherwise it is shown and the guard's file is left as it was. The
guard compares the larger of the kept one and this machine's figure with the
local budgets.yaml, never their sum, without opening a connection. A
standing the workspace's plan does not include is said, and is not a
failure.

A send is a person's decision, as `nable org confirm` is: without a terminal
(stdin and stdout) it refuses unless --as names who decided. The guard asks
before an agent runs --send, --out or `cloud status` (guard._SELF_RULES).

Coding-agent usage is sent as the developer who sends it, the rule `nable
org` uses for who decided: --as WHO, else at a terminal git's user.email, then
$USER. The dry run shows that name and builds the records with it, so the
confirm code covers it: a dry run made with one --as and a send with another
are two payloads, and the send is refused. Without a terminal and without
--as there is no developer, and the records are left out (said in the dry
run). --no-agent-usage leaves them out on purpose.

The workspace key is a bound analyst key made on the workspace's /workspace
page, read from NABLE_WORKSPACE_KEY, a --key-file with mode 0600, or a hidden
prompt at a terminal. It is never an argument: a --key given anyway is refused
without being echoed or used, and so is any word left over or any flag this
command does not have, since either could be one. None of them is repeated,
and neither is a value argparse would quote back (--send=X, an abbreviation):
these parsers take no abbreviation and never print what they could not read.

Exit codes:
    0  the dry run was built, the send finished, or the status was read
    1  refused or not reached: the payload changed since the dry run or the
       dry run was for another box, the box said no or could not be reached,
       or FINOPS_AIRGAP is set
    2  a usage error: a box that is not https://host, --send without
       --confirm or without a terminal or --as, an --out nable never writes,
       no workspace key, a key file that is not 0600, or a key on the
       command line
    3  nothing to send: no org model and no ledger records here
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from . import cloud_connect as cc

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_NOTHING = 3

_PARSE_ERROR = ("the command line was not read, and is not repeated here: a workspace key is "
                "never taken from the command line, and any of it could be one. "
                "`nable cloud connect --help` lists the options (none is abbreviated).")

_ARGV_KEY = ("the workspace key is never taken from the command line, so it was not used "
             "(and is not repeated here). Set NABLE_WORKSPACE_KEY, pass --key-file with a "
             "0600 file, or run at a terminal to be asked. A key typed on a command line "
             "is in your shell history: revoke it on /workspace and make another.")


# argparse's errors that quote nothing a person typed. Every other one
# (an explicit value a flag does not take, an ambiguous abbreviation, an
# invalid choice) prints the word back, value and all.
_SAFE_ERRORS = ("the following arguments are required:", "expected one argument")


def _quiet_error(parser: argparse.ArgumentParser):
    def error(message: str):
        parser.print_usage(sys.stderr)
        said = message if any(m in message for m in _SAFE_ERRORS) else _PARSE_ERROR
        parser.exit(EXIT_USAGE, f"nable cloud: {said}\n")
    return error


def add_parser(sub) -> None:
    """Register `cloud connect` and `cloud status` on the wizard's argparse tree."""
    p = sub.add_parser(
        "cloud",
        help="Bring your org model and guard ledger into a paid workspace (a dry run first)",
        allow_abbrev=False,
    )
    p.error = _quiet_error(p)
    actions = p.add_subparsers(dest="cloud_action", metavar="<action>")
    c = actions.add_parser(
        "connect", allow_abbrev=False,
        help="show, then send, your org model and guard ledger to a paid workspace",
        description=("Builds the payload from the org model files in your org dir and the "
                     "guard ledger's lines as stored, saves the exact bytes, and prints "
                     "their destination, contents, size, sha256 and confirm code. Nothing "
                     "is sent without --send --confirm CODE, at a terminal or with --as."),
        epilog="exit codes:\n  0  done\n  1  refused or not reached\n  2  usage error\n"
               "  3  nothing to send",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    s = actions.add_parser(
        "status", help="what the workspace holds from the free tool", allow_abbrev=False,
        description="What the workspace holds: org model versions and ledger chains.")
    for q in (c, s):
        q.error = _quiet_error(q)
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
    s.add_argument("--as", dest="cloud_as", metavar="WHO",
                   help="whose month to date to read from the box (default: "
                        "FINOPS_GUARD_PERSON, else git user.email, as the guard reads it)")
    c.add_argument("--org-dir", metavar="DIR",
                   help="the org model dir (default: FINOPS_ORG_DIR, a trusted repo's "
                        "nable.org/, else the data dir's)")
    c.add_argument("--out", metavar="PATH",
                   help=f"where to save the payload (default: {cc.PAYLOAD_NAME} beside the "
                        "guard ledger)")
    c.add_argument("--send", action="store_true",
                   help="send the payload; needs --confirm with the code the dry run printed")
    c.add_argument("--confirm", metavar="CODE",
                   help="the confirm code the dry run printed for this box")
    c.add_argument("--as", dest="cloud_as", metavar="WHO",
                   help="who decided to send, when this does not run at a terminal; also "
                        "the developer coding-agent usage is sent as (default at a "
                        "terminal: git user.email, then $USER)")
    c.add_argument("--no-agent-usage", dest="no_agent_usage", action="store_true",
                   help="leave this machine's coding-agent usage out of the payload")
    c.add_argument("--delete-org-file", dest="delete_org_files", action="append",
                   metavar="NAME", default=None,
                   help="remove this org model file from the workspace (repeatable; one of "
                        + ", ".join(cc.org_file_names()) + ")")
    c.add_argument("--without-org-file", dest="without_org_files", action="append",
                   metavar="NAME", default=None,
                   help="leave this org model file out of what is sent (repeatable; shown in "
                        "the dry run and covered by the confirm code)")


def refuse_extras(extras: list[str], *, as_json: bool, stdout: Any = None,
                  stderr: Any = None) -> int:
    """Words on a `nable cloud` command line that no option took: refused
    like a key on the command line, and none of them repeated, since any could
    be one (the wizard's parser would print each back, value and all)."""
    n = len(extras)
    return _fail("key_in_argv", EXIT_USAGE,
                 f"{n} argument{'s' if n != 1 else ''} not recognized, and not repeated here: "
                 "a workspace key is never taken from the command line, and any of them could "
                 "be one. `nable cloud connect --help` lists the options. A key typed on a "
                 "command line is in your shell history: revoke it on /workspace and make "
                 "another.", as_json=as_json, out=stdout or sys.stdout, err=stderr or sys.stderr)


def _err_doc(klass: str, code: int, message: str) -> str:
    return json.dumps({"error": {"class": klass, "exit_code": code, "message": message}})


def _fail(klass: str, code: int, message: str, *, as_json: bool, out: Any, err: Any) -> int:
    print(f"nable cloud: {message}", file=err)
    if as_json:
        print(_err_doc(klass, code, message), file=out)
    return code


def _is_tty() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _git_email() -> str | None:
    """git's user.email where this runs, or None (the `nable org` rule)."""
    from .org.cli import _git_email as org_git_email
    return org_git_email()


def _developer(who: str, at_terminal: bool) -> tuple[str | None, str]:
    """(the developer coding-agent usage is sent as, where the name came
    from): --as, else at a terminal git's user.email, then $USER."""
    if who:
        return who, "--as"
    if not at_terminal:
        return None, ""
    email = _git_email()
    if email:
        return email, "git user.email"
    user = os.environ.get("USER", "").strip()
    return (user, "$USER") if user else (None, "")


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
        per_file = om.get("files") if isinstance(om.get("files"), dict) else {}
        for name, f in sorted(per_file.items()):
            if isinstance(f, dict):
                lines.append(f"  {name!s:<20} {f.get('bytes', 0):>9,} bytes, sha256 "
                             f"{str(f.get('sha256'))[:16]}, uploaded {f.get('uploaded_at')} "
                             f"by {f.get('by')}")
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
        stdout: Any = None, stderr: Any = None, terminal: bool | None = None,
        now: datetime | None = None) -> int:
    """`prompt` True means a hidden prompt when stdin is a terminal; None
    means never ask. `terminal` None means whether stdin and stdout are one.
    `now` fixes today for the coding-agent usage window."""
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    as_json = bool(getattr(args, "json", False))
    action = getattr(args, "cloud_action", None)
    if action not in ("connect", "status"):
        print("usage: nable cloud connect --box https://HOST [--send --confirm CODE]\n"
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
        who = str(getattr(args, "cloud_as", None) or "").strip()
        if who and (len(who) > STANDING_PERSON_MAX
                    or any(ord(c) < 0x20 or ord(c) == 0x7f for c in who)):
            return _fail("usage", EXIT_USAGE, f"--as takes one line of at most "
                         f"{STANDING_PERSON_MAX} characters (an email or github:LOGIN)",
                         as_json=as_json, out=out, err=err)
        try:
            k = key()
        except cc.ConnectError as exc:
            return _fail("no_key", EXIT_USAGE, str(exc), as_json=as_json, out=out, err=err)
        try:
            st = cc.status(box, k, opener=opener)
        except cc.ConnectError as exc:
            return _fail("refused", EXIT_REFUSED, str(exc), as_json=as_json, out=out, err=err)
        got = _standing(box, k, st, who, opener=opener, now=now)
        rates = _rates(box, st, now=now)
        from datetime import UTC
        authority = cc.keep_authority(st, box=box, fetched_at=now or datetime.now(UTC))
        if as_json:
            print(json.dumps({**st, "standing": got, "rates_kept": rates,
                              **({"authority_kept": authority} if authority is not None
                                 else {})}, indent=2, default=str), file=out)
        else:
            print(_status_text(box, st), file=out)
            print(_standing_text(got), file=out)
            print(_rates_text(rates, st), file=out)
            said = cc.authority_text(authority, st) if authority is not None else None
            if said:
                print(said, file=out)
        failed = got["read"] is False and got.get("asked") and not got.get("plan")
        return EXIT_REFUSED if failed else EXIT_OK

    sending = bool(getattr(args, "send", False))
    confirm = str(getattr(args, "confirm", None) or "").strip().lower()
    if sending and not confirm:
        return _fail("usage", EXIT_USAGE, "--send needs --confirm CODE, the confirm code the dry "
                     "run printed for the bytes it saved", as_json=as_json, out=out, err=err)
    if confirm and not sending:
        return _fail("usage", EXIT_USAGE, "--confirm goes with --send; without --send this "
                     "is a dry run", as_json=as_json, out=out, err=err)
    who = str(getattr(args, "cloud_as", None) or "").strip()
    at_terminal = _is_tty() if terminal is None else terminal
    if sending and not who and not at_terminal:
        return _fail("usage", EXIT_USAGE, "sending is a person's decision: run it at your own "
                     "terminal, or pass --as WHO (your name or email) to say who decided",
                     as_json=as_json, out=out, err=err)
    developer, developer_from = _developer(who, at_terminal)
    no_usage = bool(getattr(args, "no_agent_usage", False))
    deletes = list(getattr(args, "delete_org_files", None) or [])
    known = cc.org_file_names()
    without = list(getattr(args, "without_org_files", None) or [])
    for flag, names in (("--delete-org-file", deletes), ("--without-org-file", without)):
        if any(n not in known for n in names):
            # The name is not repeated: anything on a `nable cloud` command
            # line could be a key typed in the wrong place.
            return _fail("usage", EXIT_USAGE, f"{flag} takes an org model file name: "
                         + ", ".join(known), as_json=as_json, out=out, err=err)
    payload = cc.build(org_dir=getattr(args, "org_dir", None), developer=developer,
                       developer_from=developer_from, agent_usage=not no_usage, now=now,
                       delete_files=deletes, without_files=without)
    if not sending:
        if not payload.data:
            if as_json:
                print(json.dumps(cc.summary(payload, box, None), indent=2), file=out)
            else:
                print(cc.describe(payload, box, None), file=out)
            return EXIT_NOTHING
        try:
            saved = cc.save(payload, Path(args.out) if getattr(args, "out", None) else None)
        except cc.ConnectError as exc:
            return _fail("usage", EXIT_USAGE, str(exc), as_json=as_json, out=out, err=err)
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

    if cc.confirm_code(payload, box) != confirm:
        return _fail("payload_changed", EXIT_REFUSED,
                     f"the confirm code is not the one for these bytes and {cc.host_of(box)}: "
                     "the payload changed since you looked, or the dry run was for another "
                     f"box. Run the dry run again with --box {box}, read what it saved, and "
                     "confirm the code it prints.",
                     as_json=as_json, out=out, err=err)
    if not payload.data:
        return _fail("nothing", EXIT_NOTHING, "nothing to send", as_json=as_json, out=out,
                     err=err)
    try:
        k = key()
    except cc.ConnectError as exc:
        return _fail("no_key", EXIT_USAGE, str(exc), as_json=as_json, out=out, err=err)
    chrome = err if as_json else out
    print(f"nable cloud connect: sending to {box} (host {cc.host_of(box)})"
          + (f", as {who} decided" if who else ""), file=chrome)
    print(f"  payload sha256 {payload.sha256} confirmed for this box, "
          f"{len(payload.data):,} bytes", file=chrome)
    try:
        result = cc.send(payload, box, k, opener=opener,
                         say=lambda line: print(line, file=chrome))
    except cc.ConnectError as exc:
        return _fail("refused", EXIT_REFUSED, str(exc), as_json=as_json, out=out, err=err)
    if as_json:
        print(json.dumps({"sent": True, "sha256": payload.sha256, "decided_by": who or None,
                          **result}, indent=2, default=str), file=out)
    else:
        held = result["bytes_already_held"]
        print(f"Sent {result['bytes_sent']:,} bytes in {result['batches']} batch(es)"
              + (f"; the workspace already held the other {held:,}" if held else "") + ".",
              file=out)
        for line in _org_files_text(result.get("org_files") or {}):
            print(line, file=out)
        print(_status_text(box, result.get("status") or {}), file=out)
        if isinstance(result.get("rates"), Mapping):
            print(_rates_text(result["rates"], result.get("status") or {}), file=out)
        if isinstance(result.get("authority"), Mapping):
            said = cc.authority_text(result["authority"], result.get("status") or {})
            if said:
                print(said, file=out)
    return EXIT_OK


def _rates(box: str, st: Mapping[str, Any], *, now: datetime | None) -> dict[str, Any]:
    """Keep the status answer's rates block for the guard (D147), only as
    the box's and the workspace's this machine last sent to; otherwise it is
    shown and not kept, with why (cloud_rates.keep)."""
    from datetime import UTC

    from . import cloud_rates
    return cloud_rates.keep(st.get("rates"), box=box, workspace_ref=st.get("workspace_ref"),
                            fetched_at=now or datetime.now(UTC))


def _rates_text(got: Mapping[str, Any], st: Mapping[str, Any]) -> str:
    from . import cloud_rates
    return cloud_rates.text(dict(got), st.get("rates"))


def _org_files_text(got: Mapping[str, Any]) -> list[str]:
    """The box's per-file answer on the org model files a send carried."""
    if not got:
        return []
    out = ["Org model files, as the workspace took them:"]
    words = {"applied": "applied", "unchanged": "unchanged (the workspace holds this text)",
             "deleted": "deleted", "not_held": "not held, so nothing to delete"}
    for what in ("applied", "unchanged", "deleted", "not_held"):
        for name in got.get(what) or []:
            if isinstance(name, str):
                out.append(f"  {name:<20} {words[what]}")
    for x in got.get("stale") or []:
        if isinstance(x, Mapping):
            out.append(f"  {x.get('file')!s:<20} stale, not applied: the workspace held this "
                       f"text and replaced it at {x.get('replaced_at')}. To go back to it, "
                       f"send again with --delete-org-file {x.get('file')}")
    for x in got.get("held") or []:
        if isinstance(x, Mapping):
            what = "its delete" if x.get("delete") else "this text"
            out.append(f"  {x.get('file')!s:<20} held, not applied: {what} would widen "
                       "purchase authority, so the workspace keeps what it holds until a "
                       "workspace admin who holds the purchase grant confirms it on /rds-ri")
    return out


# The longest person the box takes (an email or github:LOGIN).
STANDING_PERSON_MAX = 320
# The shapes the box takes for a person (agent_usage.standing.person_of).
_EMAIL_RE = re.compile(r"^[^@\s:]+@[^@\s]+\.[^@\s]+$")
_GITHUB_RE = re.compile(r"^github:[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){0,38}$")


def _person_sendable(person: str) -> bool:
    """An email or github:LOGIN of at most STANDING_PERSON_MAX characters,
    on one line: what is sent is what the box takes, and nothing longer."""
    return (len(person) <= STANDING_PERSON_MAX
            and not any(ord(c) < 0x20 or ord(c) == 0x7f for c in person)
            and bool(_EMAIL_RE.match(person) or _GITHUB_RE.match(person)))


def _person(who: str) -> tuple[str | None, str]:
    """(whose standing to read, where that came from): --as, else the
    guard's own rule (ai_budget.developer: FINOPS_GUARD_PERSON, else git's
    user.email for this directory), lowercased."""
    if who:
        return who.strip().lower(), "--as"
    from . import ai_budget
    person, source = ai_budget.developer(os.getcwd())
    return person, source


def _standing(box: str, key: str, st: Mapping[str, Any], who: str, *,
              opener: cc.Opener | None, now: datetime | None) -> dict[str, Any]:
    """Read and keep the box's standing for one person, when the box reads
    it. {"read": True, "file", "standing"}, or {"read": False, "why",
    "asked"}: asked says whether a request was made."""
    from . import cloud_standing
    reads = st.get("reads")
    if not (isinstance(reads, list) and cc.READS_STANDING in reads):
        return {"read": False, "asked": False,
                "why": ("the box does not offer a person's standing to this workspace (a box "
                        "before it, or a plan below Cloud), so nothing was asked; the guard "
                        "keeps to this machine's figure")}
    person, source = _person(who)
    if not person:
        return {"read": False, "asked": False,
                "why": ("nobody is named on this machine, so nothing was asked: pass --as WHO, "
                        "or set FINOPS_GUARD_PERSON")}
    if not _person_sendable(person):
        # Not repeated: it is not what the box takes, and could be anything.
        return {"read": False, "asked": False, "person_from": source,
                "why": (f"the person from {source} is not an email or github:LOGIN of at most "
                        f"{STANDING_PERSON_MAX} characters, so nothing was asked")}
    asked = {"person": person, "person_from": source}
    try:
        answer = cc.standing(box, key, person, opener=opener)
        from datetime import UTC
        doc = cloud_standing.normalize(answer, box=box, person=person,
                                       fetched_at=now or datetime.now(UTC))
    except cc.NotOnPlan as exc:
        return {"read": False, "asked": True, "plan": True, **asked,
                "why": f"{exc}; the guard keeps to this machine's figure"}
    except cc.ConnectError as exc:
        return {"read": False, "asked": True, **asked, "why": str(exc)}
    except cloud_standing.StandingError as exc:
        return {"read": False, "asked": True, **asked,
                "why": f"the box's answer was not kept ({exc})"}
    # Kept only where the guard would use it: about its own person, from the
    # box and workspace this machine sends to. Anything else would replace
    # the guard's one file with a standing it then ignores.
    from . import ai_budget
    mine, _from = ai_budget.developer(os.getcwd())
    not_kept = cloud_standing.usable(doc, None, person=mine, month=doc["month"],
                                     what="this answer")
    if not_kept:
        return {"read": True, "kept": False, "not_kept": not_kept, **asked,
                "file": str(cloud_standing.path()), "standing": doc}
    try:
        path = cloud_standing.write(doc)
    except (cloud_standing.StandingError, OSError) as exc:
        return {"read": False, "asked": True, **asked,
                "why": f"the box's answer was not kept ({exc})"}
    return {"read": True, "kept": True, **asked, "file": str(path), "standing": doc}


def _money(v: Any) -> str:
    """A decimal string as dollars to the cent, rounded up; None as no records."""
    from decimal import ROUND_CEILING, Decimal, InvalidOperation
    if v is None:
        return "no records received"
    try:
        return f"${Decimal(str(v)).quantize(Decimal('0.01'), rounding=ROUND_CEILING):,.2f}"
    except InvalidOperation:
        return "not read"


def _budget_text(b: Mapping[str, Any] | None) -> str:
    if not isinstance(b, Mapping):
        return "no confirmed budget on the box"
    parts = []
    if b.get("monthly_usd") is not None:
        parts.append(f"{_money(b['monthly_usd'])} a month")
    if b.get("per_person_usd") is not None:
        parts.append(f"{_money(b['per_person_usd'])} per person")
    return f"budget {', '.join(parts) or 'with no figure'}: {b.get('state')}"


def _standing_text(got: Mapping[str, Any]) -> str:
    if not got.get("read"):
        who = (f" for {got['person']} ({got.get('person_from')})" if got.get("person")
               else f" (the person from {got['person_from']})" if got.get("person_from")
               else "")
        return f"Standing: not read{who}: {got.get('why')}"
    d = got["standing"]
    f = d["figures"]
    p = f["person"]
    head = (f"Standing for {d['person']} ({got.get('person_from')}), {d['month']} UTC, as of "
            f"{d['as_of'] or 'no records received'}, {d['basis']} dollars from what people "
            "sent:")
    # "across", not "over": the budget's state on the same line may be "over".
    days = (f" across {p['days']} day(s), last day {p['last_day']}" if p["usd"] is not None
            else "")
    lines = [head, f"  {d['person']:<24} {_money(p['usd'])}{days}; {_budget_text(p['budget'])}"]
    for t in f["teams"]:
        lines.append(f"  team {t['team']:<19} {_money(t['usd'])}; {t['people_sent']} of "
                     f"{t['people']} people sent, latest day {t['latest_day'] or 'none'}, "
                     f"oldest last day {t['oldest_last_day'] or 'none'}; "
                     f"{_budget_text(t['budget'])}")
    o = f["org"]
    lines.append(f"  {'the org':<24} {_money(o['usd'])}; {o['people_sent']} people sent, latest "
                 f"day {o['latest_day'] or 'none'}; {_budget_text(o['budget'])}")
    if got.get("kept") is False:
        lines.append(f"Not kept for the guard, whose {got['file']} is as it was: "
                     f"{got.get('not_kept')}.")
        return "\n".join(lines)
    lines.append(f"Kept in {got['file']} for the guard, which compares the larger of this "
                 "machine's month to date and the box's with your local budgets.yaml, never "
                 "their sum. The box's budgets are shown here only.")
    return "\n".join(lines)


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
