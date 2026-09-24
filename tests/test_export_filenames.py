"""Model-supplied titles cannot steer an export outside the exports directory."""
from __future__ import annotations

from pathlib import Path

import pytest

from finops.reporting import exporter


@pytest.mark.parametrize("title", ["..\\..\\Startup\\x", "C:\\Users\\a\\x", "../../etc/x", "a b/c", ""])
def test_export_stays_in_the_exports_directory(tmp_path, monkeypatch, title):
    monkeypatch.setattr(exporter, "_export_dir", lambda: tmp_path)
    out = exporter.write_report(title=title, period_start="2026-09-01",
                                period_end="2026-09-23", sections={}, formats=["html"])
    written = Path(out["html"])
    assert written.parent.resolve() == tmp_path.resolve()
    assert "\\" not in written.name and ".." not in written.name and ":" not in written.name
