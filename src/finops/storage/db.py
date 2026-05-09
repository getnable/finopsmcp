from __future__ import annotations

import os
import stat
from pathlib import Path

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer, MetaData,
    String, Table, Text, create_engine, text,
)
from sqlalchemy.engine import Engine

_DATA_DIR: Path | None = None
_ENGINE: Engine | None = None

metadata = MetaData()

cost_snapshots = Table(
    "cost_snapshots", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("provider", String(64), nullable=False),
    Column("service", String(256), nullable=False),
    Column("account_id", String(128), nullable=False),
    Column("region", String(64), nullable=False, default=""),
    Column("snapshot_date", String(10), nullable=False),   # YYYY-MM-DD
    Column("amount_usd", Float, nullable=False, default=0.0),
    Column("granularity", String(16), nullable=False, default="DAILY"),
    Column("captured_at", DateTime, nullable=False),
)

anomalies = Table(
    "anomalies", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("provider", String(64), nullable=False),
    Column("service", String(256), nullable=False),
    Column("account_id", String(128), nullable=False),
    Column("detected_at", DateTime, nullable=False),
    Column("snapshot_date", String(10), nullable=False),
    Column("severity", String(16), nullable=False),        # high / medium / low
    Column("direction", String(8), nullable=False),        # spike / drop
    Column("pct_change", Float, nullable=False),
    Column("z_score", Float, nullable=False),
    Column("baseline_mean", Float, nullable=False),
    Column("current_amount", Float, nullable=False),
    Column("acknowledged", Boolean, nullable=False, default=False),
    Column("notified", Boolean, nullable=False, default=False),
)

tag_rules = Table(
    "tag_rules", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("provider", String(64), nullable=False, default="*"),
    Column("tag_key", String(128), nullable=False),
    Column("tag_value_pattern", String(256), nullable=False, default="*"),
    Column("maps_to_field", String(64), nullable=False),   # team / service / env
    Column("maps_to_value", String(256), nullable=False),
    Column("priority", Integer, nullable=False, default=100),
)

attributed_costs = Table(
    "attributed_costs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("provider", String(64), nullable=False),
    Column("service", String(256), nullable=False),
    Column("account_id", String(128), nullable=False),
    Column("team", String(256), nullable=False, default="unattributed"),
    Column("environment", String(64), nullable=False, default=""),
    Column("snapshot_date", String(10), nullable=False),
    Column("amount_usd", Float, nullable=False, default=0.0),
    Column("captured_at", DateTime, nullable=False),
)

audit_log = Table(
    "audit_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False),
    Column("operation", String(32), nullable=False),
    Column("key_name", String(256), nullable=False),
    Column("client_pid", Integer, nullable=True),
    Column("client_user", String(128), nullable=True),
    Column("detail", Text, nullable=True),
)


def data_dir() -> Path:
    global _DATA_DIR
    if _DATA_DIR is None:
        raw = os.environ.get("FINOPS_DATA_DIR", "")
        _DATA_DIR = Path(raw).expanduser() if raw else Path.home() / ".finops"
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        # Restrict directory to owner only
        _DATA_DIR.chmod(stat.S_IRWXU)
    return _DATA_DIR


def get_engine() -> Engine:
    global _ENGINE
    if _ENGINE is None:
        db_path = data_dir() / "finops.db"
        _ENGINE = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        metadata.create_all(_ENGINE)
        # SQLite WAL mode for concurrent readers
        with _ENGINE.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.execute(text("PRAGMA foreign_keys=ON"))
        # Restrict DB file to owner only
        db_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return _ENGINE
