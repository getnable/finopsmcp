# SPDX-License-Identifier: Apache-2.0
"""Notice the AWS reads that failed, wherever the code that made them swallowed it.

Why this exists, stated plainly: an audit of the MCP tools with every AWS call
answering AccessDenied found twenty scanners that answered "No orphaned alarms
found", "No Graviton migration candidates", "$0.00", and a full cost audit that
said "Your infrastructure looks well-optimized". Each scanner catches its own
per-region failures and carries on, which is right for one bad region and wrong
when every region failed, and none of them said which it was.

Fixing that scanner by scanner is twenty chances to forget and a twenty-first
that ships without it. This module is the one place that sees every call:
botocore emits after-call (with the HTTP status) and after-call-error (with the
exception) for every API call on every client, and a handler registered here
records the ones that failed for a reason that means "not read": denied,
throttled, expired or missing credentials, unreachable. A "not found" error
(NoSuchLifecycleConfiguration and friends) is an answer, not a failed read, and
is not recorded.

Recording is scoped: watching() opens a scope for the current context (one tool
call, one scanner in a sweep), and only reads made inside it are counted there.
Scopes nest, so a scanner's failures count for the sweep and for the tool call
around it. Worker threads started with a plain ThreadPoolExecutor do not inherit
the scope; carry() wraps a function so that they do.
"""
from __future__ import annotations

import asyncio
import contextvars
import weakref
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

# Error codes that mean the read did not happen. Matched as substrings so the
# service-specific spellings (AccessDeniedException, AccessDenied,
# ThrottlingException, Throttling, RequestLimitExceeded) all count.
_UNREAD_CODES = (
    "AccessDenied", "UnauthorizedOperation", "AuthFailure", "AuthorizationError",
    "InvalidClientTokenId", "UnrecognizedClient", "ExpiredToken", "RequestExpired",
    "SignatureDoesNotMatch", "InvalidAccessKeyId", "MissingAuthenticationToken",
    "Throttl", "RequestLimitExceeded", "TooManyRequests", "SlowDown",
    "OptInRequired", "ServiceUnavailable", "InternalFailure", "InternalError",
)


# Answered calls that are not a read of the data a tool reports on: knowing
# which account the credentials belong to is not having looked at it. They
# still count when they fail.
_NOT_DATA = frozenset({"sts:GetCallerIdentity", "ec2:DescribeRegions"})


@dataclass
class ReadLog:
    """What one scope saw: how many calls were answered, and which failed."""

    answered: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)  # (operation, error code)

    @property
    def nothing_read(self) -> bool:
        return bool(self.failed) and self.answered == 0

    def summary(self) -> dict:
        codes = Counter(code for _, code in self.failed)
        ops = []
        for op, code in self.failed:
            label = f"{op} ({code})"
            if label not in ops:
                ops.append(label)
        return {
            "calls_failed": len(self.failed),
            "calls_answered": self.answered,
            "by_error": dict(codes.most_common()),
            "operations": ops[:8],
        }

    def sentence(self) -> str:
        s = self.summary()
        errs = ", ".join(f"{c} x{n}" for c, n in s["by_error"].items())
        if self.nothing_read:
            return (f"Every AWS read this needed failed ({s['calls_failed']} calls: {errs}), "
                    "so nothing was checked. This is not a finding of zero.")
        return (f"{s['calls_failed']} AWS read(s) failed ({errs}) while "
                f"{s['calls_answered']} answered, so the result covers only what could "
                "be read and may be missing findings.")


_scopes: contextvars.ContextVar[tuple[ReadLog, ...]] = contextvars.ContextVar(
    "finops_aws_read_scopes", default=())


@contextmanager
def watching() -> Iterator[ReadLog]:
    """Record the AWS reads made in this context until the block exits."""
    log = ReadLog()
    token = _scopes.set(_scopes.get() + (log,))
    try:
        yield log
    finally:
        _scopes.reset(token)


def carry(fn: Callable) -> Callable:
    """Wrap fn so that a ThreadPoolExecutor worker runs it inside the caller's
    scopes. Each call gets its own copy of the caller's context, since one
    Context cannot be entered by two threads at once."""
    ctx = contextvars.copy_context()

    def _run(*args: Any, **kwargs: Any) -> Any:
        return ctx.copy().run(fn, *args, **kwargs)
    return _run


class _ContextExecutor(ThreadPoolExecutor):
    """A default executor whose workers run in the submitter's context.
    loop.run_in_executor, unlike asyncio.to_thread, does not carry contextvars,
    and a dozen scanners hand their boto3 calls to it."""

    def submit(self, fn, /, *args, **kwargs):
        return super().submit(contextvars.copy_context().run, fn, *args, **kwargs)


_carried_loops: weakref.WeakSet = weakref.WeakSet()


def carry_into_default_executor(loop: asyncio.AbstractEventLoop) -> None:
    """Make loop.run_in_executor(None, ...) carry the read scopes. Once per loop."""
    if loop in _carried_loops:
        return
    try:
        loop.set_default_executor(_ContextExecutor(thread_name_prefix="nable-io"))
        _carried_loops.add(loop)
    except Exception:  # noqa: BLE001 - scoping must never stop a call
        return


def _is_unread(code: str) -> bool:
    return any(marker in code for marker in _UNREAD_CODES)


def _operation(event_name: str) -> str:
    # "after-call.cloudwatch.ListMetrics" -> "cloudwatch:ListMetrics"
    parts = (event_name or "").split(".")
    return f"{parts[1]}:{parts[2]}" if len(parts) >= 3 else event_name


def _after_call(http_response=None, parsed=None, event_name: str = "", **_kw) -> None:
    scopes = _scopes.get()
    if not scopes:
        return
    status = getattr(http_response, "status_code", 200) or 200
    code = ""
    if status >= 300 and isinstance(parsed, dict):
        code = str((parsed.get("Error") or {}).get("Code") or "")
    op = _operation(event_name)
    for log in scopes:
        if status >= 300 and _is_unread(code):
            log.failed.append((op, code))
        elif op not in _NOT_DATA:
            log.answered += 1


def _after_call_error(exception=None, event_name: str = "", **_kw) -> None:
    # Raised before any HTTP answer: no credentials, an expired SSO token, a
    # timeout, an endpoint that does not resolve. None of those is an answer.
    scopes = _scopes.get()
    if not scopes:
        return
    code = type(exception).__name__ if exception is not None else "Error"
    for log in scopes:
        log.failed.append((_operation(event_name), code))


def install() -> None:
    """Register the two handlers on every botocore session, present and future.
    Idempotent. Observing only: the handlers never change a call or its result."""
    try:
        from botocore import handlers
        for spec in (("after-call", _after_call), ("after-call-error", _after_call_error)):
            if spec not in handlers.BUILTIN_HANDLERS:
                handlers.BUILTIN_HANDLERS.append(spec)
        # boto3's default session may already exist, built before this ran.
        import boto3
        default = getattr(boto3, "DEFAULT_SESSION", None)
        if default is not None:
            default._session.register("after-call", _after_call,
                                      unique_id="finops-read-watch-after-call")
            default._session.register("after-call-error", _after_call_error,
                                      unique_id="finops-read-watch-after-call-error")
    except Exception:  # noqa: BLE001 - observing must never stop a read
        return
