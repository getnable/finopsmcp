# SPDX-License-Identifier: Apache-2.0
"""For pack authors: what a code pack's entry point receives.

A connector, adapter or sink is one function named in the manifest's
[provides] (`entry = "my_pack.costs:fetch"`). nable never imports it in its
own process: the broker starts `finops.packs.host` in a subprocess, which
imports the function and calls it with a Context and the call's parameters.

    connectors  fetch(ctx, start: str, end: str) -> iterable of FOCUS rows
                (dicts with FocusRecord's columns; ISO dates or datetimes)
    adapters    propose(ctx, context: dict) -> iterable of org facts
                ({"fact", "subject", "value", "source", "confidence"}); they
                land as proposals a person confirms, whatever they say
    sinks       deliver(ctx, payload: dict) -> dict receipt; payload["kind"]
                is one of the pack's declared `act` kinds

    def fetch(ctx, start, end):
        path = ctx.secret("COSTS_CSV_PATH")          # declared in [capabilities].secrets
        owners = ctx.read_data("org.owners")        # declared in read_data
        ...

The pack's process has a scrubbed environment (PATH, a throwaway HOME, LANG
and the secrets it declared), its stdout is not the protocol channel (print
freely; it goes to the pack's log), and network connections to hosts it did
not declare are refused. ctx.secret() and ctx.read_data() refuse anything the
manifest did not declare, and the core refuses it again on its side.

Context.for_testing() gives a Context with canned data and secrets, so a pack's
own tests can call its entry point without nable's broker.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import Any


class DataError(Exception):
    """The core refused a data.read: a scope that is not declared, or one this
    nable does not serve yet. `code` is the JSON-RPC error code."""

    def __init__(self, message: str, code: int = -32001):
        super().__init__(message)
        self.code = code


class Context:
    """What the host hands an entry point. Read-only by convention."""

    def __init__(self, *, pack_id: str, kind: str, entry_id: str, api_version: str,
                 capabilities: dict[str, Any] | None = None,
                 request: Callable[[str, dict[str, Any]], Any] | None = None,
                 secrets: dict[str, str] | None = None):
        self.pack_id = pack_id
        self.kind = kind
        self.entry_id = entry_id
        self.api_version = api_version
        self.capabilities: dict[str, Any] = dict(capabilities or {})
        self._request = request
        self._secrets = secrets

    def _declared(self, key: str) -> tuple[str, ...]:
        return tuple(self.capabilities.get(key) or ())

    def secret(self, name: str) -> str | None:
        """A secret the manifest declares, passed in by the core; None when
        the org has not set it. Raises PermissionError for an undeclared name."""
        if name not in self._declared("secrets"):
            raise PermissionError(f"{name} is not in this pack's declared secrets")
        if self._secrets is not None:
            return self._secrets.get(name)
        return os.environ.get(name)

    def read_data(self, scope: str, query: dict[str, Any] | None = None) -> Any:
        """Ask the core for data in a declared read_data scope. Raises
        DataError when the scope is not declared or not available."""
        if scope not in self._declared("read_data"):
            raise DataError(f"{scope} is not in this pack's declared read_data")
        if self._request is None:
            raise DataError("no broker is connected, so no data can be read")
        return self._request("data.read", {"scope": scope, "query": dict(query or {})})

    def log(self, message: str) -> None:
        """A line in the pack's log (its stderr, which the core keeps, truncated)."""
        print(message, file=sys.stderr, flush=True)

    @classmethod
    def for_testing(cls, *, pack_id: str = "io.github.example/test-pack",
                    kind: str = "connectors", entry_id: str = "test",
                    capabilities: dict[str, Any] | None = None,
                    data: dict[str, Any] | None = None,
                    secrets: dict[str, str] | None = None) -> Context:
        """A Context for a pack's own unit tests. `data` maps a scope to what
        read_data returns (or to a function of the query)."""
        canned = dict(data or {})

        def request(method: str, params: dict[str, Any]) -> Any:
            scope = params.get("scope")
            if scope not in canned:
                raise DataError(f"{scope} has no test data")
            val = canned[scope]
            return val(params.get("query") or {}) if callable(val) else val

        from . import API_VERSION
        return cls(pack_id=pack_id, kind=kind, entry_id=entry_id, api_version=API_VERSION,
                   capabilities=capabilities, request=request, secrets=dict(secrets or {}))
