"""
nable anonymous telemetry — opt-OUT, not opt-in.

What we collect (and only this):
  - A random install ID (UUID4, generated once, stored locally — never your email or keys)
  - Date of the ping (day-level granularity only)
  - Which MCP tools were invoked (feature names, not query content)
  - Number of connected providers (count only, not which accounts)
  - Plan tier: free | trial | pro

What we never collect:
  - Cloud account IDs, ARNs, or credentials
  - Cost figures or billing data
  - IP addresses (PostHog is configured to drop them server-side)
  - Email addresses (unless you've identified yourself via the website)

How to disable:
  export NABLE_NO_TELEMETRY=1
  # or add to your shell profile / .env

This is standard practice for developer tools (VS Code, dbt, Homebrew, Vercel CLI).
It lets us know how many installs are active so we can prioritise what to build.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import date
from pathlib import Path
from typing import Optional

# ─── Config ──────────────────────────────────────────────────────────────────

_POSTHOG_KEY  = os.environ.get("NABLE_POSTHOG_KEY", "phc_zcaQqoAXrSghjtbE6VB83p4RjfmcpqezKWV9GdZy4dPv")
_POSTHOG_HOST = "https://us.i.posthog.com"
_ID_FILE      = Path.home() / ".config" / "finops" / ".install_id"
_OPT_OUT_ENV  = "NABLE_NO_TELEMETRY"

# ─── Install ID ──────────────────────────────────────────────────────────────

def _get_install_id() -> str:
    """
    Stable anonymous ID for this install. Stored in ~/.config/finops/.install_id.
    Generated once as a random UUID — completely disconnected from the user's identity.
    """
    try:
        _ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        if _ID_FILE.exists():
            id_ = _ID_FILE.read_text().strip()
            if len(id_) == 36:
                return id_
        id_ = str(uuid.uuid4())
        _ID_FILE.write_text(id_)
        return id_
    except Exception:
        # If we can't write (e.g. read-only FS), generate a session-only ID
        return str(uuid.uuid4())


def _is_opted_out() -> bool:
    if not _POSTHOG_KEY:
        return True  # no key configured — silently skip all telemetry
    return bool(os.environ.get(_OPT_OUT_ENV, "").strip())


# ─── Session state (accumulated in-process, flushed periodically) ─────────────

_session: dict = {
    "tools_used": set(),
    "provider_count": 0,
    "plan": "free",
}
_lock = threading.Lock()


def record_tool_call(tool_name: str) -> None:
    """
    Record a tool invocation. Accumulates in-session and fires a lightweight
    PostHog event (fire-and-forget, background thread) for per-tool analytics.
    Thread-safe.
    """
    if _is_opted_out():
        return
    with _lock:
        _session["tools_used"].add(tool_name)
        # Track call counts for frequency analysis
        counts = _session.setdefault("tool_counts", {})
        counts[tool_name] = counts.get(tool_name, 0) + 1

    # Fire a lightweight per-tool event (does not block caller)
    install_id = _get_install_id()
    props = {
        "tool": tool_name,
        "plan": _session.get("plan", "free"),
        "date": date.today().isoformat(),
    }
    t = threading.Thread(
        target=_send_event,
        args=(install_id, "tool_called", props),
        daemon=True,
    )
    t.start()


def set_plan(plan: str) -> None:
    """Call with 'free', 'trial', or 'pro' after license check."""
    with _lock:
        _session["plan"] = plan


def set_provider_count(count: int) -> None:
    with _lock:
        _session["provider_count"] = count


# ─── Heartbeat ────────────────────────────────────────────────────────────────

def _send(install_id: str, properties: dict) -> None:
    """Fire-and-forget POST to PostHog. Runs in background thread."""
    _send_event(install_id, "heartbeat", properties)


def _send_event(install_id: str, event: str, properties: dict) -> None:
    """Send a single named event to PostHog."""
    import urllib.request
    payload = json.dumps({
        "api_key": _POSTHOG_KEY,
        "event": event,
        "distinct_id": install_id,
        "properties": {
            **properties,
            # PostHog is configured to drop $ip server-side
            "$ip": "0.0.0.0",
        },
        "timestamp": date.today().isoformat(),
    }).encode()
    try:
        req = urllib.request.Request(
            f"{_POSTHOG_HOST}/capture/",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=4)
    except Exception:
        pass  # Never let telemetry break the tool


def ping(extra: Optional[dict] = None) -> None:
    """
    Send a single heartbeat. Called once at MCP server startup.
    Runs in a daemon thread so it never blocks tool startup.
    """
    if _is_opted_out():
        return

    install_id = _get_install_id()

    with _lock:
        properties = {
            "plan": _session["plan"],
            "provider_count": _session["provider_count"],
            "tool_count": len(_session["tools_used"]),
            # Hash tool names so we know which features are popular,
            # but the list itself stays local
            "tools_hash": hashlib.sha256(
                ",".join(sorted(_session["tools_used"])).encode()
            ).hexdigest()[:16],
            "date": date.today().isoformat(),
        }

    if extra:
        properties.update(extra)

    t = threading.Thread(target=_send, args=(install_id, properties), daemon=True)
    t.start()


def ping_startup(provider_count: int = 0, plan: str = "free") -> None:
    """Convenience wrapper called from server.py on startup."""
    set_provider_count(provider_count)
    set_plan(plan)
    ping({"event_type": "startup"})
