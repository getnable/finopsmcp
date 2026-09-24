# SPDX-License-Identifier: Apache-2.0
"""Per-family MCP tool modules.

server.py grew to ~14k lines. Tools are being extracted here one cohesive family
at a time; each module registers its tools against the shared, telemetry-wrapped
`mcp` instance from finops.server the moment server.py imports it (near main()).
Extraction is behavior-preserving: tool bodies move verbatim, only their relative
imports shift one level deeper (`from .x` -> `from ..x`).

Sync or async: a tool whose body calls boto3, httpx or a synchronous connector is
a plain `def`. The registration shim in server.py runs sync tools on a worker
thread, so they never hold the event loop. An `async def` tool runs ON the loop
and must reach anything blocking through `await asyncio.to_thread(...)`; one
that never awaits gains nothing from being async and freezes the whole server
for the length of its slowest call. tests/test_tool_loop_offload.py enforces
both rules.
"""
