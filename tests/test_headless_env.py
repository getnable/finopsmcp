"""Configured by environment only: what headless use sets instead of a wizard.

A pipeline has no prompt to answer, so every decision is an environment
variable, and a variable that half works is worse than one that is missing.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")

_READERS = """
import json
from finops import config, license as lic, update_check
from finops.security import vault
print(json.dumps({
    "config.is_airgap": config.is_airgap(),
    "update_check._disabled": update_check._disabled(),
    "vault._keyring_disabled": vault._keyring_disabled(),
    "license._keyring_disabled": lic._keyring_disabled(),
}))
"""


@pytest.mark.parametrize("value,on", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("no", False), ("off", False), ("OFF", False),
])
def test_every_reader_of_finops_airgap_reads_it_the_same_way(tmp_path, value, on):
    """FINOPS_AIRGAP had three rules. "true" turned air-gap on but left OS
    keychain access on (vault and license compared to "1"); "off" turned it
    ON for config and telemetry (which only knew 0/false/no) and off for the
    update check. One rule now: set to anything but 0/false/no/off."""
    import json
    env = {k: v for k, v in os.environ.items()
           if k not in ("FINOPS_NO_KEYRING", "NABLE_NO_UPDATE_CHECK", "FINOPS_NO_UPDATE_CHECK",
                        "NABLE_NO_TELEMETRY", "DO_NOT_TRACK")}
    env.update(FINOPS_AIRGAP=value, HOME=str(tmp_path), PYTHONPATH=SRC)
    r = subprocess.run([sys.executable, "-c", _READERS], env=env, capture_output=True,
                       text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr
    seen = json.loads(r.stdout)
    if sys.platform == "darwin" and not on:
        # No login keychain on a CI mac also disables the keychain; not this test's subject.
        seen.pop("vault._keyring_disabled")
        seen.pop("license._keyring_disabled")
    assert seen == {k: on for k in seen}, seen
