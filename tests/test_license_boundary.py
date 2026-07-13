"""The open-core license boundary is a promise: everything a local user runs is
Apache-2.0, and only the hosted control plane (server_web.py, auth/, billing/) is
Elastic-2.0. These tests lock that boundary so a new file can't silently land on
the wrong side of it, the same self-healing pattern as the tool_surface map.
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src" / "finops"
_SPDX = "SPDX-License-Identifier: LicenseRef-Elastic-2.0"

# The ONLY paths that stay Elastic-2.0. Everything else under src/finops is Apache-2.0.
_CLOSED_GLOBS = ("server_web.py", "auth/**/*.py", "billing/**/*.py")


def _closed_files() -> set[Path]:
    out: set[Path] = set()
    for pattern in _CLOSED_GLOBS:
        out.update(p for p in _SRC.glob(pattern) if "__pycache__" not in p.parts)
    return out


def test_closed_files_carry_elastic_header():
    closed = _closed_files()
    assert closed, "expected to find the Elastic-licensed files; glob matched nothing"
    missing = [p.relative_to(_ROOT) for p in closed if _SPDX not in p.read_text()]
    assert not missing, f"Elastic-licensed files missing the SPDX header: {missing}"


def test_no_apache_file_claims_elastic():
    # Nothing outside the closed set may carry the Elastic header — that would
    # quietly pull an open file into the proprietary bucket.
    closed = _closed_files()
    stray = [
        p.relative_to(_ROOT)
        for p in _SRC.rglob("*.py")
        if "__pycache__" not in p.parts and p not in closed and _SPDX in p.read_text()
    ]
    assert not stray, f"these Apache files wrongly carry the Elastic header: {stray}"


def test_license_files_present_and_correct():
    assert "Apache License" in (_ROOT / "LICENSE").read_text(), "LICENSE must be Apache-2.0"
    assert "Elastic License 2.0" in (_ROOT / "LICENSE.enterprise").read_text()
    notice = (_ROOT / "NOTICE").read_text()
    for path in ("server_web.py", "auth/", "billing/"):
        assert path in notice, f"NOTICE must document the closed path {path}"
