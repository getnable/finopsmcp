"""Terminal hyperlinks degrade safely.

link() must emit OSC 8 only when color is on, and the visible text must always
be the URL itself so terminals that ignore OSC 8 (and piped logs) still show a
copyable address.
"""
import finops.welcome as w


def test_link_plain_when_no_color(monkeypatch):
    monkeypatch.setattr(w, "_USE_COLOR", False)
    assert w.link("https://getnable.com") == "https://getnable.com"


def test_link_osc8_when_color(monkeypatch):
    monkeypatch.setattr(w, "_USE_COLOR", True)
    out = w.link("https://getnable.com")
    assert out.startswith("\033]8;;https://getnable.com\033\\")
    assert out.endswith("\033]8;;\033\\")
    assert "https://getnable.com" in out  # URL stays visible
