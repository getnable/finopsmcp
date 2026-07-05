"""Staleness self-check: warn when this process is running an old finops-mcp.

Exists because staleness was SILENT in the wild: the `nable` launcher shim
allowed Python 3.10 while finops-mcp 0.8.90+ requires 3.11, so on 3.10 machines
the resolver quietly served 0.8.89 forever. Five weeks of onboarding work never
reached those users and nothing ever told them. The shim is fixed (0.1.1), but
resolver skew, cached uvx environments, and pinned configs can always recreate
the situation, so the product now notices on its own.

Design constraints:
  - Never blocks. The check runs with a 2 second cap and swallows everything.
  - Never phones home beyond PyPI's public JSON endpoint, and respects
    FINOPS_AIRGAP and FINOPS_NO_UPDATE_CHECK (plus NABLE_NO_TELEMETRY for the
    users who expressed the strictest preference).
  - Never nags: at most one check per process, and the caller decides where the
    one-line note surfaces (end of welcome, server startup log).
"""
from __future__ import annotations

import os

_checked: list[str | None] = []  # memoized result for the life of the process


def _disabled() -> bool:
    for var in ("FINOPS_AIRGAP", "FINOPS_NO_UPDATE_CHECK", "NABLE_NO_TELEMETRY"):
        if os.environ.get(var, "").strip().lower() in ("1", "true", "yes"):
            return True
    return False


def _parse(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.strip().split("."))
    except ValueError:
        return ()


def latest_version(timeout: float = 2.0) -> str | None:
    """Latest finops-mcp on PyPI, or None on any failure. Capped and quiet."""
    if _disabled():
        return None
    try:
        import httpx

        r = httpx.get("https://pypi.org/pypi/finops-mcp/json", timeout=timeout)
        return r.json()["info"]["version"]
    except Exception:
        return None


def staleness_note() -> str | None:
    """One human sentence when this process runs an outdated build, else None.

    Memoized: the network is hit at most once per process.
    """
    if _checked:
        return _checked[0]
    note: str | None = None
    try:
        from . import __version__

        latest = latest_version()
        if latest and _parse(latest) > _parse(__version__) != ():
            note = (
                f"nable {latest} is out (you are on {__version__}). "
                f"Your launcher resolved an older build; run: finops upgrade"
            )
    except Exception:
        note = None
    _checked.append(note)
    return note
