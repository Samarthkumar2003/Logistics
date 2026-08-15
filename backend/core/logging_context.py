"""
logging_context.py
------------------
Correlation ids, so a log line says *which* request, scan, or email it belongs to.

Before this, answering "what happened to this email?" meant grepping for a
message id and hoping every relevant line happened to mention it. Most did not:
`Supabase client initialised`, `Rate limit on attempt 2`, and
`Failed to persist email` carried no clue which of 50 emails in the batch they
were about.

Three ids, set by whoever owns the boundary:

    request_id   one HTTP request      set by middleware, echoed as X-Request-ID
    scan_id      one automation run    set by run_scan
    email_id     one email             set around per-email work

They live in `contextvars`, so they follow the work without being threaded
through every function signature — including into FastAPI's threadpool, which
copies the context when it runs a sync endpoint.

    with email_context(msg_id):
        ...                      # every log line in here carries email=<msg_id>
"""

import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

_request_id: ContextVar[str] = ContextVar("request_id", default="")
_scan_id: ContextVar[str] = ContextVar("scan_id", default="")
_email_id: ContextVar[str] = ContextVar("email_id", default="")


def new_id(prefix: str = "") -> str:
    """A short correlation id. Eight hex characters is plenty to disambiguate
    concurrent work in a log file, and stays readable inline."""
    token = uuid.uuid4().hex[:8]
    return f"{prefix}{token}" if prefix else token


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
def email_context(email_id: str) -> Iterator[str]:
    token = _email_id.set(email_id or "")
    try:
        yield email_id
    finally:
        _email_id.reset(token)


def current_ids() -> dict[str, str]:
    """The ids in scope right now — for attaching to an error report."""
    return {
        "request_id": _request_id.get(),
        "scan_id": _scan_id.get(),
        "email_id": _email_id.get(),
    }


class CorrelationFilter(logging.Filter):
    """Stamps every record with the ids in scope.

    Also builds `ctx`, a pre-rendered fragment that is empty when nothing is
    set — `logging.Formatter` cannot omit a field conditionally, so a plain
    `%(request_id)s` would litter every startup line with dashes.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        rid, sid, eid = _request_id.get(), _scan_id.get(), _email_id.get()
        record.request_id = rid
        record.scan_id = sid
        record.email_id = eid

        parts = []
        if rid:
            parts.append(f"req={rid}")
        if sid:
            parts.append(f"scan={sid}")
        if eid:
            parts.append(f"email={eid}")
        record.ctx = f" [{' '.join(parts)}]" if parts else ""
        return True
