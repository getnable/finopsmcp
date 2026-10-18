# SPDX-License-Identifier: Apache-2.0
"""`nable cloud connect`: the free tool's org model and guard ledger, carried
into a paid workspace, and nothing else (D58).

What has to stay true:
  - a dry run first, always: it builds the payload from the resolved org
    dir's known org model files and the guard ledger's lines exactly as
    stored (already redacted when they were written), checks the chain on
    the very bytes it holds, shows a broken chain and leaves it out, prints
    the destination host, the file names, the record counts, the byte size
    and the sha256, and writes those exact bytes to a file the person can
    read. It opens no connection;
  - a send needs --confirm with that sha256, rebuilds the payload, and
    refuses when the bytes changed since the person looked;
  - what leaves is exactly what was shown: ordered batches of at most 256 KiB,
    each a slice of the bytes shown, cut where a line ends; a resumed send
    skips only what the box says it already holds;
  - HTTPS only; http, another scheme, credentials in the URL, a redirect and
    air-gap mode are refused;
  - the workspace key comes from NABLE_WORKSPACE_KEY, a key file with mode
    0600, or a prompt, and is never stored, printed or logged.

No listener and no network: every request goes to a fake opener, a function
the code under test calls with each urllib Request.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import urllib.request
from pathlib import Path

import pytest

from finops import cloud_connect as cc
from finops import guard_ledger

KEY = "nbl_test_workspace_key_0123456789abcdef"  # pragma: allowlist secret
BOX = "https://box.example.com"
SHA = hashlib.sha256


# ── fixtures: an org model, a ledger, a box ─────────────────────────────────

ORG = {
    "owners.yaml": ("# nable org model v1\n- fact: owner\n  subject: aws_account:123456789012\n"
                    "  value: {team: payments}\n  status: confirmed\n"),
    "teams.yaml": "# nable org model v1\n- fact: team\n  subject: team:payments\n  value: {}\n",
}


@pytest.fixture
def org_dir(tmp_path, monkeypatch):
    d = tmp_path / "org"
    d.mkdir()
    for name, text in ORG.items():
        (d / name).write_text(text, encoding="utf-8")
    (d / "notes.yaml").write_text("secret: not an org model file\n")  # not a known name
    (d / "README.md").write_text("not yaml\n")
    monkeypatch.setenv("FINOPS_ORG_DIR", str(d))
    return d


def records(n: int, *, start: int = 0, reason: str = "fixture") -> list[dict]:
    return [{"ts": f"2026-10-{1 + (start + i) // 1440 % 28:02d}T"
                   f"{(start + i) // 60 % 24:02d}:{(start + i) % 60:02d}:00+00:00",
             "decision": "ask" if i % 3 == 0 else "allow", "action_type": "one_way",
             "harness": "claude-code", "session": "s-1", "tool": "Bash",
             "command": "aws ec2 terminate-instances --instance-ids i-0abc",
             "reason": reason} for i in range(n)]


def write_ledger(recs: list[dict], path: Path | None = None) -> list[bytes]:
    """Chained exactly as guard_ledger.append chains, at fixture times."""
    path = path or guard_ledger.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = path.read_bytes() if path.exists() else b""
    prev = SHA(raw.rstrip(b"\n").rsplit(b"\n", 1)[-1]).hexdigest() if raw else guard_ledger.GENESIS
    out = []
    with path.open("ab") as fh:
        for rec in recs:
            line = json.dumps({"v": 1, **rec, "prev": prev}, separators=(",", ":"),
                              sort_keys=True).encode()
            prev = SHA(line).hexdigest()
            fh.write(line + b"\n")
            out.append(line)
    return out


@pytest.fixture
def ledger(org_dir):
    return write_ledger(records(5))


class Box:
    """A paid workspace's /api/free-tool/ as far as the client relies on it:
    status reports what is held, connect continues each chain's head. Every
    request is recorded with its headers, so a test reads exactly what left."""

    def __init__(self, *, refuse_batch: int | None = None):
        self.requests: list[dict] = []
        self.org_sha: str | None = None
        self.chains: dict[str, list[str]] = {}
        self.refuse_batch = refuse_batch
        self.posts = 0

    def __call__(self, req: urllib.request.Request, timeout: float):
        assert timeout and timeout <= 60
        self.requests.append({"method": req.get_method(), "url": req.full_url,
                              "headers": dict(req.header_items()), "body": req.data or b""})
        path = req.full_url.split("://", 1)[1].split("/", 1)[1]
        if req.get_method() == "GET" and path == "api/free-tool/status":
            return 200, json.dumps(self.status()).encode()
        if req.get_method() == "POST" and path == "api/free-tool/connect":
            self.posts += 1
            if self.refuse_batch == self.posts:
                return 409, json.dumps({"error": "chain_conflict",
                                        "message": "does not continue the head"}).encode()
            for raw in (req.data or b"").split(b"\n"):
                if not raw:
                    continue
                doc = json.loads(raw)
                if doc["kind"] == "org_model":
                    self.org_sha = SHA(raw).hexdigest()
                else:
                    held = self.chains.setdefault(doc["chain"], [])
                    h = SHA(doc["line"].encode()).hexdigest()
                    if h not in held:
                        held.append(h)
            return 200, json.dumps({"accepted": True}).encode()
        return 404, b'{"error": "not_found"}'

    def status(self) -> dict:
        return {"org_model": ({"versions": 1, "latest": {"sha256": self.org_sha}}
                              if self.org_sha else None),
                "chains": [{"chain_id": c, "lines": len(h), "head": h[-1]}
                           for c, h in self.chains.items()],
                "totals": {"lines": sum(len(h) for h in self.chains.values())}}

    @property
    def posted(self) -> list[bytes]:
        return [r["body"] for r in self.requests if r["method"] == "POST"]


def no_opener(req, timeout):
    raise AssertionError(f"a dry run opened {req.full_url}")


# ── C3: the dry run ──────────────────────────────────────────────────────────

def test_the_payload_is_the_known_org_files_and_the_ledger_lines_as_stored(ledger, org_dir):
    p = cc.build()
    docs = [json.loads(x) for x in p.data.split(b"\n") if x]
    assert docs[0]["kind"] == "org_model" and docs[0]["files"] == ORG
    assert "notes.yaml" not in json.dumps(docs[0]), "a file that is not an org model file left"
    lines = [d for d in docs[1:]]
    assert [d["kind"] for d in lines] == ["ledger_line"] * 5
    assert [d["line"].encode() for d in lines] == ledger, "a ledger line was not sent as stored"
    assert {d["chain"] for d in lines} == {SHA(ledger[0]).hexdigest()}
    assert set().union(*(d.keys() for d in docs)) == {"v", "kind", "files", "chain", "line"}
    assert p.data.endswith(b"\n") and p.sha256 == SHA(p.data).hexdigest()
    assert p.ledger["records"] == 5 and p.ledger["ok"] is True
    assert p.ledger["chain_id"] == SHA(ledger[0]).hexdigest()
    assert p.org["files"] == {n: len(t.encode()) for n, t in ORG.items()}
    assert cc.build().data == p.data, "the same inputs gave different bytes"
    assert cc.plan(p, None) == [(0, len(p.data))], "a payload that fits is one request"


def test_the_ledger_lines_keep_the_redaction_they_were_written_with(org_dir):
    guard_ledger.append({"decision": "ask", "action_type": "one_way", "harness": "claude-code",
                         "command": guard_ledger.redact(
                             "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY "
                             "aws ec2 terminate-instances --instance-ids i-1")})
    p = cc.build()
    assert b"wJalrXUtnFEMIK7MDENG" not in p.data
    assert b"[REDACTED]" in p.data
    assert json.loads(p.data.split(b"\n")[1])["line"].encode() == \
        guard_ledger.ledger_path().read_bytes().rstrip(b"\n")


def test_a_broken_chain_is_shown_and_left_out(ledger, org_dir):
    path = guard_ledger.ledger_path()
    raw = path.read_bytes().replace(b'"reason":"fixture"', b'"reason":"edited!!"', 1)
    path.write_bytes(raw)
    p = cc.build()
    # Record 1 was edited, so record 2's prev no longer names it.
    assert p.ledger["ok"] is False and p.ledger["broken_at"] == 2
    assert "breaks at record 2" in p.ledger["left_out"]
    assert [json.loads(x)["kind"] for x in p.data.split(b"\n") if x] == ["org_model"]


def test_a_line_still_being_written_is_left_for_next_time(ledger, org_dir):
    with guard_ledger.ledger_path().open("ab") as fh:
        fh.write(b'{"v":1,"ts":"2026-10-18')
    p = cc.build()
    assert p.ledger["records"] == 5 and p.ledger["partial"] is True and p.ledger["ok"]


def test_an_org_file_that_is_a_symlink_too_large_or_not_text_is_left_out(org_dir, tmp_path):
    secret = tmp_path / "id_rsa"
    secret.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")  # pragma: allowlist secret
    (org_dir / "accounts.yaml").symlink_to(secret)
    (org_dir / "policy.yaml").write_bytes(b"x" * (cc.ORG_FILE_MAX + 1))
    (org_dir / "tags.yaml").write_bytes(b"\xff\xfe not utf-8")
    p = cc.build()
    assert b"OPENSSH" not in p.data
    assert set(json.loads(p.data.split(b"\n")[0])["files"]) == set(ORG)
    assert set(p.org["not_read"]) == {"accounts.yaml", "policy.yaml", "tags.yaml"}
    assert "symlink" in p.org["not_read"]["accounts.yaml"]


def test_an_untrusted_repo_org_dir_is_not_sent(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "nable.org").mkdir()
    (repo / "nable.org" / "owners.yaml").write_text(ORG["owners.yaml"])
    monkeypatch.delenv("FINOPS_ORG_DIR", raising=False)
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(repo)
    p = cc.build()
    assert p.org["rule"] == "repo" and "not trusted" in p.org["left_out"]
    assert p.data == b""


def test_nothing_to_send_is_an_empty_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("FINOPS_ORG_DIR", str(tmp_path / "none"))
    p = cc.build()
    assert p.data == b"" and p.ledger["records"] == 0 and p.org["files"] == {}


def test_a_ledger_over_the_box_s_cap_is_left_out_and_says_why(ledger, org_dir, monkeypatch):
    monkeypatch.setattr(cc, "LEDGER_LINES_MAX", 4)
    p = cc.build()
    assert "4" in p.ledger["left_out"] and p.ledger["records"] == 5
    monkeypatch.setattr(cc, "LEDGER_LINES_MAX", 100_000)
    monkeypatch.setattr(cc, "LEDGER_BYTES_MAX", 100)
    assert "bytes" in cc.build().ledger["left_out"]


def test_the_dry_run_saves_exactly_the_bytes_and_says_what_and_where(ledger, org_dir,
                                                                     tmp_path):
    p = cc.build()
    out = cc.save(p, tmp_path / "payload.jsonl")
    assert out.read_bytes() == p.data
    assert out.stat().st_mode & 0o077 == 0
    text = cc.describe(p, "https://box.example.com:8443", out)
    for shown in ("box.example.com:8443", "owners.yaml", "teams.yaml", "5 records",
                  f"{len(p.data):,} bytes", p.sha256, str(out), "Nothing was sent"):
        assert shown in text, shown
    assert f"--confirm {p.sha256}" in text


def test_the_default_payload_file_sits_beside_the_ledger(ledger, org_dir):
    out = cc.save(cc.build())
    assert out.parent == guard_ledger.ledger_path().parent


def test_saving_never_follows_a_symlink(ledger, org_dir, tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("keep me")
    link = tmp_path / "payload.jsonl"
    link.symlink_to(target)
    cc.save(cc.build(), link)
    assert target.read_text() == "keep me" and not link.is_symlink()


# ── C4: the box, the key, the send ───────────────────────────────────────────

@pytest.mark.parametrize("url, ok", [
    ("https://box.example.com", "https://box.example.com"),
    ("https://box.example.com/", "https://box.example.com"),
    ("HTTPS://Box.Example.com:8443", "https://box.example.com:8443"),
    ("http://box.example.com", None),
    ("ftp://box.example.com", None),
    ("box.example.com", None),
    ("https://user:pass@box.example.com", None),  # pragma: allowlist secret
    ("https://box.example.com/api?x=1", None),
    ("https://", None),
    ("https://[::1]:8443", "https://[::1]:8443"),
])
def test_only_an_https_box_is_a_destination(url, ok):
    if ok is None:
        with pytest.raises(cc.ConnectError):
            cc.parse_box(url)
    else:
        assert cc.parse_box(url) == ok


def test_air_gap_refuses_any_send(ledger, org_dir, monkeypatch):
    monkeypatch.setenv("FINOPS_AIRGAP", "1")
    p = cc.build()
    with pytest.raises(cc.ConnectError, match="FINOPS_AIRGAP"):
        cc.send(p, BOX, KEY, opener=no_opener)
    with pytest.raises(cc.ConnectError, match="FINOPS_AIRGAP"):
        cc.status(BOX, KEY, opener=no_opener)


def test_an_http_url_is_refused_even_past_the_parser(ledger, org_dir):
    with pytest.raises(cc.ConnectError, match="HTTPS"):
        cc.send(cc.build(), "http://box.example.com", KEY, opener=no_opener)


def test_a_send_is_exactly_the_bytes_shown_in_ordered_slices(org_dir):
    write_ledger(records(1300, reason="r" * 400))
    p = cc.build()
    assert len(p.data) > 3 * cc.BATCH_MAX, "the fixture is too small to need batches"
    box = Box()
    result = cc.send(p, BOX, KEY, opener=box)
    bodies = box.posted
    assert len(bodies) >= 4 and all(len(b) <= cc.BATCH_MAX for b in bodies)
    assert b"".join(bodies) == p.data, "what left is not what was shown"
    at = 0
    for b in bodies:
        assert p.data[at:at + len(b)] == b and b.endswith(b"\n"), "not a slice cut at a line"
        at += len(b)
    assert result["bytes_sent"] == len(p.data) and result["batches"] == len(bodies)
    assert box.requests[0]["method"] == "GET", "the send did not ask what the box holds first"


def test_a_resumed_send_sends_only_what_the_box_does_not_hold(ledger, org_dir):
    p = cc.build()
    box = Box()
    box.org_sha = p.org_sha256
    box.chains[p.ledger["chain_id"]] = [SHA(x).hexdigest() for x in ledger[:3]]
    result = cc.send(p, BOX, KEY, opener=box)
    (body,) = box.posted
    starts = [a for kind, a, _end in p.lines if kind == "ledger_line"]
    assert body == p.data[starts[3]:] and body.count(b"\n") == 2
    assert result["bytes_sent"] == len(body) and result["bytes_already_held"] == \
        len(p.data) - len(body)
    # Everything held now: nothing is posted at all.
    box.requests.clear()
    assert cc.send(p, BOX, KEY, opener=box)["bytes_sent"] == 0
    assert box.posted == []


def test_a_changed_org_model_is_sent_without_resending_the_ledger(ledger, org_dir):
    p = cc.build()
    box = Box()
    cc.send(p, BOX, KEY, opener=box)
    (org_dir / "teams.yaml").write_text(ORG["teams.yaml"] + "# changed\n")
    p2 = cc.build()
    box.requests.clear()
    cc.send(p2, BOX, KEY, opener=box)
    (body,) = box.posted
    assert json.loads(body)["kind"] == "org_model" and p2.data.startswith(body)


def test_a_refused_batch_stops_the_send_and_says_how_far_it_got(org_dir):
    write_ledger(records(900, reason="r" * 400))
    p = cc.build()
    box = Box(refuse_batch=2)
    with pytest.raises(cc.ConnectError) as err:
        cc.send(p, BOX, KEY, opener=box)
    assert len(box.posted) == 2, "it kept sending after a refusal"
    msg = str(err.value)
    assert "batch 2 of" in msg and "409" in msg and "chain_conflict" in msg
    assert KEY not in msg


def test_the_key_is_only_ever_in_the_authorization_header(ledger, org_dir, tmp_path, caplog,
                                                          capsys):
    caplog.set_level(logging.DEBUG)
    p = cc.build()
    out = cc.save(p, tmp_path / "payload.jsonl")
    box = Box()
    cc.send(p, BOX, KEY, opener=box)
    cc.status(BOX, KEY, opener=box)
    for r in box.requests:
        assert r["headers"]["Authorization"] == f"Bearer {KEY}"
        assert KEY not in r["url"] and KEY.encode() not in r["body"]
        assert r["url"].startswith("https://box.example.com/api/free-tool/")
    captured = capsys.readouterr()
    assert KEY not in captured.out + captured.err + caplog.text
    assert KEY.encode() not in out.read_bytes() and KEY.encode() not in p.data
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert KEY.encode() not in f.read_bytes(), f
    for f in guard_ledger.ledger_path().parent.rglob("*"):
        if f.is_file():
            assert KEY.encode() not in f.read_bytes(), f


# ── the key ──────────────────────────────────────────────────────────────────

def test_the_key_from_the_environment():
    assert cc.read_key(environ={cc.KEY_ENV: f"  {KEY}\n"}, prompt=None) == KEY


def test_the_key_from_a_file_only_with_mode_0600(tmp_path):
    f = tmp_path / "key"
    f.write_text(KEY + "\n")
    f.chmod(0o600)
    assert cc.read_key(key_file=str(f), environ={}, prompt=None) == KEY
    f.chmod(0o644)
    with pytest.raises(cc.ConnectError, match="0600"):
        cc.read_key(key_file=str(f), environ={}, prompt=None)
    link = tmp_path / "link"
    f.chmod(0o600)
    link.symlink_to(f)
    with pytest.raises(cc.ConnectError):
        cc.read_key(key_file=str(link), environ={}, prompt=None)


def _within(seconds, fn):
    """fn()'s result or exception, failing the test if it has not returned in
    time (a blocking open of a FIFO never returns)."""
    import threading
    box: dict = {}

    def go():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed back to the test
            box["error"] = exc
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), "it waited on a FIFO"
    if "error" in box:
        raise box["error"]
    return box["value"]


def test_a_fifo_in_a_key_file_s_or_an_org_file_s_place_never_hangs(org_dir, tmp_path):
    fifo = tmp_path / "key"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(cc.ConnectError, match="regular file"):
        _within(5, lambda: cc.read_key(key_file=str(fifo), environ={}, prompt=None))
    (org_dir / "teams.yaml").unlink()
    os.mkfifo(org_dir / "teams.yaml")
    p = _within(5, cc.build)
    assert p.org["not_read"]["teams.yaml"] == "not a regular file"


def test_the_key_from_a_prompt_and_never_without_one(monkeypatch):
    asked = []
    got = cc.read_key(environ={}, prompt=lambda text: asked.append(text) or KEY)
    assert got == KEY and asked and KEY not in asked[0]
    with pytest.raises(cc.ConnectError, match=cc.KEY_ENV):
        cc.read_key(environ={}, prompt=None)


def test_a_key_with_a_newline_or_a_control_character_is_refused():
    for bad in ("a\nb", "a\x00b", "", "   "):
        with pytest.raises(cc.ConnectError):
            cc.read_key(environ={cc.KEY_ENV: bad}, prompt=None)


# ── the default opener ───────────────────────────────────────────────────────

def test_the_default_opener_follows_no_redirect():
    handler = cc._NoRedirect()
    req = urllib.request.Request("https://box.example.com/api/free-tool/status",
                                 headers={"Authorization": f"Bearer {KEY}"})
    with pytest.raises(cc.ConnectError, match="redirect"):
        handler.redirect_request(req, io.BytesIO(), 302, "Found", {},
                                 "http://evil.example.com/")


def test_the_default_opener_is_https_only():
    opener = cc.default_opener()
    names = {type(h).__name__ for h in opener.handlers}
    assert "_NoRedirect" in names and "HTTPRedirectHandler" not in names
    assert "HTTPHandler" not in names and "FTPHandler" not in names
    with pytest.raises(cc.ConnectError, match="HTTPS"):
        cc._request(lambda r, t: (200, b"{}"), "GET", "http://box.example.com/x", KEY)


def test_status_reports_what_the_box_holds(ledger, org_dir):
    box = Box()
    cc.send(cc.build(), BOX, KEY, opener=box)
    st = cc.status(BOX, KEY, opener=box)
    assert st["totals"]["lines"] == 5 and st["org_model"]["latest"]["sha256"]


def test_a_status_the_box_refuses_is_an_error_without_the_key():
    def refuse(req, timeout):
        return 403, b'{"error": "forbidden", "message": "not an analyst key"}'
    with pytest.raises(cc.ConnectError) as err:
        cc.status(BOX, KEY, opener=refuse)
    assert "403" in str(err.value) and "forbidden" in str(err.value)
    assert KEY not in str(err.value)


def test_nothing_in_the_module_reads_a_key_from_argv():
    src = Path(cc.__file__).read_text(encoding="utf-8")
    assert "sys.argv" not in src
    assert os.environ.get(cc.KEY_ENV) is None


def test_the_network_manifest_lists_the_box_a_person_names():
    root = Path(__file__).resolve().parents[1]
    doc = json.loads((root / "docs" / "network-manifest.json").read_text(encoding="utf-8"))
    (box,) = [e for e in doc["endpoints"] if e["category"] == "user_named_workspace"]
    assert "--box" in box["host"] and "FINOPS_AIRGAP=1" in box["disabled_by"]
    for said in (cc.CONNECT_PATH, cc.STATUS_PATH, "--confirm", "HTTPS"):
        assert said in box["purpose"], said
