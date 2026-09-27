# SPDX-License-Identifier: Apache-2.0
"""The subprocess side of the broker: import one entry point, speak JSON-RPC.

broker.py starts this as `python -I -B -c <bootstrap> <dir containing finops>`
(the bootstrap puts nable's own package directory first on sys.path and calls
main(); `-I` ignores PYTHON* variables, the user site and the working
directory, `-B` keeps bytecode out of the pack, and main() points
sys.pycache_prefix at a fresh empty directory so no .pyc beside the source
is ever read). It then speaks JSON-RPC 2.0, one JSON object per line:

    core -> host   initialize {pack, kind, entry_id, entry, pack_root,
                               api_version, capabilities, network,
                               allow_external_code}
    core -> host   connector.fetch_costs {start, end}
                   adapter.propose {context}
                   sink.deliver {payload}
    host -> core   data.read {scope, query}        (a request; the core answers)
    host -> core   audit.network {event, host, port, allowed}   (a notification)
    core -> host   shutdown

Before any pack code is imported this module:

  - moves the protocol to private copies of stdin and stdout, points fd 0 at
    /dev/null and fd 1 at stderr, so whatever the pack prints lands in its log
    and cannot forge a protocol message by accident;
  - installs an audit hook (sys.addaudithook) that refuses socket connects,
    sends and name lookups for hosts the manifest did not declare, refuses unix
    sockets, and refuses starting other programs (subprocess, os.exec*,
    os.spawn*, os.system, os.posix_spawn) and loading native libraries through
    ctypes, since each of those would step around the hook. Every connect, and
    every refusal, is reported to the core as audit.network.

This is auditing, not isolation. An audit hook runs inside the process it
watches: a determined pack can reach the hook's state through the garbage
collector, load a C extension that calls connect() directly, read the
private protocol descriptors, or start a program through
_posixsubprocess.fork_exec, which raises no audit event at all (the
subprocess module's audit event is raised by its Python wrapper, which such a
pack skips). That is why the broker runs code only from a pack signed by a
key the org trusts (or allowlisted by content digest), and on Linux puts a
pack that declares no network in its own empty network namespace when the
kernel allows it.
"""
from __future__ import annotations

import contextlib
import importlib
import importlib.util
import ipaddress
import json
import os
import socket
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

PROTOCOL = 1
METHODS = {"connectors": "connector.fetch_costs", "adapters": "adapter.propose",
           "sinks": "sink.deliver"}
# Audit events that start another program or load native code: each would
# leave this process's audit hook behind, so none is allowed.
_BLOCKED_EVENTS = ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
                   "os.startfile", "ctypes.dlopen", "pty.spawn")


class Channel:
    """Line-delimited JSON-RPC over the private stdin/stdout copies."""

    def __init__(self, fin, fout):
        self._in, self._out = fin, fout
        self._lock = threading.RLock()
        self._next = 0

    def send(self, obj: dict[str, Any]) -> None:
        line = json.dumps(obj, separators=(",", ":"), default=str)
        with self._lock:
            self._out.write(line + "\n")
            self._out.flush()

    def read(self) -> dict[str, Any] | None:
        line = self._in.readline()
        if not line:
            return None
        msg = json.loads(line)
        if not isinstance(msg, dict):
            raise ValueError("a protocol message must be a JSON object")  # noqa: TRY004 - a protocol error, like bad JSON
        return msg

    def respond(self, mid: Any, result: Any) -> None:
        self.send({"jsonrpc": "2.0", "id": mid, "result": result})

    def fail(self, mid: Any, code: int, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}})

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict[str, Any]) -> Any:
        """Ask the core and wait for its answer (the core answers in order)."""
        from .sdk import DataError
        with self._lock:
            self._next += 1
            mid = f"h{self._next}"
            self.send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
            msg = self.read()
        if msg is None:
            raise DataError("the core closed the channel", -32099)
        if msg.get("id") != mid:
            raise DataError("the core answered a different request", -32099)
        if "error" in msg:
            err = msg["error"] or {}
            raise DataError(str(err.get("message", "refused")), int(err.get("code", -32001)))
        return msg.get("result")


class NetworkAudit:
    """The declared egress allowlist, enforced (best effort) by an audit hook."""

    def __init__(self, declared: list[str], channel: Channel | None):
        self.channel = channel
        self.names: dict[str, set[int | None]] = {}
        self.addrs: set[tuple[str, int | None]] = set()
        self._seen: set[tuple[str, str, Any, bool]] = set()
        self._local = threading.local()
        for entry in declared:
            host, sep, port = entry.rpartition(":") if ":" in entry else (entry, "", "")
            p = int(port) if sep and port.isdigit() else None
            host = host.lower().rstrip(".")
            if _is_ip(host):
                self.addrs.add((host, p))
            else:
                self.names.setdefault(host, set()).add(p)
        self._resolve_declared()

    def _resolve_declared(self) -> None:
        for name, ports in self.names.items():
            try:
                infos = socket.getaddrinfo(name, None)
            except OSError:
                continue  # unreachable now; a later lookup adds what it finds
            self._learn(name, infos, ports)

    def _learn(self, name: str, infos: Any, ports: set[int | None]) -> None:
        for info in infos or ():
            try:
                ip = _norm_ip(str(info[4][0]))
            except (IndexError, TypeError, ValueError):
                continue
            for p in ports:
                self.addrs.add((ip, p))

    def wrap_getaddrinfo(self) -> None:
        """Addresses a declared name resolves to later are allowed too."""
        orig = socket.getaddrinfo
        audit = self

        def getaddrinfo(host, port, *args, **kwargs):
            infos = orig(host, port, *args, **kwargs)
            name = _name(host)
            if name in audit.names:
                audit._learn(name, infos, audit.names[name])
            return infos

        socket.getaddrinfo = getaddrinfo

    def _report(self, event: str, host: str, port: Any, allowed: bool) -> None:
        key = (event, host, port, allowed)
        if key in self._seen or self.channel is None or getattr(self._local, "busy", False):
            return
        self._seen.add(key)
        self._local.busy = True
        try:
            # A report must never break the call it reports on.
            with contextlib.suppress(Exception):
                self.channel.notify("audit.network", {"event": event, "host": host,
                                                      "port": port, "allowed": allowed})
        finally:
            self._local.busy = False

    def _refuse(self, event: str, host: str, port: Any, why: str) -> None:
        self._report(event, host, port, False)
        raise PermissionError(f"nable broker: {why}")

    def _name_ok(self, name: str) -> bool:
        if name in self.names:
            return True
        return _is_ip(name) and any(a == _norm_ip(name) for a, _ in self.addrs)

    def check_name(self, event: str, host: Any, port: Any = None) -> None:
        if host is None:
            return  # a local wildcard lookup, as for bind(); no name leaves the machine
        name = _name(host)
        if not self._name_ok(name):
            self._refuse(event, name, port, f"{name} is not in this pack's declared network")

    def check_addr(self, event: str, sock: Any, addr: Any) -> None:
        family = getattr(sock, "family", None)
        if isinstance(addr, str | bytes) or family == getattr(socket, "AF_UNIX", object()):
            self._refuse(event, str(addr), None, "unix sockets are not available to packs")
        if not isinstance(addr, tuple) or len(addr) < 2:
            self._refuse(event, repr(addr), None, "only IP addresses a pack declared are allowed")
        ip, port = _norm_ip(str(addr[0])), addr[1]
        if (ip, port) in self.addrs or (ip, None) in self.addrs:
            self._report(event, ip, port, True)
            return
        self._refuse(event, ip, port, f"{ip}:{port} is not in this pack's declared network")

    def hook(self, event: str, args: tuple) -> None:
        if event in ("socket.connect", "socket.sendto", "socket.sendmsg"):
            if len(args) >= 2 and args[1] is not None:
                self.check_addr(event, args[0], args[1])
        elif event == "socket.getaddrinfo":
            self.check_name(event, args[0], args[1] if len(args) > 1 else None)
        elif event in ("socket.gethostbyname", "socket.gethostbyname_ex",
                       "socket.gethostbyaddr", "socket.getnameinfo"):
            host = args[0][0] if event == "socket.getnameinfo" and args and \
                isinstance(args[0], tuple) else (args[0] if args else None)
            self.check_name(event, host)
        elif event.startswith(_BLOCKED_EVENTS):
            raise PermissionError(f"nable broker: a pack may not {event} (starting programs "
                                  "and loading native libraries would step around its sandbox)")


def _name(host: Any) -> str:
    if isinstance(host, bytes):
        host = host.decode("idna", "replace")
    return str(host).lower().rstrip(".")


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text.split("%", 1)[0])
        return True
    except ValueError:
        return False


def _norm_ip(text: str) -> str:
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return text.lower()
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return str(ip.ipv4_mapped)
    return str(ip)


def _under(path: str | None, root: Path) -> bool:
    if not path:
        return False
    try:
        return Path(os.path.realpath(path)).is_relative_to(root)
    except (OSError, ValueError):
        return False


def resolve_entry(entry: str, pack_root: Path, *, allow_external: bool) -> tuple[Any, str]:
    """Import `module:attr` and return (callable, "pack" | "environment").

    Code inside the pack directory is covered by the pack's signature. Code
    that resolves anywhere else (an installed distribution) is not, so it is
    refused unless the core says the org allowlisted this pack. The check uses
    find_spec on the top-level package, which runs none of its code."""
    module, _, attr = entry.partition(":")
    top = module.split(".", 1)[0]
    spec = importlib.util.find_spec(top)
    if spec is None:
        raise ImportError(f"{top} is not in the pack or the environment")
    root = Path(os.path.realpath(pack_root))
    locations = list(spec.submodule_search_locations or []) + [spec.origin or ""]
    inside = any(_under(loc, root) for loc in locations if loc)
    if not inside and not allow_external:
        raise ImportError(f"{top} resolves outside the pack ({spec.origin or 'a namespace'}), "
                          "where its signature does not cover it; only a pack the org "
                          "allowlists in packs.allow_unsigned_code may run code from the "
                          "environment")
    obj: Any = importlib.import_module(module)
    for part in attr.split("."):
        obj = getattr(obj, part)
    if not callable(obj):
        raise TypeError(f"{entry} is not callable")
    return obj, ("pack" if inside else "environment")


def _call(kind: str, fn: Any, ctx: Any, params: dict[str, Any]) -> Any:
    if kind == "connectors":
        rows = fn(ctx, str(params.get("start", "")), str(params.get("end", "")))
        return {"rows": list(rows or [])}
    if kind == "adapters":
        facts = fn(ctx, dict(params.get("context") or {}))
        return {"facts": list(facts or [])}
    receipt = fn(ctx, dict(params.get("payload") or {}))
    return {"receipt": receipt}


def main(argv: list[str] | None = None) -> int:
    # Compiled bytecode is never read from the pack: import looks for it only
    # under a fresh, empty pycache_prefix, so it always compiles the source
    # that was reviewed and hashed (install also refuses __pycache__ and
    # .pyc files outright), and -B with dont_write_bytecode writes none.
    sys.dont_write_bytecode = True
    import tempfile
    home = os.environ.get("HOME")   # the broker's throwaway HOME, removed after the call
    sys.pycache_prefix = tempfile.mkdtemp(prefix="nable-pack-pycache-",
                                          dir=home if home and os.path.isdir(home) else None)
    proto_in = os.fdopen(os.dup(0), "r", encoding="utf-8", newline="\n")
    proto_out = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    os.dup2(2, 1)
    sys.stdin = open(os.devnull, encoding="utf-8")  # noqa: SIM115 - lives as long as the process
    sys.stdout = sys.stderr
    chan = Channel(proto_in, proto_out)
    try:
        msg = chan.read()
    except ValueError:
        return 2
    if not msg or msg.get("method") != "initialize":
        return 2
    p = msg.get("params") or {}
    kind = p.get("kind")
    try:
        if kind not in METHODS:
            raise ValueError(f"kind {kind!r} is not one of {', '.join(METHODS)}")
        pack_root = Path(str(p["pack_root"]))
        audit = NetworkAudit([str(x) for x in p.get("network") or []], chan)
        audit.wrap_getaddrinfo()
        sys.addaudithook(audit.hook)
        sys.path.insert(1, str(pack_root))
        fn, loaded_from = resolve_entry(str(p["entry"]), pack_root,
                                        allow_external=bool(p.get("allow_external_code")))
        from .sdk import Context
        ctx = Context(pack_id=str(p.get("pack")), kind=kind, entry_id=str(p.get("entry_id")),
                      api_version=str(p.get("api_version")),
                      capabilities=p.get("capabilities") or {}, request=chan.request)
    except Exception as e:  # noqa: BLE001 - reported to the core, which refuses the run
        traceback.print_exc()
        chan.fail(msg.get("id"), -32010, f"{type(e).__name__}: {e}")
        return 3
    chan.respond(msg.get("id"), {"protocol": PROTOCOL, "loaded_from": loaded_from,
                                 "python": sys.version.split()[0]})
    while True:
        try:
            msg = chan.read()
        except ValueError:
            return 2
        if msg is None:
            return 0
        method, mid = msg.get("method"), msg.get("id")
        if method == "shutdown":
            chan.respond(mid, None)
            return 0
        if method != METHODS[kind]:
            chan.fail(mid, -32601, f"{method} is not a method of a {kind[:-1]}")
            continue
        try:
            result = _call(kind, fn, ctx, msg.get("params") or {})
            json.dumps(result, default=str)
        except Exception as e:  # noqa: BLE001 - a pack's failure is the call's error
            traceback.print_exc()
            chan.fail(mid, -32000, f"{type(e).__name__}: {e}")
            continue
        chan.respond(mid, result)


if __name__ == "__main__":  # pragma: no cover - the broker uses the bootstrap
    raise SystemExit(main(sys.argv[1:]))
