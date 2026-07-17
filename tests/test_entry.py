"""
Entry-point routing tests.

finops/entry.py is the console-script target for `nable` and `finops-mcp`.
Its routing must stay behaviorally identical to finops.server:main's own
argv/TTY routing (old `nable` shim installs still resolve there). The pipe
regression test pins BOTH dispatchers: piped no-args stdin must reach the MCP
server, or every installed editor config breaks on upgrade.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import finops.entry as entry


def _run_with(argv: list[str], isatty: bool):
    """Run entry.main() with patched argv/TTY and stubbed heavy targets."""
    wizard = MagicMock()
    server = MagicMock()
    with (
        patch.object(sys, "argv", ["nable", *argv]),
        patch.object(sys.stdin, "isatty", return_value=isatty),
        patch.dict(
            sys.modules,
            {
                "finops.setup_wizard": MagicMock(main=wizard),
                "finops.server": MagicMock(main=server),
            },
        ),
    ):
        entry.main()
    return wizard, server


def test_argv_routes_to_wizard():
    wizard, server = _run_with(["scan"], isatty=True)
    wizard.assert_called_once_with(["scan"])
    server.assert_not_called()


def test_argv_routes_to_wizard_even_when_piped():
    # `echo | nable doctor` is still a human/CI invocation, not an MCP client.
    wizard, server = _run_with(["doctor"], isatty=False)
    wizard.assert_called_once_with(["doctor"])
    server.assert_not_called()


def test_bare_tty_routes_to_welcome():
    # Matches server.py's routing: bare terminal run launches onboarding,
    # never a stdio server that silently hangs.
    wizard, server = _run_with([], isatty=True)
    wizard.assert_called_once_with(["welcome"])
    server.assert_not_called()


def test_pipe_no_args_serves_mcp():
    # CRITICAL regression: every installed editor config (Claude Desktop,
    # Cursor) launches with piped stdio and no args. That must reach the
    # MCP server, not the wizard.
    wizard, server = _run_with([], isatty=False)
    server.assert_called_once_with()
    wizard.assert_not_called()


def test_server_main_pipe_routing_matches_entry():
    # The OTHER dispatcher: old `nable` shims hardcode finops.server:main.
    # Its piped no-args path must serve MCP too. We stop the run at the
    # server-serving boundary (mcp.run) rather than starting a real server.
    import finops.server as srv

    with (
        patch.object(sys, "argv", ["nable"]),
        patch.object(sys.stdin, "isatty", return_value=False),
        patch.object(srv, "mcp") as mock_mcp,
    ):
        mock_mcp.run.side_effect = SystemExit(0)
        try:
            srv.main()
        except SystemExit:
            pass
        assert mock_mcp.run.called, (
            "finops.server:main piped no-args no longer reaches mcp.run(); "
            "old shim installs would break"
        )


def test_server_main_argv_routing_matches_entry():
    import finops.server as srv

    with (
        patch.object(sys, "argv", ["nable", "doctor"]),
        patch("finops.setup_wizard.main") as wizard,
    ):
        srv.main()
        wizard.assert_called_once_with(["doctor"])


def test_entry_module_is_light():
    # The whole point of entry.py: importing it must not drag in the server.
    import subprocess

    code = (
        "import sys; import finops.entry; "
        "assert 'finops.server' not in sys.modules, 'entry imported server eagerly'; "
        "assert 'mcp' not in sys.modules, 'entry imported FastMCP eagerly'; "
        "print('light')"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    assert "light" in out.stdout


def test_python_dash_m_entry_actually_runs():
    # `python -m finops.entry scan --demo` must execute main(), not silently
    # no-op. Regression guard for the missing __main__ block: the console
    # scripts call finops.entry:main directly, but the -m form (and anyone
    # muscle-memorying `python -m finops.server`'s documented pattern) needs
    # the __main__ guard or it exits 0 having printed nothing.
    import subprocess

    out = subprocess.run(
        [sys.executable, "-m", "finops.entry", "scan", "--demo"],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "NABLE_NO_TELEMETRY": "1", "NO_COLOR": "1"},
    )
    assert out.returncode == 0, out.stderr
    assert "nable scan" in out.stdout and "recoverable" in out.stdout
