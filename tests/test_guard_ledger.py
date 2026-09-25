"""The decision ledger: every guard verdict, kept, chained and redacted.

Invariants under test:
  - every verdict on an infrastructure action is recorded, allows included,
    and nothing else is (an `ls` is not an audit event)
  - a fail-open is recorded too: an audit has to be able to count the calls
    the guard let through without looking
  - the file is append-only in practice: 0600, O_APPEND, each record chained
    to the one before, so an edit, a deletion or a reordering is detectable
  - secrets in a command never reach the file
  - writing the ledger can never break the gate
  - `nable guard report` and `nable guard verify-log` read it back
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import itertools
import json
import os
import stat
import sys
import threading

import pytest

import finops.guard as g
import finops.guard_ledger as gl
from finops import ai_budget

# Figures come from the price table, never typed in: the p4d rate is revised
# when AWS cuts GPU prices, and a test that pins yesterday's rate fails for a
# reason that has nothing to do with the guard.
from finops.connectors.terraform_estimate import _EC2_HOURLY

P4D_HOURLY = _EC2_HOURLY["p4d.24xlarge"]
P4D_X8_MONTHLY = 8 * P4D_HOURLY * 730
P4D_X8_TEXT = f"${P4D_X8_MONTHLY:,.0f}"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS",
                "FINOPS_GUARD_STOP_ON_BUDGET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ai_budget, "status", lambda: {"verdict": ai_budget.BUDGET_OK})


def _records():
    p = gl.ledger_path()
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


# ── what gets recorded ────────────────────────────────────────────────────────

def test_an_ask_is_recorded_with_its_classification():
    g.gate_command("terraform destroy -auto-approve", harness="claude-code", tool="Bash")
    [r] = _records()
    assert r["decision"] == "ask"
    assert (r["door"], r["action_type"]) == ("one_way", "delete_resource")
    assert (r["harness"], r["tool"]) == ("claude-code", "Bash")
    assert r["command"] == "terraform destroy -auto-approve"
    assert "one-way door" in r["reason"]
    assert r["policy_version"] and r["nable_version"]
    assert r["ts"].endswith("+00:00")


def test_an_allow_with_a_figure_is_recorded_though_the_agent_hears_nothing():
    assert g.gate_command("aws ec2 run-instances --instance-type t3.micro") is None
    [r] = _records()
    assert r["decision"] == "allow"
    assert r["monthly_usd"] == 7.59                           # 0.0104 x 730, in cents
    assert "list price" in r["basis"]


def test_warn_and_deny_are_recorded(monkeypatch):
    g.gate_command("aws ec2 run-instances --instance-type c5.4xlarge")
    monkeypatch.setenv("FINOPS_POLICY_ALLOWED_ACTIONS", "ticket")
    g.gate_command("aws ec2 stop-instances --instance-ids i-1")
    warn, deny = _records()
    assert warn["decision"] == "warn" and warn["monthly_usd"] == pytest.approx(0.68 * 730)
    assert deny["decision"] == "deny" and deny["outcome"] == "not_run"


def test_a_budget_stop_is_recorded(monkeypatch):
    monkeypatch.setattr(ai_budget, "status", lambda: {
        "verdict": ai_budget.BUDGET_OVER, "verdict_basis": "tokens", "pct_of_budget": 1.2,
        "billable_tokens_mtd": 12, "budget": {"monthly_tokens": 10}})
    g.gate_command("ls -la")
    [r] = _records()
    assert (r["decision"], r["action_type"]) == ("ask", "ai_budget")


def test_commands_the_guard_has_no_opinion_on_are_not_recorded():
    for cmd in ("ls -la", "git status", "aws s3 ls", "terraform plan"):
        assert g.gate_command(cmd) is None
    assert _records() == []


def test_mcp_verdicts_are_recorded_under_the_tool_name():
    g.gate_mcp_call("mcp__terraform__create_run",
                    {"workspace_name": "net", "run_type": "is_destroy"})
    g.gate_mcp_call("mcp__github__delete_file", {"path": "x"})      # unknown: not an event
    [r] = _records()
    assert r["tool"] == "mcp__terraform__create_run"
    assert r["command"] == "terraform destroy"
    assert r["decision"] == "ask"


def test_a_human_asking_is_not_an_agent_doing(capsys):
    from finops import setup_wizard
    setup_wizard._run_guard(argparse.Namespace(guard_action="check", guard_global=False,
                                               guard_command="terraform destroy"))
    setup_wizard._run_guard(argparse.Namespace(guard_action="try", guard_global=False))
    assert _records() == [], "`guard check` / `guard try` wrote to the audit log"


def test_a_guard_crash_fails_open_and_is_recorded(monkeypatch):
    def boom(cmd):
        raise RuntimeError("classifier bug")
    monkeypatch.setattr(g, "classify_command", boom)
    assert g.gate_command("terraform destroy", harness="cursor") is None
    [r] = _records()
    assert (r["decision"], r["error"], r["harness"]) == ("fail_open", "RuntimeError", "cursor")


def test_an_unreadable_hook_payload_fails_open_and_is_recorded():
    out = io.StringIO()
    assert g.run_hook(stdin=io.StringIO("not json"), stdout=out) == 0
    assert out.getvalue() == ""
    [r] = _records()
    assert r["decision"] == "fail_open" and r["error"] == "JSONDecodeError"


def test_a_ledger_that_cannot_be_written_never_costs_the_verdict(monkeypatch, tmp_path):
    monkeypatch.setattr(gl, "_path_override", tmp_path)          # a directory, not a file
    v = g.gate_command("terraform destroy")
    assert v and v["decision"] == "ask"
    assert gl.append({"decision": "ask"}) is False


# ── redaction ─────────────────────────────────────────────────────────────────

# AWS's own documentation example key, not a credential.
SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"  # pragma: allowlist secret


@pytest.mark.parametrize("raw,gone,kept", [
    (f"AWS_SECRET_ACCESS_KEY={SECRET} terraform destroy", SECRET,
     "AWS_SECRET_ACCESS_KEY=[REDACTED] terraform destroy"),
    ("GITHUB_TOKEN='ghp_x' db_password=hunter2 terraform apply", "hunter2",
     "db_password=[REDACTED]"),
    ("aws rds create-db-instance --master-user-password hunter2 --engine mysql", "hunter2",
     "--master-user-password [REDACTED] --engine mysql"),
    ("aws configure set aws_access_key_id AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE",
     "[REDACTED-AWS-KEY-ID]"),
    ("curl -H 'Authorization: Bearer abc.def.ghi' https://api", "abc.def.ghi", "Bearer [REDACTED]"),
    ("git clone https://me:s3cretPass@github.com/org/repo",  # pragma: allowlist secret
     "s3cretPass", "https://me:[REDACTED]@"),
    ("helm install x --set token=Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MEFCQ0RFRkdI", "Zm9vYmFyYmF6",
     "helm install x"),
    # The secret word as the whole name, not a suffix of one.
    ("helm upgrade app ./chart --set db.password=hunter2", "hunter2",
     "--set db.password=[REDACTED]"),
    ("PASSWORD=hunter2 terraform apply", "hunter2", "PASSWORD=[REDACTED] terraform apply"),
    ("helm install x --set auth.token=abc123", "abc123", "auth.token=[REDACTED]"),
    # Red-team finds: each reached the ledger in the clear.
    ("curl 'https://api.x.io/v1?token=abc123&page=2'", "abc123", "?token=[REDACTED]"),
    ("terraform apply -var db_pass=hunter2", "hunter2", "-var db_pass=[REDACTED]"),
    ("terraform apply -var=admin_pw=hunter2", "hunter2", "admin_pw=[REDACTED]"),
    ("mysql -uroot -phunter2 -h db.internal", "hunter2", "-p[REDACTED] -h db.internal"),
    ("mysqldump -u root -p'hunter2' app", "hunter2", "-p[REDACTED] app"),
    ("curl -u admin:hunter2 https://x.io", "hunter2", "-u admin:[REDACTED] https://x.io"),
    ("az login --service-principal -u app -p Pa55w0rd --tenant t", "Pa55w0rd",
     "-p [REDACTED] --tenant t"),
    ("docker login -u me -p hunter2 registry.io", "hunter2", "-p [REDACTED] registry.io"),
    ("curl -H 'Authorization: xoxb-1234-5678-abcdefgh' https://slack.com", "xoxb-1234",  # pragma: allowlist secret
     "[REDACTED-SLACK-TOKEN]"),
    ("curl -d t=xoxp-99-88-77aa https://slack.com", "xoxp-99", "[REDACTED-SLACK-TOKEN]"),  # pragma: allowlist secret
    ("azcopy copy 'https://a.blob.core.windows.net/c?sv=2020&sig=AbC%2Bdef' .", "AbC%2Bdef",
     "&sig=[REDACTED]"),
    ("echo ghp_abcdefghijklmnop1234 | gh auth login", "ghp_abcdef", "[REDACTED-GITHUB-TOKEN]"),
    ("export OPENAI=sk-proj-abcdefghijklmnop", "sk-proj-abc", "[REDACTED-API-KEY]"),
    ("aws s3 cp s3://b/k . --expires 1 --pass-phrase x", "--pass-phrase x",
     "--pass-phrase [REDACTED]"),
])

def test_secrets_never_reach_the_ledger(raw, gone, kept):
    out = gl.redact(raw)
    assert gone not in out
    assert kept in out


@pytest.mark.parametrize("cmd", [
    "ssh -p 22 host uptime",
    "docker run -p 8080:80 nginx",
    "git push -u origin main",
    "aws ec2 run-instances --instance-type m5.large --count 2",
    "kubectl -n prod get pods -o wide",
])
def test_ordinary_flags_are_left_alone(cmd):
    assert gl.redact(cmd) == cmd


def test_redaction_keeps_what_an_auditor_needs():
    cmd = ("terraform -chdir=/home/me/Infra2025/prod apply "
           "-var-file=prod.tfvars plan.out && sha256sum 0f1e2d3c4b5a69788796a5b4c3d2e1f0aabbccdd")
    assert gl.redact(cmd) == cmd


def test_a_secret_in_a_recorded_command_is_redacted_on_disk():
    g.gate_command(f"AWS_SECRET_ACCESS_KEY={SECRET} terraform destroy")
    blob = gl.ledger_path().read_text()
    assert SECRET not in blob and "[REDACTED]" in blob


def test_long_commands_are_bounded():
    assert len(gl.redact("kubectl delete pod " + "x " * 1000)) <= 400


# ── the file and its chain ────────────────────────────────────────────────────

def test_the_file_is_owner_only():
    g.gate_command("terraform destroy")
    assert stat.S_IMODE(gl.ledger_path().stat().st_mode) == 0o600


def test_a_pre_existing_loose_file_is_tightened():
    p = gl.ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch(mode=0o644)
    p.chmod(0o644)
    gl.append({"decision": "ask"})
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def _three():
    for d in ("ask", "deny", "allow"):
        gl.append({"decision": d, "command": f"cmd-{d}"})


def test_each_record_carries_the_hash_of_the_line_before_it():
    _three()
    lines = gl.ledger_path().read_bytes().splitlines()
    assert json.loads(lines[0])["prev"] == gl.GENESIS
    for before, after in itertools.pairwise(lines):
        assert json.loads(after)["prev"] == hashlib.sha256(before).hexdigest()
    v = gl.verify()
    assert v["ok"] and v["records"] == 3
    assert v["head"] == hashlib.sha256(lines[-1]).hexdigest()


@pytest.mark.parametrize("tamper", ["edit", "delete", "reorder"])
def test_tampering_breaks_the_chain(tamper):
    _three()
    p = gl.ledger_path()
    lines = p.read_bytes().splitlines()
    if tamper == "edit":
        lines[1] = lines[1].replace(b'"deny"', b'"allow"')
    elif tamper == "delete":
        del lines[1]
    else:
        lines[0], lines[1] = lines[1], lines[0]
    p.write_bytes(b"\n".join(lines) + b"\n")
    v = gl.verify()
    assert not v["ok"]
    assert v["broken_at"] in (1, 2, 3)


def test_a_torn_last_write_is_never_glued_to_the_next_record():
    gl.append({"decision": "ask"})
    p = gl.ledger_path()
    with p.open("ab") as fh:
        fh.write(b'{"v":1,"decision":"de')                 # a crash mid-write
    gl.append({"decision": "allow"})
    lines = p.read_bytes().splitlines()
    assert json.loads(lines[-1])["decision"] == "allow", "the new record was corrupted"
    v = gl.verify()
    assert not v["ok"] and v["broken_at"] == 2, "a torn record is an integrity event"


def test_concurrent_agents_do_not_fork_the_chain():
    """Two agent sessions can hit the hook at the same moment. Without the
    lock both read the same last line and the chain forks."""
    def worker():
        for _ in range(25):
            gl.append({"decision": "ask", "command": "terraform destroy"})
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    v = gl.verify()
    assert v["ok"], v
    assert v["records"] == 200


# ── where it lives ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("env", [
    {"FINOPS_DATA_DIR": "{tmp}/data"},
    {"FINOPS_PROFILE": "work"},
    {},
])
def test_the_ledger_lives_in_the_same_data_dir_as_everything_else(env, monkeypatch, tmp_path):
    """guard_ledger copies storage.db.data_dir()'s rule rather than import
    SQLAlchemy into the hook. The copy must not drift."""
    from finops.storage import db
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in ("FINOPS_DATA_DIR", "FINOPS_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v.format(tmp=tmp_path))
    monkeypatch.setattr(db, "_DATA_DIR", None)
    expected = db.data_dir()
    monkeypatch.delitem(sys.modules, "finops.storage.db")
    assert gl._data_dir() == expected
    monkeypatch.setattr(gl, "_path_override", None)
    assert gl.ledger_path() == expected / "guard-ledger.jsonl"


def test_the_hook_path_does_not_import_sqlalchemy():
    code = ("import sys, io, json; import finops.guard as g; "
            "g.gate_command('terraform destroy'); "
            "print('sqlalchemy' in sys.modules)")
    import subprocess
    env = {**os.environ, "HOME": str(gl.ledger_path().parent), "NABLE_NO_TELEMETRY": "1"}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env=env, timeout=60, check=False)
    assert r.stdout.strip() == "False", r.stderr


# ── reading it back ───────────────────────────────────────────────────────────

def _cli(action, **kw):
    from finops import setup_wizard
    kw.setdefault("guard_global", False)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        setup_wizard._run_guard(argparse.Namespace(guard_action=action, **kw))
    return out.getvalue()


def _activity(monkeypatch):
    g.gate_command("aws ec2 run-instances --instance-type p4d.24xlarge --count 8")   # ask
    g.gate_command("aws ec2 run-instances --instance-type t3.micro")                 # allow
    g.gate_command("terraform destroy")                                               # ask
    g.gate_command("aws savingsplans create-savings-plan --commitment 1")            # ask
    monkeypatch.setenv("FINOPS_POLICY_ALLOWED_ACTIONS", "ticket")
    g.gate_command("aws ec2 stop-instances --instance-ids i-1")                      # deny
    monkeypatch.delenv("FINOPS_POLICY_ALLOWED_ACTIONS")


def test_the_summary_counts_and_sums(monkeypatch):
    _activity(monkeypatch)
    s = gl.summarize(30)
    assert s["records"] == 5
    assert s["by_decision"]["ask"] == 3 and s["by_decision"]["deny"] == 1
    assert s["by_decision"]["allow"] == 1
    assert s["usd_per_month_escalated_or_blocked"] == pytest.approx(
        P4D_X8_MONTHLY + 730, rel=1e-3)
    assert s["usd_per_month_allowed_with_a_figure"] == pytest.approx(0.0104 * 730, rel=1e-3)
    assert s["largest"][0]["monthly_usd"] == pytest.approx(P4D_X8_MONTHLY, rel=1e-3)


def test_old_records_fall_outside_the_window():
    gl.append({"decision": "ask", "monthly_usd": 100.0})
    p = gl.ledger_path()
    rec = json.loads(p.read_text())
    rec["ts"] = "2020-01-01T00:00:00+00:00"
    p.write_text(json.dumps(rec) + "\n")
    assert gl.summarize(30)["records"] == 0
    assert gl.summarize(365 * 20)["records"] == 1


def test_report_cli_prints_escalations_and_dollars(monkeypatch):
    _activity(monkeypatch)
    out = _cli("report", guard_days=30, guard_json=False)
    assert "asked a human        3" in out
    assert "blocked              1" in out
    assert f"${P4D_X8_MONTHLY + 730:,.0f}/mo at stake" in out
    assert "aws ec2 run-instances --instance-type p4d.24xlarge --count 8" in out, \
        "the largest escalation is named"
    assert "nable guard verify-log" in out


def test_report_cli_json_is_parseable(monkeypatch):
    _activity(monkeypatch)
    data = json.loads(_cli("report", guard_days=7, guard_json=True))
    assert data["records"] == 5 and data["days"] == 7


def test_report_on_an_empty_ledger_says_so():
    assert "Nothing recorded yet" in _cli("report", guard_days=30, guard_json=False)


def test_verify_log_cli_passes_and_fails():
    _three()
    assert "Decision ledger intact: 3 record(s)" in _cli("verify-log", guard_json=False)
    p = gl.ledger_path()
    p.write_bytes(p.read_bytes().replace(b'"deny"', b'"allow"'))
    with pytest.raises(SystemExit) as e:
        _cli("verify-log", guard_json=False)
    assert e.value.code == 1


def test_verify_log_json():
    _three()
    assert json.loads(_cli("verify-log", guard_json=True))["ok"] is True


# ── the anchor: what a chain cannot show ──────────────────────────────────────

def _verify_log(**kw):
    kw.setdefault("guard_json", False)
    kw.setdefault("guard_reanchor", False)
    return _cli("verify-log", **kw)


def test_a_first_check_anchors_the_head():
    _three()
    _verify_log()
    a = gl.read_anchor()
    assert a["records"] == 3 and a["head"] == gl.verify()["head"]


def test_new_records_after_the_anchor_are_fine():
    _three()
    _verify_log()
    gl.append({"decision": "ask"})
    assert "Nothing removed or rewritten since" in _verify_log()
    assert gl.read_anchor()["records"] == 4, "a clean check moves the anchor forward"


@pytest.mark.parametrize("damage,said", [
    (lambda p: p.write_bytes(b"".join(p.read_bytes().splitlines(keepends=True)[:2])),
     "had 3 record(s)"),
    (lambda p: p.write_bytes(b""), "the file is empty or gone"),
    (lambda p: p.unlink(), "the file is empty or gone"),
])
def test_records_cut_from_the_end_are_reported(damage, said):
    _three()
    _verify_log()
    damage(gl.ledger_path())
    assert gl.verify()["ok"], "the chain alone cannot see this"
    with pytest.raises(SystemExit) as e:
        _verify_log()
    assert e.value.code == 1
    r = gl.check()
    assert not r["clean"] and said in r["warnings"][0]


def test_an_emptied_ledger_is_not_a_check_mark(capsys):
    _three()
    _verify_log()
    gl.ledger_path().write_bytes(b"")
    from finops import setup_wizard
    with pytest.raises(SystemExit):
        setup_wizard._run_guard(argparse.Namespace(guard_action="verify-log", guard_global=False,
                                                   guard_json=False, guard_reanchor=False))
    out = capsys.readouterr().out
    assert "Decision ledger intact" not in out and "empty or gone" in out


def test_a_rewritten_head_is_reported():
    _three()
    _verify_log()
    p = gl.ledger_path()
    lines = p.read_bytes().splitlines(keepends=True)
    lines[-1] = lines[-1].replace(b'"allow"', b'"deny"')     # the last line: no chain after it
    p.write_bytes(b"".join(lines))
    assert gl.verify()["ok"]
    r = gl.check()
    assert not r["clean"] and "record 3 is not the one seen" in r["warnings"][0]


def test_the_whole_file_regenerated_is_reported():
    _three()
    _verify_log()
    gl.ledger_path().unlink()
    for d in ("allow", "allow", "allow", "allow"):
        gl.append({"decision": d})
    r = gl.check()
    assert r["ok"] and not r["clean"] and "rewritten" in r["warnings"][0]


def test_reanchor_accepts_a_rotated_ledger():
    _three()
    _verify_log()
    gl.ledger_path().unlink()
    out = _verify_log(guard_reanchor=True)
    assert "Re-anchored at 0 record(s)" in out
    assert gl.check()["clean"]


def test_doctor_is_not_ok_when_the_ledger_shrank(monkeypatch):
    _three()
    _verify_log()
    gl.ledger_path().write_bytes(b"")
    d = g.doctor()
    assert d["ok"] is False
    assert any("empty or gone" in fix for fix in d["recommendations"])


def test_report_warns_on_a_broken_chain():
    _three()
    p = gl.ledger_path()
    p.write_bytes(p.read_bytes().replace(b'"deny"', b'"allow"'))
    out = _cli("report", guard_days=30, guard_json=False)
    assert "does not verify" in out and "breaks at line" in out
    data = json.loads(_cli("report", guard_days=30, guard_json=True))
    assert data["ledger_problems"]


# ── what "at stake" sums ──────────────────────────────────────────────────────

def _at(minutes_ago: float) -> str:
    from datetime import UTC, datetime, timedelta
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def test_a_destroy_plans_saving_is_not_subtracted_from_what_is_at_stake():
    gl.append({"ts": _at(30), "decision": "ask", "command": "terraform apply big.plan",
               "monthly_usd": 5000.0})
    gl.append({"ts": _at(20), "decision": "ask", "command": "terraform apply destroy.plan",
               "monthly_usd": -1328.0})
    gl.append({"ts": _at(10), "decision": "allow", "command": "terraform apply shrink.plan",
               "monthly_usd": -40.0})
    s = gl.summarize(30)
    assert s["usd_per_month_escalated_or_blocked"] == 5000.0
    assert s["usd_per_month_allowed_with_a_figure"] == 0.0
    assert [r["monthly_usd"] for r in s["largest"]] == [5000.0]


def test_a_retried_command_is_counted_once():
    cmd = "aws ec2 run-instances --instance-type p4d.24xlarge --count 8"
    for m in (9, 6, 3):
        gl.append({"ts": _at(m), "decision": "ask", "command": cmd, "session": "s1",
                   "monthly_usd": 100_000.0})
    s = gl.summarize(30)
    assert s["by_decision"]["ask"] == 3, "every decision is still counted"
    assert s["usd_per_month_escalated_or_blocked"] == 100_000.0
    assert s["repeats_not_summed"] == 2 and len(s["largest"]) == 1


def test_the_same_command_is_summed_again_from_another_session_or_later():
    cmd = "aws ec2 run-instances --instance-type p4d.24xlarge"
    gl.append({"ts": _at(50), "decision": "ask", "command": cmd, "session": "s1",
               "monthly_usd": 100.0})
    gl.append({"ts": _at(49), "decision": "ask", "command": cmd, "session": "s2",
               "monthly_usd": 100.0})
    gl.append({"ts": _at(20), "decision": "ask", "command": cmd, "session": "s1",
               "monthly_usd": 100.0})
    assert gl.summarize(30)["usd_per_month_escalated_or_blocked"] == 300.0


def test_report_says_repeats_were_counted_once():
    cmd = "aws ec2 run-instances --instance-type p4d.24xlarge --count 8"
    for m in (4, 2):
        gl.append({"ts": _at(m), "decision": "ask", "command": cmd, "monthly_usd": 10.0})
    assert "1 repeat(s) of the same command within 10 minutes counted once" in \
        _cli("report", guard_days=30, guard_json=False)


def test_report_into_a_closed_pipe_exits_quietly():
    """`nable guard report | head`: the reader leaves early; no traceback."""
    import subprocess
    _activity_without_env()
    env = {**os.environ, "HOME": str(gl.ledger_path().parent), "NABLE_NO_TELEMETRY": "1",
           "FINOPS_DATA_DIR": str(gl.ledger_path().parent)}
    env.pop("FINOPS_PROFILE", None)
    code = "from finops.setup_wizard import main; main(['guard', 'report'])"
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env)
    p.stdout.close()                                  # the reader is already gone
    _, err = p.communicate(timeout=60)
    assert b"Traceback" not in err and b"BrokenPipe" not in err, err.decode()
    assert p.returncode == 0


def _activity_without_env():
    for cmd in ("terraform destroy", "aws ec2 run-instances --instance-type t3.micro"):
        g.gate_command(cmd)


# ── per-session ───────────────────────────────────────────────────────────────

def test_the_session_is_recorded():
    g.gate_command("aws ec2 run-instances --instance-type t3.micro", session_id="sess-a")
    g.gate_mcp_call("mcp__aws-api__call_aws", {"cli_command": "aws ec2 terminate-instances "
                                                              "--instance-ids i-1"},
                    session_id="sess-b")
    g.gate_command("ls")                                        # not infra: no record
    g.gate_command("terraform destroy")                          # no session given
    recs = _records()
    assert [r.get("session") for r in recs] == ["sess-a", "sess-b", None]


def test_the_hook_records_the_payloads_session():
    payload = {"tool_name": "Bash", "session_id": "0a1b2c3d-4e5f-6789-abcd-ef0123456789",
               "tool_input": {"command": "terraform destroy"}}
    g.run_hook(io.StringIO(json.dumps(payload)), io.StringIO())
    assert _records()[0]["session"] == "0a1b2c3d-4e5f-6789-abcd-ef0123456789"


def test_a_session_id_goes_through_the_same_redaction():
    g.gate_command("terraform destroy", session_id=f"AWS_SECRET_ACCESS_KEY={SECRET}")
    assert SECRET not in gl.ledger_path().read_text()


def test_a_fail_open_keeps_its_session(monkeypatch):
    monkeypatch.setattr(g, "classify_command", lambda c: 1 / 0)
    g.gate_command("terraform destroy", session_id="sess-z")
    assert _records()[0]["session"] == "sess-z"


def _sessions(monkeypatch):
    g.gate_command("aws ec2 run-instances --instance-type p4d.24xlarge --count 8",
                   session_id="big")                                                # ask
    g.gate_command("aws ec2 run-instances --instance-type t3.micro", session_id="small")
    g.gate_command("aws ec2 run-instances --instance-type t3.large", session_id="small")


def test_summary_has_per_session_priced_totals(monkeypatch):
    _sessions(monkeypatch)
    s = gl.summarize(30)
    assert list(s["by_session"]) == ["big", "small"], "largest first"
    assert s["by_session"]["big"]["usd_per_month_escalated_or_blocked"] == \
        pytest.approx(P4D_X8_MONTHLY, rel=1e-3)
    assert s["by_session"]["small"]["usd_per_month_allowed_with_a_figure"] == \
        pytest.approx((0.0104 + 0.0832) * 730, rel=1e-3)
    assert s["by_session"]["small"]["records"] == 2


def test_summary_for_one_session(monkeypatch):
    _sessions(monkeypatch)
    s = gl.summarize(30, session="small")
    assert s["records"] == 2 and s["session"] == "small"
    assert s["usd_per_month_escalated_or_blocked"] == 0
    assert list(s["by_session"]) == ["small"]


def test_report_cli_shows_sessions_and_filters_by_one(monkeypatch):
    _sessions(monkeypatch)
    out = _cli("report", guard_days=30, guard_json=False)
    assert "By agent session" in out and "big" in out and "small" in out
    one = _cli("report", guard_days=30, guard_json=False, guard_session="small")
    assert "in session small, 2 decision(s)" in one
    assert "p4d.24xlarge" not in one
    data = json.loads(_cli("report", guard_days=30, guard_json=True, guard_session="big"))
    assert data["records"] == 1 and data["session"] == "big"


def test_report_for_an_unknown_session_says_so():
    g.gate_command("terraform destroy", session_id="a")
    out = _cli("report", guard_days=30, guard_json=False, guard_session="nope")
    assert "Nothing recorded for session nope" in out


def test_report_session_flag_parses():
    from finops import setup_wizard
    seen = {}
    real = setup_wizard._run_guard

    def spy(parsed):
        seen["session"] = parsed.guard_session
    setup_wizard._run_guard = spy
    try:
        setup_wizard.main(["guard", "report", "--session", "abc"])
    finally:
        setup_wizard._run_guard = real
    assert seen["session"] == "abc"
