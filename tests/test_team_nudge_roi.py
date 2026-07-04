"""The upgrade nudge should lead with a real savings number when nable has
already found enough to dwarf the $25/mo plan, and stay quiet about ROI when the
number is weak or the user already pays."""
from finops import server


class _Lic:
    def __init__(self, mode):
        self.mode = mode


def test_nudge_is_none_for_paying_users(monkeypatch):
    monkeypatch.setattr(server, "get_status", lambda: _Lic("pro"))
    assert server._team_nudge("Upgrade for ticket auto-creation.") is None
    monkeypatch.setattr(server, "get_status", lambda: _Lic("trial"))
    assert server._team_nudge("Upgrade for ticket auto-creation.") is None


def test_nudge_plain_when_no_savings_found(monkeypatch):
    monkeypatch.setattr(server, "get_status", lambda: _Lic("free"))
    monkeypatch.setattr(server, "_savings_found_monthly", lambda: 0.0)
    out = server._team_nudge("Auto-create tickets with Team.")
    assert "Auto-create tickets with Team." in out
    assert "x the" not in out  # no ROI multiplier when nothing has been found


def test_nudge_leads_with_roi_when_compelling(monkeypatch):
    monkeypatch.setattr(server, "get_status", lambda: _Lic("free"))
    monkeypatch.setattr(server, "_savings_found_monthly", lambda: 8432.0)
    out = server._team_nudge("Auto-create tickets with Team.")
    assert "$8,432/mo" in out
    assert "337x" in out  # 8432 / 25 = 337.3 -> 337x
    assert "$25/mo Team plan" in out
    assert "Auto-create tickets with Team." in out


def test_nudge_suppresses_weak_multiplier(monkeypatch):
    # Found less than the plan price: a "0.6x" pitch would hurt, so suppress it.
    monkeypatch.setattr(server, "get_status", lambda: _Lic("free"))
    monkeypatch.setattr(server, "_savings_found_monthly", lambda: 12.0)
    out = server._team_nudge("Upgrade to keep going.")
    assert "x the" not in out
    assert "Upgrade to keep going." in out


def test_savings_reader_never_raises(monkeypatch):
    # If the DB read blows up, the helper returns 0.0 and the nudge stays plain.
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(server, "get_status", lambda: _Lic("free"))
    monkeypatch.setattr(server, "_savings_found_monthly", _boom)
    # _team_nudge wraps everything; a failing reader must not break the response.
    assert server._team_nudge("Upgrade.") is None or "Upgrade." in server._team_nudge("Upgrade.")
