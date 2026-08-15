"""
What `send_rfqs` writes down, versus what actually happened.

This is the only path by which mail reaches a vendor, and the job row it writes
is the operator's whole record of it. A row saying `rfqs_sent` for mail that
never left is the worst failure the desk can have: attribution is by RFQ
reference alone, so no reply will ever arrive to contradict it, and the job sits
looking like an agent who did not bother to answer.
"""

import pytest

from backend.domain.models import OPEN_JOB_STATUSES
from backend.services import rfq_service
from backend.services.rfq_service import SelectedAgent

SHIPMENT = {"origin": "Nhava Sheva", "destination": "Hamburg", "mode": "sea"}
CUSTOMER = {"email_id": "", "sender": "shipper@example.com", "subject": "Rates?"}


@pytest.fixture
def sent_jobs(monkeypatch):
    """Capture every RfqJob handed to the repository."""
    captured = []
    monkeypatch.setattr(rfq_service.job_repo, "insert", captured.append)
    monkeypatch.setattr(rfq_service.agent_repo, "ensure_agents", lambda _a: None)
    monkeypatch.setattr(rfq_service.email_repo, "get_thread_id", lambda _e: "")
    return captured


@pytest.fixture
def drafts_succeed(monkeypatch):
    """Drafting always works, and echoes the agent name back as vendor_name."""
    class _Draft:
        def __init__(self, name):
            self.vendor_name = name
            self.vendor_email = f"{name}@example.com".replace(" ", "")
            self.subject = "RFQ"
            self.body = "body"

    class _Result:
        def __init__(self, names):
            self.drafts = [_Draft(n) for n in names]

    monkeypatch.setattr(
        rfq_service, "generate_rfq_drafts",
        lambda shipment_data, agents, reference: _Result([a["agent_name"] for a in agents]),
    )


@pytest.fixture
def sender(monkeypatch):
    """Install a batch sender returning the given per-draft statuses, in order."""
    def _install(statuses):
        def _batch(drafts):
            return [{"vendor_name": d["vendor_name"], "status": s}
                    for d, s in zip(drafts, statuses)]
        monkeypatch.setattr(rfq_service, "send_rfq_emails_batch", _batch)
    return _install


def _agents(*names):
    return [SelectedAgent(n, f"{n}@example.com".replace(" ", "")) for n in names]


def _send(agents):
    return rfq_service.send_rfqs(SHIPMENT, agents, CUSTOMER)


# ---------------------------------------------------------------------------
# The regression: a failed send must not be recorded as a sent RFQ
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("failure", ["failed", "skipped", "batch_error: auth denied"])
def test_a_send_that_did_not_succeed_is_not_recorded_as_sent(
    drafts_succeed, sender, sent_jobs, failure,
):
    sender([failure])
    result = _send(_agents("Alpha"))

    assert [j.status for j in sent_jobs] == ["send_failed"]
    assert result["total_sent"] == 0
    assert result["jobs"][0]["status"] == failure


def test_a_successful_send_is_recorded_as_sent(drafts_succeed, sender, sent_jobs):
    sender(["sent"])
    result = _send(_agents("Alpha"))

    assert [j.status for j in sent_jobs] == ["rfqs_sent"]
    assert result["total_sent"] == 1


def test_a_mixed_batch_records_each_agent_on_its_own_outcome(
    drafts_succeed, sender, sent_jobs,
):
    """The case a name-keyed lookup got wrong: one agent's outcome must never be
    applied to another's job."""
    sender(["sent", "failed", "sent"])
    result = _send(_agents("Alpha", "Beta", "Gamma"))

    by_agent = {j.agents_contacted[0]: j.status for j in sent_jobs}
    assert by_agent == {"Alpha": "rfqs_sent", "Beta": "send_failed", "Gamma": "rfqs_sent"}
    assert result["total_sent"] == 2


def test_total_sent_counts_confirmed_sends_not_drafts(drafts_succeed, sender, sent_jobs):
    """The UI renders this as "N RFQs sent"."""
    sender(["failed", "failed", "sent"])
    assert _send(_agents("Alpha", "Beta", "Gamma"))["total_sent"] == 1


# ---------------------------------------------------------------------------
# Correlation must not rely on the model-supplied vendor_name
# ---------------------------------------------------------------------------

def test_outcomes_correlate_by_position_when_the_model_renames_the_vendor(
    monkeypatch, sender, sent_jobs,
):
    """`DraftEmail.vendor_name` comes from the LLM, so it need not equal the
    agent we picked. Correlating on it silently yielded "unknown"."""
    class _Draft:
        def __init__(self, name):
            self.vendor_name = f"{name} Logistics Pvt Ltd"   # model embellished it
            self.vendor_email = "x@example.com"
            self.subject = "RFQ"
            self.body = "body"

    class _Result:
        def __init__(self, names):
            self.drafts = [_Draft(n) for n in names]

    monkeypatch.setattr(
        rfq_service, "generate_rfq_drafts",
        lambda shipment_data, agents, reference: _Result([a["agent_name"] for a in agents]),
    )
    sender(["sent", "failed"])
    _send(_agents("Alpha", "Beta"))

    by_agent = {j.agents_contacted[0]: j.status for j in sent_jobs}
    assert by_agent == {"Alpha": "rfqs_sent", "Beta": "send_failed"}


def test_two_offices_sharing_one_agent_name_get_separate_outcomes(
    drafts_succeed, sender, sent_jobs,
):
    """Multi-office agents share a name (BUGS.md P2-1). A name-keyed dict kept
    only the last outcome and applied it to both."""
    sender(["sent", "failed"])
    agents = [SelectedAgent("Emu Lines", "mumbai@emu.example"),
              SelectedAgent("Emu Lines", "chennai@emu.example")]
    _send(agents)

    assert sorted(j.status for j in sent_jobs) == ["rfqs_sent", "send_failed"]


def test_an_uncorrelatable_result_count_is_not_treated_as_success(
    drafts_succeed, monkeypatch, sent_jobs,
):
    """If the sender returns the wrong number of results, positions mean nothing
    and nothing may be claimed as sent."""
    monkeypatch.setattr(rfq_service, "send_rfq_emails_batch",
                        lambda drafts: [{"status": "sent"}])
    result = _send(_agents("Alpha", "Beta"))

    assert [j.status for j in sent_jobs] == ["send_failed", "send_failed"]
    assert result["total_sent"] == 0


def test_the_batch_call_raising_records_no_sends(drafts_succeed, monkeypatch, sent_jobs):
    def _boom(_drafts):
        raise RuntimeError("smtp unreachable")

    monkeypatch.setattr(rfq_service, "send_rfq_emails_batch", _boom)
    result = _send(_agents("Alpha", "Beta"))

    assert [j.status for j in sent_jobs] == ["send_failed", "send_failed"]
    assert result["total_sent"] == 0
    assert "smtp unreachable" in result["jobs"][0]["status"]


# ---------------------------------------------------------------------------
# Rows still get written on failure, and drafting failures write none
# ---------------------------------------------------------------------------

def test_a_failed_send_still_writes_a_row_so_an_ambiguous_delivery_stays_linkable(
    drafts_succeed, sender, sent_jobs,
):
    """A timeout may have delivered. Dropping the row would strand the reply."""
    sender(["failed"])
    result = _send(_agents("Alpha"))

    assert len(sent_jobs) == 1
    assert sent_jobs[0].reference == result["jobs"][0]["reference"]


def test_send_failed_can_still_receive_a_reply():
    """`link_reply` advances a job only when its status is open, so a reply
    arriving after an ambiguous failure must not be locked out."""
    assert "send_failed" in OPEN_JOB_STATUSES


def test_a_drafting_failure_writes_no_row(monkeypatch, sender, sent_jobs):
    """Nothing was addressed and no reference reached anyone, so there is
    nothing for a reply to attach to."""
    def _boom(shipment_data, agents, reference):
        raise RuntimeError("model refused")

    monkeypatch.setattr(rfq_service, "generate_rfq_drafts", _boom)
    sender([])
    result = _send(_agents("Alpha"))

    assert sent_jobs == []
    assert result["total_sent"] == 0
    assert "draft failed" in result["jobs"][0]["status"]


def test_a_persist_failure_is_surfaced_and_not_reported_as_a_clean_send(
    drafts_succeed, sender, monkeypatch,
):
    monkeypatch.setattr(rfq_service.agent_repo, "ensure_agents", lambda _a: None)
    monkeypatch.setattr(rfq_service.email_repo, "get_thread_id", lambda _e: "")

    def _boom(_job):
        raise RuntimeError("supabase down")

    monkeypatch.setattr(rfq_service.job_repo, "insert", _boom)
    sender(["sent"])
    result = _send(_agents("Alpha"))

    assert "job persist failed" in result["jobs"][0]["status"]
