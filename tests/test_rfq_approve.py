"""
What `approve` writes down, versus whether the winning agent was actually told.

Awarding is the point at which the desk commits to a vendor. The acceptance
email is the only thing that tells that vendor they won — there is no portal, no
second channel, and the agent is not expecting a call. So a job marked
`approved` whose acceptance never left is a job that looks finished to everyone
who can see it and unanswered to the one party who has to act on it. Nothing
later contradicts the record: replies attach by RFQ reference, and an approved
job no longer accepts them (`OPEN_JOB_STATUSES`).

This is the same defect `test_rfq_send.py` pins for the send path, one function
later. `send_rfq_email` returns `{"status": "failed"}` rather than raising, so
the ordinary failure — a dead SMTP login — did not even reach the `except`.
"""

import pytest

from backend.domain.models import OPEN_JOB_STATUSES, RfqJob
from backend.services import rfq_service

REFERENCE = "RFQ-20260816-a1b2c3d4"


@pytest.fixture
def job(monkeypatch):
    """Install one open job awaiting a decision. Returns a setter for its status."""
    state = {"status": "quotes_received"}

    def _get(_reference):
        return RfqJob(
            reference=REFERENCE,
            status=state["status"],
            agents_contacted=["Alpha Freight"],
        )

    monkeypatch.setattr(rfq_service.job_repo, "get", _get)
    monkeypatch.setattr(
        rfq_service.agent_repo, "email_for_name", lambda _n: "alpha@example.com"
    )
    return state


@pytest.fixture
def status_writes(monkeypatch):
    """Capture every (reference, status) pair written to the repository."""
    captured = []
    monkeypatch.setattr(
        rfq_service.job_repo, "set_status",
        lambda reference, status: captured.append((reference, status)),
    )
    return captured


@pytest.fixture
def acceptance(monkeypatch):
    """Install a sender returning a given status dict, or raising."""
    def _install(status=None, raises=None):
        def _send(to_addr, subject, body):
            if raises is not None:
                raise raises
            return {"status": status, "to": to_addr}

        monkeypatch.setattr(rfq_service, "send_rfq_email", _send)
    return _install


# ---------------------------------------------------------------------------
# The regression: an acceptance that did not send must not award the job
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("failure", ["failed", "skipped", "unknown", ""])
def test_an_acceptance_that_did_not_send_does_not_award_the_job(
    job, status_writes, acceptance, failure,
):
    """Against the previous code every one of these wrote ("...", "approved")
    and returned status "approved"."""
    acceptance(status=failure)

    with pytest.raises(rfq_service.RfqError):
        rfq_service.approve(REFERENCE)

    assert status_writes == []


def test_the_sender_raising_does_not_award_the_job(job, status_writes, acceptance):
    acceptance(raises=RuntimeError("smtp unreachable"))

    with pytest.raises(rfq_service.RfqError) as excinfo:
        rfq_service.approve(REFERENCE)

    assert status_writes == []
    assert "smtp unreachable" in str(excinfo.value)


def test_a_missing_status_key_is_not_treated_as_success(
    job, status_writes, monkeypatch,
):
    """`.get("status", "unknown")` — a sender returning a shape we do not
    recognise must not be read as a send."""
    monkeypatch.setattr(rfq_service, "send_rfq_email",
                        lambda to_addr, subject, body: {})

    with pytest.raises(rfq_service.RfqError):
        rfq_service.approve(REFERENCE)

    assert status_writes == []


def test_a_sent_acceptance_awards_the_job(job, status_writes, acceptance):
    acceptance(status="sent")
    result = rfq_service.approve(REFERENCE)

    assert status_writes == [(REFERENCE, "approved")]
    assert result["status"] == "approved"
    assert result["acceptance_status"] == "sent"
    assert result["agent_name"] == "Alpha Freight"


# ---------------------------------------------------------------------------
# The failure has to leave the operator somewhere to go
# ---------------------------------------------------------------------------

def test_the_error_names_the_agent_the_reference_and_the_outcome(
    job, status_writes, acceptance,
):
    """This string is rendered to the operator by the 422 handler, and it is the
    only place the real outcome appears."""
    acceptance(status="failed")

    with pytest.raises(rfq_service.RfqError) as excinfo:
        rfq_service.approve(REFERENCE)

    detail = str(excinfo.value)
    assert "Alpha Freight" in detail
    assert REFERENCE in detail
    assert "failed" in detail
    assert "approve again" in detail


def test_the_failure_maps_to_422_not_404(job, status_writes, acceptance):
    """`routes/jobs.py` picks the status code with `404 if "not found" in detail`.
    A message that happened to contain that phrase would tell the operator the
    job does not exist.

    `status_writes` is taken even though nothing is asserted on it: without it a
    regression in the guard would let this test reach the real repository, and
    pytest.ini promises the whole suite runs offline.
    """
    acceptance(status="failed")

    with pytest.raises(rfq_service.RfqError) as excinfo:
        rfq_service.approve(REFERENCE)

    assert "not found" not in str(excinfo.value)


def test_the_job_is_left_open_so_it_can_be_retried_and_still_take_a_reply(
    job, status_writes, acceptance,
):
    """Refusing to award is only safe because the job keeps a status that both
    `approve` and `link_reply` will still act on."""
    acceptance(status="failed")

    with pytest.raises(rfq_service.RfqError):
        rfq_service.approve(REFERENCE)

    assert status_writes == []
    assert job["status"] in OPEN_JOB_STATUSES


def test_approving_again_after_the_sender_is_fixed_awards_the_job(
    job, status_writes, monkeypatch,
):
    """The operator's actual recovery path: fix SMTP, press approve again."""
    outcomes = iter([{"status": "failed"}, {"status": "sent"}])
    monkeypatch.setattr(rfq_service, "send_rfq_email",
                        lambda to_addr, subject, body: next(outcomes))

    with pytest.raises(rfq_service.RfqError):
        rfq_service.approve(REFERENCE)
    assert status_writes == []

    assert rfq_service.approve(REFERENCE)["status"] == "approved"
    assert status_writes == [(REFERENCE, "approved")]


# ---------------------------------------------------------------------------
# Pre-send refusals are unchanged — nothing is sent and nothing is recorded
# ---------------------------------------------------------------------------

def test_an_unknown_reference_is_refused_before_anything_is_sent(
    monkeypatch, status_writes,
):
    monkeypatch.setattr(rfq_service.job_repo, "get", lambda _r: None)

    def _never(**_kwargs):
        raise AssertionError("no acceptance may be sent for a job that does not exist")

    monkeypatch.setattr(rfq_service, "send_rfq_email", _never)

    with pytest.raises(rfq_service.RfqError) as excinfo:
        rfq_service.approve(REFERENCE)

    assert "not found" in str(excinfo.value)   # the one case that is a 404
    assert status_writes == []


def test_an_agent_with_no_single_address_is_refused_before_anything_is_sent(
    job, monkeypatch, status_writes,
):
    """BUGS.md P2-1: multi-office agents share a name. Guessing would email the
    wrong branch that it had won."""
    monkeypatch.setattr(rfq_service.agent_repo, "email_for_name", lambda _n: "")

    def _never(**_kwargs):
        raise AssertionError("no acceptance may be sent to a guessed address")

    monkeypatch.setattr(rfq_service, "send_rfq_email", _never)

    with pytest.raises(rfq_service.RfqError):
        rfq_service.approve(REFERENCE)

    assert status_writes == []
