"""
Which attachments are worth downloading, and in what order.

The queue reached 36,205 pending rows, and 25,484 of them were embedded images
under 20 kB — signature logos, icons, spacers, tracking pixels. At 150 downloads
per two-minute tick that is most of a day of work, and because the queue drained
strictly oldest-first, a vendor's rate-card PDF landing now sat behind all of it.

Two things are easy to get wrong here, and both lose real documents:

  * filtering on size alone — the queue holds genuine 1.8 kB payment PDFs and
    300-byte CSVs, so a flat "under 20 kB is junk" rule discards content;
  * filtering on embedded-ness alone — an agent pasting a rate table into the
    message body produces an embedded image indistinguishable, by disposition,
    from a logo. In this trade that screenshot *is* the quotation.

So the test is both together: embedded AND small. Everything else is kept.
"""

import pytest

from backend.connectors import email_store
from backend.connectors.email_store import INLINE_IMAGE_MIN_BYTES, is_body_furniture


def _meta(**over) -> dict:
    """Gmail part metadata as _collect_attachments returns it."""
    base = {
        "filename": "image001.png",
        "mime_type": "image/png",
        "attachment_id": "ANGjdJ_xyz",
        "size_bytes": 6_400,
        "content_id": "<image001.png@01DC0F.7A2B>",
    }
    return {**base, **over}


# ---------------------------------------------------------------------------
# Tier 3 — embedded and small: the only thing we refuse to download
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("size", [0, 800, 6_400, INLINE_IMAGE_MIN_BYTES - 1])
def test_a_small_embedded_image_is_furniture(size):
    assert is_body_furniture(_meta(size_bytes=size)) is True


@pytest.mark.parametrize("mime", ["image/png", "image/jpeg", "image/gif"])
def test_every_embedded_image_type_counts(mime):
    assert is_body_furniture(_meta(mime_type=mime, size_bytes=2_500)) is True


# ---------------------------------------------------------------------------
# Tier 1 — attached on purpose. Kept at any size.
# ---------------------------------------------------------------------------

def test_a_document_with_no_content_id_is_kept_however_small():
    """Real rows from the queue: a 1.8 kB bank PDF, a 300-byte CSV. Size is not
    evidence of worthlessness — being embedded in the body is."""
    assert is_body_furniture(_meta(
        filename="EBANKGO22154677.pdf", mime_type="application/pdf",
        size_bytes=1_900, content_id="")) is False
    assert is_body_furniture(_meta(
        filename="BMCT FORM11.csv", mime_type="text/csv",
        size_bytes=300, content_id="")) is False


def test_a_deliberately_attached_image_is_kept():
    """No Content-ID means someone attached the photo rather than embedding it —
    a photo of a damaged container, say. Keep it."""
    assert is_body_furniture(_meta(size_bytes=900, content_id="")) is False


def test_a_blank_or_whitespace_content_id_is_not_a_content_id():
    assert is_body_furniture(_meta(content_id="   ")) is False


# ---------------------------------------------------------------------------
# Tier 2 — embedded but big enough to be content
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("size", [INLINE_IMAGE_MIN_BYTES, 157_500, 426_100])
def test_a_large_embedded_image_is_kept(size):
    """A pasted rate table. The threshold is inclusive at the boundary: 20 kB
    exactly is kept, because the cost of downloading a logo is a wasted request
    and the cost of dropping a quotation is a lost sale."""
    assert is_body_furniture(_meta(size_bytes=size)) is False


def test_an_unknown_size_is_kept():
    """Gmail always reports a part size, but a missing one must not be read as
    zero — guessing in the discard direction is the one unrecoverable guess."""
    assert is_body_furniture(_meta(size_bytes=None)) is False


# ---------------------------------------------------------------------------
# What enqueue does with each tier
# ---------------------------------------------------------------------------

class _CapturingDb:
    """Enough postgrest chain to capture one insert."""

    def __init__(self):
        self.inserted: list[dict] = []

    def table(self, _name):
        return self

    def insert(self, row):
        self.inserted.append(row)
        return self

    def execute(self):
        return self


@pytest.fixture
def db(monkeypatch):
    fake = _CapturingDb()
    monkeypatch.setattr(email_store, "get_db", lambda: fake)
    return fake


def test_furniture_is_recorded_as_skipped_and_never_queued(db):
    """Recorded, not deleted. The row keeps the email's attachment list honest,
    makes the whole decision reversible with one UPDATE, and still costs no Gmail
    fetch and no bucket upload."""
    queued = email_store.enqueue_attachment("e1", "msg1", _meta())

    assert queued is False, "nothing was queued for download"
    assert len(db.inserted) == 1, "but the attachment is still written down"
    assert db.inserted[0]["processing_status"] == "skipped"
    assert db.inserted[0]["file_name"] == "image001.png"


def test_a_real_document_is_queued_as_pending(db):
    queued = email_store.enqueue_attachment("e1", "msg1", _meta(
        filename="rates.pdf", mime_type="application/pdf",
        size_bytes=255_500, content_id=""))

    assert queued is True
    assert db.inserted[0]["processing_status"] == "pending"


def test_a_part_with_no_attachment_id_is_not_written_at_all(db):
    assert email_store.enqueue_attachment("e1", "msg1", _meta(attachment_id="")) is False
    assert db.inserted == []


# ---------------------------------------------------------------------------
# Drain order: documents before embedded images
# ---------------------------------------------------------------------------

class _QueueDb:
    """A pending queue that answers the worker's two tier queries."""

    def __init__(self, docs, images):
        self._docs, self._images = docs, images
        self._negated = False
        self._images_only = False
        self._limit = None

    def table(self, _name):
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    @property
    def not_(self):
        self._negated = True            # `.not_.like(...)` means NOT LIKE
        return self

    def like(self, _col, _pat):
        self._images_only = not self._negated
        self._negated = False
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        rows = self._images if self._images_only else self._docs
        self._images_only = False       # reset for the next chained query
        return type("R", (), {"data": rows[:self._limit]})


def test_documents_are_drained_before_embedded_images(monkeypatch):
    docs = [{"id": f"d{i}"} for i in range(3)]
    images = [{"id": f"i{i}"} for i in range(200)]
    monkeypatch.setattr(email_store, "get_db", lambda: _QueueDb(docs, images))

    batch = email_store._fetch_pending_batch(10)

    assert [r["id"] for r in batch[:3]] == ["d0", "d1", "d2"]
    assert len(batch) == 10, "the rest of the batch is filled with images"


def test_images_never_crowd_out_a_full_batch_of_documents(monkeypatch):
    """A day of nothing but PDFs must still fill the batch with PDFs."""
    docs = [{"id": f"d{i}"} for i in range(400)]
    monkeypatch.setattr(email_store, "get_db", lambda: _QueueDb(docs, [{"id": "img"}]))

    batch = email_store._fetch_pending_batch(150)

    assert len(batch) == 150
    assert all(r["id"].startswith("d") for r in batch)


def test_an_empty_document_tier_still_drains_images(monkeypatch):
    monkeypatch.setattr(email_store, "get_db",
                        lambda: _QueueDb([], [{"id": "i0"}, {"id": "i1"}]))

    assert [r["id"] for r in email_store._fetch_pending_batch(50)] == ["i0", "i1"]
