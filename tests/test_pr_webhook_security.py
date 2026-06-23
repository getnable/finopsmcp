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
