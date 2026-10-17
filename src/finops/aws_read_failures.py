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
throttled, expired or missing credentials, unreachable, or the service failing
to answer. A "not found" error (NoSuchLifecycleConfiguration and friends) is an
answer, not a failed read, and is not recorded.

Recording is scoped: watching() opens a scope for the current context (one tool
call, one scanner in a sweep), and only reads made inside it are counted there.
Scopes nest, so a scanner's failures count for the sweep and for the tool call
around it. The scope is a ContextVar, so it follows the work wherever the
context goes: asyncio tasks and asyncio.to_thread carry it, and so does
ScopedThreadPool. A plain ThreadPoolExecutor or loop.run_in_executor does not,
and tests/test_aws_read_scope.py keeps them out of the package.
"""
from __future__ import annotations

import contextvars
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field

# Error codes that mean the read did not happen, matched as substrings so the
# service-specific spellings (AccessDeniedException, AccessDenied,
# ThrottlingException, Throttling, InternalServerError, InternalFailure) count.
_UNREAD_MARKERS = (
    "AccessDenied", "UnauthorizedOperation", "AuthFailure", "AuthorizationError",
    "InvalidClientTokenId", "UnrecognizedClient", "ExpiredToken", "RequestExpired",
    "SignatureDoesNotMatch", "InvalidAccessKeyId", "MissingAuthenticationToken",
    "Throttl", "RequestLimitExceeded", "TooManyRequests", "SlowDown",
    "OptInRequired", "ServiceUnavailable", "RequestTimeout",
)
# Exact codes. The throttling and transient ones are botocore's own standard
# retry lists (botocore.retries.standard), so a code botocore retries because
# the service did not answer is a read that did not happen here too: Cost
# Explorer throttles with LimitExceededException.
_UNREAD_CODES = frozenset({
    "LimitExceededException", "BandwidthLimitExceeded", "PriorRequestNotComplete",
    "ProvisionedThroughputExceededException", "TransactionInProgressException",
    "IncompleteSignature", "InvalidSignatureException", "ServiceException",
})


def is_unread(code: str, status: int = 400) -> bool:
    """True when an AWS answer of (error code, HTTP status) means the data was
    not read: denied, throttled, unauthenticated, or the service failing to
    answer. False for an answer, including "there is no such thing"."""
    code = str(code or "")
    if status >= 500 or status in (401, 429):
        return True
    if status == 403 and (not code or code.isdigit()):
        return True     # a HEAD request has no body: the denial is its status
    if code.startswith("Internal"):
        return True     # InternalError, InternalFailure, InternalServerException...
    return code in _UNREAD_CODES or any(marker in code for marker in _UNREAD_MARKERS)


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


class ScopedThreadPool(ThreadPoolExecutor):
    """A ThreadPoolExecutor whose tasks run in the submitter's context, so the
    AWS reads a scanner makes on its own worker threads count for the tool call
    that started it. Each task gets its own copy, since one Context cannot be
    entered by two threads at once. map() goes through submit(), so it carries
    the context too."""

    def submit(self, fn, /, *args, **kwargs):
        return super().submit(contextvars.copy_context().run, fn, *args, **kwargs)


def _operation(event_name) -> str:
    # "after-call.cloudwatch.ListMetrics" -> "cloudwatch:ListMetrics"
    parts = str(event_name or "").split(".")
    return f"{parts[1]}:{parts[2]}" if len(parts) >= 3 else str(event_name or "")


# A handler registered twice on one session (BUILTIN_HANDLERS and the boto3
# default session) runs twice per call. The call's own context dict marks it
# seen, so it is counted once whatever the registration count.
_SEEN = "finops_read_watch_seen"


def _first_sighting(context) -> bool:
    if not isinstance(context, dict):
        return True
    if context.get(_SEEN):
        return False
    context[_SEEN] = True
    return True


def _after_call(http_response=None, parsed=None, event_name: str = "",
                context=None, **_kw) -> None:
    try:
        scopes = _scopes.get()
        if not scopes or not _first_sighting(context):
            return
        status = getattr(http_response, "status_code", 200) or 200
        status = status if isinstance(status, int) else int(status)
        code = ""
        if status >= 300 and isinstance(parsed, dict):
            error = parsed.get("Error")
            code = str(error.get("Code") or "") if isinstance(error, dict) else ""
        op = _operation(event_name)
        unread = status >= 300 and is_unread(code, status)
        for log in scopes:
            if unread:
                log.failed.append((op, code or str(status)))
            elif op not in _NOT_DATA:
                log.answered += 1
    except Exception:  # noqa: BLE001 - observing must never stop a read
        return


def _after_call_error(exception=None, event_name: str = "", context=None, **_kw) -> None:
    # Raised before any HTTP answer: no credentials, an expired SSO token, a
    # timeout, an endpoint that does not resolve. None of those is an answer.
    try:
        scopes = _scopes.get()
        if not scopes or not _first_sighting(context):
            return
        code = type(exception).__name__ if exception is not None else "Error"
        for log in scopes:
            log.failed.append((_operation(event_name), code))
    except Exception:  # noqa: BLE001 - observing must never stop a read
        return


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
