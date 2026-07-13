# SPDX-License-Identifier: Apache-2.0
"""Per-family MCP tool modules.

server.py grew to ~14k lines. Tools are being extracted here one cohesive family
at a time; each module registers its tools against the shared, telemetry-wrapped
`mcp` instance from finops.server the moment server.py imports it (near main()).
Extraction is behavior-preserving: tool bodies move verbatim, only their relative
imports shift one level deeper (`from .x` -> `from ..x`).
"""
