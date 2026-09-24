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

import ast
import asyncio
import gc
import pathlib
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


# ── 1b. telemetry sent from the loop ─────────────────────────────────────────

@pytest.fixture
def slow_posthog(monkeypatch):
    """Telemetry switched on, with PostHog answering after BLOCK_S. Returns the
    list of event names that reached it."""
    import sys

    import httpx

    posted: list[str] = []

    def post(url, json=None, **_k):
        time.sleep(BLOCK_S)
        posted.append((json or {}).get("event"))

    # test_airgap re-imports finops.telemetry, after which server._telemetry,
    # the finops package attribute and sys.modules can each hold a different
    # module object. The wrapper sends through the first, _team_nudge's
    # `from . import telemetry` reads the second; switch every copy on.
    import finops
    copies = (_srv._telemetry, getattr(finops, "telemetry", None),
              sys.modules.get("finops.telemetry"))
    for telemetry in {id(m): m for m in copies if m is not None}.values():
        monkeypatch.setattr(telemetry, "_is_opted_out", lambda: False)
    monkeypatch.setattr(httpx, "post", post)
    return posted


async def _until(pred, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


async def test_the_upgrade_nudge_does_not_wait_on_posthog(slow_posthog, monkeypatch):
    """Async tools call _team_nudge on the event loop. Its impression event was a
    direct _send_event: a blocking POST with a 5s timeout."""
    monkeypatch.setattr(_srv, "get_status", lambda: type("S", (), {"mode": "free"})())
    monkeypatch.setattr(_srv, "_savings_found_monthly", lambda: 0.0)

    async with _Heartbeat() as hb:
        tip = _srv._team_nudge("idle resources found", "aws_audit")

    assert tip, "the nudge itself should still be returned to a free user"
    assert hb.max_stall < MAX_STALL_S, (
        f"_team_nudge held the event loop for {hb.max_stall:.2f}s sending telemetry"
    )
    assert await _until(lambda: "upgrade_nudge_shown" in slow_posthog), (
        "the impression event must still be sent, just not from the loop"
    )


async def test_the_wrapper_sends_its_funnel_events_off_the_loop(
    slow_posthog, bare_wrapper, monkeypatch,
):
    """first_cost_query_success and unconnected_cost_tool are sent by
    _instrumented_tool itself, after the tool returns, on the loop."""
    from finops import demo_data
    monkeypatch.setattr(demo_data, "DEMO_MODE", False)
    monkeypatch.setattr(demo_data, "_real_provider_connected", lambda: False)
    monkeypatch.setattr(_srv, "_first_cost_query_fired", False)
    monkeypatch.setattr(_srv, "_unconnected_hint_fired", False)
    monkeypatch.setattr(_srv, "_maybe_editor_confirmation", lambda: None)
    monkeypatch.setattr(_srv, "_maybe_team_tip", lambda name: None)

    def get_cost_summary() -> dict:      # a _COST_QUERY_TOOLS name
        return {"grand_total_usd": 1.0}

    async with _Heartbeat() as hb:
        out = await bare_wrapper()(get_cost_summary)()

    assert out["grand_total_usd"] == 1.0
    assert hb.max_stall < MAX_STALL_S, (
        f"the dispatch wrapper held the event loop for {hb.max_stall:.2f}s sending telemetry"
    )
    assert await _until(lambda: {"first_cost_query_success", "unconnected_cost_tool"}
                        <= set(slow_posthog))


# ── 1c. the AWS credential probe behind _active() ───────────────────────────

async def test_aws_is_configured_resolves_credentials_off_the_loop(monkeypatch):
    """_active() gathers is_configured() across every connector before nearly
    every cost tool. Azure and GCP already probe in a thread; AWS walked the
    botocore chain (IMDS off EC2) inline."""
    import botocore.session
    from finops.connectors.aws import AWSConnector

    class _SlowChain:
        def get_credentials(self):
            time.sleep(BLOCK_S)          # IMDS connect timeout, as the loop sees it
            return object()

    monkeypatch.setattr(botocore.session, "get_session", lambda: _SlowChain())

    async with _Heartbeat() as hb:
        assert await AWSConnector().is_configured() is True

    assert hb.max_stall < MAX_STALL_S, (
        f"AWSConnector.is_configured held the event loop for {hb.max_stall:.2f}s"
    )


async def test_aws_is_configured_still_answers_false_without_credentials(monkeypatch):
    import botocore.session
    from finops.connectors.aws import AWSConnector

    class _EmptyChain:
        def get_credentials(self):
            return None

    monkeypatch.setattr(botocore.session, "get_session", lambda: _EmptyChain())
    assert await AWSConnector().is_configured() is False

    class _Session:
        def get_credentials(self):
            raise RuntimeError("sso token expired")

    assert await AWSConnector(session=_Session()).is_configured() is False


# ── 2. real tools, blocked at the cloud boundary ─────────────────────────────

def _sleepy(ret):
    """A stand-in for one SDK round trip: holds its thread, then answers."""
    def fake(*_a, **_k):
        time.sleep(BLOCK_S)
        return ret
    return fake


class _SlowBoto3Client:
    """What boto3.client() returns here. Every API call takes BLOCK_S."""

    def __getattr__(self, name):
        return _sleepy({})


def _block_scan_cloudwatch_waste(monkeypatch):
    import finops.analyzers.optimizer as opt
    monkeypatch.setattr(opt, "scan_cloudwatch_log_waste", _sleepy({"findings": []}))
    from finops.tools import aws_waste
    return aws_waste.scan_cloudwatch_waste()


def _block_get_textract_costs(monkeypatch):
    from finops.connectors.aws_services import textract
    monkeypatch.setattr(textract.TextractAnalyzer, "get_costs", _sleepy("no spend"))
    from finops.tools import aws_waste
    return aws_waste.get_textract_costs(days=7)


def _block_get_data_transfer_costs(monkeypatch):
    import boto3
    import finops.analyzers.waste as waste
    monkeypatch.setattr(boto3, "client", _sleepy(_SlowBoto3Client()))
    monkeypatch.setattr(waste, "check_data_transfer_costs", lambda ce, **k: [])
    from finops.tools import aws
    return aws.get_data_transfer_costs()


def _block_get_s3_incomplete_multipart_uploads(monkeypatch):
    import boto3
    import finops.analyzers.waste as waste
    monkeypatch.setattr(boto3, "client", _sleepy(_SlowBoto3Client()))
    monkeypatch.setattr(waste, "check_s3_incomplete_multipart", lambda s3, **k: [])
    from finops.tools import aws_waste
    return aws_waste.get_s3_incomplete_multipart_uploads()


def _block_list_org_accounts(monkeypatch):
    from finops.connectors import aws_org
    monkeypatch.setattr(aws_org, "list_org_accounts", _sleepy([]))
    from finops.tools import attribution
    return attribution.list_org_accounts()


def _block_get_org_cost_summary(monkeypatch):
    from finops.connectors import aws_org
    monkeypatch.setattr(_srv, "require_pro", lambda feature: None)
    monkeypatch.setattr(aws_org, "org_cost_summary", _sleepy({"accounts": []}))
    from finops.tools import attribution
    return attribution.get_org_cost_summary()


# Async tools: these await other work too, so they stay async and reach their
# blocking calls through asyncio.to_thread.

def _block_get_ecs_rightsizing_recommendations(monkeypatch):
    import boto3
    import finops.analyzers.waste as waste
    from finops.tools import aws_waste
    monkeypatch.setattr(boto3, "client", _sleepy(_SlowBoto3Client()))
    monkeypatch.setattr(waste, "check_ecs_task_rightsizing", lambda *a, **k: [])

    async def list_price(findings, resource_type):
        return {}
    monkeypatch.setattr(aws_waste, "_price_on_customer_rates", list_price)
    return aws_waste.get_ecs_rightsizing_recommendations(regions=["us-east-1"])


def _block_push_to_n8n(monkeypatch):
    import finops.analyzers.optimizer as opt
    from finops.connectors.saas import n8n
    from finops.tools import notifications

    async def yes(self):
        return True

    async def sent(self, **_k):
        return True

    monkeypatch.setattr(_srv, "require_pro", lambda feature: None)
    monkeypatch.setattr(n8n.N8nConnector, "is_configured", yes)
    monkeypatch.setattr(n8n.N8nConnector, "send_audit_summary", sent)
    monkeypatch.setattr(opt, "run_deep_audit", _sleepy({"findings": []}))
    monkeypatch.setitem(_srv.CLOUD_CONNECTORS, "aws", None)
    return notifications.push_to_n8n()


def _block_get_label_costs(monkeypatch):
    from finops.connectors import kubernetes as k8s
    from finops.tools import attribution

    async def yes(self):
        return True

    report = type("R", (), {"cluster": "c1"})()
    monkeypatch.setattr(k8s.KubernetesConnector, "is_configured", yes)
    monkeypatch.setattr(k8s.KubernetesConnector, "analyze_cluster", _sleepy(report))
    monkeypatch.setattr(k8s.KubernetesConnector, "get_label_costs",
                        lambda self, r, label_key: {"by_label": []})
    return attribution.get_label_costs(label_key="team")


def _block_get_cost_summary_for_a_role_account(monkeypatch):
    """account= on a role_arn account: sts:AssumeRole before anything is fetched."""
    from finops import accounts
    from finops.connectors.aws import AWSConnector
    from finops.tools import cost_queries

    async def no(self):
        return False

    monkeypatch.setattr(accounts, "resolve_named_account", lambda name: (
        accounts.AccountConfig(name=name, account_id="111122223333",
                               region="us-east-1", role_arn="arn:aws:iam::111122223333:role/r"),
        None))
    monkeypatch.setattr(accounts, "get_boto3_session", _sleepy(object()))
    monkeypatch.setattr(AWSConnector, "is_configured", no)
    return cost_queries.get_cost_summary(account="prod")


# Keyed by tool name. Each entry installs a fake that blocks for BLOCK_S where
# the tool would leave the machine, and returns the tool's awaitable.
_BLOCKED_TOOLS = {
    "scan_cloudwatch_waste": _block_scan_cloudwatch_waste,
    "get_textract_costs": _block_get_textract_costs,
    "get_data_transfer_costs": _block_get_data_transfer_costs,
    "get_s3_incomplete_multipart_uploads": _block_get_s3_incomplete_multipart_uploads,
    "list_org_accounts": _block_list_org_accounts,
    "get_org_cost_summary": _block_get_org_cost_summary,
    "get_ecs_rightsizing_recommendations": _block_get_ecs_rightsizing_recommendations,
    "push_to_n8n": _block_push_to_n8n,
    "get_label_costs": _block_get_label_costs,
    "get_cost_summary[account]": _block_get_cost_summary_for_a_role_account,
}


@pytest.fixture
def real_tools(monkeypatch):
    """The live registered tools, with demo interception off so the call reaches
    the fake at the cloud boundary instead of being answered from the sample."""
    from finops import demo_data
    monkeypatch.setattr(demo_data, "DEMO_MODE", False)
    monkeypatch.delenv("FINOPS_DEMO_FORCE", raising=False)
    _srv._get_audit_logger()     # one-time sqlalchemy import, see bare_wrapper


async def _assert_loop_kept_ticking(name: str, make_call) -> object:
    async with _Heartbeat() as hb:
        t0 = time.monotonic()
        out = await make_call()
        took = time.monotonic() - t0

    # The fake really was reached: otherwise a stall of zero proves nothing.
    assert took >= BLOCK_S * 0.9, f"{name} returned in {took:.2f}s without reaching the fake"
    assert hb.max_stall < MAX_STALL_S, (
        f"{name} held the event loop for {hb.max_stall:.2f}s of a {took:.2f}s call. "
        f"Its blocking work must run off the loop: make it a plain def (the "
        f"registration shim threads it) or await asyncio.to_thread around the call"
    )
    return out


@pytest.mark.parametrize("name", sorted(_BLOCKED_TOOLS))
async def test_a_tool_blocked_on_the_cloud_does_not_freeze_the_loop(name, monkeypatch, real_tools):
    out = await _assert_loop_kept_ticking(name, lambda: _BLOCKED_TOOLS[name](monkeypatch))
    assert out is not None


# ── 3. the guard: keep future tools honest ───────────────────────────────────

_SRC = pathlib.Path(_srv.__file__).parent
_TOOL_FILES = [_SRC / "server.py", *sorted((_SRC / "tools").glob("*.py"))]


def _is_tool(fn: ast.AST) -> bool:
    return any(
        isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "tool"
        for d in getattr(fn, "decorator_list", ())
    )


def _tool_defs() -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    out = []
    for path in _TOOL_FILES:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_tool(node):
                out.append((path.name, node))
    return out


def _body_nodes(fn):
    """Every node in the tool's own body, not in functions it defines. A nested
    def is either handed to to_thread (fine) or is its own concern."""
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                stack.append(child)


def test_the_guard_sees_every_registered_tool():
    """If the AST scan below stopped finding tools (a new registration style, a
    moved module) the guard would pass vacuously. Tie it to the live registry."""
    from finops.plugins import loaded_plugins
    if loaded_plugins():
        pytest.skip("installed plugins register tools from outside this repo")
    scanned = {fn.name for _, fn in _tool_defs()}
    registered = {t.name for t in _srv.mcp._tool_manager.list_tools()}
    assert registered and registered <= scanned, (
        f"the loop guard cannot see these tools: {sorted(registered - scanned)}"
    )


def test_no_async_tool_body_runs_without_awaiting():
    """An `async def` tool that never awaits runs start to finish on the event
    loop. Whatever it calls (boto3, httpx, a connector, the database) holds the
    whole server while it runs. The same body as a plain `def` is threaded by
    _instrumented_tool for free. 96 tools were in this state."""
    offenders = [
        f"{mod}:{fn.name} (line {fn.lineno})"
        for mod, fn in _tool_defs()
        if isinstance(fn, ast.AsyncFunctionDef)
        and not any(isinstance(n, (ast.Await, ast.AsyncFor, ast.AsyncWith))
                    for n in _body_nodes(fn))
    ]
    assert not offenders, (
        "these async tools never await, so they run entirely on the event loop. "
        "Make them plain `def` (the registration shim runs them in a thread):\n  "
        + "\n  ".join(offenders)
    )


# Calls that leave the machine (or sleep) and return only when the far end
# answers. Matched on the callee's dotted tail, so `boto3.client`, `_srv.boto3.client`
# and `self.boto3.client` all count. Not exhaustive and not meant to be: it is
# the set this codebase actually reaches for, each of which has been found
# running on the loop at least once.
_BLOCKING_DOTTED = {
    "boto3.client", "boto3.resource", "boto3.Session",
    "httpx.get", "httpx.post", "httpx.put", "httpx.patch", "httpx.delete",
    "httpx.request", "httpx.stream", "httpx.Client",
    "requests.get", "requests.post", "requests.put", "requests.patch",
    "requests.delete", "requests.request", "requests.Session",
    "urllib.request.urlopen", "time.sleep",
    "subprocess.run", "subprocess.check_output", "subprocess.check_call",
    "subprocess.call", "subprocess.Popen",
}
# Synchronous nable helpers and SDK methods that do network I/O inside. Matched
# on the final name, called as a function or a method.
_BLOCKING_NAMES = {
    # telemetry: a blocking POST with a 5s timeout
    "_send_event", "_emit_provider_connected", "_gcp_emit_connected",
    # AWS: STS, Cost Explorer and multi-region sweeps
    "get_boto3_session", "assume_role", "get_caller_identity", "describe_regions",
    "_make_client", "_account_id", "run_deep_audit", "analyze_commitments",
    "analyze_rightsizing", "scan_idle_resources", "detect_savings_context",
    "bedrock_token_cost_split", "scan_cloudwatch_log_waste",
    "list_org_accounts", "org_cost_summary", "ou_cost_breakdown",
    # Kubernetes API
    "analyze_cluster", "analyze_all_clusters", "discover_helm_releases",
    # LLM provider usage APIs and ticketing HTTP
    "get_all_llm_costs", "create_rightsizing_ticket", "create_ticket",
}
# (module, function, callee) triples that are knowingly left on the loop, each
# with the reason. Keep this empty if at all possible.
_BLOCKING_ALLOWED: dict[tuple[str, str, str], str] = {}


def _callee(call: ast.Call) -> tuple[str, str]:
    """(dotted callee as written, its last component)."""
    try:
        dotted = ast.unparse(call.func)
    except Exception:
        dotted = ""
    f = call.func
    last = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
    return dotted, last


def _is_blocking(call: ast.Call) -> bool:
    dotted, last = _callee(call)
    if last in _BLOCKING_NAMES:
        return True
    return any(dotted == d or dotted.endswith("." + d) for d in _BLOCKING_DOTTED)


def _loop_side_blocking_calls(fn: ast.AsyncFunctionDef) -> list[tuple[int, str]]:
    """Blocking calls an async function makes on the event loop itself.

    Descends into nested async defs (they run on the loop too) but not into
    sync defs or lambdas: those are what gets handed to asyncio.to_thread.
    A call that is the direct operand of `await` is a coroutine and is fine;
    anything else, including a call in the argument list of an awaited one,
    runs synchronously right here.
    """
    awaited = {id(n.value) for n in ast.walk(fn) if isinstance(n, ast.Await)}
    found = []
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call) and id(node) not in awaited and _is_blocking(node):
            found.append((node.lineno, _callee(node)[0]))
        stack.extend(ast.iter_child_nodes(node))
    return found


def _async_defs_in_tool_modules():
    for path in _TOOL_FILES:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                yield path.name, node


def test_no_async_function_in_the_tool_layer_blocks_the_loop():
    """Every async function in server.py and finops/tools/*, tools and their
    helpers alike, reaches boto3, httpx, STS, the Kubernetes API and the other
    known-blocking calls only through asyncio.to_thread (or a plain def the
    registration shim threads). _BLOCKING_ALLOWED lists the deliberate
    exceptions, with a reason each."""
    offenders = []
    for mod, fn in _async_defs_in_tool_modules():
        for lineno, callee in _loop_side_blocking_calls(fn):
            last = callee.rsplit(".", 1)[-1].split("(")[0]
            if (mod, fn.name, last) in _BLOCKING_ALLOWED:
                continue
            offenders.append(f"{mod}:{lineno} {fn.name} calls {callee}(...) on the loop")
    assert not offenders, (
        "blocking I/O on the event loop. Wrap it in `await asyncio.to_thread(...)`, "
        "or make the tool a plain def:\n  " + "\n  ".join(sorted(offenders))
    )


def test_the_blocking_scanner_catches_what_it_claims():
    """Mutation check on the guard: a scanner that matched nothing would pass."""
    def scan(src: str) -> list[str]:
        fn = ast.parse(src).body[0]
        return [c for _, c in _loop_side_blocking_calls(fn)]

    assert scan("async def t():\n    boto3.client('s3').list_buckets()\n") == ["boto3.client"]
    assert scan("async def t():\n    r = connector.analyze_cluster(ctx)\n") == ["connector.analyze_cluster"]
    assert scan("async def t():\n    await asyncio.wait_for(x(get_boto3_session(a)), 5)\n") \
        == ["get_boto3_session"]
    # Nested async helpers run on the loop too.
    assert scan("async def t():\n    async def one():\n        _srv.time.sleep(1)\n"
                "    await one()\n") == ["_srv.time.sleep"]
    # The sanctioned forms are not reported.
    assert scan("async def t():\n    await asyncio.to_thread(boto3.client, 's3')\n") == []
    assert scan("async def t():\n    def work():\n        return boto3.client('s3')\n"
                "    await asyncio.to_thread(work)\n") == []
    assert scan("async def t():\n    await asyncio.to_thread(lambda: run_deep_audit())\n") == []
