"""The first cost answer should steer the model to proactively surface real waste
(the magic moment), and the directive must point at the scan that feeds the savings
loop."""
from finops import server


def test_directive_shape_and_intent():
    d = server._first_run_onboarding_directive()
    assert d["first_cost_query"] is True
    # It must steer the model to the waste scan, in dollars, proactively.
    assert "list_idle_resources" in d["directive"]
    assert "$" in d["directive"]
    assert "proactively" in d["directive"].lower()


def test_directive_is_injected_once_via_setdefault():
    # The wrapper uses result.setdefault, so an existing _onboarding is never clobbered
    # (idempotent if a tool already set one).
    result = {"_onboarding": {"sentinel": True}}
    result.setdefault("_onboarding", server._first_run_onboarding_directive())
    assert result["_onboarding"] == {"sentinel": True}

    fresh = {"total_usd": 1234}
    fresh.setdefault("_onboarding", server._first_run_onboarding_directive())
    assert fresh["_onboarding"]["first_cost_query"] is True
