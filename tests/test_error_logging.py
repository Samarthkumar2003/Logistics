"""
Error paths carry a correlation id and state a reason.

Three defects lived here, all of the same shape: a failure produced a correct
HTTP response and no usable log line.

    1. An unhandled 500 was logged by the catch-all handler in errors.py. FastAPI
       registers that handler on Starlette's ServerErrorMiddleware, which is built
       *outermost* — outside the correlation middleware. By the time it ran, the
       `with request_context(...)` block had exited and reset the contextvar, so
       the traceback arrived with an empty ctx. No access line either, since that
       is emitted after `call_next` returns and it never returned. The one line
       most worth tracing was the only line that could not be traced.

    2. `AppException` was returned and never logged. The access line recorded the
       status but not the reason, so `AppException(502, "postgrest upstream
       failed")` left a 502 in the log and no trace of what failed.

    3. The inbound X-Request-ID was rendered into every line of its request with
       no validation, letting a caller forge `job=` and `email=` fields.

These tests use TestClient, which exercises the real middleware stack — the point
is the ordering of that stack, so stubbing it would test nothing.
"""

import logging

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from backend.app.errors import AppException
from backend.core.logging_context import clean_incoming_id


class _Capture(logging.Handler):
    """Collects records with the ctx the filter stamped on them."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def lines(self) -> list[tuple[str, str, str]]:
        return [(r.levelname, getattr(r, "request_id", ""), r.getMessage())
                for r in self.records
                if not r.name.startswith(("httpx", "httpcore", "asyncio"))]


@pytest.fixture
def app_with_failures(monkeypatch):
    """The **real** application, with routes bolted on that fail on demand.

    Deliberately `create_app()` and not a hand-built FastAPI instance with a copy
    of the middleware. The bug under test is the *ordering* of Starlette's
    middleware stack relative to the correlation middleware; a reimplementation
    here would pass while api.py was reverted, which is the one outcome this file
    exists to prevent.

    Two side effects of importing api.py have to be contained. It calls
    `configure_logging(log_file=default_log_file())` at module scope, and
    LOG_TO_FILE defaults to on — importing it unguarded creates `logs/` and starts
    a rotating handler in the middle of a unit-test run. Patching the name in
    logging_config *before* api.py is first imported means api's `from ... import`
    binds the stub. It also builds the app at import, but the lifespan only runs
    inside `with TestClient(...)`, and this fixture never uses one, so no
    scheduler starts and no thread is spawned.
    """
    monkeypatch.setattr("backend.core.logging_config.default_log_file", lambda: None)

    from backend.app.api import create_app

    app = create_app()

    class Body(BaseModel):
        n: int

    @app.get("/__test_boom")
    def boom():
        raise RuntimeError("kaboom")

    @app.get("/__test_upstream")
    def upstream():
        raise AppException(502, "postgrest upstream failed")

    @app.get("/__test_missing")
    def missing():
        raise AppException(404, "job REF-9 not found")

    @app.post("/__test_shape")
    def shape(body: Body):
        return {"ok": body.n}

    return app


@pytest.fixture
def capture():
    handler = _Capture()
    root = logging.getLogger()
    # The stamping filter is installed on handlers by configure_logging; this
    # handler is ours, so it needs its own.
    from backend.core.logging_context import CorrelationFilter
    handler.addFilter(CorrelationFilter())
    root.addHandler(handler)
    previous = root.level
    root.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


@pytest.fixture
def client(app_with_failures):
    # raise_server_exceptions=False makes TestClient behave like a real server:
    # return the 500 rather than re-raising into the test.
    return TestClient(app_with_failures, raise_server_exceptions=False)


# --- 1. unhandled 500 -------------------------------------------------------

def test_unhandled_500_traceback_carries_request_id(client, capture):
    response = client.get("/__test_boom", headers={"X-Request-ID": "rid-500"})
    assert response.status_code == 500

    correlated = [msg for level, rid, msg in capture.lines()
                  if rid == "rid-500" and "unhandled" in msg]
    assert correlated, (
        "an unhandled 500 must be logged inside the correlation middleware; "
        f"got {capture.lines()}"
    )


def test_unhandled_500_logs_the_traceback(client, capture):
    client.get("/__test_boom", headers={"X-Request-ID": "rid-tb"})

    with_exc = [r for r in capture.records
                if getattr(r, "request_id", "") == "rid-tb" and r.exc_info]
    assert with_exc, "the 500 must be logged with exc_info, not just a message"
    assert with_exc[0].levelname == "ERROR"


def test_unhandled_500_is_not_double_logged(client, capture):
    """The catch-all handler must stay silent.

    If both it and the middleware log, every 500 writes two tracebacks — and
    tracebacks are not redacted, so duplicating them doubles the exposure.
    """
    client.get("/__test_boom", headers={"X-Request-ID": "rid-dup"})

    tracebacks = [r for r in capture.records if r.exc_info]
    assert len(tracebacks) == 1, f"expected one traceback, got {len(tracebacks)}"


# --- 2. deliberate failures state a reason ----------------------------------

def test_app_exception_detail_reaches_the_log(client, capture):
    response = client.get("/__test_upstream", headers={"X-Request-ID": "rid-502"})
    assert response.status_code == 502

    logged = [msg for _, rid, msg in capture.lines() if rid == "rid-502"]
    assert any("postgrest upstream failed" in msg for msg in logged), (
        f"the reason for a 502 must appear in the log, not only in the response body; got {logged}"
    )


def test_server_side_app_exception_is_error_level(client, capture):
    client.get("/__test_upstream", headers={"X-Request-ID": "rid-5xx"})

    levels = [level for level, rid, msg in capture.lines()
              if rid == "rid-5xx" and "AppException" in msg]
    assert levels == ["ERROR"], f"5xx is ours to fix, so ERROR; got {levels}"


def test_client_side_app_exception_is_warning_level(client, capture):
    """A 404 is the caller's mistake. ERROR means a human must act, and nobody
    needs paging because someone typed a bad reference."""
    client.get("/__test_missing", headers={"X-Request-ID": "rid-4xx"})

    levels = [level for level, rid, msg in capture.lines()
              if rid == "rid-4xx" and "AppException" in msg]
    assert levels == ["WARNING"], f"4xx is the caller's, so WARNING; got {levels}"


def test_validation_error_names_the_field(client, capture):
    response = client.post("/__test_shape", json={"n": "not-an-int"},
                           headers={"X-Request-ID": "rid-422"})
    assert response.status_code == 422

    logged = [msg for _, rid, msg in capture.lines()
              if rid == "rid-422" and "Validation failed" in msg]
    assert logged, "a 422 must be logged; silent, it looks like no request arrived"
    assert "n" in logged[0]


# --- 3. the inbound id is the only one we do not mint -----------------------

@pytest.mark.parametrize("hostile, why", [
    ("x] [job=scan:FORGED email=victim@example.com", "forges job and email fields"),
    ("\x1b[31mRED\x1b[0m", "ANSI escapes colour the terminal from input"),
    ("has space", "spaces break the single-token ctx format"),
    ("A" * 65, "just over the length limit"),
    ("A" * 8192, "multiplies every line of the request"),
    ("", "empty header must mint, not render an empty id"),
    (None, "absent header"),
])
def test_hostile_request_ids_are_rejected(hostile, why):
    assert clean_incoming_id(hostile) is None, why


@pytest.mark.parametrize("legit", [
    "trace-abc_1.2",
    "0d5a2bf7",
    "scan:0d5a2bf7",
    "A" * 64,
])
def test_legitimate_request_ids_survive(legit):
    """A guard that rejected these would break every trace in the runbook —
    the whole point of honouring an inbound id is cross-service tracing."""
    assert clean_incoming_id(legit) == legit


def test_forged_fields_never_reach_a_log_line(client, capture):
    client.get("/__test_missing",
               headers={"X-Request-ID": "x] [job=scan:FORGED email=victim@example.com"})

    rendered = " ".join(getattr(r, "ctx", "") for r in capture.records)
    assert "FORGED" not in rendered
    assert "victim@example.com" not in rendered
    assert "job=" not in rendered, "no job id was set, so none may be rendered"


def test_rejected_id_is_replaced_not_dropped(client, capture):
    """Every line still needs *an* id — falling back to no id would trade a
    forgery for an untraceable request."""
    response = client.get("/__test_missing", headers={"X-Request-ID": "bad id!"})

    assert response.headers["X-Request-ID"]
    ids = {getattr(r, "request_id", "") for r in capture.records
           if not r.name.startswith(("httpx", "httpcore", "asyncio"))}
    assert ids and "" not in ids, f"expected a minted id on every line, got {ids}"
