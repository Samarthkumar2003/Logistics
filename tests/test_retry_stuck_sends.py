"""
The recovery sweep for RFQ jobs abandoned at `sending`.

The one thing this must never do is resend a mail that already went out — a
vendor cannot un-receive a duplicate RFQ. So every test here is really about the
same question from a different angle: given what the Sent folder says (yes / no /
can't tell), does the sweep send, and does it ever send when it shouldn't.
"""

from types import SimpleNamespace

import pytest

from backend.domain.models import STATUS_SENDING, RfqJob
from backend.services import retry_service


@pytest.fixture
def jobs_table(monkeypatch):
    """A stand-in rfq_jobs that honours the status guard, like the real one.

    `rows` is the table; `list_stale_sending` returns whatever is currently at
    `sending`. `set_status_if` advances a row only while it still holds the
    expected status, so a test can prove the sweep never clobbers a reply that
    moved a job on mid-sweep.
    """
    rows: list[RfqJob] = []

    def _list_stale(_before):
        return [j for j in rows if j.status == STATUS_SENDING]

    def _set_status_if(reference, status, expected):
        for j in rows:
            if j.reference == reference and j.status == expected:
                j.status = status
                return True
        return False

    monkeypatch.setattr(retry_service.job_repo, "list_stale_sending", _list_stale)
    monkeypatch.setattr(retry_service.job_repo, "set_status_if", _set_status_if)
    return rows


@pytest.fixture
def oracle(monkeypatch):
    """Install the Sent-folder answer: True / False / None."""
    def _install(answer):
        monkeypatch.setattr(retry_service, "was_sent",
                            lambda *a, **k: answer)
    return _install


@pytest.fixture
def sender(monkeypatch):
    """Capture every resend, and control whether it succeeds."""
    sent = []

    def _install(status="sent"):
        def _send(to_addr, subject, body):
            sent.append({"to": to_addr, "subject": subject, "body": body})
            return {"status": status, "to": to_addr}
        monkeypatch.setattr(retry_service, "send_rfq_email", _send)
    _install.sent = sent
    return _install


def _stuck(reference="RFQ-20260818-991942fa", **kw):
    defaults = dict(
        reference=reference, status=STATUS_SENDING,
        agents_contacted=["Alpha"],
        created_at="2026-08-18T10:00:00+00:00",
        draft_subject="RFQId:20260818-991942fa | RFQ",
        draft_body="Please quote.",
        draft_to="alpha@example.com",
    )
    defaults.update(kw)
    return RfqJob(**defaults)


# ---------------------------------------------------------------------------
# The three oracle answers
# ---------------------------------------------------------------------------

def test_found_in_sent_reconciles_without_sending(jobs_table, oracle, sender):
    """The mail left; only our record died. Fix the status, send nothing."""
    jobs_table.append(_stuck())
    oracle(True)
    sender()

    result = retry_service.sweep_stuck_sends()

    assert jobs_table[0].status == "rfqs_sent"
    assert sender.sent == []
    assert result["reconciled"] == 1 and result["resent"] == 0


def test_absent_from_sent_resends_the_stored_draft(jobs_table, oracle, sender):
    """Proven never sent — resend the SAME mail, verbatim, then mark it sent."""
    jobs_table.append(_stuck())
    oracle(False)
    sender("sent")

    result = retry_service.sweep_stuck_sends()

    assert sender.sent == [{
        "to": "alpha@example.com",
        "subject": "RFQId:20260818-991942fa | RFQ",
        "body": "Please quote.",
    }]
    assert jobs_table[0].status == "rfqs_sent"
    assert result["resent"] == 1


def test_no_evidence_never_resends(jobs_table, oracle, sender):
    """A lookup that could not answer must not authorise a duplicate. Flag it."""
    jobs_table.append(_stuck())
    oracle(None)
    sender()

    result = retry_service.sweep_stuck_sends()

    assert sender.sent == []
    assert jobs_table[0].status == "send_failed"
    assert result["flagged"] == 1


# ---------------------------------------------------------------------------
# Guards around the resend
# ---------------------------------------------------------------------------

def test_auto_retry_off_flags_instead_of_sending(jobs_table, oracle, sender, monkeypatch):
    """With auto-retry disabled, even a proven-unsent row is only flagged — the
    operator resends via the endpoint."""
    # Settings is a frozen dataclass, so swap the module's reference wholesale.
    monkeypatch.setattr(retry_service, "settings",
                        SimpleNamespace(auto_retry_stuck_sends=False,
                                        stale_sending_minutes=15))
    jobs_table.append(_stuck())
    oracle(False)
    sender()

    retry_service.sweep_stuck_sends()

    assert sender.sent == []
    assert jobs_table[0].status == "send_failed"


def test_a_row_without_a_stored_draft_is_flagged_not_guessed(jobs_table, oracle, sender):
    """Nothing safe to send — resending would mean inventing a mail. Flag it."""
    jobs_table.append(_stuck(draft_body="", draft_subject="", draft_to=""))
    oracle(False)
    sender()

    retry_service.sweep_stuck_sends()

    assert sender.sent == []
    assert jobs_table[0].status == "send_failed"


def test_a_row_without_a_timestamp_is_flagged_not_searched(jobs_table, sender, monkeypatch):
    """No created_at means no bounded Sent window to search — refuse to guess,
    and do not consult the oracle at all."""
    jobs_table.append(_stuck(created_at=None))
    sender()

    def _must_not_run(*a, **k):
        raise AssertionError("oracle must not be consulted without a timestamp")

    monkeypatch.setattr(retry_service, "was_sent", _must_not_run)
    retry_service.sweep_stuck_sends()

    assert sender.sent == []
    assert jobs_table[0].status == "send_failed"


def test_a_failed_resend_is_recorded_send_failed(jobs_table, oracle, sender):
    """The resend itself can fail; that is a genuine send_failed, and the row must
    leave `sending` so the sweep does not loop on it forever."""
    jobs_table.append(_stuck())
    oracle(False)
    sender("failed")

    result = retry_service.sweep_stuck_sends()

    assert jobs_table[0].status == "send_failed"
    assert result["flagged"] == 1


# ---------------------------------------------------------------------------
# Race, batching, and the empty case
# ---------------------------------------------------------------------------

def test_a_reply_that_moved_the_job_mid_sweep_is_not_overwritten(jobs_table, oracle, sender, monkeypatch):
    """An auto-responder can advance the job to quotes_received before the sweep
    records its outcome. The guarded update must leave that alone."""
    job = _stuck()
    jobs_table.append(job)
    oracle(True)  # would otherwise set rfqs_sent

    # The reply lands the instant before the sweep's guarded update.
    def _set_status_if(reference, status, expected):
        job.status = "quotes_received"   # someone else got there first
        return False

    monkeypatch.setattr(retry_service.job_repo, "set_status_if", _set_status_if)
    retry_service.sweep_stuck_sends()

    assert job.status == "quotes_received"


def test_nothing_stuck_is_a_clean_no_op(jobs_table, oracle, sender):
    oracle(True)
    result = retry_service.sweep_stuck_sends()
    assert result == {"status": "ok", "scanned": 0, "reconciled": 0,
                      "resent": 0, "flagged": 0, "deferred": 0}


def test_the_sweep_is_capped_and_defers_the_rest(jobs_table, oracle, sender, monkeypatch):
    """More stuck rows than the cap: handle MAX_PER_SWEEP, report the remainder as
    deferred rather than silently dropping them."""
    monkeypatch.setattr(retry_service, "MAX_PER_SWEEP", 2)
    for i in range(5):
        jobs_table.append(_stuck(reference=f"RFQ-20260818-0000000{i}"))
    oracle(True)

    result = retry_service.sweep_stuck_sends()

    assert result["scanned"] == 2
    assert result["deferred"] == 3
