"""get_storage_info goes to the model, so the database URL it shows carries no credential."""
from __future__ import annotations

import pytest

from finops.storage import db


@pytest.mark.parametrize("url", [
    "postgresql://nable:S3cr3t@db.internal:5432/finops",  # pragma: allowlist secret
    "postgresql://db.internal/finops?user=nable&password=S3cr3t",
    "postgresql://:S3cr3t@db/finops",  # pragma: allowlist secret
    "postgresql+psycopg://nable:S3cr3t@db/finops?sslmode=require",  # pragma: allowlist secret
])
def test_storage_mode_never_shows_the_password(monkeypatch, url):
    monkeypatch.setenv("DATABASE_URL", url)
    info = db.storage_mode()
    assert info["mode"] == "postgres"
    assert "S3cr3t" not in info["url"]
    assert "db" in info["url"] and "finops" in info["url"]
