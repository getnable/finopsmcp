"""`nable lens ...` hands over to nable-lens, before any banner or telemetry."""

from __future__ import annotations

import sys
import types

import pytest

from finops import setup_wizard


def test_lens_is_handed_to_nable_lens(monkeypatch):
    seen = []
    pkg = types.ModuleType("nable_lens")
    cli = types.ModuleType("nable_lens.cli")
    cli.main = lambda argv: seen.append(argv) or 0
    monkeypatch.setitem(sys.modules, "nable_lens", pkg)
    monkeypatch.setitem(sys.modules, "nable_lens.cli", cli)
    monkeypatch.setattr("finops.welcome.show_welcome", lambda *a, **k: pytest.fail("banner"))
    with pytest.raises(SystemExit) as exc:
        setup_wizard.main(["lens", "hook"])
    assert exc.value.code == 0
    assert seen == [["hook"]]


def test_without_nable_lens_it_says_how_to_get_it(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "nable_lens", None)
    monkeypatch.setitem(sys.modules, "nable_lens.cli", None)
    with pytest.raises(SystemExit) as exc:
        setup_wizard.main(["lens", "ui"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "uv tool install nable-lens" in err


def test_help_lists_lens(capsys):
    with pytest.raises(SystemExit):
        setup_wizard.main(["--help"])
    assert "lens" in capsys.readouterr().out
