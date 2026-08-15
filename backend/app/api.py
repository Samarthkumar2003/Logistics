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

from backend.app.errors import register_handlers
from backend.app.lifespan import lifespan
from backend.app.routes import automation, inbox, jobs, ops, rfq
from backend.core.config import settings
from backend.core.logging_config import configure_logging, default_log_file
from backend.core.logging_context import request_context

configure_logging(log_file=default_log_file())
logger = logging.getLogger(__name__)

# Paths whose access log is pure noise — a monitor hitting /health every 30s
# would otherwise dominate the file.
_QUIET_PATHS = {"/health", "/metrics", "/openapi.json", "/docs", "/favicon.ico"}


def create_app() -> FastAPI:
    app = FastAPI(title="Logistics Copilot API", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def correlate(request: Request, call_next):
        """Give every request an id, echo it back, and log how it went.

        An inbound X-Request-ID is honoured so a trace can be followed across
        the frontend and here. contextvars propagate into the threadpool that
        runs sync endpoints, so the id reaches repository code without being
        passed through every signature.
        """
        incoming = request.headers.get("X-Request-ID", "")
        with request_context(incoming or None) as rid:
            started = time.perf_counter()
            response = await call_next(request)
            elapsed_ms = (time.perf_counter() - started) * 1000
            response.headers["X-Request-ID"] = rid
            if request.url.path not in _QUIET_PATHS:
                log = logger.warning if response.status_code >= 500 else logger.info
                log("%s %s -> %d (%.0fms)", request.method, request.url.path,
                    response.status_code, elapsed_ms)
            return response

    register_handlers(app)

    for module in (ops, inbox, jobs, rfq, automation):
        app.include_router(module.router)

    logger.info(
        "API ready — scheduler=%s, safe_mode=%s, llm=%s",
        settings.run_scheduler, settings.safe_mode, settings.llm_provider,
    )
    return app


app = create_app()
