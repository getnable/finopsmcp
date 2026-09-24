"""Ticket and PR creation never resends a POST that may already have landed."""
from __future__ import annotations

import httpx
import pytest

from finops.integrations import ticketing as T


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(T.time, "sleep", lambda *_: None)


def _count(monkeypatch, exc_or_status):
    calls = {"n": 0}

    def fake(method, url, **kw):
        calls["n"] += 1
        req = httpx.Request(method, url)
        if isinstance(exc_or_status, int):
            return httpx.Response(exc_or_status, request=req)
        raise exc_or_status("boom", request=req)

    monkeypatch.setattr(T.httpx, "request", fake)
    with pytest.raises((httpx.HTTPError,)):
        T.http_with_retry("POST", "https://api.example/issues", json={})
    return calls["n"]


def test_post_is_not_resent_after_a_read_timeout(monkeypatch):
    assert _count(monkeypatch, httpx.ReadTimeout) == 1


def test_post_is_not_resent_after_a_client_error(monkeypatch):
    assert _count(monkeypatch, 400) == 1


def test_post_is_resent_when_it_never_connected(monkeypatch):
    assert _count(monkeypatch, httpx.ConnectError) == 3


def test_post_is_resent_on_429(monkeypatch):
    assert _count(monkeypatch, 429) == 3


@pytest.mark.parametrize("repo", ["acme/infra/../../user", "acme", "../x/y", "a/b?x=1"])
def test_create_github_pr_rejects_repo_path_games(repo):
    with pytest.raises(ValueError):
        T.create_github_pr(repo=repo, title="t", body="b", head="h", token="x")
