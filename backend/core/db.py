"""
db.py
-----
The Supabase client, created lazily and shared.

Previously five modules each did `create_client(...)` at import time. With an
empty or wrong `SUPABASE_URL` that raises *during import*, which killed the
whole API — including endpoints that never touch the database. It also meant a
lot of defensive plumbing: imports buried inside function bodies purely so a
module wouldn't be imported until it was unavoidable.

One lazy accessor fixes both. A credential problem now fails the request that
needs the database, as a clean 503, and every other endpoint keeps working.

    from backend.core.db import get_db
    rows = get_db().table("emails").select("id").execute().data

The client is also hardened against pooled connections that die between calls;
see `_RetryTransport` for why that belongs here and not at the call sites.
"""

import logging
import socket
import threading
import time
from typing import Optional

import httpx
from supabase import Client, create_client

from backend.core.config import ConfigError, settings

logger = logging.getLogger(__name__)

_client: Optional[Client] = None
_lock = threading.Lock()

# Connection failures we may replay whatever the request method was, because the
# far end provably never received a complete request: we died while connecting,
# or partway through sending. An unfinished request cannot have been applied.
_NEVER_DELIVERED = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.WriteError,
    httpx.WriteTimeout,
)

# The connection died *after* the request went out, so we cannot tell whether it
# was applied before the response was lost. Only safe to replay for methods that
# change nothing.
_MAYBE_DELIVERED = (
    httpx.ReadError,
    httpx.RemoteProtocolError,
)

# PostgREST reads are GET and every write is POST/PATCH/PUT/DELETE, so this is
# exactly "the requests that cannot change anything".
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = 0.05

# Sub-clients worth hardening. `functions` is omitted on purpose: nothing calls
# it, and these are lazy properties, so naming it here would build a client we
# never use.
_HARDENED_SUBCLIENTS = ("postgrest", "storage")


def _body_is_replayable(request: httpx.Request) -> bool:
    """True when the request body is bytes we still hold.

    A streaming upload has already been consumed by the failed attempt, so
    replaying it would send a *different*, truncated request. Reading `.content`
    is the supported way to ask: httpx raises for a body it cannot re-read.
    """
    try:
        request.content
    except httpx.RequestNotRead:
        return False
    return True


def _replay_allowed(exc: Exception, request: httpx.Request) -> bool:
    """Decide whether `exc` is a dead connection we may retry, or a real answer.

    Errors the server *sent* us never arrive here — a PostgREST error is a
    response with a status code, which supabase-py turns into `APIError` well
    above the transport. So everything this sees is a connection that broke, and
    the only question left is whether the request could already have taken
    effect.
    """
    if isinstance(exc, _NEVER_DELIVERED):
        pass
    elif isinstance(exc, _MAYBE_DELIVERED):
        if request.method not in _IDEMPOTENT_METHODS:
            return False
    else:
        return False
    return _body_is_replayable(request)


class _RetryTransport(httpx.BaseTransport):
    """Replay a request whose *connection* failed, not whose content did.

    Supabase is reached over a pool that keeps sockets alive between calls, so a
    share of them are closed by the far end before we notice. The next request
    onto one of those raises instead of returning: `[Errno 32] Broken pipe` on
    the way out, or an HTTP/2 `ConnectionTerminated` GOAWAY on the way back.
    Nothing is wrong with the query — the connection simply expired — but the
    caller sees a 500, which is how an idle five-minute-old client ends up
    showing the operator "Failed to fetch inbox: Broken pipe".

    Because postgrest-py enables HTTP/2, one dead connection takes down every
    request multiplexed on it, which is why these failures arrive in bursts of
    three or four in the same millisecond and then clear on their own.

    Retrying belongs here rather than at the ~95 `.execute()` call sites for two
    reasons. It is the only layer that can tell a broken connection from a query
    the database rejected, and it is the only one that gets a fresh connection
    for free: httpcore has already discarded the dead socket by the time we are
    handed the exception.
    """

    def __init__(
        self,
        inner: httpx.BaseTransport,
        attempts: int = _MAX_ATTEMPTS,
        backoff: float = _BACKOFF_SECONDS,
    ) -> None:
        self._inner = inner
        self._attempts = attempts
        self._backoff = backoff

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        for attempt in range(1, self._attempts + 1):
            try:
                return self._inner.handle_request(request)
            except Exception as exc:
                if attempt == self._attempts or not _replay_allowed(exc, request):
                    raise
                delay = self._backoff * attempt
                logger.warning(
                    "Supabase connection failed on %s %s (%s: %s) — replaying in "
                    "%.2fs, attempt %d of %d",
                    request.method,
                    request.url.path,
                    type(exc).__name__,
                    exc,
                    delay,
                    attempt + 1,
                    self._attempts,
                )
                time.sleep(delay)
        raise AssertionError("unreachable: loop either returns or raises")

    def close(self) -> None:
        self._inner.close()


def _disable_http2(transport: httpx.BaseTransport, name: str) -> None:
    """Take new connections down to HTTP/1.1, because one h2 connection is not
    safe to share between threads.

    postgrest-py asks httpx for `http2=True`, which is wrong for this app in a way
    that stays invisible until it isn't. An HTTP/2 connection multiplexes every
    caller onto one socket governed by one HPACK header-compression table — and
    that table is mutated by `h2`, a library with no locking anywhere in it, from
    httpcore's `_send_request_headers`, which does not take a lock either. Our
    routes are sync `def`, so Starlette runs them in a worker threadpool that all
    shares this one client. Two threads encoding headers at once corrupt the
    table, and the far end does the only thing it can: tears the whole connection
    down with GOAWAY `COMPRESSION_ERROR`, taking every request multiplexed on it.
    That is the `<ConnectionTerminated error_code:9>` in our logs, and the
    `[Errno 32] Broken pipe` is the next thread writing to the corpse.

    Measured against the real project, 12 threads x 60 reads over one client:

        HTTP/2, client shared across threads   7.9% failed
        HTTP/2, one client per thread          0% failed
        HTTP/1.1, client shared across threads 0% failed

    at the same throughput. HTTP/1.1 checks a connection out of the pool for one
    request at a time, so the shared mutable state stops existing rather than
    being raced over less often.

    There is no supported way to ask for this. postgrest-py hardcodes the flag,
    and `ClientOptions.httpx_client` would replace the whole client, discarding
    the base URL and auth headers it configures. httpcore reads `_http2` inside
    `create_connection`, so setting it here governs every connection from now on;
    the pool is empty at this point, so there is nothing already negotiated.
    """
    pool = getattr(transport, "_pool", None)
    if pool is None or not hasattr(pool, "_http2"):
        logger.warning(
            "Supabase %s client exposes no connection pool; it will keep using "
            "HTTP/2, which is not thread-safe (httpx internals changed?)",
            name,
        )
        return
    pool._http2 = False


def _harden(client: Client) -> None:
    """Make supabase-py's clients safe for the way this app uses them: HTTP/1.1
    so that threads cannot corrupt a shared HTTP/2 connection, and a retry around
    the transport for the connection failures that remain.

    supabase-py offers no supported hook for a custom transport on the clients it
    builds itself — `ClientOptions.httpx_client` replaces the whole client, which
    would also drop the base URL, auth headers and HTTP/2 setting it configures.
    So this reaches for a private attribute, and fails soft when it isn't there:
    if a future supabase-py renames it the right outcome is an unhardened client
    plus a warning, not an API that refuses to start.
    """
    for name in _HARDENED_SUBCLIENTS:
        session = getattr(getattr(client, name, None), "session", None)
        if not isinstance(session, httpx.Client):
            logger.warning(
                "Supabase %s client exposes no httpx session; connection retries "
                "are inactive for it (supabase-py internals changed?)",
                name,
            )
            continue
        transport = getattr(session, "_transport", None)
        if transport is None or isinstance(transport, _RetryTransport):
            continue
        _disable_http2(transport, name)
        session._transport = _RetryTransport(transport)


def get_db() -> Client:
    """Return the shared Supabase client, building it on first use.

    Raises ConfigError when credentials are missing — callers at the HTTP
    boundary translate that into a 503 rather than a 500, because it is a
    deployment problem, not a bug.
    """
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is None:
            settings.require_supabase()
            client = create_client(settings.supabase_url, settings.supabase_key)
            _harden(client)
            _client = client
            logger.info("Supabase client initialised")
    return _client


def reset_db() -> None:
    """Drop the cached client. For tests — lets a fake be installed between cases."""
    global _client
    with _lock:
        _client = None


def check_connectivity() -> None:
    """Log a clear diagnosis for the two failures that look identical from the
    inside: unset credentials, and a project URL that no longer resolves.

    Non-fatal by design. Call it at startup for the operator's benefit; it must
    never stop the process, because a DNS blip is not a reason to refuse to boot.
    """
    try:
        settings.require_supabase()
    except ConfigError as e:
        logger.error("%s", e)
        return

    host = settings.supabase_url.replace("https://", "").replace("http://", "").split("/")[0]
    try:
        socket.getaddrinfo(host, 443)
    except socket.gaierror:
        logger.error(
            "Supabase host %r does not resolve (DNS NXDOMAIN). The project may be "
            "deleted, or SUPABASE_URL is wrong — copy Project URL + service_role "
            "key from Supabase Dashboard → Settings → API.",
            host,
        )
