"""
Which attachment types earn a Gmail fetch and a bucket upload.

Storage uploads are the largest single line on the Railway bill. Measured over
the 14 days before this filter: 6,212 files and 1,401 MB uploaded, of which
images were 4,798 files and 832 MB, against a `quotations` table holding zero
rows. Nothing machine-reads an attachment, so an image's only consumer is a
human clicking a signed URL, and that is not worth 832 MB a fortnight.

The rule is an allow-list on type. It deliberately REVERSES both image tiers in
test_attachment_tiers.py, which kept a deliberately-attached image at any size
and an embedded one over 50 kB. Those tiers still exist and still behave as their
tests say; they are simply no longer the last word. Both facts are pinned here so
neither can be changed by accident.
"""

import pytest

from backend.connectors import email_store
from backend.connectors.email_store import (
    STORED_EXTENSIONS,
    STORED_MIME_TYPES,
    is_stored_type,
    should_fetch_bytes,
)


def _meta(**over) -> dict:
    """Gmail part metadata, defaulting to a plainly-attached PDF."""
    base = {
        "filename": "rates.pdf",
        "mime_type": "application/pdf",
        "attachment_id": "ANGjdJ_xyz",
        "size_bytes": 255_500,
        "content_id": "",
    }
    return {**base, **over}


# --- types we keep -----------------------------------------------------------

@pytest.mark.parametrize("name,mime", [
    ("rates.pdf", "application/pdf"),
    ("Q3 rates.xlsx",
     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ("old rates.xls", "application/vnd.ms-excel"),
    ("macro rates.xlsm", "application/vnd.ms-excel.sheet.macroenabled.12"),
    ("BMCT FORM11.csv", "text/csv"),
    ("terms.doc", "application/msword"),
    ("terms.docx",
     "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("June 2026 SOA.eml", "message/rfc822"),
    ("forwarded.msg", "application/vnd.ms-outlook"),
])
def test_a_document_type_is_fetched(name, mime):
    assert should_fetch_bytes(_meta(filename=name, mime_type=mime)) is True


def test_an_eml_is_kept_because_it_carries_its_own_attachments():
    """Neither PDF nor spreadsheet, but discarding the forward discards the rate
    card inside it. The real ones on file are Proformas and Debit Notes."""
    assert should_fetch_bytes(_meta(
        filename="DEBIT NOTE // SOB - EN001151.eml",
        mime_type="message/rfc822", size_bytes=3_600_000)) is True


# --- types we now refuse ----------------------------------------------------

@pytest.mark.parametrize("size", [900, 60_000, 5_081_000])
def test_an_attached_image_is_refused_at_every_size(size):
    """The reversal. `is_body_furniture` calls this one keep-worthy because it has
    no Content-ID; type refuses it anyway."""
    meta = _meta(filename="image002.png", mime_type="image/png",
                 size_bytes=size, content_id="")
    assert email_store.is_body_furniture(meta) is False, "intent tier keeps it"
    assert should_fetch_bytes(meta) is False, "type gate must still refuse it"


def test_a_large_embedded_image_is_refused():
    """Reverses tier 2: a pasted rate table over 50 kB used to be fetched."""
    meta = _meta(filename="image003.gif", mime_type="image/gif",
                 size_bytes=597_000, content_id="<image003@01DC.7A2B>")
    assert should_fetch_bytes(meta) is False


@pytest.mark.parametrize("name,mime", [
    ("logo.png", "image/png"),
    ("scan.jpg", "image/jpeg"),
    ("banner.gif", "image/gif"),
    ("photo.webp", "image/webp"),
    ("deck.ppsx", "application/vnd.ms-powerpoint"),
    ("bundle.zip", "application/zip"),
    ("notes.rtf", "application/rtf"),
    ("mystery", "application/octet-stream"),
])
def test_an_unwanted_type_is_refused(name, mime):
    assert should_fetch_bytes(_meta(filename=name, mime_type=mime)) is False


# --- either field may be the one that is right ------------------------------

def test_a_correct_mime_rescues_a_missing_extension():
    """53 files in the last fortnight had no extension at all."""
    assert is_stored_type({"filename": "scan0001",
                           "mime_type": "application/pdf"}) is True


def test_a_correct_extension_rescues_a_generic_mime():
    """Gmail reports application/octet-stream for plenty of real PDFs, so
    requiring the mime type would discard documents."""
    assert is_stored_type({"filename": "rates.pdf",
                           "mime_type": "application/octet-stream"}) is True


def test_neither_field_matching_is_refused():
    assert is_stored_type({"filename": "img", "mime_type": "image/png"}) is False


def test_matching_ignores_case_and_mime_parameters():
    assert is_stored_type({"filename": "RATES.XLSX", "mime_type": ""}) is True
    assert is_stored_type({"filename": "x", "mime_type": "TEXT/CSV; charset=utf-8"}) is True


def test_a_missing_or_empty_meta_is_refused_not_crashed():
    for meta in ({}, {"filename": None, "mime_type": None}, {"filename": ""}):
        assert is_stored_type(meta) is False


# --- the two gates are independent ------------------------------------------

def test_a_small_embedded_pdf_is_still_fetched():
    """`is_body_furniture` only ever judges images, so an embedded PDF must not be
    caught by it. A 1.9 kB payment PDF is a real document."""
    meta = _meta(filename="EBANKGO22154677.pdf", mime_type="application/pdf",
                 size_bytes=1_900, content_id="<doc@01DC.7A2B>")
    assert should_fetch_bytes(meta) is True


# --- what enqueue writes ----------------------------------------------------

class _CapturingDb:
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


def test_a_refused_type_is_still_written_down_as_skipped(db):
    """Recorded, not deleted. The row keeps the email's attachment list honest and
    makes the whole policy reversible with one UPDATE, while costing no Gmail
    fetch and no upload. This is what makes the filter safe to be wrong about."""
    queued = email_store.enqueue_attachment("e1", "msg1", _meta(
        filename="image002.png", mime_type="image/png", size_bytes=5_081_000))

    assert queued is False
    assert len(db.inserted) == 1
    assert db.inserted[0]["processing_status"] == "skipped"
    assert db.inserted[0]["file_name"] == "image002.png"
    assert db.inserted[0]["storage_path"] == "", "no bytes were fetched"


def test_a_kept_type_is_queued_as_pending(db):
    assert email_store.enqueue_attachment("e1", "msg1", _meta()) is True
    assert db.inserted[0]["processing_status"] == "pending"


# --- pin the policy ---------------------------------------------------------

def test_the_allow_lists_are_exactly_this():
    """Pinned so widening the policy is a deliberate act with a diff, rather than
    something that drifts back to storing every image."""
    assert STORED_EXTENSIONS == {
        "pdf", "xlsx", "xls", "xlsm", "csv", "doc", "docx", "eml", "msg",
    }
    assert STORED_MIME_TYPES == {
        "application/pdf",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel.sheet.macroenabled.12",
        "text/csv",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "message/rfc822",
        "application/vnd.ms-outlook",
    }
    assert not any(m.startswith("image/") for m in STORED_MIME_TYPES), \
        "no image type may be added here without reckoning with the egress cost"
