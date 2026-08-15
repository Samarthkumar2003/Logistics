"""
errors.py
---------
The one exception type routes raise, and the handlers that turn anything else
into JSON.

Every response from this API is JSON, including failures, so the frontend can
always read `.detail` instead of guessing whether it got HTML back.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.core.config import ConfigError

logger = logging.getLogger(__name__)


class AppException(Exception):
    """A deliberate, caller-visible failure. Routes raise this; never HTTPException."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


def register_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppException)
    async def _app_exception(_request: Request, exc: AppException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(ConfigError)
    async def _config_error(_request: Request, exc: ConfigError):
        # A deployment problem, not a bug: the process is fine, a dependency is
        # unusable. 503 tells a monitor to alert rather than to file a defect.
        logger.error("Configuration problem: %s", exc)
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError):
        messages = [
            f"{' -> '.join(str(part) for part in err.get('loc', []))}: "
            f"{err.get('msg', 'invalid')}"
            for err in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": "; ".join(messages)})

    @app.exception_handler(Exception)
    async def _catch_all(_request: Request, exc: Exception):
        logger.exception("Unhandled error: %s", exc)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})
