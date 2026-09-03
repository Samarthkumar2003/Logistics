"""
The manual ingest trigger.

Nothing in the HTTP surface reached the Gmail sweep: every inbox endpoint reads
Supabase, so "Refresh Inbox" re-rendered stored rows and could not surface a
message the 5-minute scheduler had not yet pulled. Waiting up to five minutes to
see mail you know has arrived reads as the mail being lost.

The trigger must also refuse to pile runs on top of each other. A sweep holds
_ingest_lock for its whole duration; a second one would acquire nothing, log
"another ingest in progress", and return skipped — so an operator clicking twice
would get a 202 for work that never ran.
"""

import pytest

from backend.app.errors import AppException
from backend.app.routes import ingest as ingest_route
from backend.connectors import email_store


@pytest.fixture
def started(monkeypatch):
    """Count background sweeps launched, without launching a thread."""
    launched = []
    monkeypatch.setattr(ingest_route, "run_ingest_in_background",
                        lambda: launched.append(1))
    return launched


@pytest.fixture
def in_progress(monkeypatch):
    """Force the reported lock state."""
    def _set(value: bool):
        monkeypatch.setattr(ingest_route, "ingest_in_progress", lambda: value)
    return _set


def test_run_now_starts_a_sweep(started, in_progress):
    in_progress(False)

    assert ingest_route.ingest_run_now() == {"status": "started"}
    assert len(started) == 1


def test_run_now_refuses_to_stack_a_second_sweep(started, in_progress):
    """409, not a 202 for work that would immediately skip itself."""
    in_progress(True)

    with pytest.raises(AppException) as exc:
        ingest_route.ingest_run_now()

    assert exc.value.status_code == 409
    assert started == []


def test_status_reports_whether_a_sweep_is_running(in_progress):
    in_progress(True)
    assert ingest_route.ingest_status() == {"running": True}
    in_progress(False)
    assert ingest_route.ingest_status() == {"running": False}


# ---------------------------------------------------------------------------
# The lock the endpoint reports on is the one the sweep actually takes
# ---------------------------------------------------------------------------

def test_ingest_in_progress_tracks_the_real_sweep_lock():
    """A stubbed flag would drift from _ingest_lock and let the 409 lie."""
    assert email_store.ingest_in_progress() is False

    assert email_store._ingest_lock.acquire(blocking=False)
    try:
        assert email_store.ingest_in_progress() is True
    finally:
        email_store._ingest_lock.release()

    assert email_store.ingest_in_progress() is False


def test_a_sweep_already_running_makes_ingest_skip_rather_than_duplicate():
    """Why the endpoint bothers to check: the underlying call is non-blocking."""
    assert email_store._ingest_lock.acquire(blocking=False)
    try:
        stats = email_store.ingest_new_emails()
    finally:
        email_store._ingest_lock.release()

    assert stats["skipped_locked"] is True
    assert stats["new"] == 0
