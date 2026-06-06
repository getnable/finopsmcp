"""Free users get a contextual Team upsell keyed to what they asked, at most once
per topic per session. Paying users and unmapped tools get nothing."""
from finops import server


class _Lic:
    def __init__(self, mode):
        self.mode = mode


def test_free_user_gets_topic_tip_once(monkeypatch):
    server._team_tips_shown.clear()
    monkeypatch.setattr(server, "get_status", lambda: _Lic("free"))
    tip = server._maybe_team_tip("get_anomalies")
    assert tip is not None
    assert "Slack" in tip["missing_with_team"]
    assert f"{server._TEAM_MONTHLY_USD:.0f}/seat/mo" in tip["upgrade"]
    # same topic again (different tool, same "anomaly" topic) -> suppressed
    assert server._maybe_team_tip("get_account_anomalies") is None


def test_distinct_topics_each_nudge_once(monkeypatch):
    server._team_tips_shown.clear()
    monkeypatch.setattr(server, "get_status", lambda: _Lic("free"))
    assert server._maybe_team_tip("get_anomalies") is not None                    # anomaly
    assert server._maybe_team_tip("get_rightsizing_recommendations") is not None  # rightsizing
    assert server._maybe_team_tip("get_costs_by_team") is not None                # attribution
    assert server._maybe_team_tip("get_commitment_analysis") is not None          # commitment


def test_paying_users_get_nothing(monkeypatch):
    for mode in ("pro", "trial", "enterprise"):
        server._team_tips_shown.clear()
        monkeypatch.setattr(server, "get_status", lambda m=mode: _Lic(m))
        assert server._maybe_team_tip("get_anomalies") is None


def test_unmapped_tools_no_tip(monkeypatch):
    server._team_tips_shown.clear()
    monkeypatch.setattr(server, "get_status", lambda: _Lic("free"))
    assert server._maybe_team_tip("whoami") is None
    # a plain cost query is intentionally NOT mapped, so we don't nudge on every call
    assert server._maybe_team_tip("get_cost_summary") is None


def test_status_failure_is_silent(monkeypatch):
    server._team_tips_shown.clear()
    def _boom():
        raise RuntimeError("license check down")
    monkeypatch.setattr(server, "get_status", _boom)
    assert server._maybe_team_tip("get_anomalies") is None
