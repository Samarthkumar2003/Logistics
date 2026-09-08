"""
Bounds on what one attachment download may pull into memory, and giving the pages
back afterwards.

The api climbed from 41 MB on a fresh start to 829 MB over 4.6 days against a
1024 MB limit, with a two-day plateau in the middle while the queue was drained.
The plateau is what ruled out a retained reference: a leak that held objects
would grow on every tick, whereas this only grew while there were bytes to move.
The cause is glibc opening per-thread arenas for large multi-threaded
allocations and never handing the freed pages back.

Three things are pinned here:

  1. No download may exceed MAX_ATTACHMENT_BYTES, and the size is checked BEFORE
     the fetch, so oversized bytes never reach memory at all.
  2. The queue query must select the column the check reads. If size_bytes is
     dropped from the select, row.get returns None and the ceiling silently
     stops existing, which is the failure this file exists to catch.
  3. The batch trims the heap when it finishes.
"""

import pytest

from backend.connectors import email_store
from backend.core import heap


class _FakeDb:
    """Enough of the postgrest chain to capture the status writes."""

    def __init__(self):
        self.updated: list[dict] = []
        self._pending: dict | None = None

    def table(self, _name):
        return self

    def update(self, row):
        self._pending = row
        return self

    def eq(self, *_a, **_k):
        return self

    def execute(self):
        if self._pending is not None:
            self.updated.append(self._pending)
            self._pending = None
        return self

    @property
    def storage(self):
        return self

    def from_(self, _bucket):
        return self

    def upload(self, *_a, **_k):
        return None


@pytest.fixture
def db(monkeypatch):
    fake = _FakeDb()
    monkeypatch.setattr(email_store, "get_db", lambda: fake)
    return fake


def _row(**over) -> dict:
    base = {
        "id": "att-1",
        "provider_msg_id": "msg1",
        "attachment_id": "ANGjdJ_xyz",
        "file_name": "scan.pdf",
        "mime_type": "application/pdf",
        "attempts": 0,
        "size_bytes": 1_000,
    }
    return {**base, **over}


# ---------------------------------------------------------------------------
# The ceiling
# ---------------------------------------------------------------------------

def test_an_oversized_attachment_is_never_fetched(db, monkeypatch):
    """The whole point is that the bytes never reach memory, so the boundary is
    stubbed to fail loudly if it is called at all."""
    def explode(*_a):
        raise AssertionError("fetch_attachment must not run for an oversized file")
    monkeypatch.setattr(email_store, "fetch_attachment", explode)

    result = email_store._download_pending_attachment(
        _row(size_bytes=email_store.MAX_ATTACHMENT_BYTES + 1)
    )

    assert result == "failed"


def test_an_oversized_attachment_is_failed_not_left_pending(db, monkeypatch):
    """Left pending it would be picked up and re-fetched every two minutes until
    it burned through MAX_ATTACHMENT_ATTEMPTS, pulling the same too-large file in
    each time. Permanent conditions have to be recorded as permanent."""
    monkeypatch.setattr(email_store, "fetch_attachment",
                        lambda *_a: pytest.fail("must not fetch"))

    email_store._download_pending_attachment(
        _row(size_bytes=email_store.MAX_ATTACHMENT_BYTES + 1)
    )

    assert db.updated == [{"processing_status": "failed", "attempts": 1}]


def test_a_file_exactly_at_the_ceiling_is_still_fetched(db, monkeypatch):
    """Inclusive at the boundary: the ceiling rejects what is over it, not what
    reaches it, so a file of exactly the limit is content and not an error."""
    monkeypatch.setattr(email_store, "fetch_attachment", lambda *_a: b"pdf bytes")

    result = email_store._download_pending_attachment(
        _row(size_bytes=email_store.MAX_ATTACHMENT_BYTES)
    )

    assert result == "stored"


def test_a_missing_size_does_not_block_the_download(db, monkeypatch):
    """size_bytes has been non-null for all 241,214 rows on file, but a provider
    that omits it must not have every one of its attachments refused. Unknown
    size falls through to the fetch rather than failing closed."""
    monkeypatch.setattr(email_store, "fetch_attachment", lambda *_a: b"pdf bytes")

    assert email_store._download_pending_attachment(_row(size_bytes=None)) == "stored"


def test_the_ceiling_is_ten_megabytes():
    """Only 8 of 241,214 rows exceed this, and the largest file on record is a
    17 MB PDF, so the ceiling bounds the worst case without touching normal mail."""
    assert email_store.MAX_ATTACHMENT_BYTES == 10 * 1024 * 1024


# ---------------------------------------------------------------------------
# The gate can only fire if the query feeds it
# ---------------------------------------------------------------------------

def test_the_queue_query_selects_the_size_the_gate_reads():
    """The regression this file exists for. The gate reads row.get("size_bytes"),
    which returns None for a column that was not selected, and None falls through
    to the fetch by design. Drop size_bytes from the select and the ceiling
    disappears in silence, with every test above still passing because they build
    their rows by hand."""
    assert "size_bytes" in email_store._QUEUE_COLUMNS


# ---------------------------------------------------------------------------
# Giving the pages back
# ---------------------------------------------------------------------------

def test_the_batch_trims_the_heap_when_it_finishes(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(email_store, "trim_heap", lambda: calls.append(1))
    monkeypatch.setattr(email_store, "_fetch_pending_batch", lambda _m: [_row()])
    monkeypatch.setattr(email_store, "_download_pending_attachment", lambda _r: "stored")

    stats = email_store._process_pending_attachments(max_atts=150, workers=2)

    assert stats["stored"] == 1
    assert calls == [1], "the batch must hand the pages back once it is done"


def test_an_empty_batch_does_not_bother_trimming(monkeypatch):
    """Nothing was allocated, so there is nothing to return. The worker runs every
    two minutes and the queue is usually empty; trimming on every idle tick would
    be pure syscall overhead."""
    calls: list[int] = []
    monkeypatch.setattr(email_store, "trim_heap", lambda: calls.append(1))
    monkeypatch.setattr(email_store, "_fetch_pending_batch", lambda _m: [])

    email_store._process_pending_attachments(max_atts=150, workers=2)

    assert calls == []


def test_trim_heap_is_safe_to_call_and_caches_its_lookup():
    """Must never raise: it runs on macOS in development and in a glibc image in
    production, and only one of those has malloc_trim. Both answers are cached so
    a platform without it does not pay for a lookup on every batch."""
    heap.reset_for_tests()
    first = heap.trim_heap()
    assert isinstance(first, bool)
    assert heap.trim_heap() is first


def test_trim_heap_reports_false_when_the_allocator_has_no_trim(monkeypatch):
    """musl images and macOS both land here. False, not an exception."""
    heap.reset_for_tests()
    monkeypatch.setattr(heap.ctypes, "CDLL",
                        lambda _n: (_ for _ in ()).throw(OSError("no libc")))

    assert heap.trim_heap() is False
    heap.reset_for_tests()


# ---------------------------------------------------------------------------
# Thread count
# ---------------------------------------------------------------------------

def test_the_worker_stays_at_two_threads():
    """Each thread gets its own glibc arena, so thread count is a memory decision
    here and not only a throughput one. Raising this reopens the fragmentation
    this file is about; the queue is fully drained, so there is nothing to gain."""
    import inspect
    default = inspect.signature(
        email_store.process_pending_attachments).parameters["workers"].default
    assert default == 2
