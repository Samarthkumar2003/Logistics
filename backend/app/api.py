"""
api.py
------
The FastAPI application: wiring only.

Everything that used to live here — 18 endpoints, every model, the scheduler,
the exception handlers, ad-hoc Supabase queries — now sits in a layer that can
be tested without an HTTP client:

    routes/       validate input, call one service, shape the response
    services/     business rules, no FastAPI, no Supabase
    repositories/ the only modules that touch Supabase
    domain/       the dataclasses those pass around

Run with:  uvicorn backend.app.api:app --port 8001
"""

import logging
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from backend.app.auth import bearer_middleware
from backend.app.errors import register_handlers
from backend.app.lifespan import lifespan
from backend.app.routes import auth, automation, inbox, ingest, jobs, ops, rfq
from backend.core.config import settings
from backend.core.logging_config import configure_logging, default_log_file
from backend.core.logging_context import clean_incoming_id, request_context

configure_logging(log_file=default_log_file())
logger = logging.getLogger(__name__)

# Paths whose access log is pure noise — a monitor hitting /health every 30s
# would otherwise dominate the file.
_QUIET_PATHS = {"/health", "/metrics", "/openapi.json", "/docs", "/favicon.ico"}


def create_app(auth_enabled: bool | None = None) -> FastAPI:
    """Build the app. `auth_enabled=None` means "read AUTH_ENABLED", which is what
    the module-level `app` below does; tests pass an explicit bool."""
    auth_on = settings.auth_enabled if auth_enabled is None else auth_enabled

    # Refuses to boot with auth on and no usable JWT_SECRET. Before the first
    # request, so a misconfigured deploy never reports healthy.
    if auth_on:
        settings.require_auth_secret()

    app = FastAPI(title="Logistics Copilot API", lifespan=lifespan)

    # ---- Middleware order is load-bearing -----------------------------------
    #
    # `add_middleware` INSERTS AT POSITION 0, so the LAST one registered here is
    # the OUTERMOST layer at runtime. Registration order below is therefore the
    # reverse of execution order. What we want, outermost first:
    #
    #     CORS  ->  correlate  ->  auth  ->  router
    #
    # CORS outermost, and this is the part that bit: it must wrap the auth
    # middleware, not sit inside it. A 401 produced inside CORS carries no
    # `Access-Control-Allow-Origin`, so the browser refuses to expose the response
    # to JavaScript at all — `fetch` rejects with a generic TypeError and the
    # frontend cannot see the 401 it needs in order to redirect to /login. Every
    # expired session would surface as "Failed to fetch". CORS outermost also
    # means Starlette answers the preflight itself, before auth can 401 an OPTIONS
    # request that by definition carries no Authorization header.
    #
    # correlate outside auth so a rejected request still gets a request id and an
    # access line — "who was hitting this with a bad token" is exactly the
    # question a 401 raises, and it is unanswerable if the log line is inside the
    # layer that refused.
    app.add_middleware(BaseHTTPMiddleware, dispatch=bearer_middleware(auth_on))

    @app.middleware("http")
    async def correlate(request: Request, call_next):
        """Give every request an id, echo it back, and log how it went.

        An inbound X-Request-ID is honoured so a trace can be followed across
        the frontend and here — after a shape check, since that value is echoed
        into every line of the request. contextvars propagate into the threadpool
        that runs sync endpoints, so the id reaches repository code without being
        passed through every signature.
        """
        incoming = clean_incoming_id(request.headers.get("X-Request-ID"))
        with request_context(incoming) as rid:
            started = time.perf_counter()
            try:
                response = await call_next(request)
            except Exception:
                # Logged here rather than in the catch-all handler in errors.py.
                # Starlette builds ServerErrorMiddleware as the *outermost* layer,
                # so `@app.exception_handler(Exception)` runs after this `with`
                # block has already exited and reset the contextvar. The one line
                # you most need to trace — an unhandled 500 — was arriving with an
                # empty ctx, no access line, and no X-Request-ID on the response.
                # Nothing tied a stack trace to the request that caused it.
                elapsed_ms = (time.perf_counter() - started) * 1000
                logger.exception("%s %s -> 500 unhandled (%.0fms)",
                                 request.method, request.url.path, elapsed_ms)
                raise
            elapsed_ms = (time.perf_counter() - started) * 1000
            response.headers["X-Request-ID"] = rid
            if request.url.path not in _QUIET_PATHS:
                log = logger.warning if response.status_code >= 500 else logger.info
                log("%s %s -> %d (%.0fms)", request.method, request.url.path,
                    response.status_code, elapsed_ms)
            return response

    # Registered last, so it ends up outermost. See the ordering note above.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["*"],
        # `*` already covers Authorization. Spelled out anyway because the token
        # header is now the difference between a working dashboard and a wall of
        # 401s, and a future narrowing of this list must not drop it by accident.
        allow_headers=["*", "Authorization", "Content-Type", "X-Request-ID"],
        # Deliberately absent: allow_credentials. The frontend sends a bearer
        # token, not a cookie, so credentialed CORS is not needed — and enabling
        # it would forbid the `*` above.
        expose_headers=["X-Request-ID"],
    )

    register_handlers(app)

    for module in (auth, ops, inbox, ingest, jobs, rfq, automation):
        app.include_router(module.router)

    logger.info(
        "API ready — auth=%s, scheduler=%s, safe_mode=%s, llm=%s",
        auth_on, settings.run_scheduler,
        settings.safe_mode, settings.llm_provider,
    )
    if not auth_on:
        # Loud, every boot. The only legitimate use is the offline test suite,
        # and a stray AUTH_ENABLED=0 in a deployment leaves an API that mails real
        # freight agents open to anyone who can reach the port (BUGS.md P1-1).
        logger.warning(
            "AUTH_ENABLED=0 — every endpoint is UNAUTHENTICATED. Test-suite only."
        )
    return app


app = create_app()
