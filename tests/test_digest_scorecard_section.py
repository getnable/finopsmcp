"""The digest's scorecard section renders from the keys Scorecard.as_dict emits."""
from __future__ import annotations

import asyncio
import json

from finops.notifications import reports
from finops.scoring import scorecard as S


class _SC:
    def __init__(self, dims):
        self._dims = dims

    def as_dict(self):
        return {"grade": "B", "total_score": 81.5, "trend": "stable", "dimensions": self._dims}


def _dim(name, available, score=80.0, grade="B"):
    return {"name": name, "display_name": name.title(), "score": score,
            "grade": grade, "data_available": available}


def test_section_renders_when_a_dimension_has_data(monkeypatch):
    monkeypatch.setattr(S, "build_scorecard",
                        lambda **k: _SC([_dim("waste", True), _dim("commitments", False)]))
    blocks, summary = asyncio.run(reports._section_scorecard({}))
    text = json.dumps(blocks)
    assert "81.5/100" in text and "Waste" in text and "Commitments: no data" in text
    assert summary == "Scorecard: B (81.5/100)"


def test_section_is_skipped_when_nothing_was_measured(monkeypatch):
    monkeypatch.setattr(S, "build_scorecard", lambda **k: _SC([_dim("waste", False)]))
    assert asyncio.run(reports._section_scorecard({})) == ([], "")
