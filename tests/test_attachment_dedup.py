"""
One object per file, not one per row.

Every attachment row minted a fresh UUID for its storage path, so identical bytes
arriving twice were uploaded twice. A 426,103-byte animated signature banner ended
up as 5,958 separate objects — 2.5 GB of one file — because it rides on every mail
from one agent. Keying the path on `sha256(bytes)` collapses that to a single
object, and the collision that used to be an error IS the dedup, which is why the
upload passes `upsert`.

`content_id` is stored for a different reason: it is the value `is_body_furniture`
judges on, and it was read off the in-memory part and discarded. No stored row
could be re-judged on the filter's own criterion, so a retrospective prune had to
guess intent from file names.

Both columns come from sql/add_attachment_dedup.sql, and PostgREST rejects a write
naming a column that does not exist — so the migration has to land before this
code. See Documentation/06-deploying.md § Run the SQL.
"""

import hashlib

import pytest

from backend.connectors import email_store
from backend.connectors.email_store import content_address

BANNER = b"\x89PNG\r\n\x1a\n" + b"animated signature banner" * 400


# ---------------------------------------------------------------------------
# The address itself
# ---------------------------------------------------------------------------

def test_the_path_is_the_hash_of_the_bytes():
    path, digest = content_address(BANNER, "banner.png")

    assert digest == hashlib.sha256(BANNER).hexdigest()
    assert path == f"{digest[:2]}/{digest}.png"


def test_identical_bytes_address_the_same_object():
    assert content_address(BANNER, "banner.png") == content_address(BANNER, "banner.png")


def test_the_same_file_under_a_different_name_is_still_one_object():
    """Keyed on the bytes alone, not on `(hash, name)`. One rate sheet forwarded
    as `rates.pdf` from one office and `RATES (1).pdf` from another is one upload;
    each row keeps its own `file_name` for the operator to read."""
    assert content_address(b"rate sheet", "rates.pdf")[0] == \
           content_address(b"rate sheet", "RATES (1).pdf")[0]


def test_one_changed_byte_is_a_different_object():
    """The dedup must never merge two files that differ — an operator opening
    yesterday's quotation and getting today's is worse than paying for storage."""
    assert content_address(b"rate sheet", "r.pdf")[0] != \
           content_address(b"rate sheeu", "r.pdf")[0]


def test_the_extension_is_normalised_and_never_absent():
    """It only exists so a signed URL hints at the type. A path with no suffix at
    all invites a browser to sniff, so an unknown name still gets one."""
    assert content_address(b"x", "SCAN.PDF")[0].endswith(".pdf")
    assert content_address(b"x", "archive.tar.gz")[0].endswith(".gz")
    assert content_address(b"x", "")[0].endswith(".bin")
    assert content_address(b"x", "noextension")[0].endswith(".bin")


def test_the_shard_prefix_is_two_hex_characters():
    """A flat prefix makes the bucket listing unusable at this row count. Lookups
    never list — they read `storage_path` off the row — so this is for humans."""
    path, digest = content_address(BANNER, "banner.png")
    shard, _, name = path.partition("/")

    assert shard == digest[:2] and len(shard) == 2
    assert name.startswith(digest), "the full digest stays in the name"


# ---------------------------------------------------------------------------
# What the worker writes
# ---------------------------------------------------------------------------

class _FakeDb:
    """Enough postgrest + storage chain to capture inserts, updates and uploads."""

    def __init__(self):
        self.inserted: list[dict] = []
        self.updated: list[dict] = []
        self.uploads: list[tuple[str, bytes, dict]] = []
        self._pending_update: dict | None = None

    # --- table(...) chain
    def table(self, _name):
        return self

    def insert(self, row):
        self.inserted.append(row)
        return self

    def update(self, row):
        self._pending_update = row
        return self

    def eq(self, *_a, **_k):
        return self

    def execute(self):
        if self._pending_update is not None:
            self.updated.append(self._pending_update)
            self._pending_update = None
        return self

    # --- storage chain
    @property
    def storage(self):
        return self

    def from_(self, _bucket):
        return self

    def upload(self, path, data, opts):
        self.uploads.append((path, data, opts))


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
        "file_name": "banner.png",
        "mime_type": "image/png",
        "attempts": 0,
    }
    return {**base, **over}


@pytest.fixture
def bytes_from_gmail(monkeypatch):
    """Stub the one network boundary. Returns the setter."""
    def serve(data: bytes):
        monkeypatch.setattr(email_store, "fetch_attachment", lambda *_a: data)
    return serve


def test_a_stored_row_records_both_the_path_and_the_hash(db, bytes_from_gmail):
    """The hash is written as a column as well as encoded in the path, so a
    duplicate census is one `group by` rather than string surgery on paths."""
    bytes_from_gmail(BANNER)
    expected_path, expected_hash = content_address(BANNER, "banner.png")

    assert email_store._download_pending_attachment(_row()) == "stored"

    assert db.updated[0]["storage_path"] == expected_path
    assert db.updated[0]["content_hash"] == expected_hash
    assert db.updated[0]["processing_status"] == "stored"


def test_the_same_banner_on_two_emails_writes_one_object(db, bytes_from_gmail):
    """The whole point. Two rows, two uploads attempted, one destination path."""
    bytes_from_gmail(BANNER)

    email_store._download_pending_attachment(_row(id="att-1"))
    email_store._download_pending_attachment(_row(id="att-2", file_name="banner.png"))

    assert len({path for path, _, _ in db.uploads}) == 1
    assert db.updated[0]["storage_path"] == db.updated[1]["storage_path"], \
        "both rows point at the shared object"
    assert db.updated[0]["content_hash"] == db.updated[1]["content_hash"]


def test_the_upload_upserts_because_a_collision_is_the_dedup_working(db, bytes_from_gmail):
    """Without this the second row 409s on a path that already exists and the
    attachment is marked failed — dedup turning into data loss. It also lets a
    retry after a half-finished upload overwrite instead of erroring."""
    bytes_from_gmail(BANNER)

    email_store._download_pending_attachment(_row())

    assert db.uploads[0][2]["upsert"] == "true"


def test_the_uploaded_content_type_comes_from_the_row(db, bytes_from_gmail):
    """The path's extension is a hint; the content type is what a signed URL
    serves. A row with no mime type must not send `image/png` by accident."""
    bytes_from_gmail(b"%PDF-1.4 rates")

    email_store._download_pending_attachment(_row(file_name="r.pdf", mime_type=""))

    assert db.uploads[0][2]["content-type"] == "application/octet-stream"


def test_different_bytes_never_share_a_path(db, bytes_from_gmail):
    bytes_from_gmail(b"quotation for October")
    email_store._download_pending_attachment(_row(id="a1", file_name="q.pdf"))
    bytes_from_gmail(b"quotation for November")
    email_store._download_pending_attachment(_row(id="a2", file_name="q.pdf"))

    assert db.updated[0]["storage_path"] != db.updated[1]["storage_path"]


def test_a_row_with_no_bytes_is_failed_without_an_upload(db, bytes_from_gmail):
    """No bytes means no hash to key on. Permanent, not transient — Gmail does not
    grow an attachment back — so it fails now rather than burning five attempts."""
    bytes_from_gmail(b"")

    assert email_store._download_pending_attachment(_row()) == "failed"
    assert db.uploads == []
    assert db.updated[0]["processing_status"] == "failed"
    assert "storage_path" not in db.updated[0], "nothing was stored to point at"


# ---------------------------------------------------------------------------
# content_id, persisted at enqueue
# ---------------------------------------------------------------------------

def _meta(**over) -> dict:
    base = {
        "filename": "image001.png",
        "mime_type": "image/png",
        "attachment_id": "ANGjdJ_xyz",
        "size_bytes": 6_400,
        "content_id": "<image001.png@01DC0F.7A2B>",
    }
    return {**base, **over}


def test_the_content_id_the_filter_judged_on_is_written_down(db):
    """Stored so a skipped row can be re-judged on the filter's own criterion. It
    used to be read off the in-memory part and dropped, which left a retrospective
    prune inferring intent from file names."""
    email_store.enqueue_attachment("e1", "msg1", _meta())

    assert db.inserted[0]["content_id"] == "<image001.png@01DC0F.7A2B>"
    assert db.inserted[0]["processing_status"] == "skipped"


def test_a_deliberate_attachment_records_an_empty_content_id(db):
    """Empty rather than null: `is_body_furniture` treats absent and blank alike,
    and a text column with a default keeps `where content_id = ''` honest."""
    email_store.enqueue_attachment("e1", "msg1", _meta(
        filename="rates.pdf", mime_type="application/pdf",
        size_bytes=255_500, content_id=""))

    assert db.inserted[0]["content_id"] == ""
    assert db.inserted[0]["processing_status"] == "pending"


def test_a_whitespace_content_id_is_stored_as_empty(db):
    """Same normalisation the filter applies, so the stored value and the decision
    cannot disagree."""
    email_store.enqueue_attachment("e1", "msg1", _meta(content_id="   "))

    assert db.inserted[0]["content_id"] == ""


def test_a_missing_content_id_key_does_not_break_the_insert(db):
    """Outlook parts come through without the key at all."""
    meta = _meta()
    del meta["content_id"]

    email_store.enqueue_attachment("e1", "msg1", meta)

    assert db.inserted[0]["content_id"] == ""


def test_enqueue_leaves_the_storage_path_empty(db):
    """It cannot be filled in yet — the path is the hash of bytes nobody has
    fetched. The worker writes both columns together when it has them."""
    email_store.enqueue_attachment("e1", "msg1", _meta(content_id=""))

    assert db.inserted[0]["storage_path"] == ""
    assert "content_hash" not in db.inserted[0]


def test_a_synchronous_store_writes_the_hash_and_the_content_id(db, bytes_from_gmail):
    """store_attachment is the backfill script's path (scripts/ingest_window.py).
    It bypasses the queue entirely, so it has to content-address on its own."""
    bytes_from_gmail(BANNER)
    expected_path, expected_hash = content_address(BANNER, "image001.png")

    assert email_store.store_attachment("e1", "msg1", _meta()) is not None

    assert db.inserted[0]["storage_path"] == expected_path
    assert db.inserted[0]["content_hash"] == expected_hash
    assert db.inserted[0]["content_id"] == "<image001.png@01DC0F.7A2B>"
    assert db.uploads[0][0] == expected_path
    assert db.uploads[0][2]["upsert"] == "true"
