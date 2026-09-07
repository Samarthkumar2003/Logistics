"""
`mark_quotes_received` must not un-award a shipment.

The race it guards: reply_service.link_reply reads the job, then calls this with
the status it read. If an operator approves in that window, the value in hand is
stale — and the old implementation checked the stale value in Python and then
issued an UNCONDITIONAL update, putting an approved job back to
`quotes_received`. Attribution is by RFQ reference alone, so nothing later
contradicts it: the desk had committed to an agent and the record quietly said
it had not.

The fix is that the guard belongs in the UPDATE's own WHERE clause.
`set_status_if` already existed for this and was simply not being used.
"""

import pytest

from backend.domain.models import OPEN_JOB_STATUSES
from backend.repositories import job_repo


@pytest.fixture
def db(monkeypatch):
    """A stand-in rfq_jobs row that honours a conditional update like Postgres."""
    state = {"status": "rfqs_sent", "unconditional_writes": 0, "calls": []}

    def _set_status_if(reference, status, expected):
        state["calls"].append((reference, status, expected))
        if state["status"] != expected:
            return False
        state["status"] = status
        return True

    def _set_status(reference, status):
        state["unconditional_writes"] += 1
        state["status"] = status

    monkeypatch.setattr(job_repo, "set_status_if", _set_status_if)
    monkeypatch.setattr(job_repo, "set_status", _set_status)
    return state


def test_an_open_job_advances_when_a_reply_lands(db):
    assert job_repo.mark_quotes_received("RFQ-1", "rfqs_sent") is True
    assert db["status"] == "quotes_received"


def test_an_approval_that_won_the_race_is_not_overwritten(db):
    """The regression. The caller holds `rfqs_sent`; the row already says
    `approved`. The update must find nothing to change."""
    db["status"] = "approved"

    moved = job_repo.mark_quotes_received("RFQ-1", "rfqs_sent")

    assert moved is False
    assert db["status"] == "approved", "an awarded shipment was un-awarded"


def test_the_update_is_conditional_never_blind(db):
    """Not a style assertion: an unconditional write is the whole defect."""
    job_repo.mark_quotes_received("RFQ-1", "rfqs_sent")

    assert db["unconditional_writes"] == 0
    assert db["calls"] == [("RFQ-1", "quotes_received", "rfqs_sent")]


@pytest.mark.parametrize("closed", ["approved", "rejected", "quotes_received"])
def test_a_status_outside_the_open_set_is_refused_without_touching_the_row(db, closed):
    if closed in OPEN_JOB_STATUSES:
        pytest.skip(f"{closed} is an open status in this configuration")

    assert job_repo.mark_quotes_received("RFQ-1", closed) is False
    assert db["calls"] == []
    assert db["unconditional_writes"] == 0


def test_a_write_failure_reports_false_rather_than_raising(db, monkeypatch):
    """A reply that is attached but whose status lagged is recoverable. A 500 in
    the scan loop is not."""
    def _boom(reference, status, expected):
        raise RuntimeError("postgrest unreachable")

    monkeypatch.setattr(job_repo, "set_status_if", _boom)

    assert job_repo.mark_quotes_received("RFQ-1", "rfqs_sent") is False
