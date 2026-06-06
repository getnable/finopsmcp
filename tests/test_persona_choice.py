"""The role prompt must accept the role name, not just a number, and report a
miss so the caller can warn instead of silently defaulting. Guards the bug where
typing 'finops' put a FinOps analyst in the Engineer persona."""
from finops.setup_wizard import _resolve_persona_choice

KEYS = ["engineer", "finops", "finance", "platform"]


def test_number_selects_by_position():
    assert _resolve_persona_choice("2", KEYS, "engineer") == ("finops", True)


def test_empty_uses_default_one():
    # _prompt fills the default "1" before this is called, but be safe.
    assert _resolve_persona_choice("1", KEYS, "engineer") == ("engineer", True)


def test_role_name_maps_directly():
    # The exact bug: typing the role name must select that role, not default.
    assert _resolve_persona_choice("finops", KEYS, "engineer") == ("finops", True)
    assert _resolve_persona_choice("FinOps", KEYS, "engineer") == ("finops", True)


def test_keyword_match_against_label():
    # "ops" appears in the FinOps label "FinOps / Cloud Ops".
    chosen, matched = _resolve_persona_choice("ops", KEYS, "engineer")
    assert matched is True
    assert chosen in KEYS


def test_unrecognized_reports_miss_and_keeps_current():
    chosen, matched = _resolve_persona_choice("zzzz", KEYS, "platform")
    assert matched is False
    assert chosen == "platform"  # falls back to current, but caller is told to warn
