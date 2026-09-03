"""
logging_context.py
------------------
Correlation ids, so a log line says *which* request, scan, job, or email it
belongs to.

Before this, answering "what happened to this email?" meant grepping for a
message id and hoping every relevant line happened to mention it. Most did not:
`Supabase client initialised`, `Rate limit on attempt 2`, and
`Failed to persist email` carried no clue which of 50 emails in the batch they
were about.

Four ids, set by whoever owns the boundary:

    request_id   one HTTP request        set by middleware, echoed as X-Request-ID
    scan_id      one automation run      set by run_scan
    job_id       one scheduled job run   set by lifespan around every job
    email_id     one email               set around per-email work

They live in `contextvars`, so they follow the work without being threaded
through every function signature.

Threads are the exception, and it is not a small one. A contextvar is per-thread:
anything handed to a `ThreadPoolExecutor`, or to a bare `threading.Thread`,
starts with an *empty* context and logs with no ids at all — however carefully
the caller set them. asyncio tasks and anyio's threadpool (the one that runs
FastAPI's sync endpoints) copy the context for you; a pool you created yourself
does not. Wrap the worker in `carry_context`.

    with email_context(msg_id):
        ...                       # every line in here carries email=<msg_id>

    worker = carry_context(_one)  # captures the ids in scope *here*
    pool.submit(worker, item)     # ...and re-establishes them *there*
"""

import functools
import logging
import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator, Optional, TypeVar

_T = TypeVar("_T")

_request_id: ContextVar[str] = ContextVar("request_id", default="")
_scan_id: ContextVar[str] = ContextVar("scan_id", default="")
_job_id: ContextVar[str] = ContextVar("job_id", default="")
_email_id: ContextVar[str] = ContextVar("email_id", default="")

# Keyed by the attribute name stamped onto each LogRecord, which is also the
# field name in the JSON format. `carry_context` walks this, so a fifth id added
# here crosses thread boundaries without touching anything else.
_VARS: dict[str, ContextVar[str]] = {
    "request_id": _request_id,
    "scan_id": _scan_id,
    "job_id": _job_id,
    "email_id": _email_id,
}


def new_id(prefix: str = "") -> str:
    """A short correlation id. Eight hex characters is plenty to disambiguate
    concurrent work in a log file, and stays readable inline."""
    token = uuid.uuid4().hex[:8]
    return f"{prefix}{token}" if prefix else token


# The one id that does not come from us. Everything else here is minted locally;
# an inbound X-Request-ID is whatever a caller sent, and it lands in `ctx` on
# every line of that request.
_SAFE_ID = re.compile(r"\A[A-Za-z0-9._:-]{1,64}\Z")


def clean_incoming_id(value: Optional[str]) -> Optional[str]:
    """An inbound X-Request-ID if it is id-shaped, else None (mint a fresh one).

    Unvalidated, the header forges log structure. `X-Request-ID: x] [job=scan:FORGED
    email=victim@example.com` renders as

        INFO [backend.app.api] [req=x] [job=scan:FORGED email=victim@example.com] GET ...

    — a job and an email field that nothing set, on a line indistinguishable from
    a real one. Anything printable passes through, including ANSI escapes and
    tabs; a raw newline is refused by h11 but not by every proxy or ASGI server
    that might sit in front of this one. Length matters too: an 8KB id multiplies
    every line of its request, which against a 10MB × 50 rotation is a cheap way
    to evict history.

    Rejecting rather than stripping, and minting a replacement, keeps the
    guarantee the format relies on — a rendered `ctx` field was set by us.
    """
    if value and _SAFE_ID.match(value):
        return value
    return None


@contextmanager
def request_context(request_id: Optional[str] = None) -> Iterator[str]:
    rid = request_id or new_id()
    token = _request_id.set(rid)
    try:
        yield rid
    finally:
        _request_id.reset(token)


@contextmanager
def scan_context(scan_id: Optional[str] = None) -> Iterator[str]:
    sid = scan_id or new_id()
    token = _scan_id.set(sid)
    try:
        yield sid
    finally:
        _scan_id.reset(token)


@contextmanager
def job_context(name: str, job_id: Optional[str] = None) -> Iterator[str]:
    """One run of a scheduled job.

    The scheduler is the majority of this system's log volume — ingest every 5
    minutes and the attachment worker every 2 adds up to 30 runs an hour — and
    every line of it used to arrive with an empty `ctx`. One run could not be
    separated from the next, from a concurrent scan, or from HTTP traffic
    interleaved in the same file.

    The job name is part of the id (`ingest:3f9a1c22`) because "which job is
    this?" is the first question asked of such a line, and a bare hex id does not
    answer it.
    """
    jid = job_id or f"{name}:{new_id()}"
    token = _job_id.set(jid)
    try:
        yield jid
    finally:
        _job_id.reset(token)


@contextmanager
def email_context(email_id: str) -> Iterator[str]:
    token = _email_id.set(email_id or "")
    try:
        yield email_id
    finally:
        _email_id.reset(token)


def current_ids() -> dict[str, str]:
    """The ids in scope right now — for attaching to an error report, or for
    carrying across a thread boundary."""
    return {name: var.get() for name, var in _VARS.items()}


@contextmanager
def _restore_ids(ids: dict[str, str]) -> Iterator[None]:
    """Re-establish a captured set of ids in whatever context is running now."""
    tokens = [(var, var.set(ids.get(name, ""))) for name, var in _VARS.items()]
    try:
        yield
    finally:
        for var, token in tokens:
            var.reset(token)


def carry_context(fn: Callable[..., _T]) -> Callable[..., _T]:
    """Wrap `fn` so it logs under the ids in scope *here*, not in the thread that
    ends up running it.

    Capture happens at wrap time, in the calling thread. `contextvars.copy_context()`
    is deliberately not used as the wrapper: a single `Context` object may only be
    entered once at a time, so N pool workers sharing one copy would raise
    `RuntimeError: cannot enter context`. Copying the four values instead is both
    correct under concurrency and cheap.

    Wrap once outside the submit loop, not per item — the ids are the same for
    every worker in a fan-out:

        worker = carry_context(_one)
        futures = [pool.submit(worker, item) for item in items]
    """
    ids = current_ids()

    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> _T:
        with _restore_ids(ids):
            return fn(*args, **kwargs)

    return wrapper


class CorrelationFilter(logging.Filter):
    """Stamps every record with the ids in scope.

    Also builds `ctx`, a pre-rendered fragment that is empty when nothing is
    set — `logging.Formatter` cannot omit a field conditionally, so a plain
    `%(request_id)s` would litter every startup line with dashes.
    """

    # Rendered in the order work flows: what triggered it, then what it is, then
    # which item of it.
    _LABELS = (("request_id", "req"), ("job_id", "job"),
               ("scan_id", "scan"), ("email_id", "email"))

    def filter(self, record: logging.LogRecord) -> bool:
        ids = current_ids()
        for name, value in ids.items():
            setattr(record, name, value)

        parts = [f"{label}={ids[name]}" for name, label in self._LABELS if ids[name]]
        record.ctx = f" [{' '.join(parts)}]" if parts else ""
        return True
