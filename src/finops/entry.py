"""
Light console-script entry point.

`nable` and `finops-mcp` resolve here so a human typing `nable scan` never pays
the ~0.9s finops.server import (FastMCP + 190 tool registrations) before the
first line of output. Routing matches server.py's main() exactly:

    argv present            -> the CLI wizard (scan, welcome, doctor, ...)
    bare run at a TTY       -> onboarding, not a stdio server that hangs
    no args + piped stdio   -> the MCP server (Claude Desktop, Cursor)

finops.server:main keeps its own copy of this routing for old `nable` shim
installs that hardcode it; the two must stay behaviorally identical (the pipe
regression test in tests/test_entry.py pins both).
"""

from __future__ import annotations

import sys


def main() -> None:
    argv = sys.argv[1:]
    if argv:
        from .setup_wizard import main as setup_main
        setup_main(argv)
        return
    if sys.stdin.isatty():
        from .setup_wizard import main as setup_main
        setup_main(["welcome"])
        return
    from .server import main as server_main
    server_main()


if __name__ == "__main__":
    # Support `python -m finops.entry`, matching finops.server and
    # finops.setup_wizard. Without this the module import is a silent no-op.
    main()
