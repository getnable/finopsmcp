"""No MCP tool may do blocking I/O on the event loop.

FastMCP awaits every tool on the server's single event-loop thread. A tool that
calls boto3, httpx or a synchronous connector from that thread holds it for the
whole round trip: the server cannot answer another request, a cancellation or a
ping, and every asyncio.wait_for deadline elsewhere in the process is disarmed
until the call returns on its own. See test_audit_concurrency.py for how that
turns a 90 second provider deadline into minutes.

The fix is structural rather than per call site. `_instrumented_tool`, the shim
every registered tool passes through, runs a plain `def` tool on a worker
thread. An `async def` tool is trusted to await its blocking work, and the guard
at the bottom of this file keeps that trust honest.

The timing tests fake the cloud boundary with time.sleep, the way a real STS or
Cost Explorer round trip behaves, and measure how long the loop could not run a
ready task while the tool was in flight.
"""
from __future__ import annotations

import asyncio
import gc
import time

import pytest

import finops.server as _srv

# One blocked cloud round trip. The heartbeat threshold is a quarter of it.
BLOCK_S = 0.4
MAX_STALL_S = 0.1


class _Heartbeat:
    """Longest stretch the event loop could not run a task that was ready.

    Same instrument as test_audit_concurrency._Heartbeat: a coroutine that
    awaits keeps this near zero however slow it is, one that blocks pins it to
    the blocking call's duration.
    """

    def __init__(self, tick: float = 0.01) -> None:
        self._tick = tick
        self._stop = False
        self._task: asyncio.Task | None = None
        self.max_stall = 0.0

    async def _beat(self) -> None:
        last = time.monotonic()
        while not self._stop:
            await asyncio.sleep(self._tick)
            now = time.monotonic()
            self.max_stall = max(self.max_stall, now - last - self._tick)
            last = now

    async def __aenter__(self) -> "_Heartbeat":
        # A full collection late in a suite run takes a few hundred ms, and the
        # heartbeat would charge that pause to the tool under test. The
        # assertion is about the tool holding the loop, not about the heap.
        gc.collect()
        gc.freeze()
        self._task = asyncio.create_task(self._beat())
        await asyncio.sleep(0.03)   # let it take a baseline sample
        self.max_stall = 0.0
        return self

    async def __aexit__(self, *exc) -> bool:
        self._stop = True
        if self._task is not None:
            await self._task
        gc.unfreeze()
        return False


@pytest.fixture
def bare_wrapper(monkeypatch):
    """_instrumented_tool with FastMCP registration swapped for identity, so a
    test can wrap a throwaway function without adding it to the live registry
    (the tool-surface completeness tests would see it)."""
    monkeypatch.setattr(_srv, "_original_mcp_tool", lambda *a, **k: (lambda f: f))
    monkeypatch.delenv("FINOPS_DEMO", raising=False)
    monkeypatch.delenv("FINOPS_DEMO_FORCE", raising=False)
    # The first call through the wrapper imports sqlalchemy for the audit log
    # (about 0.3s, once per process). That is import cost, not I/O, and it is
    # paid before any tool runs in a real session; take it here so the
    # heartbeat measures the tool.
    _srv._get_audit_logger()
    import finops.demo_data  # noqa: F401
    return _srv._instrumented_tool


# ── 1. the wrapper itself ────────────────────────────────────────────────────

async def test_a_sync_tool_runs_off_the_event_loop(bare_wrapper):
    def blocking_tool(region: str = "us-east-1") -> dict:
        time.sleep(BLOCK_S)          # a boto3 call, as far as the loop can tell
        return {"region": region}

    wrapped = bare_wrapper()(blocking_tool)

    async with _Heartbeat() as hb:
        out = await wrapped(region="eu-west-1")

    assert out == {"region": "eu-west-1"}
    assert hb.max_stall < MAX_STALL_S, (
        f"a plain def tool held the event loop for {hb.max_stall:.2f}s of its "
        f"{BLOCK_S}s call. _instrumented_tool must run sync tools in a thread"
    )


async def test_the_wrapper_keeps_the_signature_fastmcp_reads(bare_wrapper):
    """FastMCP builds the input schema from inspect.signature, which follows
    __wrapped__. Offloading must not change what it sees, or tool parameters
    change shape on the wire."""
    import inspect

    def tool(days: int = 30, account: str = "") -> str:
        """doc"""
        return "ok"

    wrapped = bare_wrapper()(tool)
    assert inspect.iscoroutinefunction(wrapped)
    assert inspect.signature(wrapped) == inspect.signature(tool)
    assert wrapped.__name__ == "tool" and wrapped.__doc__ == "doc"
    assert await wrapped(days=7) == "ok"


async def test_a_sync_tool_error_still_reaches_the_audit_log(bare_wrapper, monkeypatch):
    seen = {}

    class _Audit:
        def log_tool_call(self, **kw):
            seen.update(kw)

    monkeypatch.setattr(_srv, "_get_audit_logger", lambda: _Audit())

    def broken_tool() -> dict:
        raise RuntimeError("sts unreachable")

    with pytest.raises(RuntimeError, match="sts unreachable"):
        await bare_wrapper()(broken_tool)()
    assert seen.get("outcome") == "error" and seen.get("tool") == "broken_tool"


async def test_a_sync_tool_can_still_refresh_the_client_tool_list(bare_wrapper, monkeypatch):
    """connect_* tools call _tool_surface_changed(), which schedules
    send_tool_list_changed on the loop. From a worker thread there is no running
    loop, and the notification must not be silently dropped because of it."""
    sent = asyncio.Event()

    class _Session:
        async def send_tool_list_changed(self):
            sent.set()

    class _Ctx:
        session = _Session()

    monkeypatch.setattr(_srv.mcp, "get_context", lambda: _Ctx())

    def connect_tool() -> dict:
        _srv._tool_surface_changed()
        return {"connected": True}

    assert await bare_wrapper()(connect_tool)() == {"connected": True}
    await asyncio.wait_for(sent.wait(), timeout=2)
