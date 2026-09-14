"""
Replaying Supabase requests whose connection died.

Production symptom this protects: bursts of HTTP 500 in the same millisecond,
each one `Failed to fetch inbox: [Errno 32] Broken pipe` or
`<ConnectionTerminated error_code:9 ...>`, clearing on their own a moment later.
Those are pooled sockets closed by the far end between calls, not bad queries —
and because postgrest-py speaks HTTP/2, one dead socket fails every request
multiplexed on it at once.

The thing actually worth testing is not "does it retry" but "does it know when it
must NOT". A replay is only safe when the request either changes nothing, or
provably never arrived. Getting that wrong duplicates a write, which for this app
means a second RFQ email to a real freight agent.
"""

import httpx
import pytest

from backend.core import db


class _FailNTimes(httpx.BaseTransport):
    """Raise `exc` for the first `failures` calls, then answer 200."""

    def __init__(self, exc: Exception, failures: int = 1):
        self.exc = exc
        self.failures = failures
        self.calls: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.method)
        if len(self.calls) <= self.failures:
            raise self.exc
        return httpx.Response(200, json={"ok": True}, request=request)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Retry backoff is real time; nothing here is testing that we can sleep."""
    monkeypatch.setattr(db.time, "sleep", lambda _s: None)


def _get(url: str = "https://db.example/rest/v1/emails") -> httpx.Request:
    return httpx.Request("GET", url)


def _post(json=None) -> httpx.Request:
    return httpx.Request("POST", "https://db.example/rest/v1/emails", json=json or {"id": 1})


# --- reads: retried for every transient connection failure -------------------


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("connection refused"),
        httpx.ConnectTimeout("timed out connecting"),
        httpx.WriteError("[Errno 32] Broken pipe"),
        httpx.WriteTimeout("timed out sending"),
        httpx.ReadError("connection reset"),
        httpx.RemoteProtocolError(
            "<ConnectionTerminated error_code:9, last_stream_id:19, additional_data:None>"
        ),
    ],
)
def test_read_is_replayed_and_succeeds(exc):
    inner = _FailNTimes(exc)
    resp = db._RetryTransport(inner).handle_request(_get())
    assert resp.status_code == 200
    assert inner.calls == ["GET", "GET"]


# --- writes: replayed only when the request cannot have landed ---------------


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("connection refused"),
        httpx.WriteError("[Errno 32] Broken pipe"),
    ],
)
def test_write_is_replayed_when_request_never_left(exc):
    """Failing to connect, or failing partway through sending, means PostgREST
    never saw a whole request — so replaying cannot insert a second row."""
    inner = _FailNTimes(exc)
    resp = db._RetryTransport(inner).handle_request(_post())
    assert resp.status_code == 200
    assert inner.calls == ["POST", "POST"]


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadError("connection reset"),
        httpx.RemoteProtocolError("<ConnectionTerminated error_code:9 ...>"),
    ],
)
def test_write_is_not_replayed_when_the_response_was_merely_lost(exc):
    """The request went out and the answer vanished. The insert may well have
    committed, so a replay would duplicate it. Surfacing the error is correct."""
    inner = _FailNTimes(exc)
    with pytest.raises(type(exc)):
        db._RetryTransport(inner).handle_request(_post())
    assert inner.calls == ["POST"]


def test_streaming_body_is_never_replayed():
    """An attachment upload's body is consumed by the failed attempt. Replaying
    it would send a truncated file under the same name, which is worse than the
    error we were trying to hide."""

    def chunks():
        yield b"attachment-bytes"

    request = httpx.Request("POST", "https://db.example/storage/v1/object/x", content=chunks())
    inner = _FailNTimes(httpx.ConnectError("connection refused"))
    with pytest.raises(httpx.ConnectError):
        db._RetryTransport(inner).handle_request(request)
    assert inner.calls == ["POST"]


# --- what must never be retried ----------------------------------------------


def test_query_errors_are_passed_straight_through():
    """A unique-constraint violation is an answer, not a broken connection.

    PostgREST sends it as a normal HTTP response, so the transport returns it
    untouched and supabase-py raises APIError above us. Retrying a 409 would turn
    one honest error into three.
    """
    calls = []

    class Conflict(httpx.BaseTransport):
        def handle_request(self, request):
            calls.append(request.method)
            return httpx.Response(409, json={"code": "23505"}, request=request)

    resp = db._RetryTransport(Conflict()).handle_request(_post())
    assert resp.status_code == 409
    assert calls == ["POST"]


def test_unrelated_exceptions_are_not_retried():
    """Timeouts are deliberately excluded: the server is probably still working
    on the request, so a replay adds load to something already struggling."""
    inner = _FailNTimes(httpx.ReadTimeout("query too slow"))
    with pytest.raises(httpx.ReadTimeout):
        db._RetryTransport(inner).handle_request(_get())
    assert inner.calls == ["GET"]


def test_gives_up_after_the_attempt_budget():
    """A Supabase project that is genuinely down must fail fast, not hold the
    request open through an unbounded number of replays."""
    inner = _FailNTimes(httpx.ConnectError("host unreachable"), failures=99)
    with pytest.raises(httpx.ConnectError):
        db._RetryTransport(inner, attempts=3).handle_request(_get())
    assert inner.calls == ["GET", "GET", "GET"]


def test_retry_is_logged_so_a_flapping_connection_is_visible(caplog):
    """Silent retries would hide a Supabase pool that is degrading."""
    inner = _FailNTimes(httpx.WriteError("[Errno 32] Broken pipe"))
    with caplog.at_level("WARNING", logger="backend.core.db"):
        db._RetryTransport(inner).handle_request(_get())
    assert "Supabase connection failed" in caplog.text
    assert "Broken pipe" in caplog.text


# --- installing it on the real client ----------------------------------------


def test_harden_wraps_the_sessions_supabase_built():
    class FakeSub:
        def __init__(self):
            self.session = httpx.Client()

    class FakeClient:
        def __init__(self):
            self.postgrest = FakeSub()
            self.storage = FakeSub()

    client = FakeClient()
    inner = client.postgrest.session._transport
    db._harden(client)

    assert isinstance(client.postgrest.session._transport, db._RetryTransport)
    assert isinstance(client.storage.session._transport, db._RetryTransport)
    assert client.postgrest.session._transport._inner is inner


def test_harden_is_idempotent():
    """`get_db` is guarded by a lock, but a double-wrap would silently square the
    attempt budget, so the guard is asserted rather than assumed."""

    class FakeSub:
        def __init__(self):
            self.session = httpx.Client()

    class FakeClient:
        def __init__(self):
            self.postgrest = FakeSub()
            self.storage = FakeSub()

    client = FakeClient()
    db._harden(client)
    wrapped = client.postgrest.session._transport
    db._harden(client)
    assert client.postgrest.session._transport is wrapped


def test_harden_fails_soft_when_supabase_internals_move(caplog):
    """This reaches into a private attribute of a pinned dependency. If a future
    supabase-py renames it, the API must still boot — unhardened and loud, not
    dead."""

    class Alien:
        pass

    client = Alien()
    with caplog.at_level("WARNING", logger="backend.core.db"):
        db._harden(client)  # must not raise
    assert "connection retries are inactive" in caplog.text

# --- HTTP/1.1 downgrade -------------------------------------------------------


def _fake_client(http2: bool = True):
    class FakeSub:
        def __init__(self):
            self.session = httpx.Client(http2=http2)

    class FakeClient:
        def __init__(self):
            self.postgrest = FakeSub()
            self.storage = FakeSub()

    return FakeClient()


def test_harden_takes_new_connections_down_to_http1():
    """The reason the retry above exists at all.

    postgrest-py sets `http2=True`, which puts every thread on one socket sharing
    one HPACK table that `h2` mutates without a lock. Measured against the real
    project, that failed 7.9% of reads under 12 threads; HTTP/1.1 failed none,
    because a connection serves one request at a time.
    """
    client = _fake_client(http2=True)
    assert client.postgrest.session._transport._pool._http2 is True

    db._harden(client)

    for sub in (client.postgrest, client.storage):
        assert sub.session._transport._inner._pool._http2 is False


def test_harden_does_not_reach_past_a_transport_it_does_not_recognise():
    """A custom transport with no pool must not crash startup, and must still get
    the retry wrapper — the two protections are independent."""

    class NoPool(httpx.BaseTransport):
        def handle_request(self, request):  # pragma: no cover - never called
            raise AssertionError

    class FakeSub:
        def __init__(self):
            self.session = httpx.Client(transport=NoPool())

    class FakeClient:
        def __init__(self):
            self.postgrest = FakeSub()
            self.storage = FakeSub()

    client = FakeClient()
    db._harden(client)  # must not raise
    assert isinstance(client.postgrest.session._transport, db._RetryTransport)
