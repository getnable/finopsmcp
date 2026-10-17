"""`nable org` and the org MCP tools: the human path and the agent path.

The line these tests hold: an agent (the MCP tools, or anything without a
terminal) can only propose. Confirming or rejecting records a person, taken
from --as, or on a terminal from git's user.email, then $USER; with neither
it refuses.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from finops import org
from finops.org import cli, store
from finops.org.cli import _who as human
from finops.org.model import local_today


@pytest.fixture
def odir(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for var in ("FINOPS_TAG_RULES", "FINOPS_ACCOUNTS_FILE", "FINOPS_REQUIRED_TAGS",
                "FINOPS_PROTECTED_TAGS", "FINOPS_PROFILE", "DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "no-such.db"))
    d = tmp_path / "orgdir"
    monkeypatch.setenv("FINOPS_ORG_DIR", str(d))
    monkeypatch.setattr(store, "_data_dir", lambda: tmp_path / "data")
    # No git identity unless a test gives one.
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(cli, "_is_tty", lambda: False)
    return d


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def proposed(team="payments", subject="aws_account:123456789012", **kw):
    f = org.make_fact("owner", subject, {"team": team}, source="codeowners:infra/",
                      confidence=0.8, **kw)
    org.propose(f)
    return f


# ── confirm and reject are human decisions ────────────────────────────────────

def test_without_a_terminal_or_as_confirm_refuses(odir, capsys):
    f = proposed()
    code, _, err = run(capsys, "confirm", f.key)
    assert code == 2 and "--as" in err
    assert org.load().facts[0].status == "proposed"
    code, _, err = run(capsys, "reject", f.key)
    assert code == 2
    code, _, err = run(capsys, "set", "owner", "--subject", "aws_account:1", "--team", "x")
    assert code == 2
    assert [x.status for x in org.load().facts] == ["proposed"]


def test_confirm_with_as_records_who(odir, capsys):
    f = proposed()
    code, out, _ = run(capsys, "confirm", f.key, "--as", "@maria")
    assert code == 0 and "confirmed by @maria" in out
    saved = org.load().facts[0]
    assert (saved.status, saved.confirmed_by, saved.confirmed_at) == \
        ("confirmed", "@maria", local_today().isoformat())


def test_on_a_terminal_git_email_is_the_default(odir, tmp_path, monkeypatch, capsys):
    (tmp_path / "gitconfig").write_text("[user]\n\temail = maria@example.com\n")
    monkeypatch.setattr(cli, "_is_tty", lambda: True)
    f = proposed()
    code, _, _ = run(capsys, "confirm", f.key)
    assert code == 0
    assert org.load().facts[0].confirmed_by == "maria@example.com"


def test_on_a_terminal_without_git_email_user_is_used(odir, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_is_tty", lambda: True)
    monkeypatch.setenv("USER", "maria")
    f = proposed()
    assert run(capsys, "reject", f.key)[0] == 0
    saved = org.load().facts[0]
    assert (saved.status, saved.confirmed_by) == ("rejected", "maria")


def test_a_bad_key_is_reported_and_the_rest_still_apply(odir, capsys):
    f = proposed()
    code, _, err = run(capsys, "confirm", "ffffffffff", f.key, "--as", "@maria")
    assert code == 1 and "ffffffffff" in err
    assert org.load().facts[0].status == "confirmed"


def test_set_owner_writes_a_confirmed_human_fact(odir, capsys):
    code, _, _ = run(capsys, "set", "owner", "--subject", "aws_account:123456789012",
                       "--team", "payments", "--channel", "#payments-oncall", "--as", "@lead")
    assert code == 0
    f = org.load().facts[0]
    assert (f.status, f.source, f.confidence, f.confirmed_by) == \
        ("confirmed", "human", 1.0, "@lead")
    assert f.value == {"team": "payments", "channel": "#payments-oncall"}
    assert (odir / "owners.yaml").exists()
    code, _, err = run(capsys, "set", "environment", "--subject", "aws_account:1",
                       "--env", "moon", "--as", "@lead")
    assert code == 1 and "env" in err


# ── read commands ─────────────────────────────────────────────────────────────

def test_review_status_questions_export(odir, tmp_path, capsys):
    f = proposed(dollars_monthly=8210)
    code, out, _ = run(capsys, "review")
    assert code == 0 and f.key in out and "$8,210/mo" in out
    code, out, _ = run(capsys, "review", "--json")
    assert json.loads(out)["proposed"][0]["key"] == f.key
    code, out, _ = run(capsys, "status", "--json")
    st = json.loads(out)
    assert st["counts"]["proposed"] == 1 and st["dir_source"] == "FINOPS_ORG_DIR"
    assert st["coverage"]["pct_confirmed"] is None
    code, out, _ = run(capsys, "status")
    assert "not read" in out and "1 proposed" in out
    code, out, _ = run(capsys, "questions", "--json")
    q = json.loads(out)["questions"][0]
    assert q["command"] == f"nable org confirm {f.key}" and q["dollars_monthly"] == 8210
    code, out, _ = run(capsys, "export", "--format", "json", "--out", str(tmp_path / "o.json"))
    assert code == 0 and json.loads((tmp_path / "o.json").read_text())[0]["key"] == f.key


UNSAFE = ("\x1b", "\x07", "\x9b", "\u202e", "\x08")


def test_read_commands_print_nothing_that_can_drive_a_terminal(odir, tmp_path, capsys):
    # A fact's text comes from adapters, packs and repos: it may hold terminal
    # escapes. On a terminal they are shown as visible escapes; JSON stays JSON.
    f = org.make_fact("owner", "service:a\x1b[2Jb", {"team": "pay\x1b]0;owned\x07ments"},
                      source="codeowners:\x9b31m\u202eevil\x08", confidence=0.8,
                      dollars_monthly=100)
    org.propose(f)
    org.propose(org.make_fact("freeze", "environment:prod", {
        "start": "2099-01-01T00:00:00+00:00", "end": "2099-01-02T00:00:00+00:00",
        "reason": "Black Friday\x1b[1A\x1b[2K\u202e"}, source="pack:x:\x1b[31m"))
    for argv in (("status",), ("review",), ("questions",), ("export",),
                 ("export", "--format", "json")):
        code, out, err = run(capsys, *argv)
        assert code == 0, (argv, err)
        assert not [c for c in UNSAFE if c in out + err], (argv, out)
    code, out, _ = run(capsys, "review")
    assert "service:a\\x1b[2Jb" in out and "codeowners:\\x9b31m\\u202eevil\\x08" in out
    code, out, _ = run(capsys, "status")
    assert "Black Friday\\x1b[1A\\x1b[2K\\u202e" in out
    for argv in (("status", "--json"), ("review", "--json"), ("questions", "--json"),
                 ("export", "--format", "json")):
        code, out, _ = run(capsys, *argv)
        assert "\x1b" not in out and json.loads(out), argv
    code, out, _ = run(capsys, "review", "--json")
    rows = {r["fact"]: r for r in json.loads(out)["proposed"]}
    assert rows["owner"]["source"] == "codeowners:\x9b31m\u202eevil\x08"


def test_init_without_a_terminal_prints_the_questions(odir, capsys):
    proposed(dollars_monthly=100)
    code, out, _ = run(capsys, "init")
    assert code == 0
    assert sorted(p.name for p in odir.iterdir()) == sorted(org.model.KNOWN_FILES)
    assert "Is it right that aws_account:123456789012 is owned by team payments" in out
    assert "nable org confirm" in out
    assert org.load().facts[0].status == "proposed"


def test_init_on_a_terminal_asks_and_records_answers(odir, monkeypatch, capsys):
    a = proposed("payments", dollars_monthly=900)
    b = proposed("search", subject="aws_account:222222222222", dollars_monthly=500)
    c = proposed("data", subject="aws_account:333333333333", dollars_monthly=100)
    monkeypatch.setattr(cli, "_is_tty", lambda: True)
    answers = iter(["", "n", "e", "analytics"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    code, _, _ = run(capsys, "init", "--as", "@maria")
    assert code == 0
    m = org.load()
    by = {f.key: f for f in m.facts}
    assert by[a.key].status == "confirmed" and by[a.key].confirmed_by == "@maria"
    assert by[b.key].status == "rejected"
    assert by[c.key].status == "rejected"
    assert m.owner_of("aws_account:333333333333").team == "analytics"
    assert m.owner_of("aws_account:333333333333").confirmed


def test_init_here_creates_the_repo_dir_and_only_then(tmp_path, odir, monkeypatch, capsys):
    monkeypatch.delenv("FINOPS_ORG_DIR")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    run(capsys, "status")
    assert not (repo / "nable.org").exists()
    code, _, _ = run(capsys, "init")
    assert code == 0 and not (repo / "nable.org").exists()
    assert (tmp_path / "data" / "org" / "owners.yaml").exists()
    code, _, _ = run(capsys, "init", "--here")
    assert code == 0 and (repo / "nable.org" / "owners.yaml").exists()
    assert org.resolve_dir() == (repo.resolve() / "nable.org", "repo")
    monkeypatch.chdir(tmp_path)
    assert run(capsys, "init", "--here")[0] == 2


def test_nable_org_is_wired_into_the_main_cli(odir, monkeypatch, capsys):
    import finops.setup_wizard as sw
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    proposed()
    with pytest.raises(SystemExit) as e:
        sw.main(["org", "review", "--json"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert json.loads(out[out.index("{"):])["proposed"][0]["status"] == "proposed"


# ── the MCP tools: read and propose only ──────────────────────────────────────

def _tool(name, **kwargs):
    from finops import server
    fn = getattr(server, name)
    return asyncio.run(fn(**kwargs))


def test_propose_org_fact_only_proposes_and_names_the_confirm_command(odir):
    r = _tool("propose_org_fact", fact="owner", subject_kind="repo_path",
              subject_id="infra/payments/", value={"team": "payments"},
              source="codeowners:infra/payments/", confidence=0.92, dollars_monthly=8210.0)
    assert r["result"] == "added" and r["status"] == "proposed"
    assert r["confirm_command"] == f"nable org confirm {r['key']}"
    f = org.load().facts[0]
    assert (f.status, f.confirmed_by, f.subject.id) == ("proposed", None, "infra/payments")
    again = _tool("propose_org_fact", fact="owner", subject_kind="repo_path",
                  subject_id="infra/payments", value={"team": "payments"},
                  source="codeowners:infra/payments/")
    assert again["result"] == "duplicate"


def test_an_agent_cannot_pass_itself_off_as_a_human_source(odir):
    r = _tool("propose_org_fact", fact="owner", subject_kind="aws_account",
              subject_id="123456789012", value={"team": "payments"}, source="human",
              confidence=1.0)
    assert r["fact"]["source"] == "agent:human"
    assert org.load().facts[0].status == "proposed"


def test_propose_org_fact_respects_a_rejection_and_reports_bad_input(odir):
    f = proposed()
    org.reject(f.key, human("@maria"))
    r = _tool("propose_org_fact", fact="owner", subject_kind="aws_account",
              subject_id="123456789012", value={"team": "payments"}, source="x:y")
    assert r["result"] == "suppressed_rejected" and r["status"] == "rejected"
    bad = _tool("propose_org_fact", fact="owner", subject_kind="planet", subject_id="mars",
                value={"team": "x"}, source="x:y")
    assert bad["result"] == "invalid" and "subject.kind" in bad["error"]


def test_read_tools(odir):
    f = proposed(dollars_monthly=500)
    m = _tool("get_org_model", kind="owner")
    assert m["counts"]["proposed"] == 1 and m["facts"][0]["key"] == f.key
    assert _tool("get_org_model", status="confirmed")["facts"] == []
    q = _tool("list_org_questions", limit=5)
    assert q["questions"][0]["command"] == f"nable org confirm {f.key}"
    cov = _tool("get_org_coverage")
    assert cov["pct_confirmed"] is None and "not read" in cov["summary"]


def test_there_is_no_tool_that_confirms_or_rejects():
    from finops import server
    from finops.tool_surface import DESTRUCTIVE_TOOLS, WRITE_TOOLS, tool_annotation
    names = {t.name for t in server.mcp._tool_manager.list_tools()}
    org_tools = {n for n in names if "org_" in n and n not in
                 ("get_org_cost_summary", "list_org_accounts")}
    assert org_tools == {"get_org_model", "get_org_coverage", "list_org_questions",
                         "propose_org_fact"}
    assert "propose_org_fact" in WRITE_TOOLS and "propose_org_fact" not in DESTRUCTIVE_TOOLS
    ann = tool_annotation("propose_org_fact")
    assert ann["readOnlyHint"] is False and ann["destructiveHint"] is False
    for n in ("get_org_model", "get_org_coverage", "list_org_questions"):
        assert tool_annotation(n)["readOnlyHint"] is True
