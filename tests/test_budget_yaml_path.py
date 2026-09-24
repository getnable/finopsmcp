"""sync_budgets_from_yaml only opens YAML files."""
from __future__ import annotations

import asyncio

import finops.server  # noqa: F401  (registers tools; import order matters)
from finops.tools import budgets as B

SYNC = getattr(B.sync_budgets_from_yaml, "fn", B.sync_budgets_from_yaml)


def test_non_yaml_path_is_refused(tmp_path):
    secret = tmp_path / "id_rsa"
    secret.write_text("-----BEGIN PRIVATE KEY-----\nabc: [\n")
    out = asyncio.run(SYNC(str(secret)))
    assert "error" in out and ".yml" in out["error"]
    assert "PRIVATE" not in str(out)
