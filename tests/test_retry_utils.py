"""
Retry policy.

`with_retry` wraps outbound OpenAI and Supabase calls. Two ways it can hurt:
retrying something that will never succeed (slow failure, wasted quota), or
giving up on something transient (lost email).
"""

import pytest

from backend.core.retry_utils import _is_retryable, with_retry


# ---------------------------------------------------------------------------
# _is_retryable
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("message", [
    "Rate limit exceeded, please try again",
    "Connection reset by peer",
    "Request timeout",
    "502 Bad Gateway",
    "503 Service Unavailable",
    "Server overloaded",
    "temporarily unavailable",
])
def test_transient_errors_are_retried(message):
    assert _is_retryable(Exception(message)) is True


@pytest.mark.parametrize("message", [
    "Invalid API key provided",
    "Authentication failed",
    "401 Unauthorized",
    "404 Not Found",
    "400 Bad Request",
    "Invalid request: missing field",
])
def test_permanent_errors_are_not_retried(message):
    assert _is_retryable(Exception(message)) is False


def test_permanent_wins_when_a_message_matches_both_lists():
    # "Invalid API key ... please try again" — providers really do word it this
    # way. Retrying a bad key three times just delays the real error.
    assert _is_retryable(Exception("Invalid API key, please try again")) is False


def test_p2_6_unrecognised_errors_are_retried_by_default():
    """KNOWN ISSUE (P2-6). Anything not on either list is retried.

    A genuine programming error — TypeError, KeyError — is therefore attempted
    three times with backoff before surfacing, turning an instant failure into
    a slow one. Pinned here so tightening the default is a deliberate change.
    """
    assert _is_retryable(TypeError("unsupported operand type(s)")) is True
    assert _is_retryable(KeyError("shipment_origin")) is True


# ---------------------------------------------------------------------------
# with_retry
# ---------------------------------------------------------------------------

def test_returns_immediately_when_the_call_succeeds(no_sleep):
    calls = []

    @with_retry(max_attempts=3, base_delay=0)
    def works():
        calls.append(1)
        return "ok"

    assert works() == "ok"
    assert len(calls) == 1
    assert no_sleep == []  # no backoff on the happy path


def test_recovers_after_transient_failures(no_sleep):
    calls = []

    @with_retry(max_attempts=3, base_delay=0)
    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise Exception("connection reset")
        return "recovered"

    assert flaky() == "recovered"
    assert len(calls) == 3


def test_gives_up_after_max_attempts_and_reraises(no_sleep):
    calls = []

    @with_retry(max_attempts=3, base_delay=0)
    def always_fails():
        calls.append(1)
        raise Exception("connection reset")

    with pytest.raises(Exception, match="connection reset"):
        always_fails()
    assert len(calls) == 3


def test_a_permanent_error_fails_on_the_first_attempt(no_sleep):
    calls = []

    @with_retry(max_attempts=5, base_delay=0)
    def bad_key():
        calls.append(1)
        raise Exception("Invalid API key")

    with pytest.raises(Exception, match="Invalid API key"):
        bad_key()
    assert len(calls) == 1, "a permanent error must not consume retries"


def test_backoff_grows_and_is_capped(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("backend.core.retry_utils.time.sleep", slept.append)

    @with_retry(max_attempts=5, base_delay=1.0, max_delay=3.0, backoff_factor=2.0)
    def always_fails():
        raise Exception("timeout")

    with pytest.raises(Exception):
        always_fails()

    # 5 attempts produce 4 waits: 1, 2, then capped at max_delay.
    assert slept == [1.0, 2.0, 3.0, 3.0]


def test_the_wrapper_keeps_the_original_function_identity():
    @with_retry()
    def named_function():
        """Docstring worth keeping."""

    # functools.wraps: without it, every log line about a retry would say
    # "wrapper failed" instead of naming the call that failed.
    assert named_function.__name__ == "named_function"
    assert named_function.__doc__ == "Docstring worth keeping."
