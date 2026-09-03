"""
What the RFQ card's reply panel shows.

An agent replies more than once. The second message is a correction — a surcharge
they forgot, a validity date, "ignore the previous rate" — and it is the one the
desk has to read. Two things used to hide it:

  * the query returned oldest-first, so the newest message sat at the bottom
    under the version it supersedes;
  * only messages carrying the RFQ reference were returned at all, and a
    follow-up routinely loses the token (retyped "Re:" chains, clients that
    rewrite the subject), so it was absent rather than late.

The panel therefore shows the whole Gmail thread of every linked reply, newest
first. Attribution does not change: nothing here writes `rfq_reference`, and a
message that did not cite the reference is flagged `linked: false` rather than
presented as attributed.
"""

import pytest

from backend.domain.models import Email
from backend.repositories import email_repo
from backend.services import reply_service

REF = "RFQ-20260819-7d704ef6"
OTHER_REF = "RFQ-20260819-aaaa1111"


def _email(mid: str, when: str, *, ref=REF, thread="t1", subject="", body="") -> Email:
    return Email(
        id=mid,
        sender="agent@carrier.example",
        subject=subject or "Re: Quotation RFQId:20260819-7d704ef6",
        body=body or "Our rate is USD 1200 per 20ft.",
        received_at=when,
        thread_id=thread,
        classification="quotation_rate_card",
        rfq_reference=ref,
    )


@pytest.fixture
def repo(monkeypatch):
    """A stand-in for the two repo reads, so ordering and filtering are the
    behaviour under test rather than postgrest's."""
    state: dict[str, list[Email]] = {"linked": [], "thread": []}

    def linked_for(refs):
        rows = [e for e in state["linked"] if e.rfq_reference in refs]
        return sorted(rows, key=lambda e: e.received_at or "", reverse=True)

    def thread_msgs(thread_ids, limit=50):
        wanted = {t for t in thread_ids if t}
        rows = [e for e in state["thread"] if e.thread_id in wanted]
        return sorted(rows, key=lambda e: e.received_at or "", reverse=True)[:limit]

    monkeypatch.setattr(email_repo, "list_replies_for", linked_for)
    monkeypatch.setattr(email_repo, "list_thread_messages", thread_msgs)
    return state


# ---------------------------------------------------------------------------
# Order: the newest message is the one being read
# ---------------------------------------------------------------------------

def test_the_latest_reply_comes_first(repo):
    first = _email("m1", "2026-08-19T15:27:00+00:00", body="Test1234")
    second = _email("m2", "2026-08-19T15:48:00+00:00", body="Test - 2")
    repo["linked"] = [first, second]
    repo["thread"] = [first, second]

    panel = reply_service.list_for_reference(REF)

    assert [r["id"] for r in panel] == ["m2", "m1"]
    assert panel[0]["body"] == "Test - 2"


def test_a_message_with_no_timestamp_does_not_take_the_top_slot(repo):
    dated = _email("m1", "2026-08-19T15:48:00+00:00")
    undated = _email("m2", None)
    repo["linked"] = [dated, undated]
    repo["thread"] = [dated, undated]

    assert [r["id"] for r in reply_service.list_for_reference(REF)] == ["m1", "m2"]


# ---------------------------------------------------------------------------
# The thread: a follow-up that dropped the reference is still shown
# ---------------------------------------------------------------------------

def test_a_follow_up_that_lost_the_reference_still_appears(repo):
    linked = _email("m1", "2026-08-19T15:27:00+00:00")
    follow_up = _email("m2", "2026-08-19T15:48:00+00:00", ref=None,
                       subject="Re: rates", body="Correction: add USD 90 THC")
    repo["linked"] = [linked]
    repo["thread"] = [linked, follow_up]

    panel = reply_service.list_for_reference(REF)

    assert [r["id"] for r in panel] == ["m2", "m1"]
    assert panel[0]["linked"] is False, "shown for context, not claimed as attributed"
    assert panel[1]["linked"] is True


def test_a_thread_sibling_belonging_to_another_rfq_is_left_out(repo):
    """Same thread is context, never a claim. A message already attributed to a
    different reference belongs on that RFQ's panel."""
    linked = _email("m1", "2026-08-19T15:27:00+00:00")
    theirs = _email("m2", "2026-08-19T15:48:00+00:00", ref=OTHER_REF)
    repo["linked"] = [linked]
    repo["thread"] = [linked, theirs]

    assert [r["id"] for r in reply_service.list_for_reference(REF)] == ["m1"]


def test_no_linked_reply_means_an_empty_panel(repo):
    """Thread widening hangs off a linked reply. With nothing linked there is no
    thread to widen from, and guessing one from the subject is exactly what this
    system refuses to do."""
    repo["linked"] = []
    repo["thread"] = [_email("m9", "2026-08-19T15:48:00+00:00", ref=None)]

    assert reply_service.list_for_reference(REF) == []


def test_the_same_message_is_never_listed_twice(repo):
    """The thread query returns linked messages too — they are in the thread."""
    linked = _email("m1", "2026-08-19T15:27:00+00:00")
    repo["linked"] = [linked]
    repo["thread"] = [linked]

    panel = reply_service.list_for_reference(REF)

    assert len(panel) == 1
    assert panel[0]["id"] == "m1"


# ---------------------------------------------------------------------------
# Shape the dashboard reads
# ---------------------------------------------------------------------------

def test_each_row_carries_its_thread_and_link_state(repo):
    linked = _email("m1", "2026-08-19T15:27:00+00:00", thread="thread-abc")
    repo["linked"] = [linked]
    repo["thread"] = [linked]

    row = reply_service.list_for_reference(REF)[0]

    assert row["thread_id"] == "thread-abc"
    assert row["linked"] is True
    assert row["rfq_reference"] == REF
    for key in ("id", "sender", "subject", "body", "received_at", "has_attachments"):
        assert key in row
