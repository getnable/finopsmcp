# SPDX-License-Identifier: Apache-2.0
"""`nable cloud connect`: this machine's org model and guard ledger, carried
into a paid workspace, and nothing else.

The free tool keeps everything on the laptop. Someone who moves to a paid
workspace can bring two things with them: the org model they built (`nable
org`: owners, teams, accounts and policy, as confirmed here) and the guard's
decision ledger. This module is the only way either leaves the machine, and it
is built so that what leaves is exactly what the person was shown.

The payload is one JSON object per line, keys sorted, no spaces:

    {"files": {"owners.yaml": "...", ...}, "kind": "org_model", "v": 1}
    {"chain": "<chain id>", "kind": "ledger_line", "line": "<as stored>", "v": 1}
    ...

  * the org model: the org model files (finops.org.model.KNOWN_FILES) found
    in the resolved org dir (finops.org.store.resolve_dir), each as its text.
    Nothing else in that dir is read. A file that is a symlink, is not a
    regular file, is over ORG_FILE_MAX bytes or is not UTF-8 is left out and
    named. A repo's nable.org/ that nobody trusted on this machine (`nable
    org trust --here`) is not sent at all: its facts are somebody else's.
  * the guard ledger: each line exactly as stored, newline-terminated ones
    only (guard_ledger.stored_lines). A record is the agent's command and
    the paths the guard judged, as the guard recorded them: credentials were
    redacted when it was written, and nothing else was, so a record can name
    an absolute path, a host and a user name (`ssh deploy@db1.internal`,
    /home/alice/...). Nothing here rewrites a line. The chain id is the
    sha256 of the ledger's first line. The chain is checked on these very
    bytes; a chain that breaks is shown, with where, and left out whole.

  * coding-agent usage (D88): one line per (source, UTC day, developer,
    agent, model, project), for the closed UTC days of the last
    AGENT_USAGE_DAYS, from what this machine's agents logged (ai_budget:
    Claude Code's transcripts, Codex CLI's rollouts, Cursor's Admin API read
    when it is scoped to one person):

    {"agent": "claude_code"|"codex"|"cursor", "basis": "list_price",
     "day": "YYYY-MM-DD", "developer": "<the sender>", "kind": "agent_usage",
     "model": "<as the agent logged it>", "price_source": "<the llm_prices
     row>", "project": "<repo identity>"|"unmapped", "source": "<32 hex>",
     "tokens": {"cache_read", "cache_write", "input", "output"},
     "unpriced_tokens": N, "usd": "<at most 6 places>", "v": 1}

    usd covers tokens of a model llm_prices has a cited row for, at that list
    price, rounded up to the millionth: list-price-equivalent dollars, usage
    and not an invoice. A model with no row is unpriced: its tokens are
    counted in unpriced_tokens and carry no dollars. The project is the repo
    the agent worked in, by its identity (the remote's slug, else the root
    directory's name), only when the org model holds a repo_path fact for
    that repo; anything else is "unmapped". The developer is the sender: the
    person --as names, else, at a terminal, git's user.email, then $USER;
    with neither there is no record. The source is a random 128-bit id made
    once per install (SOURCE_NAME beside the ledger), so a resend replaces
    the box's rows of the same (source, day, developer, agent, model,
    project). No prompt, file content, path, host name or machine id is in
    a record; a model logged as a path is sent as "unknown".

Nothing else is added: no hostname, user name or machine id of nable's own.
What the ledger's records name, they carry, and the dry run counts the
records that name a path, a host or a user and points at the saved file.

A dry run builds the payload, writes those exact bytes to a file (0600, beside
the ledger unless --out names one), and prints the destination host, the file
names, the record counts, the byte size, the payload's sha256 and the confirm
code: the sha256 of the box, a newline and the payload, so a code read for one
box never sends to another. It opens no connection. --out never names one of
the guard's own files (guard_paths), a path in nable's data dir other than the
default, or anything but a regular file of the user's.

A send (--send --confirm CODE) builds the payload again and refuses unless
the code is the one for these bytes and this box: the bytes changed since the
person looked (the guard appends a record with every agent call it judges, so
a dry run goes stale), or the dry run was for another box. It asks the box
what the workspace already holds (GET status), then
POSTs the rest as ordered batches of at most BATCH_MAX bytes, each a slice of
the bytes shown, cut where a line ends. A resumed send skips the org model the
box already holds and the ledger lines up to the head it reports, and nothing
else: coding-agent usage goes whole every time, since the box replaces rows by
their key. A box whose status does not list agent_usage among what it accepts
is told so before anything is sent (--no-agent-usage leaves the records out).

HTTPS only. The box is https://host[:port] with no path, query or user info; a
redirect is refused, not followed (it would carry the key to wherever it
pointed). Air-gap mode (FINOPS_AIRGAP) refuses every request.

The workspace key is a bound analyst key made on the workspace's /workspace
page. It is read from NABLE_WORKSPACE_KEY, from a key file with mode 0600, or
from a prompt with the input hidden, and never from the command line. It goes
in the Authorization header of each request and nowhere else: it is never
stored, printed or logged, and no error message carries it.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

from . import guard_ledger

# The box refuses a request body over this from its Content-Length.
BATCH_MAX = 262_144
# One org model file, as the box caps it.
ORG_FILE_MAX = 131_072
# What one workspace holds of guard ledger lines, as the box caps it (sized
# so the box's storage stays inside its margin model's guard-event line).
LEDGER_LINES_MAX = 50_000
LEDGER_BYTES_MAX = 32 * 1024 * 1024
KEY_ENV = "NABLE_WORKSPACE_KEY"
FORMAT = 1
CONNECT_PATH = "/api/free-tool/connect"
STATUS_PATH = "/api/free-tool/status"
PAYLOAD_NAME = "cloud-connect.payload.jsonl"
# This install's id for its coding-agent usage records, beside the ledger.
SOURCE_NAME = "agent-usage-source"
# How many closed UTC days of coding-agent usage a payload carries.
AGENT_USAGE_DAYS = 35
AGENT_NAMES = {"claude-code": "claude_code", "codex": "codex", "cursor": "cursor"}
_AGENT_SHOWN = {"claude_code": "Claude Code", "codex": "Codex", "cursor": "Cursor"}
UNMAPPED = "unmapped"
_MODEL_MAX = 128
_DEVELOPER_MAX = 254
# A project the box takes: a repo's identity, never a path. One that does not
# fit (a directory name with characters a slug never has) is sent "unmapped".
_PROJECT_RE = re.compile(r"^(?![./~])(?!.*//)[A-Za-z0-9._~@+:/ -]{1,256}$")
TIMEOUT_S = 30.0
_KEY_MAX = 512

# An opener takes a urllib Request and a timeout and answers (status, body).
Opener = Callable[[urllib.request.Request, float], "tuple[int, bytes]"]


class ConnectError(Exception):
    """Something the person has to fix or decide, said in one sentence that
    never carries the key."""


@dataclass
class Payload:
    data: bytes
    sha256: str
    # (kind, start, end) of each payload line in data; end is past its newline.
    lines: list[tuple[str, int, int]] = field(default_factory=list)
    org: dict[str, Any] = field(default_factory=dict)
    ledger: dict[str, Any] = field(default_factory=dict)
    # sha256 of the org model's payload line (no newline), None without one.
    org_sha256: str | None = None
    # sha256 of each ledger line as stored, in order: what a box's head names.
    ledger_hashes: list[str] = field(default_factory=list)
    # What the coding-agent usage records hold, for the dry run.
    usage: dict[str, Any] = field(default_factory=dict)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def confirm_code(payload: Payload, box: str) -> str:
    """What --confirm takes: the sha256 of the box, a newline and the
    payload. It names where the bytes go as well as the bytes."""
    return _sha(f"{box}\n".encode() + payload.data)


# A record that names an absolute path (/home/alice/..., or C:\\Users\\... as
# JSON keeps it), a user at a host (deploy@db1), or a host name of three
# labels or more with a letter in it (db1.internal.acme.example). A count for
# the person, so it leans toward naming too many.
_NAMING = re.compile(
    r"(?<![\w.:/])/[^\s/\"'\\]+/[^\s\"'\\]+"
    r"|\b[A-Za-z]:\\\\"
    r"|\b[\w.-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
    r"|\b(?=[\w.-]*[A-Za-z])[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+){2,}\b")


def _line(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


# ── the org model ────────────────────────────────────────────────────────────

def _read_org_file(path: Path) -> tuple[str | None, str | None]:
    """(text, None) or (None, why it was left out). Never follows a link."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"not read ({errno.errorcode.get(exc.errno or 0, 'os error')})"
    if stat.S_ISLNK(st.st_mode):
        return None, "a symlink; only a regular file in the org dir is sent"
    if not stat.S_ISREG(st.st_mode):
        return None, "not a regular file"
    if st.st_size > ORG_FILE_MAX:
        return None, f"{st.st_size:,} bytes, over the {ORG_FILE_MAX:,} a workspace takes"
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return None, "not a regular file"
            raw = fh.read(ORG_FILE_MAX + 1)
    except OSError as exc:
        return None, f"not read ({errno.errorcode.get(exc.errno or 0, 'os error')})"
    if len(raw) > ORG_FILE_MAX:
        return None, f"over the {ORG_FILE_MAX:,} bytes a workspace takes"
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "not UTF-8 text"


def _org(org_dir: str | os.PathLike | None) -> dict[str, Any]:
    from .org.model import KNOWN_FILES
    from .org.store import git_root, is_trusted, resolve_dir

    d, rule = resolve_dir(org_dir)
    out: dict[str, Any] = {"dir": str(d), "rule": rule, "files": {}, "not_read": {},
                           "texts": {}, "left_out": None}
    if rule == "repo" and not is_trusted(git_root(d.parent)):
        out["left_out"] = ("this repo's nable.org/ is not trusted on this machine, so its "
                           "facts are not sent (`nable org trust --here` trusts it; "
                           "FINOPS_ORG_DIR or --org-dir names another dir)")
        return out
    if not d.is_dir():
        return out
    for name in KNOWN_FILES:
        text, why = _read_org_file(d / name)
        if why:
            out["not_read"][name] = why
        elif text is not None:
            out["texts"][name] = text
            out["files"][name] = len(text.encode("utf-8"))
    return out


# ── the ledger ───────────────────────────────────────────────────────────────

def _ledger(path: Path | None) -> dict[str, Any]:
    path = path or guard_ledger.ledger_path()
    out: dict[str, Any] = {"path": str(path), "records": 0, "ok": True, "chain_id": None,
                           "head": guard_ledger.GENESIS, "partial": False, "lines": [],
                           "left_out": None}
    try:
        size = path.stat().st_size if path.exists() else 0
    except OSError:
        size = 0
    if size > LEDGER_BYTES_MAX * 2:
        # Not read at all: far past what any workspace takes.
        out["left_out"] = (f"the ledger is {size:,} bytes, over the {LEDGER_BYTES_MAX:,} "
                           "bytes a workspace holds, so it is not sent")
        out["ok"] = None
        return out
    try:
        got = guard_ledger.stored_lines(path)
    except OSError as exc:
        out.update(ok=None, left_out=f"the ledger was not read ({exc.strerror or exc})")
        return out
    lines, chain = got["lines"], got["chain"]
    out.update(records=len(lines), ok=chain["ok"], head=chain["head"],
               partial=got["partial"], lines=lines,
               records_naming=sum(1 for x in lines
                                  if _NAMING.search(x.decode("utf-8", "replace"))))
    if lines:
        out["chain_id"] = _sha(lines[0])
    if not chain["ok"]:
        out.update(broken_at=chain["broken_at"], problem=chain["problem"],
                   left_out=(f"the chain breaks at record {chain['broken_at']} "
                             f"({chain['problem']}), so the ledger is not sent; "
                             "`nable guard verify-log` shows the break"))
    elif len(lines) > LEDGER_LINES_MAX:
        out["left_out"] = (f"{len(lines):,} records, over the {LEDGER_LINES_MAX:,} a "
                           "workspace holds, so the ledger is not sent")
    elif sum(len(x) for x in lines) > LEDGER_BYTES_MAX:
        out["left_out"] = (f"{sum(len(x) for x in lines):,} bytes of records, over the "
                           f"{LEDGER_BYTES_MAX:,} bytes a workspace holds, so the ledger "
                           "is not sent")
    return out


# ── the payload ──────────────────────────────────────────────────────────────

def build(*, org_dir: str | os.PathLike | None = None,
          ledger_path: Path | None = None, developer: str | None = None,
          developer_from: str = "", agent_usage: bool = True,
          now: datetime | None = None, left_out: str | None = None) -> Payload:
    """The payload, from what is on this machine now. Reads files only (and
    makes this install's source id, the first time agent usage is built).

    `developer` is the sender the agent usage is sent as; None leaves it
    out, said with `left_out` (or a default reason). `now` fixes "today"."""
    org = _org(org_dir)
    led = _ledger(ledger_path)
    parts: list[tuple[str, bytes]] = []
    org_sha = None
    if org["texts"] and not org["left_out"]:
        line = _line({"files": org["texts"], "kind": "org_model", "v": FORMAT})
        if len(line) + 1 > BATCH_MAX:
            org["left_out"] = (f"the org model is {len(line):,} bytes as one line, over the "
                               f"{BATCH_MAX:,} one request takes")
        else:
            parts.append(("org_model", line))
            org_sha = _sha(line)
    if not agent_usage:
        usage = _usage_left_out(left_out or "--no-agent-usage was given")
    elif not (developer or "").strip():
        usage = _usage_left_out(left_out or "no developer is named: run this at your own "
                                             "terminal, or pass --as WHO")
    elif any(ord(c) < 0x20 or ord(c) == 0x7f for c in developer):
        usage = _usage_left_out("the developer's name holds a control character; pass --as "
                                "WHO with a plain name or email")
    else:
        records, usage = agent_usage_records(developer=developer.strip(),
                                             developer_from=developer_from,
                                             org_dir=org_dir, now=now)
        for rec in records:
            line = _line(rec)
            if len(line) + 1 > BATCH_MAX:      # bounded strings: never, but never sent whole
                usage = _usage_left_out("a record is over one request's size")
                parts = [x for x in parts if x[0] != "agent_usage"]
                break
            parts.append(("agent_usage", line))
    hashes: list[str] = []
    if led["lines"] and not led["left_out"]:
        wrapped = []
        for n, raw in enumerate(led["lines"], start=1):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                led["left_out"] = f"record {n} is not UTF-8, so the ledger is not sent"
                wrapped = []
                break
            line = _line({"chain": led["chain_id"], "kind": "ledger_line", "line": text,
                          "v": FORMAT})
            if len(line) + 1 > BATCH_MAX:
                led["left_out"] = (f"record {n} is {len(line):,} bytes as a payload line, "
                                   f"over the {BATCH_MAX:,} one request takes")
                wrapped = []
                break
            wrapped.append(("ledger_line", line))
        if wrapped:
            parts += wrapped
            hashes = [_sha(x) for x in led["lines"]]
    data = bytearray()
    spans = []
    for kind, line in parts:
        start = len(data)
        data += line + b"\n"
        spans.append((kind, start, len(data)))
    data_b = bytes(data)
    org.pop("texts", None)
    led.pop("lines", None)
    return Payload(data=data_b, sha256=_sha(data_b), lines=spans, org=org, ledger=led,
                   org_sha256=org_sha, ledger_hashes=hashes, usage=usage)


# ── coding-agent usage ───────────────────────────────────────────────────────

def _usage_left_out(why: str) -> dict[str, Any]:
    return {"records": 0, "left_out": why, "basis": "list_price"}


def source_id() -> str:
    """This install's id for its agent usage: 32 random hex digits, made
    once and kept (0600) beside the ledger. OSError when it cannot be kept:
    an id made anew each time would turn every resend into new rows."""
    path = guard_ledger.ledger_path().parent / SOURCE_NAME
    try:
        got = path.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-f]{32}", got):
            return got
    except FileNotFoundError:
        pass
    new = secrets.token_hex(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return new


def _mapped_repos(org_dir: str | os.PathLike | None) -> set[str]:
    """The repos the org model names in a repo_path fact (a live one, from a
    directory this machine trusts): the only projects a record names."""
    from .org.model import split_repo_path
    from .org.store import load
    out: set[str] = set()
    for f in load(org_dir).facts:
        if f.subject.kind != "repo_path" or not f.live or not f.trusted:
            continue
        repo = split_repo_path(f.subject.id)[0] or f.anchor
        if repo:
            out.add(repo.lower())
    return out


def _model_shown(raw: Any) -> str:
    """The model as the agent logged it, unless that could carry a path or
    is not one printable line: then "unknown"."""
    m = str(raw or "unknown").strip()
    if (not m or len(m) > _MODEL_MAX or m.startswith(("/", "~", "."))
            or "\\" in m or "/home/" in m or "/Users/" in m
            or any(ord(c) < 0x21 or ord(c) > 0x7e for c in m)):
        return "unknown"
    return m


def _usd_text(x: Decimal) -> str:
    """At most 6 places, rounded up (a cost is never shown smaller)."""
    q = x.quantize(Decimal("0.000001"), rounding=ROUND_CEILING)
    text = format(q, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def agent_usage_records(*, developer: str, developer_from: str = "",
                        org_dir: str | os.PathLike | None = None,
                        now: datetime | None = None) -> tuple[list[dict[str, Any]],
                                                              dict[str, Any]]:
    """(records in key order, what they hold), from this machine's agents
    for the closed UTC days of the last AGENT_USAGE_DAYS. A reader that
    fails leaves the usage out, said as not read, never as zero."""
    from . import ai_budget, llm_prices
    from .org.store import git_root, repo_identity

    now = (now or datetime.now(UTC)).astimezone(UTC)
    today = now.date()
    first = today - timedelta(days=AGENT_USAGE_DAYS)
    since = datetime(first.year, first.month, first.day, tzinfo=UTC).timestamp()
    until = datetime(today.year, today.month, today.day, tzinfo=UTC).timestamp()
    who = developer.strip()[:_DEVELOPER_MAX]
    try:
        source = source_id()
    except OSError as exc:
        return [], _usage_left_out(f"this install's id could not be kept beside the ledger "
                                   f"({exc.strerror or type(exc).__name__})")
    try:
        responses, notes = ai_budget.agent_responses(since, allow_network=False)
        mapped = _mapped_repos(org_dir)
    except Exception as exc:  # noqa: BLE001 - not read is said, never a zero
        return [], _usage_left_out(f"not read ({type(exc).__name__}: {str(exc)[:120]})")
    projects: dict[str, str] = {}

    def project_of(cwd: Any) -> str:
        if not isinstance(cwd, str) or not cwd:
            return UNMAPPED
        if cwd not in projects:
            name = UNMAPPED
            try:
                root = git_root(cwd) if os.path.isdir(cwd) else None
                if root is not None:
                    ident = repo_identity(root)
                    name = (ident if ident and ident.lower() in mapped
                            and _PROJECT_RE.match(ident) else UNMAPPED)
            except OSError:
                name = UNMAPPED
            projects[cwd] = name
        return projects[cwd]

    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for r in responses:
        ts = r.get("ts")
        if not isinstance(ts, (int, float)) or not since <= ts < until:
            continue
        day = datetime.fromtimestamp(ts, UTC).date().isoformat()
        agent = AGENT_NAMES.get(str(r.get("harness") or "claude-code"))
        if agent is None:
            continue
        model = _model_shown(r.get("model"))
        key = (day, agent, model, project_of(r.get("cwd")))
        g = groups.setdefault(key, {"tokens": {"cache_read": 0, "cache_write": 0, "input": 0,
                                               "output": 0},
                                    "usd": Decimal(0), "unpriced": 0, "price": None})
        t = g["tokens"]
        t["input"] += int(r["input"])
        t["output"] += int(r["output"])
        t["cache_write"] += int(r["cache_write"])
        t["cache_read"] += int(r["cache_read"])
        price = llm_prices.price_for(str(r.get("model") or ""))
        if price is None or model == "unknown":
            g["unpriced"] += (int(r["input"]) + int(r["output"]) + int(r["cache_write"])
                              + int(r["cache_read"]))
            continue
        g["price"] = price
        g["usd"] += price.cost_exact(
            input_tokens=int(r["input"]), output_tokens=int(r["output"]),
            cache_write_5m_tokens=int(r["cache_write"]) - int(r["cache_write_1h"]),
            cache_write_1h_tokens=int(r["cache_write_1h"]),
            cache_read_tokens=int(r["cache_read"]), fast=bool(r.get("fast")),
            us_only=bool(r.get("us_only")))
    records: list[dict[str, Any]] = []
    for (day, agent, model, project), g in sorted(groups.items()):
        price = g["price"]
        records.append({
            "agent": agent, "basis": "list_price", "day": day, "developer": who,
            "kind": "agent_usage", "model": model,
            "price_source": (price.citation() if price is not None
                             else "none: no llm_prices row for this model, so its tokens "
                                  "are counted and not priced"),
            "project": project, "source": source, "tokens": dict(g["tokens"]),
            "unpriced_tokens": int(g["unpriced"]), "usd": _usd_text(g["usd"]), "v": FORMAT})
    total = sum((Decimal(x["usd"]) for x in records), Decimal(0))
    tokens = {k: sum(x["tokens"][k] for x in records)
              for k in ("input", "output", "cache_write", "cache_read")}
    days = sorted({x["day"] for x in records})
    summary = {
        "records": len(records), "left_out": None, "basis": "list_price",
        "developer": who, "developer_from": developer_from, "source": source,
        "days": ({"first": days[0], "last": days[-1], "count": len(days)} if days else None),
        "window": {"first": first.isoformat(),
                   "last": (today - timedelta(days=1)).isoformat()},
        "agents": sorted({x["agent"] for x in records}),
        "models": sorted({x["model"] for x in records}),
        "projects": sorted({x["project"] for x in records if x["project"] != UNMAPPED}),
        "unmapped_records": sum(1 for x in records if x["project"] == UNMAPPED),
        "tokens": tokens, "unpriced_tokens": sum(x["unpriced_tokens"] for x in records),
        "unpriced_models": sorted({x["model"] for x in records if x["unpriced_tokens"]}),
        "usd": _usd_text(total),
        "price_sources": sorted({x["price_source"] for x in records
                                 if not x["price_source"].startswith("none:")}),
        "notes": list(notes),
    }
    return records, summary


def default_out() -> Path:
    """Beside the ledger, in the nable data dir."""
    return guard_ledger.ledger_path().parent / PAYLOAD_NAME


def _check_out(path: Path) -> None:
    """ConnectError unless `path` is somewhere a payload may be written: not
    one of the guard's own files (the off switch, the ledger, the org model,
    harness settings: guard_paths), not in nable's data dir, and not there
    already as anything but a regular file the user owns. A dry run needs no
    key, so --out is the one thing an agent could aim at the guard."""
    from . import guard_paths, guard_plugin

    hit = guard_paths.match_file(str(path))
    if hit is None:
        # The name itself, its directory resolved: a link there names the
        # link's own place, not where it points.
        hit = guard_paths.match_file(str(Path(os.path.realpath(path.parent)) / path.name))
    if hit is not None:
        raise ConnectError(f"--out names {hit.shown}, {hit.what}; the guard's own files are "
                           "never written by a dry run. Leave --out off to save beside "
                           "the ledger, or name a file of your own.")
    real = os.path.realpath(path)
    for d in dict.fromkeys((guard_paths.data_dir(), guard_plugin.data_root(),
                            guard_ledger.ledger_path().parent)):
        if guard_paths.is_under(real, os.path.realpath(d)):
            raise ConnectError(f"--out is in nable's data directory ({d}); leave --out off "
                               "to save beside the ledger, or name a file outside it")
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ConnectError(f"--out could not be checked "
                           f"({errno.errorcode.get(exc.errno or 0, 'os error')})") from None
    if not stat.S_ISREG(st.st_mode):
        raise ConnectError("--out names something that is not a regular file (a link, a "
                           "directory or a device), which is never replaced")
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        raise ConnectError("--out names a file that belongs to another user")


def save(payload: Payload, path: Path | None = None) -> Path:
    """Write the exact bytes to `path` (default_out()), 0600, through a temp
    file swapped in, so an existing file is replaced, never written through.
    A path other than the default is checked first (_check_out)."""
    if path is not None and Path(path) != default_out():
        _check_out(Path(path).expanduser())
    path = Path(path).expanduser() if path else default_out()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload.data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def _batches(payload: Payload, ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Each range cut into slices of at most BATCH_MAX bytes, at line ends."""
    out: list[tuple[int, int]] = []
    for a, b in ranges:
        start = a
        last = a
        for _kind, s, e in payload.lines:
            if s < a or e > b:
                continue
            if e - start > BATCH_MAX:
                out.append((start, last))
                start = last
            last = e
        if last > start:
            out.append((start, last))
    return out


def plan(payload: Payload, held: Mapping[str, Any] | None) -> list[tuple[int, int]]:
    """The byte ranges of `payload` still to send, as batches, given what the
    box reports it holds (its status answer, or None for nothing)."""
    held = held or {}
    om = held.get("org_model")
    latest = om.get("latest") if isinstance(om, dict) else None
    held_org = latest.get("sha256") if isinstance(latest, dict) else None
    ranges: list[tuple[int, int]] = []
    org = [x for x in payload.lines if x[0] == "org_model"]
    if org and held_org != payload.org_sha256:
        ranges.append((org[0][1], org[0][2]))
    usage = [x for x in payload.lines if x[0] == "agent_usage"]
    if usage:
        # Replaced by key on the box, so sent whole every time.
        ranges.append((usage[0][1], usage[-1][2]))
    ledger = [x for x in payload.lines if x[0] == "ledger_line"]
    if ledger:
        start = 0
        for c in held.get("chains") or []:
            if isinstance(c, dict) and c.get("chain_id") == payload.ledger.get("chain_id"):
                head = c.get("head")
                if head in payload.ledger_hashes:
                    start = payload.ledger_hashes.index(head) + 1
        if start < len(ledger):
            ranges.append((ledger[start][1], ledger[-1][2]))
    merged: list[tuple[int, int]] = []
    for a, b in ranges:
        if merged and merged[-1][1] == a:
            merged[-1] = (merged[-1][0], b)       # adjacent: one run of bytes
        else:
            merged.append((a, b))
    return _batches(payload, merged)


# ── the box ──────────────────────────────────────────────────────────────────

def parse_box(url: str) -> str:
    """https://host[:port], lower-cased, or ConnectError."""
    raw = str(url or "").strip()
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme.lower() != "https":
        raise ConnectError(f"--box must be an https:// address (got {parts.scheme or 'none'}"
                           "): the key and the payload never travel over plain HTTP")
    if parts.username or parts.password or "@" in parts.netloc:
        raise ConnectError("--box must not carry a user name or password")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ConnectError("--box is the box's address alone, https://host[:port], "
                           "with no path or query")
    host = (parts.hostname or "").lower()
    if not host:
        raise ConnectError("--box names no host")
    try:
        port = parts.port
    except ValueError as exc:
        raise ConnectError("--box has a port that is not a number") from exc
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}" + (f":{port}" if port else "")


def host_of(box: str) -> str:
    return urllib.parse.urlsplit(box).netloc


def _airgap() -> bool:
    """FINOPS_AIRGAP, read when asked, by the one rule every reader of it
    keeps (telemetry.env_flag_set: anything but 0, false, no or off)."""
    from .telemetry import env_flag_set
    return env_flag_set("FINOPS_AIRGAP")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the Authorization header to wherever it
    points, http:// included. It is refused, never followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ConnectError(f"the box answered {code} with a redirect, which is not followed; "
                           "check the --box address")


def default_opener() -> urllib.request.OpenerDirector:
    """urllib with HTTPS alone and no redirects, verifying certificates
    against certifi's bundle where it is installed."""
    import ssl
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - the platform's store, then
        ctx = ssl.create_default_context()
    od = urllib.request.OpenerDirector()
    for h in (urllib.request.ProxyHandler(), urllib.request.HTTPSHandler(context=ctx),
              urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor(),
              _NoRedirect()):
        od.add_handler(h)
    return od


def _urllib_opener(req: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    try:
        # The opener has an HTTPS handler and no other scheme's.
        with default_opener().open(req, timeout=timeout) as resp:  # nosec B310
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read() or b""
    except urllib.error.URLError as exc:
        raise ConnectError(f"the box could not be reached ({type(exc.reason).__name__})"
                           ) from None


def _request(opener: Opener | None, method: str, url: str, key: str,
             body: bytes | None = None) -> tuple[int, Any]:
    if not url.startswith("https://"):
        raise ConnectError("only HTTPS: the key and the payload never travel over plain HTTP")
    if _airgap():
        raise ConnectError("FINOPS_AIRGAP is set: air-gap allows traffic to your cost "
                           "providers only, so nothing is sent to a workspace")
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json",
               "User-Agent": "nable-cloud-connect"}
    if body is not None:
        headers["Content-Type"] = "application/x-ndjson"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        status, raw = (opener or _urllib_opener)(req, TIMEOUT_S)
    except ConnectError:
        raise
    except Exception as exc:  # noqa: BLE001 - said without the request in it
        raise ConnectError(f"the request to the box failed ({type(exc).__name__})") from None
    try:
        doc = json.loads(raw or b"{}")
    except ValueError:
        doc = {}
    return status, doc if isinstance(doc, dict) else {}


def _refused(what: str, status: int, doc: Mapping[str, Any]) -> ConnectError:
    err = str(doc.get("error") or "no reason given")[:80]
    msg = str(doc.get("message") or "")[:300]
    hint = (" Use an analyst key bound to the workspace (made on its /workspace page)."
            if status in (401, 403) else "")
    return ConnectError(f"the box refused {what} ({status}: {err}"
                        + (f", {msg}" if msg else "") + f").{hint}")


def status(box: str, key: str, *, opener: Opener | None = None) -> dict[str, Any]:
    """What the workspace holds, as the box reports it."""
    code, doc = _request(opener, "GET", box + STATUS_PATH, key)
    if code != 200:
        raise _refused("the status request", code, doc)
    return doc


def send(payload: Payload, box: str, key: str, *, opener: Opener | None = None,
         say: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Send what the box does not hold yet, as slices of payload.data.
    Stops at the first batch the box refuses (ConnectError, saying which)."""
    if not box.startswith("https://"):
        raise ConnectError("only HTTPS: the key and the payload never travel over plain HTTP")
    held = status(box, key, opener=opener)
    accepts = held.get("accepts")
    if any(kind == "agent_usage" for kind, _a, _b in payload.lines) and not (
            isinstance(accepts, list) and "agent_usage" in accepts):
        raise ConnectError("the box does not say it takes coding-agent usage records, so "
                           "nothing was sent. Run the dry run again with --no-agent-usage "
                           "to send the org model and the ledger without them.")
    batches = plan(payload, held)
    sent = 0
    for n, (a, b) in enumerate(batches, start=1):
        code, doc = _request(opener, "POST", box + CONNECT_PATH, key, payload.data[a:b])
        if code != 200:
            why = _refused(f"batch {n} of {len(batches)}", code, doc)
            raise ConnectError(f"{why} {sent:,} bytes in {n - 1} batch(es) were accepted "
                               "before it; nothing after it was sent.")
        sent += b - a
        if say:
            say(f"  batch {n} of {len(batches)}: {b - a:,} bytes accepted")
    return {"batches": len(batches), "bytes_sent": sent,
            "bytes_already_held": len(payload.data) - sent,
            "status": status(box, key, opener=opener) if batches else held}


# ── the key ──────────────────────────────────────────────────────────────────

def _clean_key(raw: str, where: str) -> str:
    key = str(raw or "").strip()
    if not key:
        raise ConnectError(f"the workspace key from {where} is empty")
    if len(key) > _KEY_MAX or any(ord(c) < 0x21 or ord(c) > 0x7e for c in key):
        raise ConnectError(f"the workspace key from {where} is not one line of printable "
                           "characters")
    return key


def read_key(*, key_file: str | None = None, environ: Mapping[str, str] | None = None,
             prompt: Callable[[str], str] | None = None) -> str:
    """The workspace key: --key-file (a regular file, mode 0600, owned by
    whoever runs this), else NABLE_WORKSPACE_KEY, else `prompt` (hidden
    input; None where there is no terminal). Never argv, never stored."""
    env = os.environ if environ is None else environ
    if key_file:
        try:
            fd = os.open(key_file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0))
        except OSError as exc:
            raise ConnectError(f"--key-file could not be opened "
                               f"({errno.errorcode.get(exc.errno or 0, 'os error')}; "
                               "a symlink is not followed)") from None
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise ConnectError("--key-file is not a regular file")
            if st.st_mode & 0o077:
                raise ConnectError(f"--key-file has mode {stat.S_IMODE(st.st_mode):04o}; "
                                   "a key file must be 0600 (chmod 600 it)")
            if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
                raise ConnectError("--key-file belongs to another user")
            raw = fh.read(_KEY_MAX + 2).decode("utf-8", "replace")
        return _clean_key(raw, "--key-file")
    if env.get(KEY_ENV, "") != "":
        return _clean_key(env[KEY_ENV], KEY_ENV)
    if prompt is None:
        raise ConnectError(f"no workspace key: set {KEY_ENV}, pass --key-file with a 0600 "
                           "file, or run this at a terminal to be asked")
    return _clean_key(prompt("Workspace key (input hidden): "), "the prompt")


# ── what a dry run says ──────────────────────────────────────────────────────

def summary(payload: Payload, box: str, out: Path | None) -> dict[str, Any]:
    """The dry run as one document."""
    code = confirm_code(payload, box) if payload.data else None
    return {"dry_run": True, "sent": False, "destination": {"box": box,
                                                            "host": host_of(box)},
            "org_model": payload.org, "ledger": payload.ledger,
            "agent_usage": payload.usage,
            "payload": {"bytes": len(payload.data), "lines": len(payload.lines),
                        "sha256": payload.sha256,
                        "batches": len(plan(payload, None)),
                        "batch_max_bytes": BATCH_MAX,
                        "file": str(out) if out else None},
            "confirm_sha256": code,
            "confirm": (f"nable cloud connect --box {box} --send --confirm {code}"
                        if code else None)}


def describe(payload: Payload, box: str, out: Path | None) -> str:
    """The dry run as a person reads it."""
    o, led = payload.org, payload.ledger
    lines = ["nable cloud connect: a dry run. Nothing was sent.", "",
             f"Destination   {box}  (host {host_of(box)})", ""]
    lines.append(f"Org model     {o.get('dir')}  (chosen by {o.get('rule')})")
    if o.get("left_out"):
        lines.append(f"  left out: {o['left_out']}")
    elif not o.get("files"):
        lines.append("  no org model files there")
    for name, size in sorted((o.get("files") or {}).items()):
        lines.append(f"  {name:<20} {size:>9,} bytes")
    for name, why in sorted((o.get("not_read") or {}).items()):
        lines.append(f"  {name:<20} left out: {why}")
    lines += ["", f"Guard ledger  {led.get('path')}"]
    n = led.get("records") or 0
    if led.get("ok") is None:
        lines.append(f"  left out: {led.get('left_out')}")
    elif not n:
        lines.append("  no records")
    else:
        chain = ("chain verified" if led.get("ok") else
                 f"chain broken at record {led.get('broken_at')}")
        lines.append(f"  {n:,} records, {chain}, chain id {str(led.get('chain_id'))[:16]}")
        if led.get("left_out"):
            lines.append(f"  left out: {led['left_out']}")
        else:
            naming = led.get("records_naming") or 0
            lines.append("  sent as stored: the agent's commands and the paths the guard "
                         "judged, with credentials redacted and nothing else changed")
            if naming:
                lines.append(f"  {naming:,} of them name a path, a host or a user; read them in "
                             "the saved file before you send")
    if led.get("partial"):
        lines.append("  the last record was still being written; it waits for the next connect")
    lines += ["", *_describe_usage(payload.usage), ""]
    if not payload.data:
        lines += ["Nothing to send: no org model, no ledger records and no coding-agent "
                  "usage here."]
        return "\n".join(lines)
    count = len(plan(payload, None))
    n_lines = len(payload.lines)
    lines += [(f"Payload       {n_lines:,} line{'s' if n_lines != 1 else ''}, "
               f"{len(payload.data):,} bytes, "
               f"{count} batch{'es' if count != 1 else ''} of at most {BATCH_MAX:,} bytes"),
              f"  sha256 {payload.sha256}"]
    if out:
        lines.append(f"  saved to {out}: exactly the bytes that would leave")
    lines += ["", f"To send exactly these bytes to {host_of(box)}, at your own terminal:",
              f"  nable cloud connect --box {box} --send --confirm {confirm_code(payload, box)}",
              ("The confirm code is the sha256 of the box, a newline and these bytes, so it "
               f"sends to {host_of(box)} alone. The workspace key is read from {KEY_ENV}, a "
               "--key-file with mode 0600, or a hidden prompt, never from the command line. "
               "A resend skips what the workspace already holds.")]
    return "\n".join(lines)


def _describe_usage(u: Mapping[str, Any]) -> list[str]:
    """The coding-agent usage part of the dry run."""
    n = u.get("records") or 0
    if u.get("left_out"):
        return ["Coding agents", f"  left out: {u['left_out']}"]
    w = u.get("window") or {}
    if not n:
        return ["Coding agents", f"  no usage on closed UTC days {w.get('first')} to "
                                 f"{w.get('last')}"]
    d = u.get("days") or {}
    t = u.get("tokens") or {}
    who = u.get("developer")
    out = [f"Coding agents {n:,} record{'s' if n != 1 else ''}, {d.get('first')} to "
           f"{d.get('last')} ({d.get('count')} UTC day{'s' if d.get('count') != 1 else ''}), "
           f"sent as developer {who}" + (f" ({u['developer_from']})"
                                         if u.get("developer_from") else ""),
           "  agents     " + ", ".join(_AGENT_SHOWN.get(a, a) for a in u.get("agents") or []),
           "  models     " + ", ".join(u.get("models") or []),
           "  projects   " + (", ".join(u.get("projects") or []) or "none named")
           + (f"; {u['unmapped_records']:,} record(s) unmapped (no repo_path fact in the org "
              "model names their repo)" if u.get("unmapped_records") else ""),
           (f"  tokens     input {t.get('input', 0):,}, output {t.get('output', 0):,}, "
            f"cache write {t.get('cache_write', 0):,}, cache read {t.get('cache_read', 0):,}"),
           (f"  USD        ${u.get('usd')} list-price equivalent: what the tokens cost at the "
            "published rate, usage and not an invoice, never added to billed dollars"),
           "  priced     " + ("; ".join(u.get("price_sources") or []) or "nothing priced")]
    if u.get("unpriced_tokens"):
        out.append(f"  unpriced   {u['unpriced_tokens']:,} tokens from "
                   f"{', '.join(u.get('unpriced_models') or [])}: counted, not priced "
                   "(llm_prices has no cited rate)")
    for note in u.get("notes") or []:
        out.append(f"  note       {note}")
    out.append("  each record is one UTC day, developer, agent, model and project: no prompt, "
               "file content, path, host name or machine id")
    return out
