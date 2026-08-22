"""
Counting replies: agents versus messages.

The dashboard used to show one number, `reply_count`, in the slot that answers
"who has come back". It counted messages. An agent who sent a rate and then a
correction made the card read `✓ replied (2)` on an RFQ that went to a single
agent, which reads as two carriers competing when there is one quote on the
table — and a freight desk decides whether to wait for more quotes off exactly
that number.

So there are two counts now, and these tests pin the difference: messages count
every linked email, agents count distinct mailboxes.
"""

import pytest

from backend.domain.models import Email
from backend.repositories import email_repo
from backend.services import reply_service

REF = "RFQ-20260822-32e091f1"
OTHER_REF = "RFQ-20260822-aaaa1111"


class _Table:
    """Minimal postgrest stand-in: records the filter, returns canned rows."""

    def __init__(self, rows: list[dict]):
        self._all = rows
        self._wanted: list[str] = []

    def select(self, columns: str):
        self.columns = columns
        return self

    def in_(self, column: str, values: list[str]):
        assert column == "rfq_reference"
        self._wanted = values
        return self

    def execute(self):
        rows = [r for r in self._all if r.get("rfq_reference") in self._wanted]
        return type("R", (), {"data": rows})()


@pytest.fixture
def rows(monkeypatch):
    """Whatever the `emails` table holds for this test."""
    state: list[dict] = []
    monkeypatch.setattr(email_repo, "get_db",
                        lambda: type("DB", (), {"table": lambda _s, _n: _Table(state)})())
    return state


# ---------------------------------------------------------------------------
# sender_address: identity for "who replied"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("Dhaval Shah <ops@carrier.example>", "ops@carrier.example"),
    ("ops@carrier.example", "ops@carrier.example"),
    ("OPS@Carrier.Example", "ops@carrier.example"),
    ('"Shah, Dhaval" <ops@carrier.example>', "ops@carrier.example"),
    ("  ops@carrier.example  ", "ops@carrier.example"),
    ("", ""),
])
def test_sender_address_reduces_a_from_header_to_one_identity(raw, expected):
    assert email_repo.sender_address(raw) == expected


def test_a_changed_display_name_is_still_one_agent(rows):
    """The same mailbox writing twice under different display names — a signature
    edit, a phone client — is one agent, not two."""
    rows += [
        {"rfq_reference": REF, "sender": "Dhaval <ops@carrier.example>"},
        {"rfq_reference": REF, "sender": "Dhaval Shah (Mobile) <ops@carrier.example>"},
    ]

    stats = email_repo.reply_stats_by_reference([REF])[REF]

    assert stats.messages == 2
    assert stats.agents == 1


# ---------------------------------------------------------------------------
# reply_stats_by_reference
# ---------------------------------------------------------------------------

def test_two_replies_from_one_agent_count_as_one_agent(rows):
    """The reported case: "Test1234" then "Test - 2" on the same thread."""
    rows += [
        {"rfq_reference": REF, "sender": "Samarth Bhutani <bhutani.samarth@gmail.com>"},
        {"rfq_reference": REF, "sender": "Samarth Bhutani <bhutani.samarth@gmail.com>"},
    ]

    stats = email_repo.reply_stats_by_reference([REF])[REF]

    assert (stats.messages, stats.agents) == (2, 1)


def test_two_different_agents_count_as_two(rows):
    rows += [
        {"rfq_reference": REF, "sender": "a@one.example"},
        {"rfq_reference": REF, "sender": "b@two.example"},
    ]

    assert email_repo.reply_stats_by_reference([REF])[REF].agents == 2


def test_counts_do_not_leak_between_references(rows):
    rows += [
        {"rfq_reference": REF, "sender": "a@one.example"},
        {"rfq_reference": OTHER_REF, "sender": "b@two.example"},
    ]

    stats = email_repo.reply_stats_by_reference([REF, OTHER_REF])

    assert stats[REF].agents == 1
    assert stats[OTHER_REF].agents == 1


def test_a_reference_with_no_reply_is_absent(rows):
    """Callers default to zero rather than the repo inventing empty rows."""
    assert email_repo.reply_stats_by_reference([REF]) == {}
    assert email_repo.reply_stats_by_reference([]) == {}


def test_a_reply_with_no_parseable_sender_is_a_message_but_not_an_agent(rows):
    """It arrived, so it is a message. There is no identity to count, so counting
    it as an agent would claim a respondent nobody can chase."""
    rows += [
        {"rfq_reference": REF, "sender": ""},
        {"rfq_reference": REF, "sender": "a@one.example"},
    ]

    stats = email_repo.reply_stats_by_reference([REF])[REF]

    assert (stats.messages, stats.agents) == (2, 1)


def test_references_are_queried_in_chunks(rows, monkeypatch):
    """PostgREST rejects a long IN list, and the dashboard asks for every job on
    screen at once."""
    seen: list[int] = []

    class _Spy(_Table):
        def in_(self, column, values):
            seen.append(len(values))
            return super().in_(column, values)

    monkeypatch.setattr(email_repo, "get_db",
                        lambda: type("DB", (), {"table": lambda _s, _n: _Spy([])})())

    email_repo.reply_stats_by_reference([f"RFQ-2026-{i:04d}" for i in range(250)])

    assert seen == [100, 100, 50]


def test_a_failed_lookup_drops_that_chunk_rather_than_the_page(monkeypatch):
    """The dashboard renders without counts; it does not 500 because one count
    query failed."""
    class _Boom(_Table):
        def execute(self):
            raise RuntimeError("statement timeout")

    monkeypatch.setattr(email_repo, "get_db",
                        lambda: type("DB", (), {"table": lambda _s, _n: _Boom([])})())

    assert email_repo.reply_stats_by_reference([REF]) == {}


# ---------------------------------------------------------------------------
# The customer request panel
# ---------------------------------------------------------------------------

def test_customer_request_counts_agents_and_messages_separately(monkeypatch):
    """Two messages from one agent on an enquiry sent to two agents: one replied."""
    from backend.domain.models import RfqJob

    def _job(ref: str, agent: str) -> RfqJob:
        return RfqJob(reference=ref, status="quotes_received", agents_contacted=[agent],
                      customer_email_id="cust-1")

    def _reply(ref: str, sender: str, when: str) -> Email:
        return Email(id=f"m-{when}", sender=sender, subject="Re: quote", body="rate",
                     received_at=when, thread_id="t1", rfq_reference=ref)

    monkeypatch.setattr(reply_service.email_repo, "get_by_id", lambda _id: None)
    monkeypatch.setattr(reply_service.job_repo, "list_for_customer_email",
                        lambda _id: [_job(REF, "Carrier One"), _job(OTHER_REF, "Carrier Two")])
    monkeypatch.setattr(reply_service.email_repo, "list_replies_for", lambda _refs: [
        _reply(REF, "Ops <ops@one.example>", "2026-08-22T10:18:02+00:00"),
        _reply(REF, "Ops <OPS@one.example>", "2026-08-22T09:57:14+00:00"),
    ])

    counts = reply_service.get_customer_request("cust-1")["counts"]

    assert counts == {"agents": 2, "replies": 2, "agents_replied": 1}
