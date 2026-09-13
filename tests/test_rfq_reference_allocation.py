"""
Allocation of sequential RFQ reference numbers.

Two things are being protected here.

First, that the batch is allocated ONCE per send. `_draft_for_agent` runs in a
ThreadPoolExecutor, so minting inside each worker would have every send race
itself N ways. The sequence would still hand out distinct numbers, but the send
would touch the database once per agent for no reason.

Second, that an unavailable sequence degrades instead of failing. The sequence
arrives in a migration somebody runs by hand, so there is a real window in which
this code is deployed and `next_rfq_references` does not exist. In that window
every send must keep working.
"""

import pytest

from backend.core.rfq_reference import (
    MAX_REFERENCE_NUMBER,
    RESERVED_PREVIEW_REFERENCE,
    extract_rfq_reference,
    subject_token,
)
from backend.repositories import reference_repo
from backend.services import rfq_service


@pytest.fixture(autouse=True)
def _reset_warning_latch():
    """`_warn_once` latches per process. Reset it so each test sees a clean one."""
    reference_repo._warned = False
    yield
    reference_repo._warned = False


class _FakeRpc:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return type("Response", (), {"data": self._data})()


class _FakeDb:
    """Records every rpc call so a test can assert the batching."""

    def __init__(self, data):
        self._data = data
        self.calls: list[tuple[str, dict]] = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        return _FakeRpc(self._data)


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------

def test_a_sequence_number_becomes_a_six_digit_reference(monkeypatch):
    monkeypatch.setattr(reference_repo, "allocate", lambda n: [1042])
    assert rfq_service.allocate_references(1) == ["RFQ-001042"]


def test_every_allocated_reference_reads_back(monkeypatch):
    """The generator and the matcher must not drift apart. A reference we can
    mint but cannot extract is an RFQ whose reply can never be attributed."""
    numbers = [1, 9, 10, 999, 1000, 54321, MAX_REFERENCE_NUMBER]
    monkeypatch.setattr(reference_repo, "allocate", lambda n: numbers)
    for reference in rfq_service.allocate_references(len(numbers)):
        assert extract_rfq_reference(subject_token(reference)) == reference


def test_references_are_allocated_for_the_whole_batch_in_one_call(monkeypatch):
    db = _FakeDb([1000, 1001, 1002, 1003, 1004, 1005, 1006])
    monkeypatch.setattr(reference_repo, "get_db", lambda: db)

    references = rfq_service.allocate_references(7)

    assert references == [f"RFQ-00{n}" for n in range(1000, 1007)]
    assert len(db.calls) == 1, "one round trip per send, not one per agent"
    assert db.calls[0] == ("next_rfq_references", {"n": 7})


# ---------------------------------------------------------------------------
# degradation
# ---------------------------------------------------------------------------

def test_an_unavailable_sequence_falls_back_to_the_random_form(monkeypatch):
    monkeypatch.setattr(reference_repo, "allocate", lambda n: None)
    references = rfq_service.allocate_references(3)
    assert len(references) == 3
    assert len(set(references)) == 3
    for reference in references:
        # Still attributable, just not readable aloud.
        assert extract_rfq_reference(reference) == reference


def test_the_fallback_is_all_or_nothing(monkeypatch):
    """Never half sequential and half random. A batch mixing the two is harder to
    reason about than either, and the fallback is meant to be recognisable when
    an operator reads it off a screen."""
    monkeypatch.setattr(reference_repo, "allocate", lambda n: None)
    references = rfq_service.allocate_references(4)
    assert all(r.startswith("RFQ-2") for r in references), references


@pytest.mark.parametrize("number", [0, -1, MAX_REFERENCE_NUMBER + 1, 10_000_000])
def test_a_number_outside_the_format_is_refused(monkeypatch, number):
    """Past six digits the reference stops matching, so emitting it would mail a
    vendor something whose reply can never be filed. Falling back to the random
    form keeps attribution working while somebody widens the format."""
    monkeypatch.setattr(reference_repo, "allocate", lambda n: [number])
    reference = rfq_service.allocate_references(1)[0]
    assert reference != f"RFQ-{number:06d}"
    assert extract_rfq_reference(reference) == reference


def test_a_raising_client_is_reported_as_unavailable(monkeypatch):
    def _boom():
        raise RuntimeError("function next_rfq_references does not exist")

    monkeypatch.setattr(reference_repo, "get_db", _boom)
    assert reference_repo.allocate(3) is None


def test_a_short_answer_is_refused(monkeypatch):
    """Three numbers for four agents would either send an RFQ with no reference
    or reuse one. Neither is better than falling back."""
    monkeypatch.setattr(reference_repo, "get_db", lambda: _FakeDb([1000, 1001, 1002]))
    assert reference_repo.allocate(4) is None


# ---------------------------------------------------------------------------
# response shapes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("data,expected", [
    ([1000, 1001], [1000, 1001]),                                       # bigint[]
    ([{"next_rfq_references": 1000}, {"next_rfq_references": 1001}],     # setof bigint
     [1000, 1001]),
])
def test_both_postgrest_shapes_are_understood(monkeypatch, data, expected):
    monkeypatch.setattr(reference_repo, "get_db", lambda: _FakeDb(data))
    assert reference_repo.allocate(2) == expected


@pytest.mark.parametrize("data", [
    None,
    "1000",
    [None, None],
    ["1000", "1001"],
    [True, False],                       # bool is an int subclass; not a number here
    [{"a": 1, "b": 2}],
    [[1000]],
])
def test_an_unrecognised_shape_is_unavailable_rather_than_guessed(monkeypatch, data):
    """A mis-parsed allocation would mint a reference nobody can read back, which
    is the one failure this module exists to avoid."""
    monkeypatch.setattr(reference_repo, "get_db", lambda: _FakeDb(data))
    assert reference_repo.allocate(2) is None


def test_asking_for_nothing_costs_no_round_trip(monkeypatch):
    db = _FakeDb([])
    monkeypatch.setattr(reference_repo, "get_db", lambda: db)
    assert reference_repo.allocate(0) == []
    assert db.calls == []


# ---------------------------------------------------------------------------
# the preview must not consume a number
# ---------------------------------------------------------------------------

def test_the_draft_preview_does_not_burn_a_sequence_number(monkeypatch):
    """An operator opening the draft editor five times would otherwise leave five
    permanent gaps in the numbering for mail nobody sent."""
    calls: list[int] = []
    monkeypatch.setattr(reference_repo, "allocate", lambda n: calls.append(n) or [1042])

    class _Draft:
        vendor_name = "Alpha"
        subject = "Subject"
        body = "Body"

    monkeypatch.setattr(rfq_service, "generate_rfq_drafts",
                        lambda **_kw: type("R", (), {"drafts": [_Draft()]})())

    result = rfq_service.preview_draft(
        {"origin": "A", "destination": "B"},
        rfq_service.SelectedAgent("Alpha", "alpha@example.com", "CHA"),
        rfq_service.SenderIdentity(name="Asha Nair", company="Bhatia Shipping Group"),
    )

    assert result["reference"] == RESERVED_PREVIEW_REFERENCE
    assert calls == [], "a preview must not touch the sequence"


# ---------------------------------------------------------------------------
# the wiring, through the real send path
# ---------------------------------------------------------------------------

def test_send_rfqs_allocates_once_for_the_whole_batch(monkeypatch):
    """The load-bearing claim. Drafting fans out across a ThreadPoolExecutor, so
    the allocation has to happen before the pool starts — once, with the agent
    count — rather than once inside each worker."""
    asked: list[int] = []

    def _allocate(count):
        asked.append(count)
        return [f"RFQ-{1000 + i:06d}" for i in range(count)]

    monkeypatch.setattr(rfq_service, "allocate_references", _allocate)

    class _Draft:
        def __init__(self, name):
            self.vendor_name = name
            self.vendor_email = "v@example.com"
            self.subject = "Subject"
            self.body = "Body"

    monkeypatch.setattr(
        rfq_service, "generate_rfq_drafts",
        lambda **kw: type("R", (), {"drafts": [_Draft(kw["agents"][0]["agent_name"])]})(),
    )
    reserved: list = []
    monkeypatch.setattr(rfq_service.job_repo, "insert_many", reserved.extend)
    monkeypatch.setattr(rfq_service.job_repo, "set_status_if",
                        lambda _r, _s, _e: True)
    monkeypatch.setattr(rfq_service.agent_repo, "ensure_agents", lambda _a: None)
    monkeypatch.setattr(rfq_service.email_repo, "get_thread_id", lambda _e: "")
    monkeypatch.setattr(
        rfq_service, "send_rfq_emails_batch",
        lambda **kw: [{"status": "sent", "to": d["vendor_email"]} for d in kw["drafts"]],
    )

    agents = [rfq_service.SelectedAgent(f"Agent {i}", f"a{i}@example.com", "CHA")
              for i in range(3)]
    rfq_service.send_rfqs(
        {"origin": "A", "destination": "B", "mode": "sea"},
        agents,
        {"email_id": "", "sender": "s@example.com", "subject": "Rates?"},
        rfq_service.SenderIdentity(name="Asha Nair", company="Bhatia Shipping Group"),
    )

    assert asked == [3], "one allocation for the batch, not one per agent"
    assert [j.reference for j in reserved] == ["RFQ-001000", "RFQ-001001", "RFQ-001002"]
