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


# ---------------------------------------------------------------------------
# Pagination bounds — BUGS.md P2-7
#
# `limit` went straight into `.range(offset, offset + limit - 1)`, so
# `?limit=1000000` was a full-table read, and the unlinked route selects bodies.
# These have to run over HTTP: `Query(le=...)` is enforced by FastAPI's
# validation layer, so calling the route function directly bypasses it entirely.
# ---------------------------------------------------------------------------

@pytest.fixture
def bounded_client(monkeypatch):
    """A client whose routes record the limit they were handed instead of
    querying Supabase, so a 200 proves the value that got through."""
    from fastapi.testclient import TestClient

    from backend.app.api import create_app
    from backend.services import inbox_service, reply_service

    seen: dict[str, int] = {}

    def _page(*, limit, offset, search="", label=""):
        seen["limit"], seen["offset"] = limit, offset
        return {"emails": [], "total": 0, "has_more": False}

    def _unlinked(*, limit, offset):
        seen["limit"], seen["offset"] = limit, offset
        return {"rate_cards": []}

    monkeypatch.setattr(inbox_service, "get_inbox_page", _page)
    monkeypatch.setattr(reply_service, "list_unlinked", _unlinked)
    # No `with`: entering TestClient runs the lifespan, which starts the
    # scheduler and opens sockets.
    return TestClient(create_app(), raise_server_exceptions=False), seen


def test_a_million_row_limit_is_refused(bounded_client):
    """The defect itself. Reverting the Query() ceiling makes this a 200."""
    client, _ = bounded_client
    assert client.get("/fetch-inbox?limit=1000000").status_code == 422


@pytest.mark.parametrize("limit,expected", [(1, 200), (200, 200), (201, 422), (0, 422), (-5, 422)])
def test_the_inbox_limit_ceiling_is_exactly_200(bounded_client, limit, expected):
    client, _ = bounded_client
    assert client.get(f"/fetch-inbox?limit={limit}").status_code == expected


def test_a_negative_offset_is_refused(bounded_client):
    """`.range(-1, 18)` is not a page of anything."""
    client, _ = bounded_client
    assert client.get("/fetch-inbox?offset=-1").status_code == 422


def test_the_inbox_default_page_is_unchanged(bounded_client):
    """The ceiling must not move the default — 20 is what the frontend expects."""
    client, seen = bounded_client
    assert client.get("/fetch-inbox").status_code == 200
    assert seen == {"limit": 20, "offset": 0}


@pytest.mark.parametrize("limit,expected", [(100, 200), (101, 422)])
def test_the_unlinked_ceiling_is_tighter_because_it_selects_bodies(bounded_client, limit, expected):
    client, _ = bounded_client
    assert client.get(f"/rate-cards/unlinked?limit={limit}").status_code == expected


def test_the_unlinked_default_page_is_unchanged(bounded_client):
    client, seen = bounded_client
    assert client.get("/rate-cards/unlinked").status_code == 200
    assert seen == {"limit": 50, "offset": 0}


def test_the_bounds_are_annotated_so_direct_callers_still_get_ints():
    """`Annotated[int, Query(ge=1, le=200)] = 20`, never `Query(20, ge=1, le=200)`.

    The second form makes the *default* a Query object, so any caller that is not
    an HTTP request — twenty-odd tests in this suite call these routes as plain
    functions — gets a Query where it expects an int and dies with
    `unsupported operand type(s) for +: 'Query' and 'int'` deep inside the
    repository, reported to the operator as a 500.
    """
    import inspect

    from backend.app.routes.inbox import fetch_inbox, list_unlinked_rate_cards

    cases = (
        (fetch_inbox, {"limit": 20, "offset": 0}),
        (list_unlinked_rate_cards, {"limit": 50, "offset": 0}),
    )
    for fn, expected in cases:
        defaults = {n: p.default for n, p in inspect.signature(fn).parameters.items()}
        for name, value in expected.items():
            assert type(defaults[name]) is int, f"{fn.__name__}.{name} is not a plain int"
            assert defaults[name] == value, f"{fn.__name__}.{name} default moved"
