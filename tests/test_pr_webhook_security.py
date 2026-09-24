"""Security hardening for the PR-comment webhook: validate path segments before
they reach api.github.com (CodeQL py/partial-ssrf) and reject malformed input at
the trust boundary, before any HTTP call."""
from __future__ import annotations

import finops.pr_comments.github_app as ga
import finops.pr_comments.webhook as wh


def test_handle_event_rejects_path_injection_in_owner():
    payload = {
        "action": "opened",
        "pull_request": {"number": 1, "head": {"sha": "abc"}},
        "repository": {"owner": {"login": "../../evil"}, "name": "repo"},
        "installation": {"id": 5},
    }
    out = ga.handle_pull_request_event(payload)
    assert out["status"] == "rejected" and "owner/repo" in out["reason"]


def test_handle_event_rejects_non_int_pr_number():
    payload = {
        "action": "opened",
        "pull_request": {"number": "1/../x", "head": {"sha": "abc"}},
        "repository": {"owner": {"login": "acme"}, "name": "infra"},
        "installation": {"id": 5},
    }
    out = ga.handle_pull_request_event(payload)
    assert out["status"] == "rejected"


def test_path_segment_regexes_reject_traversal_and_newlines():
    assert ga._GH_SEGMENT.match("acme-corp_1.2")
    assert not ga._GH_SEGMENT.match("a/b")       # no path separators
    assert not ga._GH_SEGMENT.match("a\nb")      # no CR/LF
    assert wh._GH_REPO.match("acme/infra")
    assert not wh._GH_REPO.match("acme/infra/../x")
    assert not wh._GH_REPO.match("acme")          # must be owner/repo


def test_a_dot_segment_is_rejected_even_though_its_characters_are_legal():
    """The gap the character class left open.

    "." and ".." are built entirely from permitted characters, so they passed
    the regex, and `owner=".."` produced

        https://api.github.com/repos/../{repo}/pulls/{n}/files

    which a URL normaliser resolves upwards into a different endpoint than the
    caller intended. GitHub allows neither name, so refusing them costs nothing.
    """
    assert ga._valid_segment("acme-corp_1.2")
    assert ga._valid_segment("docs.github.com")   # a real repo name, still fine
    for bad in (".", "..", "...", "", None):
        assert not ga._valid_segment(bad), f"{bad!r} was accepted as a path segment"


def test_an_unbounded_segment_cannot_be_used_to_build_an_enormous_url():
    assert ga._valid_segment("a" * 100)
    assert not ga._valid_segment("a" * 101)


def test_handle_event_rejects_a_dot_owner_before_any_http(monkeypatch):
    """The wiring, not the helper. Deleting the guard from handle_pull_request_event
    must fail something, or the validator above is decoration."""
    called = []
    monkeypatch.setattr(ga, "_headers", lambda *a, **k: called.append(1) or {})
    out = ga.handle_pull_request_event({
        "action": "opened",
        "pull_request": {"number": 1, "head": {"sha": "abc"}},
        "repository": {"owner": {"login": ".."}, "name": "infra"},
        "installation": {"id": 5},
    })
    assert out["status"] == "rejected"
    assert called == [], "built auth headers before validating the path segments"


def test_webhook_rejects_bad_repo_before_any_http(monkeypatch):
    calls = []
    monkeypatch.setattr(wh, "_get_pr_files", lambda *a, **k: calls.append(1) or [])
    wh._handle_pr_event({
        "action": "opened",
        "pull_request": {"number": 1},
        "repository": {"full_name": "acme/infra/../../x"},
    })
    assert calls == []  # rejected before fetching anything


def test_verify_signature_is_constant_time_and_correct():
    import hashlib
    import hmac
    secret, body = "s3cr3t", b'{"a":1}'
    good = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert ga.verify_signature(body, good, secret) is True
    assert ga.verify_signature(body, "sha256=deadbeef", secret) is False
    assert ga.verify_signature(body, "", secret) is False


# ── comment ownership and request bounds ─────────────────────────────────────

class _Resp:
    def __init__(self, data, ok=True):
        self._d, self.is_success = data, ok

    def json(self):
        return self._d


def test_webhook_never_edits_a_comment_it_did_not_write(monkeypatch):
    calls = []
    comments = [{"id": 7, "body": f"{wh.COMMENT_TAG}\nfake estimate", "user": {"login": "pr-author"}}]

    def fake_get(url, **_):
        return _Resp({"login": "nable-bot"}) if url.endswith("/user") else _Resp(comments)

    monkeypatch.setattr(wh, "_TOKEN_LOGIN", None)
    monkeypatch.setattr(wh.httpx, "get", fake_get)
    monkeypatch.setattr(wh.httpx, "patch", lambda url, **_: calls.append(("patch", url)))
    monkeypatch.setattr(wh.httpx, "post", lambda url, **_: calls.append(("post", url)))
    wh._post_or_update_comment("acme/infra", 3, "real estimate")
    assert [c[0] for c in calls] == ["post"]


def test_webhook_updates_its_own_comment(monkeypatch):
    calls = []
    comments = [{"id": 9, "body": f"{wh.COMMENT_TAG}\nold", "user": {"login": "nable-bot"}}]

    def fake_get(url, **_):
        return _Resp({"login": "nable-bot"}) if url.endswith("/user") else _Resp(comments)

    monkeypatch.setattr(wh, "_TOKEN_LOGIN", None)
    monkeypatch.setattr(wh.httpx, "get", fake_get)
    monkeypatch.setattr(wh.httpx, "patch", lambda url, **_: calls.append(("patch", url)))
    monkeypatch.setattr(wh.httpx, "post", lambda url, **_: calls.append(("post", url)))
    wh._post_or_update_comment("acme/infra", 3, "new")
    assert calls == [("patch", "https://api.github.com/repos/acme/infra/issues/comments/9")]


def test_app_never_edits_a_human_comment(monkeypatch):
    calls = []
    tag = f"<!-- {ga.COMMENT_TAG} -->"

    def fake_get(url, headers):
        if url.endswith("/user"):
            raise RuntimeError("installation tokens cannot call /user")
        return [{"id": 5, "body": tag, "user": {"login": "pr-author", "type": "User"}}]

    monkeypatch.setattr(ga, "_gh_get", fake_get)
    monkeypatch.setattr(ga, "_gh_patch", lambda *a: calls.append("patch"))
    monkeypatch.setattr(ga, "_gh_post", lambda *a: calls.append("post"))
    ga._upsert_comment("acme", "infra", 3, tag + "\nbody", {})
    assert calls == ["post"]


def test_webhook_rejects_unbounded_content_length():
    import io
    import threading
    import http.client
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), wh.WebhookHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        for length, want in (("-1", 400), ("abc", 400), (str(wh.MAX_PAYLOAD_BYTES + 1), 413)):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            conn.putrequest("POST", "/webhook/github")
            conn.putheader("Content-Length", length)
            conn.endheaders()
            assert conn.getresponse().status == want
            conn.close()
    finally:
        server.shutdown()
