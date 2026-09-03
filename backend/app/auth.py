"""
auth.py
-------
The bearer check that every route is behind.

Middleware rather than a `Depends(...)` on each router. A dependency has to be
added to each of the six routers and to every router added later; the one that
gets forgotten is a hole nobody notices, because the endpoint keeps working. A
middleware fails the other way: forget to exempt a public path and it 401s
loudly on the first request.

Closes BUGS.md P1-1 — `POST /send-rfq` and `POST /jobs/{ref}/approve` mail real
freight agents and `GET /agents` returns the whole contact database, all of it
previously reachable by anyone who could open the port.
"""

import logging
from typing import Callable

from fastapi import Request
from fastapi.responses import JSONResponse

from backend.core import security
from backend.core.config import settings

logger = logging.getLogger(__name__)

# Reachable without a token.
#
# `/health` is here because Railway's healthcheck cannot send an Authorization
# header — a 401 there marks the deployment failed and rolls it back. It leaks
# only a boolean per subsystem, which is the point of a probe.
#
# `/auth/login` obviously, or nobody can ever get a token.
#
# NOT here, and each for a reason:
#   /metrics       row counts per status, per label, per agent — a live read on
#                  the desk's volumes. No monitor scrapes it yet, so nothing
#                  breaks by requiring a token.
#   /docs,
#   /openapi.json  the full request shape of every endpoint. Swagger UI fetches
#                  the schema without an Authorization header, so these two
#                  cannot be split — either both are public or both are closed.
#                  Closed. Run locally with AUTH_ENABLED=0 to use them.
PUBLIC_PATHS = frozenset({"/health", "/auth/login"})


def _unauthorized(detail: str, path: str) -> JSONResponse:
    """401 as JSON, built here rather than raised as AppException.

    `@app.middleware("http")` runs on Starlette's BaseHTTPMiddleware, which sits
    OUTSIDE the ExceptionMiddleware that FastAPI registers `@app.exception_handler`
    on. An AppException raised here reaches ServerErrorMiddleware instead of its
    handler, and the caller gets a 500 with body `Internal Server Error` — the
    project rule that every response is JSON, broken by the one response the
    frontend most needs to parse. Same layering that api.py's `correlate` comment
    describes for unhandled 500s.

    `WWW-Authenticate` is set because RFC 7235 requires it on a 401 and because
    it stops browsers treating the response as a broken CORS request.
    """
    logger.warning("401 %s: %s", path, detail)
    return JSONResponse(
        status_code=401,
        content={"detail": detail},
        headers={"WWW-Authenticate": "Bearer"},
    )


def bearer_middleware(enabled: bool | None = None) -> Callable:
    """Build the dispatch function, with the on/off decision resolved ONCE here.

    A factory rather than a plain `async def` reading `settings.auth_enabled` per
    request, for two reasons. `settings` is a frozen dataclass built at import, so
    a test cannot flip one field on it; and the offline suite needs the check OFF
    for the twenty test files that predate auth and ON for tests/test_auth.py. An
    explicit argument threaded through `create_app(auth_enabled=True)` gives both
    without any module-attribute patching, and without the per-request branch
    depending on global state that a test might mutate halfway through a request.

    `None` means "whatever AUTH_ENABLED says", which is what production passes.
    """
    active = settings.auth_enabled if enabled is None else enabled

    async def require_bearer_token(request: Request, call_next):
        """Reject anything without a valid signed token, then stash its claims.

        Signature-only: no database read on the hot path. The dashboard fires ~30
        requests on load, and a `select` per request to confirm the operator still
        exists would add a Supabase round trip to each one. The cost of that
        choice is bounded and explicit — deactivating an operator stops new logins
        immediately but leaves an issued token working until it expires, at most
        JWT_EXPIRY_MINUTES. `/auth/me` does re-read, so the frontend learns within
        one page load.

        Routes read `request.state.claims` for the caller's identity. Nothing does
        yet — no endpoint is per-user — but an audit line on /send-rfq wants it.
        """
        if not active:
            # Offline test suite only; config.require_auth_secret() is what stops
            # this reaching a deployment.
            return await call_next(request)

        path = request.url.path
        if path in PUBLIC_PATHS:
            return await call_next(request)

        # CORSMiddleware is registered outermost in create_app() and answers
        # preflight itself, so an OPTIONS should never arrive here. Handled anyway:
        # if that ordering is ever changed, the failure is a browser that cannot
        # read a single response, which is a confusing way to find out.
        if request.method == "OPTIONS":
            return await call_next(request)

        try:
            token = security.bearer_token(request.headers.get("Authorization"))
            request.state.claims = security.decode_token(token)
        except security.TokenError as exc:
            return _unauthorized(str(exc), path)

        return await call_next(request)

    return require_bearer_token
