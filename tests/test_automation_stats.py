"""
Scan bookkeeping.

`run_scan` returns zeros in three completely different situations — a genuine
empty inbox, a scan already running, and automation switched off. The dashboard
renders all three identically unless `status` distinguishes them, and a skipped
run must never overwrite the last real result.

A tiny fake stands in for the Supabase client: these are the decisions made
around the database call, not the call itself.
"""

from backend.automation.automation import ScanStats, _save_stats


class FakeTable:
    def __init__(self, recorder):
        self._recorder = recorder

    def upsert(self, row, on_conflict=None):
        self._recorder.append(row)
        return self

    def execute(self):
        return self


class FakeDb:
    """Records what would have been written."""

    def __init__(self):
        self.upserts: list[dict] = []

    def table(self, _name):
        return FakeTable(self.upserts)


def test_a_completed_run_is_persisted():
    db = FakeDb()
    _save_stats(db, ScanStats(status="completed", new_emails=3, replies_linked=1))
    assert len(db.upserts) == 1
    assert db.upserts[0]["last_stats"]["new_emails"] == 3


def test_a_skipped_run_is_not_persisted():
    """Otherwise a scheduler tick that collided with a manual run would replace
    a real result with zeros, and the dashboard would report an empty inbox."""
    db = FakeDb()
    for status in ("already_running", "disabled", "error"):
        _save_stats(db, ScanStats(status=status))
    assert db.upserts == []


def test_the_default_status_is_completed():
    # Stats built before the outcome is known must default to the value that
    # gets persisted; the skip paths set their own.
    assert ScanStats().status == "completed"


def test_stats_serialise_to_the_shape_metrics_reads_back():
    db = FakeDb()
    _save_stats(db, ScanStats(status="completed", duration_seconds=32.85, errors=0))
    stored = db.upserts[0]["last_stats"]
    for key in ("run_at", "status", "errors", "duration_seconds"):
        assert key in stored, f"/metrics reads last_scan.{key}"


def test_a_write_failure_does_not_propagate():
    """Losing one run's stats must not fail the scan that produced them — the
    emails were already processed by this point."""
    class Exploding(FakeDb):
        def table(self, _name):
            raise RuntimeError("supabase down")

    _save_stats(Exploding(), ScanStats(status="completed"))  # must not raise


def test_customer_emails_default_is_not_shared_between_instances():
    # A mutable default on a dataclass field is a classic aliasing bug: every
    # scan would accumulate every previous scan's emails.
    first, second = ScanStats(), ScanStats()
    first.customer_emails.append({"id": "a"})
    assert second.customer_emails == []
