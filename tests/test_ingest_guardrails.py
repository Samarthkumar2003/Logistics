"""
Bounds that used to disappear when nobody was looking.

The watermark incident (see test_email_store_watermark.py) had a shape worth
naming: a limit expressed as an optional value, where "absent" silently meant
"no limit". A sweep that should have listed 268 ids listed 30,500.

An audit for more of the same class found four, and this file pins each one. They
are not variations on a theme — each fails in its own way:

  * `count_inbox_messages` — optional bounds. None meant "omit that filter",
    so calling it bare counted the whole mailbox with no ceiling at all.
  * `_ingest_new_emails` — the cap measured the WRONG QUANTITY. It bounded mail
    we lack, so a window full of mail we already have never tripped it.
  * `heal_sync_gaps` / `backfill_window` — two caps that MULTIPLIED, and a
    guardrail comparing a count subtraction against a set difference.
  * `backfill_classifications` — a missing circuit breaker, on a startup job
    that spends money per row.

Every test here asks the same question: when the input is degenerate, does the
bound still hold, or does it evaporate?
"""

from datetime import datetime, timezone

import pytest

from backend.connectors import email_store, gmail_connector

WM = datetime(2026, 8, 17, 13, 6, 24, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# count_inbox_messages: bounds are required, not optional
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("after,before", [
    (None, None),
    (None, 1_755_000_000),
    (1_755_000_000, None),
])
def test_counting_without_both_bounds_raises_instead_of_counting_everything(
    monkeypatch, after, before,
):
    """The old signature defaulted both to None and treated None as "skip that
    side of the filter", so `count_inbox_messages()` built no query at all and
    summed page lengths across the entire mailbox — with no ceiling, unlike
    fetch_messages_since. Refuse the question rather than answer it expensively."""
    def _must_not_page(*_a, **_k):
        raise AssertionError("Gmail must not be paged without both bounds")

    monkeypatch.setattr(gmail_connector, "iter_message_id_pages", _must_not_page)

    with pytest.raises(ValueError, match="requires both"):
        gmail_connector.count_inbox_messages(after, before)


def test_counting_a_day_window_asks_gmail_for_exactly_that_window(monkeypatch):
    """The healthy path: one bounded query, and the count is the sum of pages."""
    seen = {}

    def _pages(query=None, **_k):
        seen["query"] = query
        return iter([["a", "b"], ["c"]])

    monkeypatch.setattr(gmail_connector, "iter_message_id_pages", _pages)

    assert gmail_connector.count_inbox_messages(1_755_000_000, 1_755_086_400) == 3
    assert seen["query"] == "after:1755000000 before:1755086400"


# ---------------------------------------------------------------------------
# Ingest: cap the ids SWEPT, not just the ids that turn out to be new
# ---------------------------------------------------------------------------

@pytest.fixture
def wide_window(monkeypatch):
    """A readable but far-lagging watermark against a mailbox we already hold.

    `_existing_provider_ids` returns every id back, so `unknown_ids` never grows
    and MAX_INGEST_BATCH can never trip. That is the whole point: the old loop
    had no other exit and paged the window to its end.
    """
    rig = {"pages_served": 0, "advanced": []}

    def _pages(after_epoch=None, **_k):
        def _gen():
            while True:                      # a mailbox that never runs out
                rig["pages_served"] += 1
                yield [f"id-{rig['pages_served']}-{n}" for n in range(500)]
        return _gen()

    monkeypatch.setattr(email_store, "_get_watermark", lambda _p: WM)
    monkeypatch.setattr(email_store, "iter_message_id_pages", _pages)
    monkeypatch.setattr(email_store, "_existing_provider_ids", lambda page: set(page))
    monkeypatch.setattr(email_store, "_max_received_at", lambda: None)
    monkeypatch.setattr(email_store, "_advance_watermark",
                        lambda provider, when: rig["advanced"].append(when))
    monkeypatch.setattr(email_store, "_ingest_id_list",
                        lambda ids, provider: {"new": 0, "attachments": 0,
                                               "fetched": 0, "newest_seen": None})
    return rig


def test_a_sweep_full_of_already_ingested_mail_stops_at_the_id_ceiling(wide_window):
    """MAX_INGEST_BATCH counts mail we lack. With 0 unknown ids it never fires, so
    before MAX_SWEEP_IDS existed this paged all 195k ids — ~390 sequential Gmail
    requests — every five minutes."""
    stats = email_store.ingest_new_emails()

    assert stats["sweep_capped"] is True
    assert stats["swept"] == email_store.MAX_SWEEP_IDS
    assert wide_window["pages_served"] == email_store.MAX_SWEEP_IDS // 500


def test_the_id_ceiling_does_not_block_the_watermark_from_advancing(
    monkeypatch, wide_window,
):
    """The cap must not borrow `truncated`, which suppresses the _max_received_at
    promotion. With 0 unknown ids there is no `newest_seen`, so that promotion is
    the ONLY way a lagging watermark moves — suppressing it would re-page the cap
    every five minutes forever, a permanent stall rather than a bounded cost.

    `newest` sits well clear of the watermark on purpose: the promotion advances to
    `newest - WATERMARK_LOOKBACK`, so a stored-newest less than a day ahead cannot
    move the floor at all. That is the real scenario too — this state means the
    watermark is lagging badly while the DB holds current mail."""
    newest = datetime(2026, 8, 25, 9, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(email_store, "_max_received_at", lambda: newest)

    email_store.ingest_new_emails()

    assert wide_window["advanced"] == [newest - email_store.WATERMARK_LOOKBACK]


def test_a_normal_sweep_is_untouched_by_the_ceiling(monkeypatch):
    """Guard against the cap firing on healthy traffic: a couple of pages, done."""
    rig = {"advanced": []}
    monkeypatch.setattr(email_store, "_get_watermark", lambda _p: WM)
    monkeypatch.setattr(email_store, "iter_message_id_pages",
                        lambda after_epoch=None, **_k: iter([["a", "b"], []]))
    monkeypatch.setattr(email_store, "_existing_provider_ids", lambda page: set())
    monkeypatch.setattr(email_store, "_max_received_at", lambda: None)
    monkeypatch.setattr(email_store, "_advance_watermark",
                        lambda p, when: rig["advanced"].append(when))
    monkeypatch.setattr(email_store, "_ingest_id_list",
                        lambda ids, provider: {"new": len(ids), "attachments": 0,
                                               "fetched": len(ids), "newest_seen": None})

    stats = email_store.ingest_new_emails()

    assert "sweep_capped" not in stats
    assert stats["swept"] == 2


# ---------------------------------------------------------------------------
# backfill_window: cap the pull where the real quantity is finally known
# ---------------------------------------------------------------------------

@pytest.fixture
def window_rig(monkeypatch):
    """A day window holding `listed` ids, none of them stored yet."""
    rig = {"pulled": []}

    def _install(listed):
        monkeypatch.setattr(email_store, "iter_message_id_pages",
                            lambda query=None, **_k: iter([listed, []]))
        monkeypatch.setattr(email_store, "_existing_provider_ids", lambda ids: set())

        def _ingest(ids, provider):
            rig["pulled"] = list(ids)
            return {"new": len(ids), "attachments": 0, "fetched": len(ids),
                    "newest_seen": None}

        monkeypatch.setattr(email_store, "_ingest_id_list", _ingest)
    _install.rig = rig
    return _install


def test_a_day_is_clipped_to_max_new_and_says_so(window_rig):
    """The guardrail upstream reasons about the audit's `missing` estimate; the
    real number is only known here. Clipping here is what makes the estimate's
    accuracy stop mattering."""
    window_rig([f"m{n}" for n in range(50)])

    result = email_store.backfill_window(1_755_000_000, 1_755_086_400, max_new=10)

    assert result["truncated"] is True
    assert result["new"] == 10
    assert len(window_rig.rig["pulled"]) == 10


def test_clipping_keeps_the_newest_ids_so_progress_is_monotonic(window_rig):
    """Gmail lists newest-first and the pull is reversed to oldest-first for the
    thread rule. A clipped day must keep the NEWEST unknowns, or repeated runs
    re-pull the same tail and never converge."""
    window_rig(["new1", "new2", "old1", "old2"])

    email_store.backfill_window(1_755_000_000, 1_755_086_400, max_new=2)

    assert window_rig.rig["pulled"] == ["new2", "new1"]   # reversed to oldest-first


def test_an_uncapped_window_still_pulls_everything(window_rig):
    """The manual scripts call this with an explicit date range and no cap; that
    is a deliberate human decision and must keep working."""
    window_rig([f"m{n}" for n in range(30)])

    result = email_store.backfill_window(1_755_000_000, 1_755_086_400)

    assert "truncated" not in result
    assert result["new"] == 30


# ---------------------------------------------------------------------------
# heal_sync_gaps: the two old caps multiplied
# ---------------------------------------------------------------------------

@pytest.fixture
def heal_rig(monkeypatch):
    """Drive the heal loop off a fixed gap list; record each day it backfills."""
    rig = {"backfilled": [], "alerted": []}

    def _install(gaps, per_day_new=None):
        monkeypatch.setattr(email_store, "audit_sync_gaps", lambda days=14: gaps)
        monkeypatch.setattr(email_store, "count_inbox_messages",
                            lambda after_epoch_s=None, before_epoch_s=None: 0)
        monkeypatch.setattr(email_store, "get_db",
                            lambda: (_ for _ in ()).throw(RuntimeError("no db in test")))
        monkeypatch.setattr(email_store, "_alert_sync_drift",
                            lambda gaps, healed: rig["alerted"].append((len(gaps), healed)))

        def _backfill(after, before, provider="gmail", max_new=None):
            rig["backfilled"].append({"after": after, "max_new": max_new})
            pulled = per_day_new if per_day_new is not None else 0
            if max_new is not None:
                pulled = min(pulled, max_new)
            return {"listed": pulled, "new": pulled, "attachments": 0}

        monkeypatch.setattr(email_store, "backfill_window", _backfill)
    _install.rig = rig
    return _install


def _gaps(n, missing):
    return [{"day": f"2026-08-{d:02d}", "gmail": missing, "db": 0, "missing": missing}
            for d in range(1, n + 1)]


def test_many_modest_days_are_refused_because_the_caps_multiply(heal_rig):
    """14 days x 1000 missing cleared both old guardrails while nothing looked at
    the product: 14,000 Gmail fetches and 14,000 LLM classifications, unattended,
    on a 24-hour timer. The total check is what closes that."""
    heal_rig(_gaps(14, 1000))

    result = email_store.heal_sync_gaps(days=14)

    assert result["healed"] == 0
    assert heal_rig.rig["backfilled"] == []          # nothing pulled
    assert heal_rig.rig["alerted"] == [(14, False)]  # a human hears about it


def test_a_gap_within_the_total_budget_is_still_healed_automatically(heal_rig):
    """The point is a budget, not a freeze. Ordinary drift must still self-heal."""
    heal_rig(_gaps(3, 100), per_day_new=100)

    result = email_store.heal_sync_gaps(days=14)

    assert len(heal_rig.rig["backfilled"]) == 3
    assert result["gaps_found"] == 3


def test_every_day_is_capped_at_the_per_day_limit_not_just_the_total(heal_rig):
    """Each day carries max_new, so a day whose true unknown count dwarfs its
    estimate cannot overshoot on its own."""
    heal_rig(_gaps(2, 100), per_day_new=100)

    email_store.heal_sync_gaps(days=14)

    caps = [b["max_new"] for b in heal_rig.rig["backfilled"]]
    assert all(c <= email_store.AUTO_HEAL_MAX_MISSING_PER_DAY for c in caps)


def test_a_lying_estimate_cannot_spend_past_the_budget(heal_rig, monkeypatch):
    """The upfront total check trusts `missing`, a count subtraction that can
    understate the set difference badly (archived rows inflate the DB count). So
    the budget is charged for mail actually PULLED, and days past it are deferred
    rather than silently attempted."""
    monkeypatch.setattr(email_store, "AUTO_HEAL_MAX_TOTAL_MISSING", 250)
    # Each day claims 40 missing — 240 total, so the upfront check waves it through
    # — while really holding 100 unknown ids each. 600 pulled if nothing stopped it.
    heal_rig(_gaps(6, 40), per_day_new=100)

    result = email_store.heal_sync_gaps(days=14)

    assert len(heal_rig.rig["backfilled"]) == 3      # 3 x 100 exhausts 250
    assert len(result["deferred"]) == 3
    assert [d["day"] for d in result["deferred"]] == ["2026-08-04", "2026-08-05",
                                                      "2026-08-06"]


def test_no_gaps_is_a_clean_no_op(heal_rig):
    heal_rig([])
    assert email_store.heal_sync_gaps(days=14) == {
        "gaps_found": 0, "healed": 0, "unhealed": [], "deferred": [],
    }


# ---------------------------------------------------------------------------
# backfill_classifications: the missing circuit breaker
# ---------------------------------------------------------------------------

@pytest.fixture
def classify_rig(monkeypatch):
    """`rows` uncached emails; record every batch handed to the classifier."""
    rig = {"batches": []}

    def _install(count, outcome="ok"):
        rows = [{"provider_msg_id": f"m{n}", "subject": "s", "body": "b",
                 "sender": "x@y.com"} for n in range(count)]

        class _Q:
            def table(self, name):
                self._t = name
                return self

            def select(self, *_a, **_k):
                return self

            def order(self, *_a, **_k):
                return self

            def limit(self, *_a, **_k):
                return self

            def in_(self, *_a, **_k):
                return self

            def neq(self, *_a, **_k):
                return self

            def execute(self):
                data = rows if self._t == "emails" else []
                return type("_R", (), {"data": data})()

        monkeypatch.setattr(email_store, "get_db", lambda: _Q())

        def _classify(batch):
            rig["batches"].append([e["id"] for e in batch])
            method = "error" if outcome == "error" else "rules"
            return {e["id"]: {"label": "general", "method": method} for e in batch}

        monkeypatch.setattr(email_store, "classify_with_cache", _classify)
    _install.rig = rig
    return _install


def test_a_provider_outage_stops_the_backfill_after_one_batch(classify_rig):
    """This runs on STARTUP. Without a breaker it ground through all 500 rows in
    chunks of 20 while every LLM call failed — 25 doomed batches per boot, and
    `--reload` charges for that on every file save. Its sibling
    retry_pending_classifications already had this check."""
    classify_rig(100, outcome="error")

    result = email_store.backfill_classifications(batch_size=20)

    assert len(classify_rig.rig["batches"]) == 1
    assert result["backfilled"] == 20


def test_a_healthy_provider_still_backfills_every_batch(classify_rig):
    """The breaker must trip on total failure only, never on ordinary work."""
    classify_rig(100)

    result = email_store.backfill_classifications(batch_size=20)

    assert len(classify_rig.rig["batches"]) == 5
    assert result["backfilled"] == 100
