"""Two scheduled runs at once on one fresh data directory.

Headless use means cron and CI, and two jobs firing in the same minute on a
box nable has never run on is the ordinary case, not an edge. Both runs must
succeed, and neither may leave state the other cannot read.

Each test starts N real processes, lets every one finish its imports, then
releases them together through a file barrier, so they reach the first-run
code at the same moment the way two cron jobs do.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
PROCS = 8
ROUNDS = 3

_BARRIER = """
import sys, time
from pathlib import Path
go = Path(sys.argv[1])
deadline = time.time() + 60
while not go.exists() and time.time() < deadline:
    time.sleep(0.001)
"""


def _env(home: Path, data: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("FINOPS_DB_PATH", "DATABASE_URL", "FINOPS_PROFILE",
                        "FINOPS_VAULT_KEY", "FINOPS_VAULT_KEYCHAIN_ONLY")}
    env.update(HOME=str(home), FINOPS_DATA_DIR=str(data), PYTHONPATH=SRC,
               FINOPS_NO_KEYRING="1", NABLE_NO_TELEMETRY="1")
    return env


def _together(tmp_path: Path, round_no: int, code: str, n: int = PROCS
              ) -> tuple[dict[str, str], list[subprocess.CompletedProcess]]:
    home = tmp_path / f"home{round_no}"
    data = tmp_path / f"data{round_no}"
    home.mkdir()
    go = tmp_path / f"go{round_no}"
    env = _env(home, data)
    procs = [subprocess.Popen([sys.executable, "-c", code, str(go), str(i)],
                              env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for i in range(n)]
    time.sleep(2.0)          # let every child finish importing and reach the barrier
    go.touch()
    done = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        done.append(subprocess.CompletedProcess(p.args, p.returncode, out, err))
    return env, done


def test_concurrent_first_runs_all_get_a_working_database(tmp_path):
    """Before the fix one run in a few died on `table cost_snapshots already
    exists` (sqlite3.OperationalError): two processes each saw no table and
    each issued CREATE TABLE. `nable budget refresh` exited 1."""
    code = ("from finops.storage import db\n" + _BARRIER +
            "db.get_engine()\nprint('ok')\n")
    failures = []
    for r in range(ROUNDS):
        _, done = _together(tmp_path, r, code)
        failures += [[ln for ln in d.stderr.splitlines() if "Error" in ln][-1:]
                     for d in done if d.returncode != 0]
    assert not failures, failures


def test_concurrent_first_runs_agree_on_one_vault_key(tmp_path):
    """Before the fix each racer generated its own master key and the last
    writer's file won, so a credential stored by any other racer could never
    be decrypted again ("Failed to decrypt credential - check vault key")."""
    code = ("from finops.security.vault import Vault\n" + _BARRIER +
            "v = Vault.default()\n"
            "v.store('K' + sys.argv[2], 'value-' + sys.argv[2])\n"
            "print('ok')\n")
    reader = ("from finops.security.vault import Vault, VaultError\n"
              "v = Vault.default()\n"
              "bad = []\n"
              "for k in v.list_keys():\n"
              "    try:\n"
              "        v.get(k)\n"
              "    except VaultError:\n"
              "        bad.append(k)\n"
              "print(len(v.list_keys()), ','.join(bad))\n")
    for r in range(ROUNDS):
        env, done = _together(tmp_path, r, code)
        assert all(d.returncode == 0 for d in done), [d.stderr[-400:] for d in done]
        out = subprocess.run([sys.executable, "-c", reader], env=env, capture_output=True,
                             text=True, timeout=60, stdin=subprocess.DEVNULL)
        stored, _, unreadable = out.stdout.strip().partition(" ")
        assert stored == str(PROCS), out.stdout + out.stderr
        assert unreadable == "", f"round {r}: undecryptable after a concurrent first run: {unreadable}"


def test_a_new_database_is_never_open_to_other_users(tmp_path, monkeypatch):
    """SQLite made the cost history file (and its -wal and -shm files) at the
    process umask, 0644 on most machines, and it was clamped to 0600 only
    after the schema was built, outside the cross-process lock. A second run
    starting meanwhile, a cron job beside `nable doctor`, found every local
    user able to read it, and the doctor failed its permissions check: two
    of eight concurrent `nable doctor --json` runs exited 1."""
    import stat

    import finops.storage.db as db

    path = tmp_path / "finops.db"
    monkeypatch.setenv("FINOPS_DB_PATH", str(path))
    seen: dict[str, int] = {}
    real = db.metadata.create_all

    def create_all_then_look(engine, *a, **k):
        real(engine, *a, **k)
        for f in tmp_path.iterdir():
            seen[f.name] = stat.S_IMODE(f.stat().st_mode)

    monkeypatch.setattr(db.metadata, "create_all", create_all_then_look)
    old = os.umask(0o022)
    try:
        db._build_engine().dispose()
    finally:
        os.umask(old)
    files = {name: mode for name, mode in seen.items() if name.startswith("finops.db")}
    assert files, seen
    assert all(mode == 0o600 for mode in files.values()), {k: oct(v) for k, v in files.items()}
