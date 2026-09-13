"""
Reading `rfq_jobs` as shipments instead of as rows.

The table is one row per *agent*, which is right for sending and wrong for
reading. Twenty-one rows on file are seven pieces of work: one enquiry to seven
agents produced seven rows that all describe the same shipment to Dammam, and a
list of rows showed it seven times.

Three things are pinned here, in the order they can hurt:

1. **Grouping is complete.** A shipment carries every RFQ it produced, even when
   that is more rows than a page of `/jobs` would ever return. Grouping in the
   browser instead under-reports exactly the busiest shipment.
2. **The aggregate status is honest.** One chip cannot describe
   `{approved: 1, rfqs_sent: 2}`, so the breakdown always rides with it and a
   failed send is never hidden behind a healthy headline.
3. **The agent type is resolved, not guessed.** The category lives on `agents`,
   not on the job, so it is joined back at read time and returns empty rather
   than a default when the roster cannot say.
"""

import pytest

from backend.repositories import agent_repo, email_repo, job_repo
from backend.services import shipment_service

CUST_A = "1a094edaf01deb41"
CUST_B = "1a00b35cfd01f911"


# ---------------------------------------------------------------------------
# Fake postgrest
# ---------------------------------------------------------------------------

class _Table:
    """Minimal postgrest stand-in supporting select/order/limit/in_/eq."""

    def __init__(self, rows: list[dict]):
        self._rows = rows
        self._in: tuple[str, list] | None = None
        self._eq: tuple[str, object] | None = None
        self._order: str | None = None
        self._desc = False
        self._limit: int | None = None

    def select(self, columns: str):
        self.columns = columns
        return self

    def order(self, column: str, desc: bool = False):
        self._order, self._desc = column, desc
        return self

    def limit(self, n: int):
        self._limit = n
        return self

    def in_(self, column: str, values):
        self._in = (column, list(values))
        return self

    def eq(self, column: str, value):
        self._eq = (column, value)
        return self

    def execute(self):
        rows = list(self._rows)
        if self._in:
            column, values = self._in
            rows = [r for r in rows if r.get(column) in values]
        if self._eq:
            column, value = self._eq
            rows = [r for r in rows if r.get(column) == value]
        if self._order:
            rows.sort(key=lambda r: r.get(self._order) or "", reverse=self._desc)
        if self._limit is not None:
            rows = rows[:self._limit]
        return type("R", (), {"data": rows})()


@pytest.fixture
def jobs_table(monkeypatch):
    """Whatever `rfq_jobs` holds for this test."""
    state: list[dict] = []
    monkeypatch.setattr(job_repo, "get_db",
                        lambda: type("DB", (), {"table": lambda _s, _n: _Table(state)})())
    return state


def row(reference, customer_email_id, agent, created_at, status="rfqs_sent", **extra):
    base = {
        "reference": reference,
        "customer_email_id": customer_email_id,
        "agents_contacted": [agent] if agent else [],
        "created_at": created_at,
        "status": status,
        "shipment_origin": "Nhava Sheva, India",
        "shipment_destination": "Hamad, Qatar",
        "shipment_mode": "sea_freight",
        "shipment_commodity": "Ceramic tiles",
        "customer_email_sender": "Buyer <buyer@example.com>",
        "customer_email_subject": "Rates please",
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# headline_status: the one chip the card leads with
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("statuses, expected", [
    # The ladder, rung by rung.
    (["rfqs_sent", "quotes_received", "approved"], "approved"),
    (["rfqs_sent", "quotes_received"], "quotes_received"),
    (["sending", "rfqs_sent"], "rfqs_sent"),
    (["sending"], "sending"),
    # One awarded RFQ means the shipment is awarded, even with five losers still
    # sitting at rfqs_sent.
    (["approved", "rfqs_sent", "rfqs_sent", "rfqs_sent"], "approved"),
])
def test_the_best_status_in_a_shipment_is_the_headline(statuses, expected):
    assert shipment_service.headline_status(statuses) == expected


def test_a_wholly_failed_shipment_says_so():
    """Nothing else to report, so the failure is the headline."""
    assert shipment_service.headline_status(["send_failed", "send_failed"]) == "send_failed"


@pytest.mark.parametrize("statuses, expected", [
    (["send_failed", "rfqs_sent"], "rfqs_sent"),
    (["send_failed", "approved"], "approved"),
    (["send_failed", "sending"], "sending"),
])
def test_a_failed_send_never_becomes_the_headline_on_its_own(statuses, expected):
    """A shipment where one of six RFQs bounced still leads with the progress it
    made. The failure is surfaced as its own badge instead — see
    `test_a_failed_send_is_reported_next_to_a_healthy_headline`, which is the half
    of this that stops the badge being dropped."""
    assert shipment_service.headline_status(statuses) == expected


def test_no_rfqs_has_no_headline():
    assert shipment_service.headline_status([]) == ""


def test_an_unknown_status_is_shown_as_itself():
    """A status nobody taught this function about should look wrong on screen
    rather than be quietly mapped onto a plausible default."""
    assert shipment_service.headline_status(["cancelled"]) == "cancelled"


def test_the_breakdown_that_rides_with_the_chip():
    from backend.domain.models import RfqJob
    jobs = [RfqJob(reference="a", status="approved"),
            RfqJob(reference="b", status="rfqs_sent"),
            RfqJob(reference="c", status="rfqs_sent")]

    assert shipment_service.status_counts(jobs) == {"approved": 1, "rfqs_sent": 2}


# ---------------------------------------------------------------------------
# Grouping rows back into shipments
# ---------------------------------------------------------------------------

def test_one_enquiry_to_seven_agents_is_one_shipment(jobs_table):
    jobs_table += [
        row(f"RFQ-{i}", CUST_A, f"Agent {i}", f"2026-09-13T10:0{i}:00Z")
        for i in range(7)
    ]

    page = job_repo.list_shipment_groups(limit=20, offset=0)

    assert len(page.groups) == 1
    assert page.total == 1
    assert len(page.groups[0]) == 7


def test_a_shipment_keeps_every_rfq_even_when_the_page_holds_one_shipment(jobs_table):
    """The bug this endpoint exists to prevent. `/jobs` caps at 20 rows, so a
    browser grouping a page of rows would report this seven-RFQ shipment as
    however many of its rows happened to fit — and would do it silently."""
    jobs_table += [
        row(f"RFQ-a{i}", CUST_A, f"Agent {i}", f"2026-09-13T10:0{i}:00Z") for i in range(7)
    ] + [
        row(f"RFQ-b{i}", CUST_B, f"Other {i}", f"2026-09-12T10:0{i}:00Z") for i in range(6)
    ]

    page = job_repo.list_shipment_groups(limit=1, offset=0)

    assert len(page.groups) == 1
    assert len(page.groups[0]) == 7, "the page limit must bound shipments, not RFQs"
    assert page.total == 2


def test_shipments_are_ordered_by_their_most_recent_rfq(jobs_table):
    jobs_table += [
        row("RFQ-old", CUST_B, "Agent B", "2026-09-01T10:00:00Z"),
        row("RFQ-new", CUST_A, "Agent A", "2026-09-13T10:00:00Z"),
    ]

    page = job_repo.list_shipment_groups(limit=20, offset=0)

    assert [g[0].customer_email_id for g in page.groups] == [CUST_A, CUST_B]


def test_rfqs_within_a_shipment_read_oldest_first(jobs_table):
    """The order they were sent, which is the order a thread makes sense in."""
    jobs_table += [
        row("RFQ-2", CUST_A, "Second", "2026-09-13T11:00:00Z"),
        row("RFQ-1", CUST_A, "First", "2026-09-13T10:00:00Z"),
    ]

    group = job_repo.list_shipment_groups(limit=20, offset=0).groups[0]

    assert [j.reference for j in group] == ["RFQ-1", "RFQ-2"]


def test_paging_walks_shipments_not_rows(jobs_table):
    jobs_table += [
        row(f"RFQ-{c}{i}", c, f"Agent {i}", f"2026-09-1{n}T10:00:00Z")
        for n, c in enumerate(["cust-1", "cust-2", "cust-3"], start=1)
        for i in range(3)
    ]

    first = job_repo.list_shipment_groups(limit=2, offset=0)
    second = job_repo.list_shipment_groups(limit=2, offset=2)

    assert [g[0].customer_email_id for g in first.groups] == ["cust-3", "cust-2"]
    assert [g[0].customer_email_id for g in second.groups] == ["cust-1"]
    assert first.total == second.total == 3


def test_a_shipment_that_never_sent_anything_is_not_listed(jobs_table):
    """A reserved row that never reached a send is not work the desk can act on."""
    jobs_table += [
        row("RFQ-sent", CUST_A, "Agent A", "2026-09-13T10:00:00Z"),
        row("RFQ-never", CUST_B, None, "2026-09-13T11:00:00Z", status="sending"),
    ]

    page = job_repo.list_shipment_groups(limit=20, offset=0)

    assert [g[0].customer_email_id for g in page.groups] == [CUST_A]
    assert page.total == 1


def test_a_shipment_with_one_unsent_rfq_still_appears_whole(jobs_table):
    """One row of six failing to reach a send must not hide the shipment, and must
    not vanish from it either — the desk needs to see the gap."""
    jobs_table += [
        row("RFQ-1", CUST_A, "Agent A", "2026-09-13T10:00:00Z"),
        row("RFQ-2", CUST_A, None, "2026-09-13T10:01:00Z", status="sending"),
    ]

    group = job_repo.list_shipment_groups(limit=20, offset=0).groups[0]

    assert [j.reference for j in group] == ["RFQ-1", "RFQ-2"]


def test_orphan_sends_stand_alone_rather_than_merging(jobs_table):
    """`insert` writes `customer_email_id or None`, so a send with no source email
    has nothing to group on. Keying those on NULL would collapse every orphan ever
    sent into one bogus shipment."""
    jobs_table += [
        row("RFQ-x", None, "Agent X", "2026-09-13T10:00:00Z"),
        row("RFQ-y", None, "Agent Y", "2026-09-13T11:00:00Z"),
    ]

    page = job_repo.list_shipment_groups(limit=20, offset=0)

    assert page.total == 2
    assert [[j.reference for j in g] for g in page.groups] == [["RFQ-y"], ["RFQ-x"]]


def test_an_empty_table_is_an_empty_page(jobs_table):
    page = job_repo.list_shipment_groups(limit=20, offset=0)

    assert (page.groups, page.total, page.truncated) == ([], 0, False)


def test_an_offset_past_the_end_is_empty_but_still_counts(jobs_table):
    jobs_table += [row("RFQ-1", CUST_A, "Agent A", "2026-09-13T10:00:00Z")]

    page = job_repo.list_shipment_groups(limit=20, offset=50)

    assert page.groups == []
    assert page.total == 1


def test_a_filled_scan_window_is_reported_as_truncated(jobs_table, monkeypatch):
    """`total` becomes a floor once the window fills, and the caller has to say
    "at least N" rather than print a number it cannot stand behind."""
    monkeypatch.setattr(job_repo, "_SHIPMENT_SCAN_ROWS", 2)
    jobs_table += [
        row(f"RFQ-{i}", f"cust-{i}", "Agent", f"2026-09-13T10:0{i}:00Z") for i in range(5)
    ]

    page = job_repo.list_shipment_groups(limit=20, offset=0)

    assert page.truncated is True
    assert page.total == 2, "only what the window saw"


# ---------------------------------------------------------------------------
# The shipment card's numbers
# ---------------------------------------------------------------------------

@pytest.fixture
def shipments(monkeypatch, jobs_table):
    """`list_shipments` over `jobs_table`, with replies and roster stubbed."""
    def _run(stats=None, roster=None):
        monkeypatch.setattr(shipment_service.email_repo, "reply_stats_by_reference",
                            lambda _refs: stats or {})
        monkeypatch.setattr(shipment_service.agent_repo, "category_index",
                            lambda: roster or agent_repo.CategoryIndex())
        return shipment_service.list_shipments(limit=20, offset=0)["shipments"]
    return _run


def test_vendors_who_replied_are_counted_not_their_messages(shipments, jobs_table):
    """Three vendors asked, one answered twice. One vendor came back."""
    jobs_table += [
        row("RFQ-1", CUST_A, "Agent One", "2026-09-13T10:00:00Z"),
        row("RFQ-2", CUST_A, "Agent Two", "2026-09-13T10:01:00Z"),
        row("RFQ-3", CUST_A, "Agent Three", "2026-09-13T10:02:00Z"),
    ]

    s = shipments(stats={"RFQ-1": email_repo.ReplyStats(messages=2, agents=1)})[0]

    assert s["rfq_count"] == 3
    assert s["agents_replied"] == 1
    assert s["reply_count"] == 2
    assert s["awaiting"] == 2


def test_a_failed_send_is_not_awaiting_a_reply(shipments, jobs_table):
    """Nothing was delivered, so there is nobody to wait for. Counting it as
    awaiting is how a broken send hides inside an ordinary-looking wait."""
    jobs_table += [
        row("RFQ-1", CUST_A, "Agent One", "2026-09-13T10:00:00Z"),
        row("RFQ-2", CUST_A, "Agent Two", "2026-09-13T10:01:00Z", status="send_failed"),
    ]

    s = shipments()[0]

    assert s["awaiting"] == 1
    assert s["send_failed"] == 1


def test_an_awarded_rfq_is_not_still_awaiting_a_reply(shipments, jobs_table):
    """Found in the real table: the Chennai enquiry is awarded with no reply ever
    linked against the winning RFQ, and read as "3 awaiting" on a finished job.
    The desk is not waiting to hear from a vendor it has already chosen — but it is
    still waiting on the two that were not chosen."""
    jobs_table += [
        row("RFQ-1", CUST_A, "Winner", "2026-09-13T10:00:00Z", status="approved"),
        row("RFQ-2", CUST_A, "Agent Two", "2026-09-13T10:01:00Z"),
        row("RFQ-3", CUST_A, "Agent Three", "2026-09-13T10:02:00Z"),
    ]

    assert shipments()[0]["awaiting"] == 2


def test_a_failed_send_is_reported_next_to_a_healthy_headline(shipments, jobs_table):
    """The other half of the ladder rule: `approved` leads, and the failure is
    still on the card. An awarded shipment must not be able to hide an RFQ that
    never left."""
    jobs_table += [
        row("RFQ-1", CUST_A, "Winner", "2026-09-13T10:00:00Z", status="approved"),
        row("RFQ-2", CUST_A, "Bounced", "2026-09-13T10:01:00Z", status="send_failed"),
    ]

    s = shipments()[0]

    assert s["status"] == "approved"
    assert s["send_failed"] == 1
    assert s["statuses"] == {"approved": 1, "send_failed": 1}


def test_the_full_status_breakdown_travels_with_the_headline(shipments, jobs_table):
    """`{approved: 1, rfqs_sent: 2}` is not "approved" in any complete sense, so
    the card is given both and renders both."""
    jobs_table += [
        row("RFQ-1", CUST_A, "Winner", "2026-09-13T10:00:00Z", status="approved"),
        row("RFQ-2", CUST_A, "Agent Two", "2026-09-13T10:01:00Z"),
        row("RFQ-3", CUST_A, "Agent Three", "2026-09-13T10:02:00Z"),
    ]

    s = shipments()[0]

    assert s["status"] == "approved"
    assert s["statuses"] == {"approved": 1, "rfqs_sent": 2}


def test_a_later_correction_wins_the_shipment_field(shipments, jobs_table):
    """A second batch for the same enquiry carrying a corrected weight is the
    current truth; the first batch is not."""
    jobs_table += [
        row("RFQ-1", CUST_A, "Agent One", "2026-09-13T10:00:00Z", shipment_weight_kg=1000),
        row("RFQ-2", CUST_A, "Agent Two", "2026-09-13T11:00:00Z", shipment_weight_kg=1250),
    ]

    assert shipments()[0]["shipment_weight_kg"] == 1250


def test_a_blank_never_overwrites_a_populated_field(shipments, jobs_table):
    jobs_table += [
        row("RFQ-1", CUST_A, "Agent One", "2026-09-13T10:00:00Z", shipment_commodity="Tiles"),
        row("RFQ-2", CUST_A, "Agent Two", "2026-09-13T11:00:00Z", shipment_commodity=""),
    ]

    assert shipments()[0]["shipment_commodity"] == "Tiles"


def test_an_orphan_shipment_has_no_request_page_to_open(shipments, jobs_table):
    """Null rather than a fabricated id: the frontend must disable the button, not
    navigate somewhere that 404s."""
    jobs_table += [row("RFQ-x", None, "Agent X", "2026-09-13T10:00:00Z")]

    assert shipments()[0]["customer_email_id"] is None


def test_a_truncated_scan_always_reports_more_to_come(monkeypatch, jobs_table):
    """`total` is only a floor once the grouping window fills, so comparing against
    it alone would tell a paging caller it had reached the end at the exact table
    size where there is most certainly more to see."""
    monkeypatch.setattr(job_repo, "_SHIPMENT_SCAN_ROWS", 2)
    jobs_table += [
        row(f"RFQ-{i}", f"cust-{i}", "Agent", f"2026-09-13T10:0{i}:00Z") for i in range(5)
    ]
    monkeypatch.setattr(shipment_service.email_repo, "reply_stats_by_reference", lambda _r: {})
    monkeypatch.setattr(shipment_service.agent_repo, "category_index",
                        lambda: agent_repo.CategoryIndex())

    page = shipment_service.list_shipments(limit=20, offset=0)

    assert page["truncated"] is True
    assert page["has_more"] is True, "the window filled, so there is more beyond it"


def test_an_untruncated_page_that_holds_everything_says_so(monkeypatch, jobs_table):
    jobs_table += [row("RFQ-1", CUST_A, "Agent A", "2026-09-13T10:00:00Z")]
    monkeypatch.setattr(shipment_service.email_repo, "reply_stats_by_reference", lambda _r: {})
    monkeypatch.setattr(shipment_service.agent_repo, "category_index",
                        lambda: agent_repo.CategoryIndex())

    page = shipment_service.list_shipments(limit=20, offset=0)

    assert (page["truncated"], page["has_more"]) == (False, False)


# ---------------------------------------------------------------------------
# What kind of agent each RFQ went to
# ---------------------------------------------------------------------------

def test_each_rfq_carries_the_type_of_agent_it_went_to(shipments, jobs_table):
    jobs_table += [
        row("RFQ-1", CUST_A, "Clearing Co", "2026-09-13T10:00:00Z",
            draft_to="ops@clearing.example"),
        row("RFQ-2", CUST_A, "Forwarder Co", "2026-09-13T10:01:00Z",
            draft_to="ops@forwarder.example"),
    ]
    roster = agent_repo.CategoryIndex(by_email={
        "ops@clearing.example": "CHA",
        "ops@forwarder.example": "FREIGHT_FORWARDER",
    })

    s = shipments(roster=roster)[0]

    assert [r["agent_category"] for r in s["rfqs"]] == ["CHA", "FREIGHT_FORWARDER"]
    assert s["agent_types"] == {"CHA": 1, "FREIGHT_FORWARDER": 1}


def test_an_older_rfq_with_no_stored_address_resolves_by_name(shipments, jobs_table):
    """`draft_to` was added later, so eleven of the twenty-one rows on file have
    none. Falling back to the name is what keeps those labelled."""
    jobs_table += [row("RFQ-1", CUST_A, "Clearing Co", "2026-09-13T10:00:00Z")]
    roster = agent_repo.CategoryIndex(by_name={"Clearing Co": "CHA"})

    assert shipments(roster=roster)[0]["rfqs"][0]["agent_category"] == "CHA"


def test_an_agent_the_roster_does_not_know_is_typed_unknown(shipments, jobs_table):
    """Empty, not a default. Six RFQs on file resolve to empty today and all six
    went to QA addresses that were never roster agents."""
    jobs_table += [row("RFQ-1", CUST_A, "QA E2E Test", "2026-09-13T10:00:00Z")]

    s = shipments()[0]

    assert s["rfqs"][0]["agent_category"] == ""
    assert s["agent_types"] == {"": 1}


# ---------------------------------------------------------------------------
# The roster lookup itself
# ---------------------------------------------------------------------------

def test_an_exact_address_beats_a_name(monkeypatch):
    """`agents` is one row per office, so the address is the only exact key."""
    idx = agent_repo.CategoryIndex(by_email={"branch@dpworld.example": "CARRIER"},
                                  by_name={"DP World": "FREIGHT_FORWARDER"})

    assert idx.category_for("DP World", "branch@dpworld.example") == "CARRIER"


def test_a_name_spanning_two_categories_is_dropped_not_guessed(monkeypatch):
    """The same refusal `email_for_name` makes. Zero names span two categories in
    the roster today; this is what keeps it honest when one does."""
    rows = [
        {"agent_name": "Split Co", "email": "a@split.example", "category": "CHA"},
        {"agent_name": "Split Co", "email": "b@split.example", "category": "CARRIER"},
        {"agent_name": "Solo Co", "email": "c@solo.example", "category": "CHA"},
    ]
    monkeypatch.setattr(agent_repo, "get_db",
                        lambda: type("DB", (), {"table": lambda _s, _n: _Table(rows)})())

    idx = agent_repo.category_index()

    assert idx.category_for("Split Co") == "", "ambiguous by name"
    assert idx.category_for("Split Co", "a@split.example") == "CHA", "exact by address"
    assert idx.category_for("Solo Co") == "CHA"


def test_a_failed_roster_read_labels_everyone_unknown(monkeypatch):
    """Degrades the display and breaks nothing — a 500 here would take out the
    whole shipments page over a cosmetic label."""
    class _Boom(_Table):
        def execute(self):
            raise RuntimeError("statement timeout")

    monkeypatch.setattr(agent_repo, "get_db",
                        lambda: type("DB", (), {"table": lambda _s, _n: _Boom([])})())

    assert agent_repo.category_index().category_for("Anyone", "a@b.example") == ""


def test_a_roster_row_with_no_category_is_skipped(monkeypatch):
    rows = [{"agent_name": "Blank Co", "email": "a@blank.example", "category": ""}]
    monkeypatch.setattr(agent_repo, "get_db",
                        lambda: type("DB", (), {"table": lambda _s, _n: _Table(rows)})())

    assert agent_repo.category_index().category_for("Blank Co", "a@blank.example") == ""
