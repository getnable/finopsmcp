# SPDX-License-Identifier: Apache-2.0
"""FINOPS_DATA_DIR, read in one place (D139).

Every module that keeps a file in nable's data directory asks env_data_dir for
it, with its own default (~/.finops for the vault, the database, the ledger
and the guard's switch; ~/.nable for the AI budget and the response cache), so
they all read the variable by one rule and the guard protects the directory
the others write:

  * unset or blank: the default;
  * a leading ~ (a settings.json env entry gives "~/x" as it is written) is
    expanded;
  * a relative path is refused: it would name a different directory from
    every working directory an agent's hook or a CLI runs in, so what nable
    wrote in one place the guard would protect in another. The default is
    used instead, and the reason comes back with it;
  * so is a value with whitespace before or after it: the shell splits
    `$FINOPS_DATA_DIR/guard-off` on it, so a command would reach the
    directory without the space while the guard reads the value as written.
    Stripping it here would protect one directory and let the shell write
    the same one unasked.

Nothing here prints or raises. A CLI says the reason once on stderr
(say_once, called by the wizard's main and never on the guard hook's path),
and `nable guard doctor` names the value and the directory used (doctor). The
module is standard library only: the guard hook reads it on every call.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

ENV = "FINOPS_DATA_DIR"
_SAID: set[str] = set()


def env_data_dir(default: Path, *, environ: Mapping[str, str] | None = None
                 ) -> tuple[Path, str | None]:
    """(the directory, None) for FINOPS_DATA_DIR, or (default, why the value
    was refused). `default` is the caller's own, used when the variable is
    unset or blank too (with None)."""
    env = os.environ if environ is None else environ
    raw = env.get(ENV, "")
    value = raw if isinstance(raw, str) else ""
    if not value.strip():
        return default, None
    if value != value.strip():
        return default, (f"{ENV}={value!r} has whitespace before or after it, which a shell "
                         f"splits off, so nable uses {default}. Set {ENV} to the path alone.")
    try:
        path = Path(value).expanduser()
    except (RuntimeError, ValueError):     # ~user with no such user, or no home
        path = Path(value)
    if not path.is_absolute():
        return default, (f"{ENV}={value!r} is not an absolute path, so nable uses {default}. "
                         f"Set {ENV} to an absolute path (a leading ~ is fine).")
    return path, None


def refused(environ: Mapping[str, str] | None = None) -> str | None:
    """Why the FINOPS_DATA_DIR value is refused, or None when it is not (or
    is unset), as the readers with ~/.finops as their default say it."""
    return env_data_dir(Path.home() / ".finops", environ=environ)[1]


def say_once(stream: TextIO | None = None, *,
             environ: Mapping[str, str] | None = None) -> bool:
    """Print why FINOPS_DATA_DIR is refused, once per process, to `stream`
    (stderr). True when it printed. For a CLI only: the guard hook's output
    is the harness's to read, and it never calls this."""
    why = refused(environ)
    if why is None or why in _SAID:
        return False
    _SAID.add(why)
    try:
        print(f"nable: {why}", file=stream or sys.stderr)
    except (OSError, ValueError):
        return False
    return True


def doctor(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """{value, dir, refused}: the variable as set (None when unset), the
    directory the data dir readers use, and why the value was refused."""
    env = os.environ if environ is None else environ
    raw = env.get(ENV)
    used, why = env_data_dir(Path.home() / ".finops", environ=env)
    return {"value": raw if raw else None, "dir": str(used), "refused": why}
