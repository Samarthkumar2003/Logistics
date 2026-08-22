"""
What incremental ingest does when it cannot trust its own floor.

The watermark is the only thing bounding a sweep. `_get_watermark` used to
swallow every exception and return None, which is also what it returns on a
first-ever run — so a transient Supabase "Server disconnected" was
indistinguishable from a brand-new account. The `after:` filter was dropped and
one 5-minute tick became a full-mailbox crawl: 30,500 ids listed, 5,279 "new",
_ingest_lock held for hours, the attachment worker yielding every run, and new
mail arriving on the frontend an hour late. Worse, the BOUNDED LOAD ceiling then
advanced the watermark past everything the run had not pulled — destroying the
position that was only ever unreadable.

So: unknown and none are now different states, and neither sweeps unbounded.
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.connectors import email_store
from backend.connectors.email_store import WatermarkUnavailable

WM = datetime(2026, 8, 17, 13, 6, 24, tzinfo=timezone.utc)


class _FakeQuery:
    """Minimal postgrest chain: .table().select().eq().execute().data"""

    def __init__(self, outcome):
        self._outcome = outcome

    def table(self, _name):
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def gte(self, *_a, **_k):
        return self

    def lt(self, *_a, **_k):
        return self

    def upsert(self, *_a, **_k):
        return self

    def execute(self):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return type("_Result", (), {"data": self._outcome})()


@pytest.fixture
def db(monkeypatch):
    """Install a sync_state read outcome: a row list, or an exception to raise."""
    def _install(outcome):
        calls = []

        def _get_db():
            calls.append(1)
            return _FakeQuery(outcome)

        monkeypatch.setattr(email_store, "get_db", _get_db)
        return calls
    return _install


@pytest.fixture
def ingest_rig(monkeypatch):
    """Stub every boundary the ingest touches; report what it reached for."""
    rig = {"listed": [], "advanced": [], "ingested": []}

    def _pages(after_epoch=None, **_kwargs):
        rig["listed"].append(after_epoch)
        return iter([])

    monkeypatch.setattr(email_store, "iter_message_id_pages", _pages)
    monkeypatch.setattr(email_store, "_existing_provider_ids", lambda page: set())
    monkeypatch.setattr(email_store, "_max_received_at", lambda: None)
    monkeypatch.setattr(email_store, "_advance_watermark",
                        lambda provider, when: rig["advanced"].append(when))

    def _ingest_list(ids, provider):
        rig["ingested"].append(list(ids))
        return {"new": 0, "attachments": 0, "fetched": 0, "newest_seen": None}

    monkeypatch.setattr(email_store, "_ingest_id_list", _ingest_list)
    return rig


# ---------------------------------------------------------------------------
# _get_watermark: unknown, none, and known are three different answers
# ---------------------------------------------------------------------------

def test_a_failed_read_raises_rather_than_reporting_no_watermark(db, no_sleep):
    db(RuntimeError("Server disconnected"))

    with pytest.raises(WatermarkUnavailable) as exc:
        email_store._get_watermark("gmail")

    assert "Server disconnected" in str(exc.value)


def test_a_missing_row_still_means_no_watermark(db):
    db([])
    assert email_store._get_watermark("gmail") is None


def test_a_null_timestamp_means_no_watermark(db):
    db([{"last_received_at": None}])
    assert email_store._get_watermark("gmail") is None


def test_a_recorded_watermark_is_parsed_as_an_aware_datetime(db):
    db([{"last_received_at": "2026-08-17T13:06:24Z"}])
    assert email_store._get_watermark("gmail") == WM


def test_a_transient_read_is_retried_before_giving_up(monkeypatch, no_sleep):
    """The disconnect that caused this was a one-off network blip. Retrying is
    cheaper than skipping a whole 5-minute tick."""
    attempts = []

    def _get_db():
        attempts.append(1)
        if len(attempts) < 3:
            return _FakeQuery(RuntimeError("Server disconnected"))
        return _FakeQuery([{"last_received_at": "2026-08-17T13:06:24Z"}])

    monkeypatch.setattr(email_store, "get_db", _get_db)

    assert email_store._get_watermark("gmail") == WM
    assert len(attempts) == 3


# ---------------------------------------------------------------------------
# Ingest fails closed — the regression
# ---------------------------------------------------------------------------

def test_an_unreadable_watermark_skips_the_run_without_listing_anything(
    monkeypatch, ingest_rig,
):
    def _boom(_provider):
        raise WatermarkUnavailable("watermark read failed for gmail: Server disconnected")

    monkeypatch.setattr(email_store, "_get_watermark", _boom)

    stats = email_store.ingest_new_emails()

    assert stats["skipped_watermark_unavailable"] is True
    assert stats["new"] == 0 and stats["swept"] == 0
    assert ingest_rig["listed"] == []      # never touched Gmail
    assert ingest_rig["advanced"] == []    # never moved the floor it could not read


def test_no_recorded_watermark_does_not_sweep_the_whole_mailbox(monkeypatch, ingest_rig):
    """`after_epoch=None` lists every message in the mailbox — 195k here. The
    scheduled job must never do that."""
    monkeypatch.setattr(email_store, "_get_watermark", lambda _p: None)

    stats = email_store.ingest_new_emails()

    assert stats["skipped_watermark_unset"] is True
    assert ingest_rig["listed"] == []
    assert ingest_rig["ingested"] == []


def test_no_recorded_watermark_seeds_a_floor_so_the_next_run_can_proceed(
    monkeypatch, ingest_rig,
):
    """Skipping forever is its own outage: nothing else seeds sync_state, so a
    fresh install would never ingest."""
    monkeypatch.setattr(email_store, "_get_watermark", lambda _p: None)

    email_store.ingest_new_emails()

    assert len(ingest_rig["advanced"]) == 1
    seeded = ingest_rig["advanced"][0]
    expected = datetime.now(timezone.utc) - email_store.WATERMARK_LOOKBACK
    assert abs((seeded - expected).total_seconds()) < 60


def test_a_known_watermark_still_bounds_the_sweep_with_an_after_filter(
    monkeypatch, ingest_rig,
):
    """The healthy path, unchanged: ~268 ids listed, not 30,500."""
    monkeypatch.setattr(email_store, "_get_watermark", lambda _p: WM)

    stats = email_store.ingest_new_emails()

    assert ingest_rig["listed"] == [int(WM.timestamp())]
    assert "skipped_watermark_unavailable" not in stats
    assert "skipped_watermark_unset" not in stats


# ---------------------------------------------------------------------------
# The audit is not safety-critical, so it degrades instead of skipping
# ---------------------------------------------------------------------------

def test_the_gap_audit_survives_an_unreadable_watermark(monkeypatch):
    """It only uses the watermark to widen its window, so a failed read costs
    coverage, not correctness — the audit still runs on its default window."""
    def _boom(_provider):
        raise WatermarkUnavailable("watermark read failed for gmail: Server disconnected")

    monkeypatch.setattr(email_store, "_get_watermark", _boom)
    windows = []

    def _count(after_epoch_s=None, before_epoch_s=None):
        windows.append((after_epoch_s, before_epoch_s))
        return 0

    monkeypatch.setattr(email_store, "count_inbox_messages", _count)
    monkeypatch.setattr(email_store, "get_db",
                        lambda: _FakeQuery(RuntimeError("Server disconnected")))

    assert email_store.audit_sync_gaps(days=3) == []
    assert len(windows) == 3   # the default floor, not a widened crawl
