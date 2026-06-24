"""Onboarding stays seamless.

The hero command (`uvx nable`, which runs bare `finops`) must launch the guided
welcome flow, not the persona quiz + 26-provider menu. The first run must never
crash on a broken import, and next-step hints must match how nable was launched
(a `uvx nable` run is ephemeral, so `finops doctor` would be "command not found").
"""
from __future__ import annotations

import contextlib
import io
import sys

import finops.setup_wizard as sw
import finops.welcome as w


def _route(argv, monkeypatch):
    """Run main() with the welcome flow and the persona menu stubbed, so routing
    is observable without writing real editor configs or blocking on input."""
    calls: list[str] = []
    monkeypatch.setattr(w, "run_welcome_flow", lambda *a, **k: calls.append("welcome"))

    def _stop_menu(*a, **k):
        calls.append("menu")
        raise SystemExit(0)

    monkeypatch.setattr(sw, "_wizard_select_persona", _stop_menu)
    with contextlib.suppress(SystemExit), contextlib.redirect_stdout(io.StringIO()):
        sw.main(argv)
    return calls


def test_bare_finops_launches_welcome(monkeypatch):
    # `uvx nable` runs bare `finops`: it must launch the guided flow, not the menu.
    assert _route([], monkeypatch) == ["welcome"]


def test_explicit_setup_shows_provider_menu(monkeypatch):
    # `finops setup` keeps the power-user provider menu, distinct from first-run.
    assert _route(["setup"], monkeypatch) == ["menu"]


def test_setup_with_provider_dispatches_directly(monkeypatch):
    # `finops setup aws` goes straight to the provider, never welcome or the menu.
    calls: list[str] = []
    monkeypatch.setattr(w, "run_welcome_flow", lambda *a, **k: calls.append("welcome"))
    monkeypatch.setattr(sw, "_wizard_select_persona", lambda *a, **k: calls.append("menu"))
    monkeypatch.setattr(sw, "setup_aws_account", lambda *a, **k: calls.append("aws"))
    with contextlib.redirect_stdout(io.StringIO()):
        sw.main(["setup", "aws"])
    assert calls == ["aws"]


def test_hints_match_launch_method(monkeypatch):
    monkeypatch.setattr(sys, "prefix", "/Library/Frameworks/Python.framework/Versions/3.12")
    assert w._cli("doctor") == "finops doctor"
    assert w._cli("setup aws") == "finops setup aws"
    # uvx/uv runs live under the uv cache; hint the brand command that works there.
    monkeypatch.setattr(sys, "prefix", "/Users/x/.cache/uv/archive-v0/abc123")
    assert w._cli("doctor") == "uvx nable doctor"
    assert w._cli("welcome") == "uvx nable welcome"


def test_value_moment_never_crashes_onboarding(monkeypatch):
    # A failed import or scan inside the value moment must degrade to False, never
    # raise, or the welcome flow would die with a traceback at the first number.
    def boom(*a, **k):
        raise ImportError("simulated arch-mismatched native dep")

    monkeypatch.setattr(w, "_value_moment_body", boom)
    assert w._show_value_moment(demo=False) is False
    assert w._show_value_moment(demo=True) is False
