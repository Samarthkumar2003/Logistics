"""
A page of ONE label: fifteen customer requests, not fifteen inbox rows.

Customer requests are ~4% of stored mail, so a page of twenty inbox rows held
about one of them and the tab read as "these are all your requests". The page is
now filtered in the database — and then filtered again on the label actually
displayed, because a cached human correction outranks the stored classification.
That second filter is what made pages short: fifteen rows asked for, seven shown.

What this pins is the top-up that keeps a page full, and the cursor that goes
with it. `next_offset` counts rows *scanned*, not rows kept, so a caller that
pages by `offset + limit` instead would re-serve rows it has already shown.
"""

import pytest

from backend.app.errors import AppException
from backend.app.routes.inbox import fetch_inbox
from backend.domain.models import Email
from backend.repositories import email_repo
from backend.services import inbox_service


def _email(i: int, stored: str = "customer_requirement", status: str = "classified") -> Email:
    return Email(id=f"m{i}", sender=f"a{i}@carrier.example", subject=f"subject {i}",
                 received_at=f"2026-08-{(i % 28) + 1:02d}T10:00:00+00:00",
                 classification=stored, classification_status=status)


@pytest.fixture
def store(monkeypatch):
    """A fake `emails` table plus label cache, recording the reads made.

    `rows` are in the order the database would return them (newest first) and
    already filtered by label, which is what `list_inbox` does server-side.
    """
    reads: list[tuple[int, int]] = []

    def _install(rows: list[Email], cache: dict[str, dict], total: int = 0):
        def _list_inbox(limit, offset, search="", label=""):
            reads.append((limit, offset))
            matching = [e for e in rows if not label or e.classification == label]
            return matching[offset:offset + limit], (total or len(matching))

        monkeypatch.setattr(email_repo, "list_inbox", _list_inbox)
        monkeypatch.setattr(email_repo, "get_cached_labels",
                            lambda ids: {i: cache[i] for i in ids if i in cache})
        monkeypatch.setattr(email_repo, "count_displayed_label",
                            lambda label, search="": sum(
                                1 for e in rows
                                if inbox_service._effective_label(e, cache) == label))
        return reads

    return _install


def test_a_page_of_fifteen_is_fifteen_requests_despite_stale_stored_labels(store):
    """THE REGRESSION. Every third row was relabelled `general` in the cache only,
    so the guard dropped it and page two came back with seven rows."""
    rows = [_email(i) for i in range(60)]
    cache = {f"m{i}": {"label": "general", "method": "thread_rule"}
             for i in range(60) if i % 3 == 0}
    reads = store(rows, cache)

    page = inbox_service.get_inbox_page(limit=15, offset=0, label="customer_requirement")

    assert len(page["emails"]) == 15
    assert {e["label"] for e in page["emails"]} == {"customer_requirement"}
    assert len(reads) > 1, "a short first read must be topped up, not returned short"


def test_the_cursor_counts_rows_scanned_so_pages_do_not_overlap(store):
    rows = [_email(i) for i in range(60)]
    cache = {f"m{i}": {"label": "general", "method": "thread_rule"}
             for i in range(60) if i % 3 == 0}
    store(rows, cache)

    first = inbox_service.get_inbox_page(limit=15, offset=0, label="customer_requirement")
    second = inbox_service.get_inbox_page(limit=15, offset=first["next_offset"],
                                         label="customer_requirement")

    assert first["next_offset"] > 15, "15 kept rows took more than 15 rows of scanning"
    ids = [e["id"] for e in first["emails"]] + [e["id"] for e in second["emails"]]
    assert len(set(ids)) == len(ids), "paging by the cursor must not repeat a row"


def test_the_total_counts_the_label_as_displayed_not_as_stored(store):
    """1054 customer requests, not the stored column's 1086. The tab lists what
    it displays, and a total nobody can page to is a total that reads as a bug."""
    rows = [_email(i) for i in range(20)]
    cache = {"m0": {"label": "general", "method": "thread_rule"},
             "m1": {"label": "general", "method": "thread_rule"}}
    store(rows, cache, total=20)

    page = inbox_service.get_inbox_page(limit=5, offset=0, label="customer_requirement")
    assert page["total"] == 18


def test_a_correction_moves_an_email_between_tabs(store):
    """The point of a correction: absent from the tab its old label put it in,
    present in the new one."""
    rows = [_email(0), _email(1, stored="quotation_rate_card")]
    cache = {"m0": {"label": "quotation_rate_card", "method": "human"}}
    store(rows, cache)

    requests = inbox_service.get_inbox_page(limit=5, offset=0, label="customer_requirement")
    assert [e["id"] for e in requests["emails"]] == []

    # m0 is only reachable in the rate-card tab once the stored column agrees;
    # until then the tab shows the rows the stored column selects. See
    # get_inbox_page on the 34 rows this leaves stranded.
    cards = inbox_service.get_inbox_page(limit=5, offset=0, label="quotation_rate_card")
    assert [e["id"] for e in cards["emails"]] == ["m1"]


def test_running_out_of_matches_ends_the_list(store):
    rows = [_email(i) for i in range(4)]
    store(rows, {})

    page = inbox_service.get_inbox_page(limit=15, offset=0, label="customer_requirement")
    assert len(page["emails"]) == 4
    assert page["has_more"] is False, "four rows and no more is the end, not a short page"


def test_the_scan_is_bounded_so_one_page_request_cannot_walk_the_inbox(store):
    """Every row stale: the top-up must give up, not read 27,599 rows."""
    rows = [_email(i) for i in range(500)]
    cache = {f"m{i}": {"label": "general", "method": "thread_rule"} for i in range(500)}
    reads = store(rows, cache)

    page = inbox_service.get_inbox_page(limit=15, offset=0, label="customer_requirement")

    assert page["emails"] == []
    assert len(reads) <= inbox_service._SCAN_ROUNDS
    assert page["next_offset"] > 0, "the cursor still advances, so the next page moves on"


def test_pending_is_a_status_not_a_stored_label(store):
    rows = [_email(0), _email(1, stored="", status="pending")]
    store(rows, {})

    # `list_inbox` filters `pending` on classification_status; the fake filters on
    # the stored label, so assert on the guard instead: a classified email must
    # not display as pending.
    assert inbox_service._effective_label(rows[0], {}) == "customer_requirement"
    assert inbox_service._effective_label(rows[1], {}) == "pending"


def test_the_unfiltered_inbox_still_pages_by_the_page_size(store):
    rows = [_email(i, stored="general") for i in range(50)]
    store(rows, {})

    page = inbox_service.get_inbox_page(limit=20, offset=0)
    assert len(page["emails"]) == 20
    assert page["next_offset"] == 20
    assert page["total"] == 50
    assert page["has_more"] is True


def test_an_unknown_label_is_a_422_not_an_empty_page():
    """"No customer requests" is the wrong thing to tell an operator about a typo
    in a query string."""
    with pytest.raises(AppException) as err:
        fetch_inbox(limit=15, label="custmer_requirement")
    assert err.value.status_code == 422
    assert "customer_requirement" in err.value.detail


@pytest.mark.parametrize("label", inbox_service.LABELS)
def test_every_offered_label_is_accepted(label, store):
    store([_email(0)], {})
    assert fetch_inbox(limit=1, label=label)["label"] == label


# ---------------------------------------------------------------------------
# Where the total comes from: the cache holds the label that gets displayed.
# ---------------------------------------------------------------------------

class _CountTable:
    """Minimal postgrest stand-in for a `count="exact"` query."""

    def __init__(self, name: str, counts: dict[str, int], asked: list[str]):
        self._name, self._counts, self._asked = name, counts, asked
        self._label = ""

    def select(self, _columns, count=None):
        return self

    def eq(self, column, value):
        if column == "label":
            self._label = value
        return self

    def limit(self, _n):
        return self

    def execute(self):
        self._asked.append(self._name)
        return type("R", (), {"data": [], "count": self._counts.get(self._label, 0)})()


@pytest.fixture
def counted(monkeypatch):
    """Install a db whose `email_classifications` count is known, and record
    which table each count was read from."""
    asked: list[str] = []

    def _install(counts: dict[str, int], stored_total: int = 999):
        monkeypatch.setattr(
            email_repo, "get_db",
            lambda: type("DB", (), {
                "table": lambda _s, name: _CountTable(name, counts, asked)})())

        def _list_inbox(limit, offset, search="", label=""):
            asked.append("emails")
            return [], stored_total

        monkeypatch.setattr(email_repo, "list_inbox", _list_inbox)
        return asked

    return _install


def test_the_count_comes_from_the_cache_because_that_is_what_is_displayed(counted):
    asked = counted({"customer_requirement": 1054}, stored_total=1086)
    assert email_repo.count_displayed_label("customer_requirement") == 1054
    assert asked == ["email_classifications"], "the stored column overstates by 32"


def test_a_subject_search_falls_back_to_the_stored_column(counted):
    """The cache holds no subject, so a search cannot be counted there."""
    asked = counted({"customer_requirement": 1054}, stored_total=86)
    assert email_repo.count_displayed_label("customer_requirement", search="quote") == 86
    assert asked == ["emails"]


def test_pending_is_counted_on_the_status_not_the_cache(counted):
    asked = counted({"pending": 7}, stored_total=0)
    assert email_repo.count_displayed_label("pending") == 0
    assert asked == ["emails"], "pending is a status; no cache row ever says it"


def test_a_broken_cache_count_falls_back_rather_than_failing_the_page(monkeypatch):
    def _boom():
        raise RuntimeError("supabase unreachable")

    monkeypatch.setattr(email_repo, "get_db", _boom)
    monkeypatch.setattr(email_repo, "list_inbox",
                        lambda limit, offset, search="", label="": ([], 1086))
    assert email_repo.count_displayed_label("customer_requirement") == 1086
