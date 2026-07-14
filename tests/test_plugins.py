# SPDX-License-Identifier: Apache-2.0
"""The enterprise plugin seam: providers register on top of the core, and a
broken provider can never take the core server down."""
import finops.plugins as P


class _FakeEP:
    def __init__(self, name, fn):
        self.name = name
        self._fn = fn

    def load(self):
        return self._fn


def _reset():
    P._loaded.clear()


def test_no_plugins_is_noop(monkeypatch):
    _reset()
    monkeypatch.setattr(P, "entry_points", lambda group=None: [])
    assert P.load_plugins(object()) == []
    assert P.loaded_plugins() == []


def test_plugin_registers_against_mcp(monkeypatch):
    _reset()
    seen = {}

    def register(mcp):
        seen["mcp"] = mcp

    monkeypatch.setattr(P, "entry_points", lambda group=None: [_FakeEP("enterprise", register)])
    sentinel = object()
    loaded = P.load_plugins(sentinel)
    assert loaded == ["enterprise"]
    assert seen["mcp"] is sentinel
    assert P.loaded_plugins() == ["enterprise"]


def test_broken_plugin_is_skipped_not_fatal(monkeypatch):
    _reset()

    def boom(mcp):
        raise RuntimeError("provider blew up")

    monkeypatch.setattr(P, "entry_points", lambda group=None: [_FakeEP("bad", boom)])
    # Must not raise, and must not record the plugin as loaded.
    assert P.load_plugins(object()) == []
    assert P.loaded_plugins() == []


def test_load_is_idempotent(monkeypatch):
    _reset()
    calls = []

    def register(mcp):
        calls.append(1)

    monkeypatch.setattr(P, "entry_points", lambda group=None: [_FakeEP("enterprise", register)])
    P.load_plugins(object())
    P.load_plugins(object())  # second call: already loaded, skip
    assert calls == [1]
    assert P.loaded_plugins() == ["enterprise"]
