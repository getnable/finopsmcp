"""
install_completed must count real human installs, not automation.

It used to fire on ANY first CLI run, so cache-warm subprocesses, piped
invocations, CI runners, and fresh uvx environments each logged a phantom
install with a throwaway id. These tests lock the fix: only an interactive,
non-CI first run counts, and CI sends no telemetry at all.
"""
from __future__ import annotations

from unittest.mock import patch

import finops.telemetry as tel
import finops.welcome as w


def _clear_ci(monkeypatch):
    for v in tel._CI_ENV_VARS:
        monkeypatch.delenv(v, raising=False)


def test_is_ci_detects_runners(monkeypatch):
    _clear_ci(monkeypatch)
    assert not tel.is_ci()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert tel.is_ci()


def test_ci_opts_out_of_all_telemetry(monkeypatch):
    monkeypatch.setattr(tel, "_POSTHOG_KEY", "phc_x")
    monkeypatch.delenv("FINOPS_AIRGAP", raising=False)
    monkeypatch.delenv(tel._OPT_OUT_ENV, raising=False)
    _clear_ci(monkeypatch)
    assert not tel._is_opted_out()
    monkeypatch.setenv("CI", "1")
    assert tel._is_opted_out()


def _run_show_welcome(monkeypatch, *, first_run, ci, stdin_tty, stdout_tty):
    _clear_ci(monkeypatch)
    if ci:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(w, "_is_first_run", lambda: first_run)
    monkeypatch.setattr(w, "_mark_welcomed", lambda: None)
    monkeypatch.setattr(w, "_print_header", lambda: None)
    monkeypatch.setattr("sys.stdin.isatty", lambda: stdin_tty, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: stdout_tty, raising=False)
    fired = []
    monkeypatch.setattr(w, "_fire_telemetry", lambda e, p: fired.append((e, p)))
    # Stop after the gated telemetry so we don't print the whole welcome.
    monkeypatch.setattr(w, "_line", lambda *a, **k: None)
    monkeypatch.setattr(w, "_blank", lambda *a, **k: None)
    monkeypatch.setattr(w, "bold", lambda t: t)
    w.show_welcome()
    return fired


def test_interactive_first_run_fires_install(monkeypatch):
    fired = _run_show_welcome(monkeypatch, first_run=True, ci=False, stdin_tty=True, stdout_tty=True)
    assert [e for e, _ in fired] == ["install_completed"]


def test_non_interactive_first_run_does_not_fire(monkeypatch):
    # piped stdin (cache-warm subprocess, scripts) -> not an install
    fired = _run_show_welcome(monkeypatch, first_run=True, ci=False, stdin_tty=False, stdout_tty=True)
    assert fired == []


def test_ci_first_run_does_not_fire(monkeypatch):
    fired = _run_show_welcome(monkeypatch, first_run=True, ci=True, stdin_tty=True, stdout_tty=True)
    assert fired == []


def test_already_welcomed_does_not_fire(monkeypatch):
    fired = _run_show_welcome(monkeypatch, first_run=False, ci=False, stdin_tty=True, stdout_tty=True)
    assert fired == []


def test_non_interactive_leaves_sentinel_unset(monkeypatch):
    """A piped first run must NOT consume the first-run sentinel, so the next
    genuine human run still counts."""
    marked = []
    _clear_ci(monkeypatch)
    monkeypatch.setattr(w, "_is_first_run", lambda: True)
    monkeypatch.setattr(w, "_mark_welcomed", lambda: marked.append(True))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    monkeypatch.setattr(w, "_fire_telemetry", lambda e, p: None)
    w.show_welcome()
    assert marked == []  # sentinel never marked on a non-interactive run
