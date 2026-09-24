"""Tag-derived names cannot inject Slack mentions or links into bot messages."""
from __future__ import annotations

import json

from finops.notifications.slack import anomaly_blocks, esc

EVIL = "<!channel> <https://evil.example/login|Re-authenticate nable>"


def test_esc_neutralises_mentions_and_links():
    out = esc(EVIL)
    assert "<" not in out and ">" not in out
    assert "&lt;!channel&gt;" in out


def test_anomaly_blocks_escape_the_service_name():
    blocks = anomaly_blocks({
        "severity": "high", "direction": "spike", "pct_change": 300.0,
        "provider": "aws", "service": EVIL, "current_amount": 10.0,
        "baseline_mean": 1.0, "z_score": 5.0, "account_id": "123",
    })
    rendered = json.dumps(blocks)
    assert "<!channel>" not in rendered
    assert "<https://evil" not in rendered
