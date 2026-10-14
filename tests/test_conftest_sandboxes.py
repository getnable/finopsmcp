"""The suite-wide sandboxes in conftest.py: isolated, fresh, and cheap.

Every test gets its own empty guard ledger, data dir, Claude user dir, org
model dir, packs root and CODEX_HOME. They used to be six
tmp_path_factory.mktemp calls per test, each of which lists the whole
basetemp to number its directory, so fixture setup grew with every test the
run had made and became most of the suite's wall time. They now live under
one directory per session, and a test adds nothing to basetemp itself.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

_SEEN: dict[str, object] = {}


def _sandboxes() -> dict[str, Path]:
    import finops.guard_ledger as ledger
    import finops.guard_plugin as gp
    from finops.packs import store
    return {"ledger": Path(ledger._path_override).parent,
            "data": Path(gp._data_root_override), "user": Path(gp._user_dir_override),
            "org": Path(os.environ["FINOPS_ORG_DIR"]),
            "packs": Path(store._root_override).parent,
            "codex": Path(os.environ["CODEX_HOME"])}


def test_each_sandbox_is_its_own_empty_directory_under_basetemp(tmp_path_factory):
    boxes = _sandboxes()
    base = tmp_path_factory.getbasetemp()
    assert len(set(boxes.values())) == len(boxes)
    for name, path in boxes.items():
        assert path.is_dir(), name
        assert list(path.iterdir()) == [], name
        assert path.is_relative_to(base), name
    # The files the overrides name do not exist yet, as before.
    import finops.guard_ledger as ledger
    from finops.packs import store
    assert not Path(ledger._path_override).exists()
    assert not Path(store._root_override).exists()
    (boxes["org"] / "left-behind.yaml").write_text("- fact: owner\n")
    _SEEN["boxes"] = boxes
    _SEEN["count"] = len(list(base.iterdir()))


def test_the_next_test_gets_fresh_sandboxes_and_adds_nothing_to_basetemp(tmp_path_factory):
    if "boxes" not in _SEEN:
        pytest.skip("runs after the test above")
    boxes = _sandboxes()
    for name, path in boxes.items():
        assert path != _SEEN["boxes"][name], name
        assert list(path.iterdir()) == [], name
    assert len(list(tmp_path_factory.getbasetemp().iterdir())) == _SEEN["count"]
