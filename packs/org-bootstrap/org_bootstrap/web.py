# SPDX-License-Identifier: Apache-2.0
"""A small JSON-over-HTTPS reader for the adapters, standard library only.

- A URL must be https (plain http only to this machine's loopback address),
  carry no user or password, and name a host the pack declares in its
  network. A URL that fails any of that is never fetched.
- No proxy and no redirect: a redirect could carry the Authorization header
  to another host, so one is an error.
- A token goes in the Authorization header and nowhere else: never in a URL,
  an error message or the log.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

TIMEOUT_S = 30
MAX_BODY_BYTES = 8 * 1024 * 1024
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_NEXT = re.compile(r'<([^<>\s]+)>\s*;\s*rel="next"')


class HttpError(RuntimeError):
    """A request that failed. The message names the path and the status,
    never a header or a token."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HttpError(f"{urllib.parse.urlsplit(req.full_url).path}: HTTP {code} redirect "
                        "refused (a redirect could carry the token to another host)")


def _host_port(url: str) -> tuple[str, int, str]:
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    port = u.port or (443 if u.scheme == "https" else 80)
    return host, port, u.scheme


def url_problem(ctx: Any, url: str) -> str | None:
    """Why `url` may not be fetched by this pack, or None."""
    try:
        u = urllib.parse.urlsplit(url)
        host, port, scheme = _host_port(url)
    except ValueError:
        return "is not a URL"
    if u.username or u.password:
        return "must not carry a user name or password (the token is a separate secret)"
    if scheme not in ("https", "http") or not host:
        return "must be an https:// URL"
    if scheme == "http" and host not in LOOPBACK:
        return "must be https (plain http only to this machine)"
    declared = [str(x) for x in (ctx.capabilities.get("network") or ())]
    for entry in declared:
        h, sep, p = entry.rpartition(":") if ":" in entry else (entry, "", "")
        if h.lower() == host and (not sep or (p.isdigit() and int(p) == port)):
            return None
    return (f"{host}:{port} is not in this pack's declared network "
            f"({', '.join(declared) or 'none'}); add it to [capabilities].network and have "
            "the change approved (the pack's digest changes with it)")


def get_json(url: str, token: str | None, *, accept: str = "application/json",
             extra_headers: dict[str, str] | None = None) -> tuple[Any, dict[str, str]]:
    """(the decoded JSON body, the response headers). Raises HttpError."""
    headers = {"Accept": accept, "User-Agent": "nable-org-bootstrap/1.0"}
    headers.update(extra_headers or {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    path = urllib.parse.urlsplit(url).path
    try:
        with opener.open(req, timeout=TIMEOUT_S) as resp:  # nosec B310 - https or loopback only, checked by url_problem
            body = resp.read(MAX_BODY_BYTES + 1)
            got = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as e:
        raise HttpError(f"{path}: HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise HttpError(f"{path}: {type(e).__name__} ({reason})") from None
    if len(body) > MAX_BODY_BYTES:
        raise HttpError(f"{path}: the response is larger than {MAX_BODY_BYTES} bytes")
    try:
        return json.loads(body.decode("utf-8")), got
    except (UnicodeDecodeError, ValueError):
        raise HttpError(f"{path}: the response is not JSON") from None


def next_link(headers: dict[str, str], base: str) -> str | None:
    """The rel="next" URL of a Link header, when it is on the same scheme,
    host and port as `base`; None otherwise."""
    m = _NEXT.search(headers.get("link", ""))
    if not m:
        return None
    nxt = m.group(1)
    try:
        same = _host_port(nxt) == _host_port(base)
    except ValueError:
        return None
    return nxt if same else None
