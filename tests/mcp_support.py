"""Shared helpers for tests that drive MCP tools the way a client does.

A tool call goes through the in-memory MCP client, so the dispatch wrapper,
argument scoping, validation and serialisation are all on the path. Cost
connectors are fakes that answer or fail the way the real ones do.
"""
from __future__ import annotations

import asyncio
import json

from finops.connectors.base import CostEntry, CostSummary


def summary(provider, start, end, by_service) -> CostSummary:
    total = sum(by_service.values())
    entries = [CostEntry(provider=provider, account_id="1", account_name="1",
                         service=s, region="", amount=a) for s, a in by_service.items()]
    return CostSummary(provider=provider, start_date=start, end_date=end,
                       total_usd=total, by_service=dict(by_service),
                       by_account={"1": total} if by_service else {},
                       by_region={}, entries=entries)


class Reads:
    """A connected provider whose bill is `by_service` for any window. Remembers
    the windows it was asked for."""

    def __init__(self, provider, by_service):
        self.provider, self._by = provider, by_service
        self.asked: list = []

    async def is_configured(self):
        return True

    async def get_costs(self, start, end, granularity="MONTHLY", **kw):
        self.asked.append((start, end))
        return summary(self.provider, start, end, self._by)


class Denied(Reads):
    """A connected provider whose every read is refused, as an IAM gap is."""

    def __init__(self, provider):
        super().__init__(provider, {})

    async def get_costs(self, start, end, granularity="MONTHLY", **kw):
        self.asked.append((start, end))
        raise RuntimeError(
            "AWS credentials are valid but missing Cost Explorer permissions. "
            "Add ce:GetCostAndUsage to your IAM policy")


def connectors_fixture(monkeypatch, tmp_path):
    """Generator body for a `connectors` fixture: yields a setter that installs
    fake connectors as the connected set."""
    from finops import cache, demo_data, server

    cache.clear()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("FINOPS_DEMO", raising=False)
    monkeypatch.setattr(demo_data, "_real_provider_connected", lambda: True)
    state: dict = {}

    async def _active(subset=None):
        pool = server._ALL_CONNECTORS if subset is None else subset
        return {n: c for n, c in state.items() if n in pool}

    monkeypatch.setattr(server, "_active", _active)

    def _set(**conns):
        state.clear()
        state.update(conns)
        cache.clear()
    yield _set
    cache.clear()


def call(name: str, args: dict | None = None, identity=None):
    """One tool call through the in-memory MCP client. JSON answers come back
    decoded, text answers as text. `identity` is the API key identity the
    server process runs as (FINOPS_API_KEY)."""
    from mcp.shared.memory import create_connected_server_and_client_session

    from finops import server
    from finops.auth.rbac import set_current_identity

    async def run():
        set_current_identity(identity)
        async with create_connected_server_and_client_session(server.mcp._mcp_server) as c:
            res = await c.call_tool(name, args or {})
            text = "".join(getattr(b, "text", "") for b in res.content)
            assert not res.isError, text
            try:
                return json.loads(text)
            except ValueError:
                return text
    return asyncio.run(run())
