# SPDX-License-Identifier: Apache-2.0
"""`nable pack report`: a pack's report template filled by the core, from a
source the pack's read_data declares, and from nothing else."""
from __future__ import annotations

import pytest

from finops.packs import install as inst
from finops.packs import reports
from finops.packs.errors import PackError
from finops.setup_wizard import main
from tests import packs_support
from tests.packs_support import make_pack

packs_env = packs_support.packs_env

TEMPLATE = "Total ${ai.total_usd} for ${ai.period}; ${who.knows} stays; ${period}\n"


@pytest.fixture
def filled(monkeypatch):
    calls = []

    def fill(*, days, pack_id, today=None):
        calls.append((days, pack_id))
        return {"total_usd": "12.50", "period": "last week", "table": ["not", "a", "scalar"]}

    monkeypatch.setitem(reports.SOURCES, "ai", reports.Source("focus.cost", fill, "test"))
    return calls


def _pack(tmp_path, *, caps: str, reports_: dict[str, str]):
    src = make_pack(tmp_path / "src", capabilities=caps,
                    provides='reports = ["reports/*.md"]\n')
    for name, text in reports_.items():
        (src / "reports").mkdir(exist_ok=True)
        (src / "reports" / name).write_text(text)
    inst.install(str(src), yes=True)
    return "io.github.example/demo"


def test_a_declared_source_fills_its_placeholders(packs_env, tmp_path, filled, capsys):
    pid = _pack(tmp_path, caps='read_data = ["focus.cost"]\n', reports_={"r.md": TEMPLATE})
    r = reports.render(pid, days=7)
    assert r["text"] == "Total 12.50 for last week; ${who.knows} stays; ${period}\n"
    assert r["sources"] == ["ai"] and r["notes"] == [] and filled == [(7, pid)]
    with pytest.raises(SystemExit) as ei:
        main(["pack", "report", pid, "r"])
    assert ei.value.code == 0 and "Total 12.50" in capsys.readouterr().out


def test_an_undeclared_source_is_not_read_and_says_so(packs_env, tmp_path, filled):
    pid = _pack(tmp_path, caps='read_data = ["org.owners"]\n', reports_={"r.md": TEMPLATE})
    r = reports.render(pid)
    assert filled == [] and r["sources"] == []
    assert r["text"].startswith("Total ${ai.total_usd} for ${ai.period}")
    assert "does not declare in read_data" in r["notes"][0]


def test_picking_a_report(packs_env, tmp_path, filled, capsys):
    pid = _pack(tmp_path, caps='read_data = ["focus.cost"]\n',
                reports_={"a.md": "A ${ai.total_usd}", "b.md": "B"})
    with pytest.raises(PackError, match="has 2 reports; name one"):
        reports.render(pid)
    assert reports.render(pid, "reports/a.md")["text"] == "A 12.50"
    assert reports.render(pid, "b.md")["text"] == "B"
    with pytest.raises(PackError, match="has no report 'c'"):
        reports.render(pid, "c")
    with pytest.raises(PackError, match="not installed"):
        reports.render("io.github.example/absent")
    with pytest.raises(PackError, match="at least 1"):
        reports.render(pid, "a", days=0)
    with pytest.raises(SystemExit) as ei:
        main(["pack", "report", pid])
    assert ei.value.code == 1 and "name one" in capsys.readouterr().err
