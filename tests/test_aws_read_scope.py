"""The scope behind the failed-read label: what it sees, and what it must not.

aws_read_failures watches botocore's after-call events and records each AWS
call into the scope of the tool call that made it (a ContextVar). The label on
a tool's answer is only as good as three properties, each pinned here with real
botocore clients whose HTTP answer is canned per operation:

  - a call is counted in the scope that made it, and only there: two tool calls
    in flight at once never label each other, and a call made on a worker
    thread a scanner starts for itself still counts for the tool call around it
  - a call is counted once, however often install() runs
  - a failure is counted when the read did not happen (denied, throttled,
    expired, the service not answering) and never when the service answered
    "there is nothing here"
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import threading

import pytest

import finops.server as _srv
from finops import aws_read_failures as R

SRC = pathlib.Path(R.__file__).parent


# ── canned answers per operation ─────────────────────────────────────────────

@pytest.fixture
def answers(monkeypatch):
    """Every real botocore client answers from `table`, keyed "service:Operation",
    the way botocore's Stubber does: a before-call handler returns the response,
    so nothing is signed or sent and after-call runs as it does for real.
    A value is (status, error code) or (200, parsed body)."""
    import boto3
    from botocore import handlers
    from botocore.awsrequest import AWSResponse

    R.install()
    table: dict[str, tuple[int, object]] = {}

    def _before_call(model=None, **_kw):
        op = f"{model.service_model.endpoint_prefix}:{model.name}"
        entry = table.get(op, table.get("*", (200, {})))
        status, body = entry() if callable(entry) else entry
        if status < 300:
            parsed = dict(body or {})
            parsed.setdefault("ResponseMetadata", {"HTTPStatusCode": status})
            return AWSResponse(None, status, {}, None), parsed
        return AWSResponse(None, status, {}, None), {
            "Error": {"Code": body, "Message": f"{op} answered {body}"},
            "ResponseMetadata": {"HTTPStatusCode": status}}

    monkeypatch.setattr(handlers, "BUILTIN_HANDLERS",
                        [*handlers.BUILTIN_HANDLERS, ("before-call", _before_call)])
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    return table


def _call_and_swallow(fn, **kw):
    try:
        return fn(**kw)
    except Exception:  # noqa: BLE001 - the scanners swallow it too
        return None


# ── 1. two tool calls at once ────────────────────────────────────────────────

@pytest.fixture
def wrap(monkeypatch):
    """_instrumented_tool without registering anything in the live registry."""
    monkeypatch.setattr(_srv, "_original_mcp_tool", lambda *a, **k: (lambda f: f))
    monkeypatch.delenv("FINOPS_DEMO", raising=False)
    monkeypatch.delenv("FINOPS_DEMO_FORCE", raising=False)
    from finops import demo_data
    monkeypatch.setattr(demo_data, "DEMO_MODE", False)
    return lambda fn: _srv._instrumented_tool()(fn)


async def test_two_tool_calls_in_flight_never_label_each_other(answers, wrap):
    """One tool's reads are all denied, the other's all answer, and both are in
    flight at the same moment: each makes its first call only once the other
    has made its own. Each reads on the tool thread, on a scanner's own pool,
    and through asyncio.to_thread on a loop of its own."""
    import boto3

    answers["monitoring:ListMetrics"] = (400, "AccessDenied")
    answers["ec2:DescribeVolumes"] = (200, {"Volumes": []})
    both_started = threading.Barrier(2, timeout=10)

    def reads(service, op, **kw):
        client = boto3.client(service, region_name="us-east-1")
        _call_and_swallow(getattr(client, op), **kw)

    def scanner(service, op):
        both_started.wait()
        reads(service, op)
        with R.ScopedThreadPool(max_workers=2) as pool:
            list(pool.map(lambda _: reads(service, op), range(2)))

        async def on_a_loop_of_its_own():
            await asyncio.to_thread(reads, service, op)
        asyncio.run(on_a_loop_of_its_own())
        return {"findings": []}

    def denied_tool() -> dict:
        return scanner("cloudwatch", "list_metrics")

    def answered_tool() -> dict:
        return scanner("ec2", "describe_volumes")

    denied, answered = await asyncio.gather(wrap(denied_tool)(), wrap(answered_tool)())

    assert denied["status"] == "not_evaluated", denied
    assert denied["aws_reads_failed"]["calls_failed"] == 4
    assert denied["aws_reads_failed"]["calls_answered"] == 0
    assert "aws_reads_failed" not in answered and "status" not in answered, answered


# ── 2. worker threads the code starts for itself ─────────────────────────────

def test_a_failure_on_the_cloudwatch_read_pool_is_counted(answers):
    """fetch_metric_values reads each series on a pool of its own. Its calls
    did not count: every read denied, and the scope saw nothing at all."""
    from datetime import UTC, datetime, timedelta

    import boto3

    from finops.analyzers.cloudwatch import MetricQuery, fetch_metric_values

    answers["monitoring:GetMetricStatistics"] = (400, "AccessDenied")
    cw = boto3.client("cloudwatch", region_name="us-east-1")
    queries = [MetricQuery(key=i, namespace="AWS/EC2", metric_name="CPUUtilization",
                           dimensions=[{"Name": "InstanceId", "Value": f"i-{i}"}],
                           stat="Average", period=3600) for i in range(3)]
    end = datetime.now(UTC)
    with R.watching() as reads:
        out = fetch_metric_values(cw, queries, end - timedelta(days=1), end,
                                  use_get_metric_data=False)

    assert out == {0: None, 1: None, 2: None}
    assert len(reads.failed) == 3, reads.failed
    assert reads.nothing_read


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and (getattr(n.func, "attr", None) == name or getattr(n.func, "id", None) == name)]


# Pools that never run during an MCP tool call, so there is no scope to carry.
_POOLS_OUTSIDE_TOOL_CALLS = {
    "setup_wizard.py": "the interactive setup CLI",
    "welcome.py": "the first-run CLI",
    "scan_assembler.py": "`nable scan`, the CLI",
}


def test_every_worker_thread_the_package_starts_carries_the_scope():
    """A plain ThreadPoolExecutor or loop.run_in_executor runs its work outside
    the caller's context, so the reads it makes count for no tool call. Five
    scanners' pools did exactly that. asyncio.to_thread and ScopedThreadPool
    carry it."""
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel == "aws_read_failures.py" or rel in _POOLS_OUTSIDE_TOOL_CALLS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _calls(tree, "ThreadPoolExecutor") + _calls(tree, "run_in_executor"):
            offenders.append(f"{rel}:{call.lineno} {ast.unparse(call.func)}")
    assert not offenders, (
        "use aws_read_failures.ScopedThreadPool or asyncio.to_thread, so AWS reads "
        "made there count for the tool call:\n  " + "\n  ".join(offenders))


# ── 3. once per call ─────────────────────────────────────────────────────────

def test_install_twice_still_counts_each_call_once(answers):
    """install() registers on boto3's default session as well as on future
    sessions. A default session built after the first install() already had
    the handlers, so a second install() registered them again and every call
    counted twice."""
    import boto3

    answers["ec2:DescribeVolumes"] = (200, {"Volumes": []})
    answers["monitoring:ListMetrics"] = (403, "AccessDenied")
    boto3.client("sts")      # builds the default session, handlers included
    R.install()
    with R.watching() as reads:
        boto3.client("ec2").describe_volumes()
        _call_and_swallow(boto3.client("cloudwatch").list_metrics)
    assert reads.answered == 1 and len(reads.failed) == 1, (reads.answered, reads.failed)


def test_a_paginator_that_fails_on_a_later_page_is_partial(answers):
    import boto3

    pages = iter([(200, {"Metrics": [{"MetricName": "a"}], "NextToken": "2"}),
                  (400, "ThrottlingException")])
    calls = {"n": 0}

    def _next_page():
        calls["n"] += 1
        return next(pages)

    answers["monitoring:ListMetrics"] = _next_page
    with R.watching() as reads, pytest.raises(Exception, match="Throttling"):
        for _page in boto3.client("cloudwatch").get_paginator("list_metrics").paginate():
            pass
    assert calls["n"] == 2
    assert reads.answered == 1 and reads.failed == [("cloudwatch:ListMetrics", "ThrottlingException")]
    assert not reads.nothing_read


def test_outside_a_tool_call_nothing_is_recorded_and_the_call_is_untouched(answers):
    """The CLI and the guard share the process-wide botocore handlers. With no
    scope open the handlers return at once and the call's own answer stands."""
    import boto3

    answers["ec2:DescribeVolumes"] = (200, {"Volumes": [{"VolumeId": "vol-1"}]})
    out = boto3.client("ec2").describe_volumes()
    assert out["Volumes"] == [{"VolumeId": "vol-1"}]
    with R.watching() as reads:
        pass
    assert reads.failed == [] and reads.answered == 0


def test_the_handlers_never_raise():
    """They observe a real call; whatever they are handed, the call goes on."""
    from types import SimpleNamespace

    odd = [
        {"http_response": SimpleNamespace(status_code=403), "parsed": {"Error": "denied"}},
        {"http_response": SimpleNamespace(status_code="403"), "parsed": {}},
        {"http_response": None, "parsed": None, "event_name": None},
        {"http_response": SimpleNamespace(status_code=500), "parsed": []},
    ]
    with R.watching():
        for kw in odd:
            R._after_call(event_name=kw.pop("event_name", "after-call.ec2.X"), **kw)
        R._after_call_error(exception=None, event_name=None)
        R._after_call_error(exception=RuntimeError("x"), event_name="after-call-error.ec2.X",
                            context="not a dict")


# ── 4. what counts as a read that did not happen ─────────────────────────────

def _one_call_answered(answers, status, code) -> R.ReadLog:
    """One real EC2 call answered with (status, code). (Cost Explorer itself is
    off limits in this suite: tests/conftest.py blocks billed APIs.)"""
    import boto3

    answers["ec2:DescribeVolumes"] = (status, code) if status >= 300 else (status, {})
    with R.watching() as reads:
        _call_and_swallow(boto3.client("ec2", region_name="us-east-1").describe_volumes)
    return reads


@pytest.mark.parametrize(("status", "code"), [
    (400, "AccessDeniedException"), (403, "AccessDenied"), (400, "UnauthorizedOperation"),
    (400, "ExpiredToken"), (400, "ExpiredTokenException"), (403, "InvalidClientTokenId"),
    (400, "ThrottlingException"), (400, "Throttling"), (503, "RequestLimitExceeded"),
    (400, "TooManyRequestsException"), (503, "SlowDown"),
    # Cost Explorer's throttle: botocore's own retry handler counts it as one.
    (400, "LimitExceededException"),
    # The service did not answer the question.
    (500, "InternalServerException"), (500, "InternalServiceError"),
    (500, "InternalFailure"), (503, "ServiceUnavailableException"), (503, ""),
    (504, "GatewayTimeout"), (400, "RequestTimeout"), (429, "TooManyRequests"),
    # A HEAD request has no body, so a denial carries only its status.
    (403, "403"), (401, "401"),
    (400, "IncompleteSignature"), (403, "InvalidSignatureException"),
    (403, "OptInRequiredException"),
])
def test_a_read_that_did_not_happen_is_a_failure(answers, status, code):
    reads = _one_call_answered(answers, status, code)
    assert reads.failed and reads.answered == 0, (status, code, reads)


@pytest.mark.parametrize(("status", "code"), [
    (404, "NoSuchEntity"), (404, "NoSuchLifecycleConfiguration"), (404, "NotFound"),
    (400, "ResourceNotFoundException"), (404, "NoSuchBucketPolicy"),
    (400, "AWSOrganizationsNotInUseException"), (400, "DataUnavailableException"),
    (400, "InvalidParameterValue"), (400, "ValidationException"),
    (400, "ExpiredNextTokenException"), (400, "InvalidNextTokenException"),
    (404, "404"), (400, "DBInstanceNotFound"), (400, "InvalidVolume.NotFound"),
    (200, ""),
])
def test_an_answer_is_never_a_failure(answers, status, code):
    reads = _one_call_answered(answers, status, code)
    assert reads.failed == [] and reads.answered == 1, (status, code, reads)


def test_every_code_botocore_retries_as_throttling_or_transient_is_a_failure(answers):
    from botocore.retries import standard

    codes = (list(standard.ThrottledRetryableChecker._THROTTLED_ERROR_CODES)
             + list(standard.TransientRetryableChecker._TRANSIENT_ERROR_CODES))
    missed = [c for c in codes if not _one_call_answered(answers, 400, c).failed]
    assert not missed, missed


def test_no_modeled_not_found_error_of_a_service_the_tools_read_is_a_failure(answers):
    """Every error the services' own models define whose name says the thing is
    not there (NotFound, NoSuch...) is an answer. Those the model sends with a
    5xx status are the service failing, not an answer (Lambda's Invoke answers
    502 KMSNotFoundException when it cannot decrypt), and are left out."""
    import botocore.session

    s = botocore.session.get_session()
    wrong = []
    for svc in ("ce", "ec2", "rds", "s3", "cloudwatch", "logs", "organizations",
                "compute-optimizer", "pricing", "lambda", "elbv2", "autoscaling",
                "savingsplans", "efs", "ecr", "ecs", "budgets", "sts"):
        model = s.get_service_model(svc)
        for shape in model.error_shapes:
            err = shape.metadata.get("error") or {}
            code = err.get("code") or shape.name
            status = err.get("httpStatusCode", 400)
            if (("NotFound" in code or code.startswith("NoSuch")) and status < 500
                    and _one_call_answered(answers, status, code).failed):
                wrong.append(f"{svc}:{code}")
    assert not wrong, wrong
