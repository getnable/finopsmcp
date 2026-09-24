"""Open a file nable wrote (a report, a dashboard) in the user's default app.

One helper because the two call sites had drifted: one handled macOS, Linux
and Windows, the other hard-coded macOS `open` and did nothing elsewhere. Both
also launched the browser with the decrypted vault in its environment.
"""
from __future__ import annotations

import os
import subprocess
import sys


def open_local_file(path: str) -> None:
    """Best-effort: never raises, because opening a viewer is a convenience."""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606, no shell involved
            return
        from .security.vault import child_env
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, path], env=child_env(),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
