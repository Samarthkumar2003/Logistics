"""
Body lookup: what the operator is told when the body cannot be produced.

The distinction under test is not cosmetic. "It may have been deleted or moved"
tells the operator to stop looking; an expired refresh token tells them to
re-consent. Collapsing the second into the first hides the only failure here
that a human can actually fix.
"""

import pytest

from backend.app.errors import AppException
from backend.app.routes.inbox import get_email_body
from backend.connectors import gmail_connector
from backend.connectors.google_oauth import GmailReauthRequired
from backend.repositories import email_repo


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _HttpError(Exception):
    """Shaped like requests.HTTPError, which is what raise_for_status raises."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.response = _FakeResponse(status_code)


@pytest.fixture
def stored_body(monkeypatch):
    """Control what the store returns for a body lookup."""
    def _set(value):
        monkeypatch.setattr(email_repo, "get_body", lambda _mid: value)
    return _set


@pytest.fixture
def gmail_raises(monkeypatch):
    """Make the live Gmail fallback fail with a given exception."""
    def _set(exc):
        def _boom(_mid):
            raise exc
        monkeypatch.setattr(gmail_connector, "fetch_full_message", _boom)
    return _set


def test_stored_body_short_circuits_the_provider(stored_body, monkeypatch):
    stored_body("the body")

    def _never(_mid):
        raise AssertionError("Gmail must not be called when the store has the body")

    monkeypatch.setattr(gmail_connector, "fetch_full_message", _never)
    assert get_email_body("msg-1") == {"body": "the body"}


def test_falls_back_to_gmail_when_the_store_has_nothing(stored_body, monkeypatch):
    stored_body(None)
    monkeypatch.setattr(gmail_connector, "fetch_full_message",
                        lambda _mid: {"body": "live from gmail"})
    assert get_email_body("msg-2") == {"body": "live from gmail"}


def test_gmails_own_404_is_a_404(stored_body, gmail_raises):
    stored_body(None)
    gmail_raises(_HttpError(404))

    with pytest.raises(AppException) as exc:
        get_email_body("msg-3")
    assert exc.value.status_code == 404
    assert "deleted or moved" in exc.value.detail


@pytest.mark.parametrize("exc", [
    _HttpError(500),
    _HttpError(429),
    TimeoutError("read timeout"),
    ConnectionError("connection reset"),
])
def test_any_other_provider_failure_is_a_502_not_a_404(stored_body, gmail_raises, exc):
    """The regression this file exists for.

    `stored` is None for every message not yet persisted, which is the ordinary
    case for this fallback. An `or stored is None` in the 404 condition therefore
    made the 502 branch unreachable and reported every provider outage as
    missing mail.
    """
    stored_body(None)
    gmail_raises(exc)

    with pytest.raises(AppException) as err:
        get_email_body("msg-4")
    assert err.value.status_code == 502
    assert "deleted or moved" not in err.value.detail


def test_a_dead_refresh_token_says_so(stored_body, gmail_raises):
    stored_body(None)
    gmail_raises(GmailReauthRequired("token rejected — re-authorise"))

    with pytest.raises(AppException) as err:
        get_email_body("msg-5")
    assert err.value.status_code == 502
    assert "re-authorisation" in err.value.detail


def test_a_store_failure_is_a_500_and_never_reaches_the_provider(monkeypatch):
    def _boom(_mid):
        raise RuntimeError("supabase unreachable")

    monkeypatch.setattr(email_repo, "get_body", _boom)

    def _never(_mid):
        raise AssertionError("must not fall back when the store itself is broken")

    monkeypatch.setattr(gmail_connector, "fetch_full_message", _never)

    with pytest.raises(AppException) as err:
        get_email_body("msg-6")
    assert err.value.status_code == 500
