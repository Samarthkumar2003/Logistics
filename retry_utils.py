"""
retry_utils.py
--------------
Exponential backoff retry decorator for OpenAI and Supabase calls.
Retries on transient network errors and rate limits — fails fast on
auth errors, bad requests, and validation errors.
"""
import logging
import time
import functools
from typing import Callable, TypeVar, ParamSpec

log = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

# Errors that are transient and worth retrying
_RETRYABLE_SUBSTRINGS = (
    "rate limit",
    "timeout",
    "connection",
    "502",
    "503",
    "504",
    "overloaded",
    "try again",
    "temporarily unavailable",
)

# Errors that are permanent — no point retrying
_PERMANENT_SUBSTRINGS = (
    "invalid api key",
    "authentication",
    "unauthorized",
    "not found",
    "bad request",
    "invalid request",
)


def _is_retryable(exc: Exception) -> bool:
    msg = str(exc).lower()
    if any(s in msg for s in _PERMANENT_SUBSTRINGS):
        return False
    if any(s in msg for s in _RETRYABLE_SUBSTRINGS):
        return True
    # Default: retry unknown errors up to the limit
    return True


def with_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """
    Decorator: retry with exponential backoff.

    Args:
        max_attempts: Total attempts including the first try.
        base_delay: Initial wait in seconds before first retry.
        max_delay: Cap on wait time between retries.
        backoff_factor: Multiplier applied to delay each retry.

    Usage:
        @with_retry(max_attempts=3)
        def call_openai(...):
            ...
    """
    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            delay = base_delay
            last_exc: Exception | None = None

            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    last_exc = exc
                    if attempt == max_attempts or not _is_retryable(exc):
                        log.error(
                            "%s failed after %d attempt(s): %s",
                            fn.__name__, attempt, exc,
                        )
                        raise
                    wait = min(delay, max_delay)
                    log.warning(
                        "%s attempt %d/%d failed (%s). Retrying in %.1fs...",
                        fn.__name__, attempt, max_attempts, exc, wait,
                    )
                    time.sleep(wait)
                    delay *= backoff_factor

            raise last_exc  # unreachable but satisfies type checker

        return wrapper
    return decorator
