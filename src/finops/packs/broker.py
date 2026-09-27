# SPDX-License-Identifier: Apache-2.0
"""The broker: run a code pack's entry point out of process, with only what
its manifest declares.

Connectors, org-context adapters and action sinks are the only packs that
carry code. The core never imports that code. For each call it:

  1. checks the installed pack as the runtime does (files match what was
     approved, today's org policy allows it, its signature holds), and refuses
     code from a pack that is not signed by the first-party key or an
     org-trusted key unless the org allowlists it (`packs.allow_unsigned_code`,
     as `id@<content digest>`: an id alone names whatever a namespace claim
     says, a digest names these files; a bare id is honoured only under
     `packs.allowed_sources`, which pins where the pack came from);
  2. copies the verified files into a private temporary directory and runs
     the pack from there, so a file swapped in the packs root after the check
     is not the file that runs; starts `python -I -B` running
     finops.packs.host in a fresh process group, in a throwaway HOME, with a
     scrubbed environment: PATH, HOME, LANG, and the values of the secrets the
     manifest declares, read only from the pack's own vault namespace
     (`pack:<namespace>/<name>:<NAME>`, set with `nable pack secret set`).
     Never from nable's environment or its provider keys: no FINOPS_*, no
     AWS_*, no cloud credentials, no vault key cross;
  3. speaks JSON-RPC 2.0 over the child's stdin/stdout, one object per line
     (host.py lists the methods), with a per-call timeout that holds even when
     the child stops reading (writes go through a bounded queue and a writer
     thread; past the deadline the child is killed), at most
     MAX_INFLIGHT of the child's requests unanswered at once, and a cap on
     everything the child writes to stdout; the child's stderr is kept,
     truncated and with declared secret values redacted, in
     <packs root>/logs/<namespace>/<name>.log;
  4. answers the child's data.read requests only for scopes the manifest
     declares in read_data (focus.cost from the cost store, org.owners and
     org.environments from finops.org, repo.files from the repositories an
     adapter call names, read here and handed over as text; the others
     return "not available");
  5. validates what comes back: FOCUS rows against finops.focus's schema
     (invalid rows are dropped and reported), org facts through
     finops.org.make_fact (always status proposed, source prefixed with the
     pack id), and sink deliveries against the declared `act` kinds and
     `max_autonomy`, checked before the child is even started.

Network, honestly. On Linux, a pack that declares no network runs in its own
empty network namespace (`unshare --user --net`) when the kernel allows
unprivileged user namespaces; it then has no route anywhere. Everywhere else,
and for any pack that declares hosts, egress is audited rather than isolated:
the host process installs an audit hook that refuses connections and lookups
for undeclared hosts and reports every connection it sees. An audit hook runs
inside the process it watches, so a determined pack can get around it; that
is why unsigned code does not run. The filesystem is not sandboxed on a
laptop: a pack can read what your user can read. Treat this like the guard:
a seatbelt, not a security boundary. Container network policy in the hosted
and self-hosted runners is where egress is actually enforced.

read_cloud is declared and shown at install, and not brokered yet: the core
hands a pack no cloud credentials. A connector that needs an API key declares
it in `secrets`.
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import math
import os
import queue
import re
import shutil
import signal
import stat

# The broker's job is to run pack code in a child process, never in this one.
import subprocess  # nosec B404
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import capabilities as caps_mod
from . import signing, store
from .errors import BrokerError, PackError, PolicyRefusal, Problem

log = logging.getLogger("finops.packs")

DEFAULT_TIMEOUT_S = 120.0
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_LOG_BYTES = 1024 * 1024
MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_DATA_ROWS = 5000
MAX_PROBLEMS = 20
# Requests from the child (data.read) whose answers the core has not yet
# written to it. A pack that reads its answers never has more than one; one
# that sends requests and stops reading fills the pipe, and past this many the
# call fails.
MAX_INFLIGHT = 64
# Messages queued for the child's stdin before the writer thread must drain
# them: room for every answer in flight and the core's own request. When it
# fills anyway, the call waits no longer than its deadline.
_WRITE_QUEUE = MAX_INFLIGHT + 4
# "auto": an empty network namespace for packs that declare no hosts, when
# the platform allows one; "off": always audit mode (tests, or a kernel whose
# user namespaces misbehave).
NETNS = "auto"

METHOD_KIND = {"connector.fetch_costs": "connectors", "adapter.propose": "adapters",
               "sink.deliver": "sinks"}
# The autonomy a delivery of each act kind implies when the payload does not say.
ACT_LEVEL = {"pr": "L2", "ticket": "L2", "execute": "L3"}

_BOOTSTRAP = ("import sys; sys.path.insert(0, sys.argv[1]); "
              "from finops.packs.host import main; raise SystemExit(main(sys.argv[2:]))")


# ── which pack, and may its code run ─────────────────────────────────────────

@dataclass(frozen=True)
class Prepared:
    pack_id: str
    namespace: str
    name: str
    version: str
    kind: str
    entry_id: str
    entry: str
    root: Path
    capabilities: dict[str, Any]
    signature: signing.Verdict
    allowlisted: bool
    tier: str = ""
    digest: str = ""
    files: dict[str, str] = field(default_factory=dict)


def prepare(pack_id: str, entry_id: str, kind: str | None = None, *,
            pp: dict[str, Any] | None = None) -> Prepared:
    """Everything the broker checks before it starts a process. Raises
    PackError (PolicyRefusal when the pack may not run)."""
    from ..policy import pack_policy
    from .install import check_installed
    from .manifest import load_manifest
    pp = pack_policy() if pp is None else pp
    idx = store.read_index()
    e = idx["packs"].get(pack_id)
    if e is None:
        raise PackError(f"{pack_id} is not installed (`nable pack list` shows what is)")
    root, verdict, why = check_installed(pack_id, e, pp)
    if why:
        raise PolicyRefusal(f"{pack_id} cannot run", [Problem("pack", w) for w in why])
    # check_installed hashed the files against the index; the manifest read
    # here is one of those files, so its capabilities and tier are the ones
    # approved (install.check_installed also refuses an index whose recorded
    # capabilities or tier differ from the manifest's).
    manifest = load_manifest(root)
    if manifest.id != pack_id:
        raise PolicyRefusal(f"{pack_id} cannot run: its manifest says it is {manifest.id}")
    matches = [c for c in manifest.code if c.id == entry_id and (kind is None or c.kind == kind)]
    if not matches:
        have = ", ".join(f"{c.kind[:-1]} {c.id}" for c in manifest.code) or "no code"
        what = kind[:-1] if kind else "code entry"
        raise PackError(f"{pack_id} has no {what} {entry_id!r} (it has {have})")
    code = matches[0]
    # Of the files check_installed just hashed, not the index's recorded digest.
    digest = store.content_digest(e.get("files") or {})
    allowlisted = allowlisted_unsigned(pack_id, digest, pp)
    if not signing.trusted(verdict) and not allowlisted:
        bare = pack_id in (pp.get("allow_unsigned_code") or [])
        raise PolicyRefusal(
            f"{pack_id} carries code and is not signed by a key this org trusts "
            f"({verdict.reason}). Unsigned code does not run: sign it with a key in "
            "packs.trusted_keys, or have an admin allowlist these exact files in the org "
            f"policy (packs.allow_unsigned_code: [{pack_id}@{digest}])"
            + (". The bare id it lists now is honoured only when packs.allowed_sources "
               "pins where packs may come from" if bare else ""))
    return Prepared(pack_id, manifest.namespace, manifest.name, manifest.version, code.kind,
                    code.id, code.entry, root, dict(manifest.capabilities), verdict, allowlisted,
                    manifest.tier, digest, dict(e.get("files") or {}))


def allowlisted_unsigned(pack_id: str, digest: str, pp: dict[str, Any]) -> bool:
    """Whether packs.allow_unsigned_code lets this pack's code run unsigned.

    An entry `id@<content digest>` names exact files, so it holds only while
    the installed files hash to that digest. A bare `id` names whatever
    carries that namespace, which nothing verifies for an unsigned pack; it
    is honoured only when packs.allowed_sources is set, so the org has also
    pinned where packs may come from (check_installed refuses any other
    source)."""
    allowed = pp.get("allow_unsigned_code") or []
    if digest and f"{pack_id}@{digest}" in allowed:
        return True
    return pack_id in allowed and pp.get("allowed_sources") is not None


# ── secrets and the child's environment ──────────────────────────────────────

def _vault_get(name: str) -> str | None:
    """A value from nable's vault, without creating a vault that is not there."""
    try:
        from ..security.vault import Vault, _vault_dir
        if not (_vault_dir() / "vault.db").is_file():
            return None
        return Vault.default().get(name)
    except Exception:  # noqa: BLE001 - a vault hiccup reads as "not set"
        return None


def vault_entry_name(pack_id: str, name: str) -> str:
    """The vault key a pack's secret lives under: pack:<namespace>/<name>:<NAME>.
    A namespace of its own, so a pack never reads nable's provider keys (or
    another pack's) by declaring the same name."""
    return f"pack:{pack_id}:{name}"


def secret_value(pack_id: str, name: str) -> str | None:
    """A pack's secret, from its own vault namespace only. Never from nable's
    environment or its provider keys: `nable pack secret set` stores it."""
    return _vault_get(vault_entry_name(pack_id, name))


def _check_secret_target(pack_id: str, name: str) -> tuple[str | None, str | None]:
    """(why the pair is refused, a note) for `nable pack secret`."""
    from .manifest import FIRST_PARTY_NAMESPACES, check_name, check_namespace
    ns, sep, pname = pack_id.partition("/")
    if not sep or check_namespace(ns) or check_name(pname):
        return f"{pack_id!r} is not a pack id such as io.github.acme/connector", None
    try:
        e = store.read_index()["packs"].get(pack_id)
    except PackError:
        e = None
    first_party = (e.get("tier") == "first-party") if e else ns in FIRST_PARTY_NAMESPACES
    why = caps_mod.check_secret(name, first_party=first_party)
    if why:
        return f"{name}: {why}", None
    note = None
    if e is None:
        note = f"{pack_id} is not installed; the secret waits for it"
    elif name not in ((e.get("capabilities") or {}).get("secrets") or ()):
        note = (f"the installed {pack_id} does not declare {name}, so it is not passed to it "
                "until a version that declares it is approved")
    return None, note


def set_secret(pack_id: str, name: str, value: str) -> dict[str, Any]:
    """Store `value` as the pack's secret `name` in nable's vault."""
    why, note = _check_secret_target(pack_id, name)
    if why:
        raise PackError(why)
    try:
        from ..security.vault import Vault
        Vault.default().store(vault_entry_name(pack_id, name), value)
    except Exception as err:  # noqa: BLE001 - the vault says why; never the value
        raise PackError(f"The secret could not be stored in nable's vault "
                        f"({type(err).__name__})") from None
    return {"pack": pack_id, "name": name, "vault_entry": vault_entry_name(pack_id, name), "note": note}


def remove_secret(pack_id: str, name: str) -> dict[str, Any]:
    why, _ = _check_secret_target(pack_id, name)
    if why:
        raise PackError(why)
    removed = False
    try:
        from ..security.vault import Vault, _vault_dir
        if (_vault_dir() / "vault.db").is_file():
            removed = Vault.default().delete(vault_entry_name(pack_id, name))
    except Exception as err:  # noqa: BLE001
        raise PackError(f"nable's vault could not be read ({type(err).__name__})") from None
    return {"pack": pack_id, "name": name, "removed": removed}


def child_env(prep: Prepared, home: str) -> tuple[dict[str, str], dict[str, str]]:
    """(the child's environment, the secret values in it). Only PATH, HOME,
    LANG and the declared secrets, each from the pack's own vault namespace;
    nothing inherited beyond those."""
    env = {"PATH": os.environ.get("PATH") or os.defpath, "HOME": home,
           "LANG": os.environ.get("LANG") or "C.UTF-8"}
    if os.name == "nt":  # Python on Windows needs these to start and to open sockets
        for k in ("SYSTEMROOT", "WINDIR"):
            if os.environ.get(k):
                env[k] = os.environ[k]
    secrets: dict[str, str] = {}
    first_party = prep.tier == "first-party"
    for name in prep.capabilities.get("secrets") or ():
        if caps_mod.check_secret(name, first_party=first_party):
            continue  # validated at install; a reserved or cloud name never crosses
        v = secret_value(prep.pack_id, name)
        if v is not None:
            env[name] = secrets[name] = v
    return env, secrets


_NETNS_OK: dict[str, bool] = {}


def netns_available() -> bool:
    """Whether `unshare --user --map-root-user --net` works here (cached)."""
    if NETNS != "auto" or not sys.platform.startswith("linux"):
        return False
    if "ok" in _NETNS_OK:
        return _NETNS_OK["ok"]
    exe = shutil.which("unshare")
    ok = False
    if exe:
        try:
            # A fixed argv and no shell: does the kernel give us a network namespace?
            r = subprocess.run(  # nosec B603
                [exe, "--user", "--map-root-user", "--net", "--", sys.executable, "-I", "-c",
                 "pass"], capture_output=True, timeout=10, env={"PATH": os.defpath},
                check=False)
            ok = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
    _NETNS_OK["ok"] = ok
    return ok


def _command(netns: bool) -> list[str]:
    import finops
    finops_dir = str(Path(finops.__file__).resolve().parent.parent)
    cmd = [sys.executable, "-I", "-B", "-c", _BOOTSTRAP, finops_dir]
    if netns:
        exe = shutil.which("unshare")
        if exe:
            return [exe, "--user", "--map-root-user", "--net", "--", *cmd]
    return cmd


# ── the child process and its protocol ───────────────────────────────────────

class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class _Session:
    """One child process: JSON-RPC requests out, its answers and requests in."""

    def __init__(self, cmd: list[str], env: dict[str, str], cwd: str, *, label: str,
                 max_output: int, on_request, on_notify):
        self.label = label
        self.max_output = max_output
        self.on_request, self.on_notify = on_request, on_notify
        self._q: queue.Queue = queue.Queue()
        # Writes to the child go through a bounded queue and one writer
        # thread, so a child that stops reading blocks that thread, never the
        # call: the call waits on the queue with its own deadline.
        self._wq: queue.Queue = queue.Queue(maxsize=_WRITE_QUEUE)
        self._write_error: BaseException | None = None
        self._inflight = 0                  # answers queued, not yet written
        self._inflight_lock = threading.Lock()
        self._stderr = bytearray()
        self._stderr_dropped = 0
        self._next = 0
        try:
            # A fixed argv (this interpreter, -I -B, the host module, optionally
            # behind unshare), no shell, and the scrubbed environment above.
            self.proc = subprocess.Popen(  # nosec B603
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, cwd=cwd, start_new_session=True, close_fds=True)
        except OSError as e:
            raise BrokerError(f"{label}: could not start the pack's process "
                              f"({e.strerror or e})") from None
        self._threads = [threading.Thread(target=self._read_out, daemon=True),
                         threading.Thread(target=self._read_err, daemon=True),
                         threading.Thread(target=self._write_in, daemon=True)]
        for t in self._threads:
            t.start()

    def _read_out(self) -> None:
        total = 0
        out = self.proc.stdout
        try:
            while True:
                line = out.readline(self.max_output - total + 1)
                if not line:
                    self._q.put(("eof", None))
                    return
                total += len(line)
                if total > self.max_output:
                    self._q.put(("overflow", None))
                    return
                self._q.put(("line", line))
        except (OSError, ValueError):
            self._q.put(("eof", None))

    def _read_err(self) -> None:
        err = self.proc.stderr
        try:
            while True:
                chunk = err.read1(65536) if hasattr(err, "read1") else err.read(65536)
                if not chunk:
                    return
                room = MAX_STDERR_BYTES - len(self._stderr)
                if room > 0:
                    self._stderr += chunk[:room]
                self._stderr_dropped += max(0, len(chunk) - max(room, 0))
        except (OSError, ValueError):
            return

    def stderr_text(self) -> str:
        text = self._stderr.decode("utf-8", "replace")
        if self._stderr_dropped:
            text += f"\n[... {self._stderr_dropped} more bytes of stderr dropped]"
        return text

    def _write_in(self) -> None:
        stdin = self.proc.stdin
        while True:
            item = self._wq.get()
            if item is None:
                return
            data, answer = item
            try:
                stdin.write(data)
                stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as e:
                self._write_error = e
                return
            if answer:
                with self._inflight_lock:
                    self._inflight -= 1

    def _send(self, obj: dict[str, Any], deadline: float, *, answer: bool = False) -> None:
        """Queue one message for the child, waiting no later than `deadline`."""
        data = (json.dumps(obj, separators=(",", ":"), default=str) + "\n").encode("utf-8")
        if self._write_error is not None:
            raise BrokerError(f"{self.label}: the pack's process stopped reading "
                              f"(exit code {self.proc.poll()})")
        if answer:
            with self._inflight_lock:
                self._inflight += 1
        try:
            self._wq.put((data, answer), timeout=max(deadline - time.monotonic(), 0.001))
        except queue.Full:
            self.kill()
            raise BrokerError(f"{self.label}: the pack stopped reading its input and was "
                              "stopped") from None

    def request(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        self._next += 1
        mid = self._next
        deadline = time.monotonic() + timeout

        def late() -> BrokerError:
            self.kill()
            return BrokerError(f"{self.label}: {method} took longer than {timeout:g}s and "
                               "was stopped")

        try:
            self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params},
                       deadline)
        except BrokerError:
            if time.monotonic() >= deadline:
                raise late() from None
            raise
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise late()
            try:
                what, line = self._q.get(timeout=remaining)
            except queue.Empty:
                continue
            if what == "overflow":
                raise BrokerError(f"{self.label}: the pack wrote more than {self.max_output} "
                                  "bytes and was stopped")
            if what == "eof":
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self.proc.wait(timeout=2)
                raise BrokerError(f"{self.label}: the pack's process exited during {method} "
                                  f"(exit code {self.proc.poll()})")
            try:
                msg = json.loads(line)
            except (ValueError, RecursionError):
                raise BrokerError(f"{self.label}: the pack broke the protocol (a line that is "
                                  "not JSON, or nested too deeply)") from None
            if not isinstance(msg, dict):
                raise BrokerError(f"{self.label}: the pack broke the protocol (not an object)")
            if "method" in msg:
                if "id" in msg and self._inflight >= MAX_INFLIGHT:
                    self.kill()
                    raise BrokerError(f"{self.label}: the pack had more than {MAX_INFLIGHT} "
                                      f"requests of the core in flight during {method} (it "
                                      "stopped reading the answers) and was stopped")
                try:
                    self._incoming(msg, deadline)
                except BrokerError:
                    if time.monotonic() >= deadline:
                        raise late() from None
                    raise
                continue
            if msg.get("id") != mid:
                raise BrokerError(f"{self.label}: the pack answered a request nobody made")
            if "error" in msg:
                err = msg.get("error")
                detail = err.get("message", "no detail") if isinstance(err, dict) else err
                raise BrokerError(f"{self.label}: {method} failed: {str(detail)[:500]}")
            return msg.get("result")

    def _incoming(self, msg: dict[str, Any], deadline: float) -> None:
        method, params = str(msg.get("method")), msg.get("params")
        if not isinstance(params, dict):
            params = {}
        if "id" not in msg:
            with contextlib.suppress(Exception):
                self.on_notify(method, params)
            return
        try:
            result = self.on_request(method, params)
        except _RpcError as e:
            self._send({"jsonrpc": "2.0", "id": msg["id"],
                        "error": {"code": e.code, "message": e.message}}, deadline, answer=True)
            return
        self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result}, deadline, answer=True)

    def close(self) -> int | None:
        # Stop the writer: drop what it has not sent, then tell it to end.
        with contextlib.suppress(queue.Empty):
            while True:
                self._wq.get_nowait()
        with contextlib.suppress(queue.Full):
            self._wq.put_nowait(None)
        # A writer still blocked on a full pipe means the child stopped
        # reading: kill it, so the write fails and closing stdin cannot wait
        # on the writer's lock.
        self._threads[2].join(timeout=1)
        if self._threads[2].is_alive():
            self.kill()
        with contextlib.suppress(OSError, ValueError):
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.kill()
        for t in self._threads:
            t.join(timeout=2)
        for s in (self.proc.stdout, self.proc.stderr):
            with contextlib.suppress(OSError, ValueError):
                s.close()
        return self.proc.returncode

    def kill(self) -> None:
        with contextlib.suppress(OSError, ProcessLookupError):
            if os.name == "posix":
                os.killpg(self.proc.pid, signal.SIGKILL)
            else:
                self.proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=5)


# ── data the pack may read back ──────────────────────────────────────────────

def local_today() -> date:
    """The local calendar day, as the rest of nable counts days."""
    return datetime.now().astimezone().date()


def _day(v: Any, default: date) -> date:
    if v in (None, ""):
        return default
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        raise _RpcError(-32602, f"{v!r} is not a date like 2026-09-01") from None


def _data_focus_cost(query: dict[str, Any]) -> dict[str, Any]:
    """nable's daily cost snapshots, in FOCUS column names."""
    from sqlalchemy import and_, select

    from ..storage.db import cost_snapshots, get_engine
    end = _day(query.get("end"), local_today())
    start = _day(query.get("start"), end - timedelta(days=30))
    conds = [cost_snapshots.c.snapshot_date >= start.isoformat(),
             cost_snapshots.c.snapshot_date < end.isoformat()]
    if query.get("provider"):
        conds.append(cost_snapshots.c.provider == str(query["provider"]))
    with get_engine().connect() as conn:
        rows = conn.execute(select(cost_snapshots).where(and_(*conds))
                            .order_by(cost_snapshots.c.snapshot_date)
                            .limit(MAX_DATA_ROWS + 1)).fetchall()
    out = []
    for r in rows[:MAX_DATA_ROWS]:
        m = r._mapping
        day = str(m["snapshot_date"])[:10]
        out.append({"ChargePeriodStart": day,
                    "ChargePeriodEnd": (date.fromisoformat(day) + timedelta(days=1)).isoformat(),
                    "ProviderName": m["provider"], "ServiceName": m["service"],
                    "SubAccountId": m["account_id"], "RegionId": m["region"] or None,
                    "BilledCost": float(m["amount_usd"] or 0.0),
                    "x_Category": m["category"]})
    return {"rows": out, "truncated": len(rows) > MAX_DATA_ROWS,
            "window": {"start": start.isoformat(), "end": end.isoformat()}}


def _org_facts(kind: str) -> list[dict[str, Any]]:
    from .. import org
    model = org.load()
    return [{"subject": str(f.subject), "value": dict(f.value), "confirmed": f.confirmed,
             "source": f.source} for f in model.by_kind(kind) if f.live][:MAX_DATA_ROWS]


def _data_org_owners(query: dict[str, Any]) -> dict[str, Any]:
    return {"owners": _org_facts("owner")}


def _data_org_environments(query: dict[str, Any]) -> dict[str, Any]:
    return {"environments": _org_facts("environment")}


# repo.files: files a pack names, from the repos the call names (an adapter
# call from `nable org init` names the repos init reads). Plain file names
# only, never a path or a glob, so a pack cannot ask for ~/.ssh/id_rsa or
# ../../anything; the walk never follows a symlink and skips vendored and
# generated trees. Bounded in count, size and time.
REPO_FILE_NAMES_MAX = 16
REPO_FILE_MAX_BYTES = 256 * 1024
REPO_FILES_MAX = 500
REPO_FILES_TOTAL_BYTES = 8 * 1024 * 1024
REPO_WALK_MAX_ENTRIES = 200_000
_REPO_SKIP_DIRS = frozenset({".git", ".hg", ".svn", "node_modules", ".terraform", ".venv",
                             "venv", "__pycache__", "vendor", "dist", "build", ".tox",
                             ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache", ".next",
                             ".idea", "target"})
_REPO_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _read_repo_file(path: Path) -> bytes | None:
    """A regular file's bytes, or None: never through a symlink (one put
    there after the walk saw the name included), never a FIFO or a device
    (opened without blocking, then checked), never more than
    REPO_FILE_MAX_BYTES however big it has grown since."""
    if path.is_symlink():
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > REPO_FILE_MAX_BYTES:
            return None
        chunks: list[bytes] = []
        left = REPO_FILE_MAX_BYTES + 1
        while left > 0:
            chunk = os.read(fd, min(left, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        raw = b"".join(chunks)
        return raw if len(raw) <= REPO_FILE_MAX_BYTES else None
    except OSError:
        return None
    finally:
        os.close(fd)


def _data_repo_files(query: dict[str, Any], repos: list[Path]) -> dict[str, Any]:
    names = query.get("names")
    if not isinstance(names, list) or not 1 <= len(names) <= REPO_FILE_NAMES_MAX or not all(
            isinstance(n, str) and _REPO_FILE_NAME.match(n) and ".." not in n for n in names):
        raise _RpcError(-32602, f"repo.files takes {{names: [...]}}: 1 to {REPO_FILE_NAMES_MAX} "
                        "plain file names such as catalog-info.yaml, never a path or a glob")
    wanted = set(names)
    files: list[dict[str, Any]] = []
    total = seen = 0
    truncated = False
    for i, root in enumerate(repos):
        if truncated:
            break
        for dirpath, dirnames, filenames in os.walk(root):   # never follows a symlink
            dirnames[:] = sorted(d for d in dirnames if d not in _REPO_SKIP_DIRS)
            seen += len(dirnames) + len(filenames)
            if seen > REPO_WALK_MAX_ENTRIES:
                truncated = True
                break
            for fn in sorted(filenames):
                if fn not in wanted:
                    continue
                path = Path(dirpath) / fn
                raw = _read_repo_file(path)
                if raw is None:
                    continue
                if len(files) >= REPO_FILES_MAX or total + len(raw) > REPO_FILES_TOTAL_BYTES:
                    truncated = True
                    break
                total += len(raw)
                files.append({"repo": i, "path": path.relative_to(root).as_posix(),
                              "text": raw.decode("utf-8", "replace")})
            if truncated:
                break
    return {"files": files, "truncated": truncated}


DATA_SCOPES = {"focus.cost": _data_focus_cost, "org.owners": _data_org_owners,
               "org.environments": _data_org_environments, "repo.files": _data_repo_files}


def read_data(prep: Prepared, params: dict[str, Any], repos: list[Path] | None = None) -> Any:
    """Answer a pack's data.read: only a declared scope, only one nable serves.
    `repos` are the repositories this call names, for repo.files."""
    scope = params.get("scope")
    query = params.get("query") or {}
    if not isinstance(scope, str) or not isinstance(query, dict):
        raise _RpcError(-32602, "data.read takes {scope: str, query: object}")
    if scope not in (prep.capabilities.get("read_data") or ()):
        raise _RpcError(-32001, f"{scope} is not in {prep.pack_id}'s declared read_data")
    fn = DATA_SCOPES.get(scope)
    if fn is None:
        raise _RpcError(-32002, f"{scope} is declared, but this nable does not serve it to "
                        f"packs yet (available: {', '.join(sorted(DATA_SCOPES))})")
    try:
        if scope == "repo.files":
            return _data_repo_files(query, list(repos or ()))
        return fn(query)
    except _RpcError:
        raise
    except Exception as e:  # noqa: BLE001 - the pack gets a reason, the core keeps running
        log.warning("finops.packs: data.read %s for %s failed: %s", scope, prep.pack_id, e)
        raise _RpcError(-32003, f"{scope} could not be read ({type(e).__name__})") from None


# ── running one call ─────────────────────────────────────────────────────────

@dataclass
class RunResult:
    pack: str
    entry: str
    kind: str
    method: str
    output: Any
    dropped: int = 0
    problems: list[str] = field(default_factory=list)
    network: dict[str, Any] = field(default_factory=dict)
    loaded_from: str = ""
    log: str = ""
    seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        out = self.output
        if self.kind == "adapters":
            out = [f.summary() for f in self.output]
        return {"pack": self.pack, "entry": self.entry, "kind": self.kind,
                "method": self.method, "output": out, "dropped": self.dropped,
                "problems": self.problems, "network": self.network,
                "loaded_from": self.loaded_from, "log": self.log,
                "seconds": round(self.seconds, 3)}


def log_path(prep: Prepared) -> Path:
    return store.packs_root() / "logs" / prep.namespace / f"{prep.name}.log"


def _redact(text: str, secrets: dict[str, str]) -> str:
    for name, value in secrets.items():
        if len(value) >= 4:
            text = text.replace(value, f"[redacted {name}]")
    return text


def _write_log(path: Path, header: str, body: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            if path.stat().st_size > MAX_LOG_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(header + "\n" + (body.rstrip() + "\n" if body.strip() else ""))
    except OSError as e:
        log.warning("finops.packs: could not write the pack log %s (%s)", path, e)


def _private_copy(prep: Prepared) -> Path:
    """The pack's files, copied into a new private directory and hashed as
    they are copied against what was approved. The child imports from the
    copy, so a file swapped in the packs root after prepare() checked it is
    not the file that runs. Raises PolicyRefusal on any difference."""
    from pathlib import PurePosixPath
    dest = Path(tempfile.mkdtemp(prefix="nable-pack-code-"))
    try:
        if not prep.files:
            raise PolicyRefusal(f"{prep.pack_id}: the index lists no files for it")
        for rel, want in sorted(prep.files.items()):
            parts = PurePosixPath(rel).parts
            if not parts or rel.startswith("/") or "\\" in rel or ".." in parts:
                raise PolicyRefusal(f"{prep.pack_id}: the index lists a file outside the pack "
                                    f"({rel!r})")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(prep.root.joinpath(*parts), flags)
                with os.fdopen(fd, "rb") as f:
                    data = f.read(store.MAX_FILE_BYTES + 1)
            except OSError as e:
                raise PolicyRefusal(f"{prep.pack_id}: {rel} could not be read "
                                    f"({e.strerror or e})") from None
            if len(data) > store.MAX_FILE_BYTES or hashlib.sha256(data).hexdigest() != want:
                raise PolicyRefusal(f"{prep.pack_id}: {rel} changed after it was checked; "
                                    "run `nable pack audit`")
            target = dest.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "xb") as out:
                out.write(data)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


def execute(prep: Prepared, method: str, params: dict[str, Any], *,
            timeout: float | None = None, max_output: int | None = None,
            repos: list[Path] | None = None) -> tuple[Any, dict[str, Any]]:
    """Start the host, initialize it, make one call, stop it. Returns (the raw
    result, a report: network mode, declared and observed hosts, where the
    entry was loaded from, the log path). `repos` are the repositories this
    call may read through repo.files. Raises BrokerError."""
    timeout = DEFAULT_TIMEOUT_S if timeout is None else float(timeout)
    max_output = MAX_OUTPUT_BYTES if max_output is None else int(max_output)
    from . import API_VERSION
    hosts = list(prep.capabilities.get("network") or ())
    netns = not hosts and netns_available()
    observed: list[dict[str, Any]] = []
    report: dict[str, Any] = {"network": {"mode": "namespace" if netns else "audit",
                                          "declared": hosts, "observed": observed},
                              "log": str(log_path(prep))}
    label = f"{prep.pack_id} {prep.kind[:-1]} {prep.entry_id}"
    code_root = _private_copy(prep) if prep.files else prep.root
    home = tempfile.mkdtemp(prefix="nable-pack-")
    env, secrets = child_env(prep, home)

    def on_notify(m: str, p: dict[str, Any]) -> None:
        if m == "audit.network" and len(observed) < 200:
            observed.append({k: p.get(k) for k in ("event", "host", "port", "allowed")})

    def on_request(m: str, p: dict[str, Any]) -> Any:
        if m == "data.read":
            return read_data(prep, p, repos)
        raise _RpcError(-32601, f"{m} is not something a pack can ask the core for")

    started = time.monotonic()
    session = None
    status = "stopped"
    try:
        session = _Session(_command(netns), env, home, label=label, max_output=max_output,
                           on_request=on_request, on_notify=on_notify)
        caps = {k: (list(v) if isinstance(v, tuple) else v)
                for k, v in prep.capabilities.items()}
        try:
            init = session.request("initialize", {
                "pack": prep.pack_id, "kind": prep.kind, "entry_id": prep.entry_id,
                "entry": prep.entry, "pack_root": str(code_root), "api_version": API_VERSION,
                "capabilities": caps, "network": hosts,
                "allow_external_code": prep.allowlisted}, timeout)
        except BrokerError as e:
            raise BrokerError(f"{label} could not be loaded: "
                              f"{e.message.removeprefix(label + ': ')}") from None
        if not isinstance(init, dict) or init.get("protocol") != 1:
            raise BrokerError(f"{label}: the host speaks a protocol this nable does not")
        report["loaded_from"] = str(init.get("loaded_from", ""))
        result = session.request(method, params, timeout)
        status = "ok"
        return result, report
    except BrokerError as e:
        status = _redact(e.message, secrets)
        raise BrokerError(f"{status} (the pack's log: {report['log']})") from None
    finally:
        if session is not None:
            if status != "ok":
                session.kill()
            rc = session.close()
            blocked = [o for o in observed if not o.get("allowed")]
            header = (f"=== {store.now_iso()} {prep.entry_id} {method} exit={rc} "
                      f"network={report['network']['mode']} declared={','.join(hosts) or '-'} "
                      f"blocked={len(blocked)} {'ok' if status == 'ok' else 'FAILED: ' + status}")
            body = _redact(session.stderr_text(), secrets)
            body += "".join(f"\nnetwork: {o['event']} {o['host']}:{o['port']} "
                            f"{'allowed' if o['allowed'] else 'REFUSED'}" for o in observed)
            _write_log(log_path(prep), _redact(header, secrets), body)
            for o in blocked:
                log.warning("finops.packs: %s tried %s:%s, which it did not declare",
                            prep.pack_id, o.get("host"), o.get("port"))
        report["seconds"] = time.monotonic() - started
        shutil.rmtree(home, ignore_errors=True)
        if code_root != prep.root:
            shutil.rmtree(code_root, ignore_errors=True)


# ── output validation ────────────────────────────────────────────────────────

_MONEY = ("BilledCost", "EffectiveCost", "ListCost")
_REQ_STR = ("ResourceId", "ResourceType", "ServiceName", "ServiceCategory", "ProviderName",
            "PublisherName", "ChargeCategory")
_OPT_STR = ("ResourceName", "RegionId", "RegionName", "ChargeDescription",
            "CommitmentDiscountId", "CommitmentDiscountType", "SubAccountId", "SubAccountName")
_DATES = ("BillingPeriodStart", "BillingPeriodEnd", "ChargePeriodStart", "ChargePeriodEnd")
_MAX_STR = 1024


def _when(v: Any, name: str) -> datetime:
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{name} must be an ISO date or datetime")
    try:
        return datetime.fromisoformat(v.strip())
    except ValueError:
        raise ValueError(f"{name} {v[:40]!r} is not an ISO date or datetime") from None


def focus_row(raw: Any) -> dict[str, Any]:
    """A connector row checked against finops.focus's FocusRecord schema and
    returned in JSON form (datetimes as ISO strings). Columns FocusRecord does
    not have are left out. Raises ValueError with the first thing wrong."""
    from ..focus.schema import CHARGE_CATEGORIES, SERVICE_CATEGORIES, FocusRecord
    if not isinstance(raw, dict):
        raise TypeError("a row must be an object")
    vals: dict[str, Any] = {}
    for k in _MONEY:
        v = raw.get(k)
        if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
            raise ValueError(f"{k} must be a finite number")
        vals[k] = float(v)
    for k in _REQ_STR:
        v = raw.get(k)
        if not isinstance(v, str) or len(v) > _MAX_STR or (k != "ResourceId" and not v.strip()):
            raise ValueError(f"{k} must be a string of at most {_MAX_STR} characters")
        vals[k] = v
    for k in _OPT_STR:
        v = raw.get(k)
        if v is not None and (not isinstance(v, str) or len(v) > _MAX_STR):
            raise ValueError(f"{k} must be a string or null")
        vals[k] = v
    if vals["ChargeCategory"] not in CHARGE_CATEGORIES:
        raise ValueError(f"ChargeCategory {vals['ChargeCategory']!r} is not one of "
                         f"{', '.join(sorted(CHARGE_CATEGORIES))}")
    if vals["ServiceCategory"] not in SERVICE_CATEGORIES:
        raise ValueError(f"ServiceCategory {vals['ServiceCategory']!r} is not one of "
                         f"{', '.join(sorted(SERVICE_CATEGORIES))}")
    for k in _DATES:
        vals[k] = _when(raw.get(k), k)
    for a, b in (("BillingPeriodStart", "BillingPeriodEnd"),
                 ("ChargePeriodStart", "ChargePeriodEnd")):
        try:
            if vals[b] < vals[a]:
                raise ValueError(f"{b} is before {a}")
        except TypeError:
            raise ValueError(f"{a} and {b} mix dates with and without a timezone") from None
    tags = raw.get("Tags") or {}
    if not isinstance(tags, dict) or len(tags) > 200 or not all(
            isinstance(k, str) and isinstance(v, str) and len(k) <= 256 and len(v) <= _MAX_STR
            for k, v in tags.items()):
        raise ValueError("Tags must be an object of string keys and string values")
    vals["Tags"] = dict(tags)
    rec = FocusRecord(**vals)
    out = dataclasses.asdict(rec)
    for k in _DATES:
        out[k] = out[k].isoformat()
    return out


def _fact(pack_id: str, raw: Any, problems: list[str], i: int):
    from ..org import make_fact
    if not isinstance(raw, dict):
        raise TypeError("a fact must be an object")
    if raw.get("status") not in (None, "proposed") or raw.get("confirmed_by"):
        problems.append(f"fact[{i}] asked to be {raw.get('status') or 'confirmed'}; a pack can "
                        "only propose, so it is a proposal")
    src = str(raw.get("source") or "").strip()
    source = (f"pack:{pack_id}" + (f":{src}" if src else ""))[:300]
    conf = raw.get("confidence", 0.5)
    return make_fact(raw.get("fact"), raw.get("subject"), raw.get("value"), source=source,
                     confidence=conf if conf is not None else 0.5,
                     dollars_monthly=raw.get("dollars_monthly"))


def _note(problems: list[str], text: str) -> None:
    if len(problems) < MAX_PROBLEMS:
        problems.append(text)


def _level(v: str) -> int:
    return caps_mod.AUTONOMY_LEVELS.index(v)


def check_delivery(prep: Prepared, payload: Any) -> dict[str, Any]:
    """A sink may deliver only its declared act kinds, at or below its
    max_autonomy. Checked before the pack's process starts."""
    if not isinstance(payload, dict):
        raise PolicyRefusal(f"{prep.pack_id}: a sink payload must be an object")
    acts = tuple(prep.capabilities.get("act") or ())
    kind = payload.get("kind")
    if kind not in acts:
        raise PolicyRefusal(f"{prep.pack_id} declares act {list(acts) or 'nothing'}, so it "
                            f"cannot deliver a {kind!r}")
    level = payload.get("autonomy") or ACT_LEVEL.get(str(kind), "L2")
    ceiling = prep.capabilities.get("max_autonomy", "L0")
    if level not in caps_mod.AUTONOMY_LEVELS or _level(level) > _level(ceiling):
        raise PolicyRefusal(f"{prep.pack_id}: a {kind} at {level} is above the pack's "
                            f"max_autonomy ({ceiling})")
    size = len(json.dumps(payload, default=str))
    if size > MAX_PAYLOAD_BYTES:
        raise PolicyRefusal(f"{prep.pack_id}: the payload is {size} bytes; the most a sink "
                            f"takes is {MAX_PAYLOAD_BYTES}")
    return payload


def run(pack_id: str, entry_id: str, method: str, params: dict[str, Any], *,
        timeout: float | None = None, max_output: int | None = None,
        pp: dict[str, Any] | None = None, repos: list[RepoRef] | None = None) -> RunResult:
    """Run one call of an installed pack's code and validate what it returns.

    `repos` (adapters): the repositories the call is about. A pack that
    declares read_data = ["repo.files"] gets each one's name, label and
    repo_path subject prefix in its context (never its path on this machine)
    and may read files from them by name; any other pack gets neither."""
    kind = METHOD_KIND.get(method)
    if kind is None:
        raise PackError(f"{method} is not a broker method; known: {', '.join(METHOD_KIND)}")
    prep = prepare(pack_id, entry_id, kind, pp=pp)
    if kind == "adapters" and "proposals" not in (prep.capabilities.get("write_org") or ()):
        raise PolicyRefusal(f"{pack_id} does not declare write_org = [\"proposals\"], so its "
                            "adapter cannot propose org facts")
    if kind == "sinks":
        check_delivery(prep, params.get("payload"))
    roots: list[Path] = []
    if kind == "adapters" and "repo.files" in (prep.capabilities.get("read_data") or ()):
        refs = list(repos or ())
        roots = [r.path for r in refs]
        params = {**params, "context": {**dict(params.get("context") or {}),
                                        "repos": [r.public(i) for i, r in enumerate(refs)]}}
    raw, report = execute(prep, method, params, timeout=timeout, max_output=max_output,
                          repos=roots)
    problems: list[str] = []
    dropped = 0
    output: Any
    if kind == "connectors":
        rows = raw.get("rows") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            raise BrokerError(f"{pack_id} {entry_id}: fetch_costs returned no rows list")
        output = []
        for i, r in enumerate(rows):
            try:
                output.append(focus_row(r))
            except (ValueError, TypeError) as e:
                dropped += 1
                _note(problems, f"row {i} dropped: {e}")
    elif kind == "adapters":
        facts = raw.get("facts") if isinstance(raw, dict) else None
        if not isinstance(facts, list):
            raise BrokerError(f"{pack_id} {entry_id}: propose returned no facts list")
        output = []
        for i, f in enumerate(facts):
            try:
                output.append(_fact(pack_id, f, problems, i))
            except (ValueError, TypeError) as e:
                dropped += 1
                _note(problems, f"fact[{i}] dropped: {e}")
    else:
        receipt = raw.get("receipt") if isinstance(raw, dict) else None
        if not isinstance(receipt, dict):
            raise BrokerError(f"{pack_id} {entry_id}: deliver returned no receipt object")
        output = receipt
    if len(problems) >= MAX_PROBLEMS and dropped > MAX_PROBLEMS:
        problems.append(f"... and {dropped - MAX_PROBLEMS} more dropped")
    return RunResult(pack_id, entry_id, kind, method, output, dropped, problems,
                     report["network"], report.get("loaded_from", ""), report["log"],
                     report.get("seconds", 0.0))


def fetch_costs(pack_id: str, entry_id: str, start: str | date, end: str | date,
                **kw) -> RunResult:
    """A connector's FOCUS rows for [start, end); invalid rows dropped."""
    return run(pack_id, entry_id, "connector.fetch_costs",
               {"start": str(start), "end": str(end)}, **kw)


def propose_facts(pack_id: str, entry_id: str, context: dict[str, Any] | None = None,
                  **kw) -> RunResult:
    """An adapter's org facts, validated, every one a proposal. `repos=`
    (RepoRefs) names the repositories it may read through repo.files."""
    return run(pack_id, entry_id, "adapter.propose", {"context": dict(context or {})}, **kw)


def deliver(pack_id: str, entry_id: str, payload: dict[str, Any], **kw) -> RunResult:
    """Hand a proposal to a sink; its receipt comes back."""
    return run(pack_id, entry_id, "sink.deliver", {"payload": payload}, **kw)


# ── org-context adapters for `nable org init` ─────────────────────────────────

@dataclass(frozen=True)
class RepoRef:
    """A repository an adapter call is about. `subject_prefix` is what a
    repo_path subject in it starts with where the proposals are written
    ("repo_path:" in that repo's own nable.org/, "repo_path:<repo>//"
    elsewhere); `label` prefixes source locators ("" for the repo nable runs
    in). The path stays in the core: the pack sees the rest."""
    path: Path
    name: str
    subject_prefix: str
    label: str = ""

    def public(self, i: int) -> dict[str, Any]:
        return {"id": i, "name": self.name, "subject_prefix": self.subject_prefix,
                "label": self.label}


def repo_refs(ctx: Any = None, roots: list[Path] | None = None) -> list[RepoRef]:
    """RepoRefs for an org AdapterContext's repos (subjects as its
    repo_subject writes them), or for bare roots (subjects that name the
    repo, as in the data dir's model)."""
    from ..org.store import repo_identity
    out: list[RepoRef] = []
    if ctx is not None and hasattr(ctx, "repos"):
        for repo in list(ctx.repos or ()):
            prefix = ctx.repo_subject(repo, "") if hasattr(ctx, "repo_subject") else \
                f"repo_path:{repo_identity(repo)}//"
            label = ctx.repo_label(repo) if hasattr(ctx, "repo_label") else ""
            out.append(RepoRef(Path(repo), repo_identity(repo), prefix, label))
        return out
    for repo in roots or ():
        out.append(RepoRef(Path(repo), repo_identity(repo), f"repo_path:{repo_identity(repo)}//"))
    return out


class PackAdapter:
    """One pack adapter as a finops.org ADAPTERS callable: adapter(model) ->
    list of Facts (proposals, source "pack:<id>:..."). The model is not sent
    to the pack; it reads org data only through its declared scopes."""

    def __init__(self, pack_id: str, entry_id: str):
        self.pack_id, self.entry_id = pack_id, entry_id
        self.name = f"pack:{pack_id}/{entry_id}"
        self.last: RunResult | None = None

    def __call__(self, model: Any = None) -> list[Any]:
        # `cwd`: where `nable org init` runs, which is the repo it is about.
        # The pack runs in a throwaway directory of its own and would not
        # otherwise know. run_adapters passes its AdapterContext: the repos
        # it reads go along (as RepoRefs), for a pack that declares repo.files.
        self.last = propose_facts(self.pack_id, self.entry_id,
                                  {"today": local_today().isoformat(), "cwd": os.getcwd()},
                                  repos=repo_refs(model))
        return list(self.last.output)

    def __repr__(self) -> str:
        return f"PackAdapter({self.name})"


def org_adapters(*, pp: dict[str, Any] | None = None) -> list[PackAdapter]:
    """Adapters from every installed pack that may run now (intact, in
    policy, signed or allowlisted, write_org = ["proposals"]). A pack that may
    not is skipped with a warning, never an error.

    How `nable org init` (finops.org.store.run_adapters) should use them, once
    the org adapters builder wires it:

        from finops import packs
        for adapter in [*ADAPTERS, *packs.org_adapters()]:
            for fact in adapter(model):
                propose(fact, dir)          # a proposal, as for any adapter

    Each call starts the pack's process through the broker; a failure raises,
    which run_adapters already counts as "failed" without stopping init."""
    from ..policy import pack_policy
    pp = pack_policy() if pp is None else pp
    try:
        idx = store.read_index()
    except PackError as e:
        log.warning("finops.packs: %s", e)
        return []
    out: list[PackAdapter] = []
    for pid, e in sorted(idx["packs"].items()):
        for c in e.get("code") or []:
            if c.get("kind") != "adapters":
                continue
            try:
                prep = prepare(pid, str(c.get("id")), "adapters", pp=pp)
            except PackError as err:
                log.warning("finops.packs: adapter %s/%s is skipped: %s", pid, c.get("id"),
                            err.message)
                continue
            if "proposals" not in (prep.capabilities.get("write_org") or ()):
                log.warning("finops.packs: adapter %s/%s is skipped: the pack does not "
                            "declare write_org = [\"proposals\"]", pid, c.get("id"))
                continue
            out.append(PackAdapter(pid, prep.entry_id))
    return out
