"""`nable scan --demo` and `nable welcome --demo` show the same account.

Both say StreamCo. welcome --demo read the sample dataset (demo_data): $5,147,600
over the last 30 days. scan --demo's AI, GCP and Azure blocks were figures of
their own, $19,600/mo visible, so an engineer comparing the two demos saw one
company 260 times the size of the other. Every figure both show now comes from
the one sample, at its one scale, and both stay labelled as sample data.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from finops import demo_data
from finops import scan_assembler as sa

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _nable(tmp_path: Path, *argv: str) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AWS_", "FINOPS_", "NABLE_"))}
    env.update(HOME=str(home), PYTHONPATH=SRC, FINOPS_NO_KEYRING="1", NABLE_NO_TELEMETRY="1",
               AWS_CONFIG_FILE=str(home / "none"), AWS_SHARED_CREDENTIALS_FILE=str(home / "none"),
               AWS_EC2_METADATA_DISABLED="true")
    return subprocess.run([sys.executable, "-m", "finops.entry", *argv], env=env, check=False,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)


def test_the_scan_demo_blocks_are_the_sample_dataset():
    llm = demo_data.llm_costs()["by_provider"]
    azure = demo_data.cost_summary({"provider": "azure"})
    free = {b.family: b for b in sa.demo_extra_blocks(spend=False)}
    paid = {b.family: b for b in sa.demo_extra_blocks(spend=True)}
    assert free["ai"].spend_usd == round(llm["openai"] + llm["anthropic"], 2)
    assert paid["ai"].spend_usd == round(sum(llm.values()), 2)
    assert free["azure"].spend_usd == azure["total_usd"]
    assert free["azure"].recoverable_usd == sum(
        o["monthly_saving"] for o in demo_data._PROVIDER_OPPS["azure"])
    assert free["gcp"].recoverable_usd == sum(
        o["monthly_saving"] for o in demo_data._PROVIDER_OPPS["gcp"])
    assert "OpenAI" in free["ai"].detail


def test_the_two_demos_agree_on_every_figure_they_share(tmp_path):
    scan = _nable(tmp_path, "scan", "--demo", "--json")
    assert scan.returncode == 0, scan.stderr[-800:]
    doc = json.loads(scan.stdout)
    assert doc["demo"] is True
    blocks = {b["family"]: b for b in doc["providers"]}

    welcome = _nable(tmp_path, "welcome", "--demo")
    assert welcome.returncode == 0, welcome.stderr[-800:]
    assert "sample data" in welcome.stdout
    m = re.search(r"Total spend\s+\$([\d,]+)", re.sub(r"\x1b\[[0-9;]*m", "", welcome.stdout))
    assert m, welcome.stdout[-1500:]
    welcome_total = float(m.group(1).replace(",", ""))

    sample = demo_data.cost_summary()
    assert welcome_total == round(sample["total_usd"])
    by = sample["by_provider"]
    assert blocks["ai"]["spend_usd"] == round(by["openai"]["total_usd"]
                                              + by["anthropic"]["total_usd"], 2)
    assert blocks["azure"]["spend_usd"] == by["azure"]["total_usd"]
    visible = sum(b["spend_usd"] or 0 for b in blocks.values())
    # A part of one account is never bigger than the whole, and never a
    # rounding error of it either: the same company, the same scale.
    assert welcome_total / 20 < visible < welcome_total
