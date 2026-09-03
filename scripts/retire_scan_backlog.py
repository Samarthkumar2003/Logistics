"""
retire_scan_backlog.py
----------------------
Retire old unprocessed emails from the scan queue.

The scan is FIFO over `received_at` and takes SCAN_BATCH (50) rows every 5
minutes — a 600/hour ceiling. That is sized for a day's mail, not for history.
When a bad ingest pulls the mailbox in bulk, every one of those rows lands with
`processed_at IS NULL` and is served *before* today's mail, because it was
received earlier. On 2026-08-18 two runaway sweeps (a fail-open watermark read
returning None, so the Gmail `after:` filter was dropped) inserted ~10,600
historical rows, and an RFQ reply that arrived at 14:11 was still queued behind
2,605 of them hours later. The reply was not lost and nothing had failed — it had
simply not been reached.

Retiring means stamping `processed_at`, which is precisely what
sql/setup_scan_state.sql already does for the pre-existing backlog when the
column is first added:

    update emails set processed_at = now() where processed_at is null;

This is that operation, scoped by date. Nothing is deleted: bodies, attachments,
classifications and the inbox UI are untouched, because outside automation.py
(and the `scan_backlog` metric) nothing reads `processed_at`. It is reversible —
set the column back to NULL for the same window to requeue.

What retirement DOES forgo, per row:
  general               nothing. The scan counts it and moves on.
  customer_requirement  nothing durable. The scan records it into that run's
                        stats only; RFQs are never sent automatically. The
                        dashboard reads `classification`, not `processed_at`.
  quotation_rate_card   the reply-to-job link. This is the one real loss, so
                        --commit refuses to run while any rate card in the
                        window cites an RFQ reference that has a job. Check
                        first, retire second.

Usage:
    python scripts/retire_scan_backlog.py                  # dry run, the default
    python scripts/retire_scan_backlog.py --commit
    python scripts/retire_scan_backlog.py --before 2026-08-01T00:00:00+00:00
"""

import argparse
import logging
from datetime import datetime, time, timezone
from typing import Any, Optional

from backend.core.db import get_db
from backend.core.logging_config import configure_logging
from backend.core.rfq_reference import extract_rfq_reference

logger = logging.getLogger(__name__)

# Rows per UPDATE. Small enough to keep any single statement short-lived, large
# enough that a few thousand rows is a handful of round trips.
UPDATE_CHUNK = 500
# How far into a body to look for a reference — matches reply_service.
BODY_SCAN_CHARS = 2000


def _today_utc_midnight() -> datetime:
    return datetime.combine(datetime.now(timezone.utc).date(), time.min,
                            tzinfo=timezone.utc)


def _fetch_window(cutoff: str) -> list[dict[str, Any]]:
    """Every unprocessed email received before `cutoff`, paged out in full.

    Metadata only. Selecting `body` here asks Supabase for a few thousand full
    email bodies in 1000-row pages, which reliably ends in `httpx.ReadTimeout`
    (and, worse, a partially-read response whose `.data` is an unparsed string).
    Bodies are needed for the rate-card guard alone — dozens of rows, fetched
    separately in _linkable_rate_cards.
    """
    db, rows, page = get_db(), [], 0
    while True:
        batch = (
            db.table("emails")
            .select("id, provider_msg_id, received_at, classification")
            .is_("processed_at", "null")
            .lt("received_at", cutoff)
            .order("received_at")
            .range(page * 1000, page * 1000 + 999)
            .execute().data or []
        )
        rows.extend(batch)
        if len(batch) < 1000:
            return rows
        page += 1


def _linkable_rate_cards(cutoff: str) -> list[dict[str, str]]:
    """Rate cards in the window whose cited reference has a job row.

    These are the only rows whose retirement would lose something recoverable, so
    they are the guard on --commit rather than a warning to scroll past. Queried
    separately from the window so only rate-card bodies cross the wire.
    """
    cards = (
        get_db().table("emails")
        .select("provider_msg_id, subject, body")
        .is_("processed_at", "null")
        .eq("classification", "quotation_rate_card")
        .lt("received_at", cutoff)
        .order("received_at")
        .execute().data or []
    )

    cited = {}
    for r in cards:
        ref = (extract_rfq_reference(r.get("subject") or "")
               or extract_rfq_reference((r.get("body") or "")[:BODY_SCAN_CHARS]))
        if ref:
            cited[r["provider_msg_id"]] = ref

    logger.info("Rate cards in the window: %d, of which %d cite a reference",
                len(cards), len(cited))
    if not cited:
        return []

    refs = sorted(set(cited.values()))
    present: set[str] = set()
    db = get_db()
    for i in range(0, len(refs), 100):
        found = (
            db.table("rfq_jobs").select("reference")
            .in_("reference", refs[i:i + 100]).execute().data or []
        )
        present.update(f["reference"] for f in found if f.get("reference"))

    return [{"provider_msg_id": msg_id, "reference": ref}
            for msg_id, ref in cited.items() if ref in present]


def _retire(row_ids: list[str], stamp: str) -> int:
    """Stamp processed_at on these rows. Returns how many the DB confirmed."""
    db, updated = get_db(), 0
    for i in range(0, len(row_ids), UPDATE_CHUNK):
        chunk = row_ids[i:i + UPDATE_CHUNK]
        # processing_error is deliberately left alone: a retirement is not a
        # failure, and that column is the signal for one.
        result = (
            db.table("emails").update({"processed_at": stamp})
            .in_("id", chunk).execute()
        )
        updated += len(result.data or [])
        logger.info("Retired %d/%d", updated, len(row_ids))
    return updated


def _summarise(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        label = r.get("classification") or "(none)"
        counts[label] = counts.get(label, 0) + 1
    return counts


def main(before: Optional[str], commit: bool) -> int:
    cutoff = before or _today_utc_midnight().isoformat()
    rows = _fetch_window(cutoff)
    logger.info("Unprocessed and received before %s: %d row(s) — %s",
                cutoff, len(rows), _summarise(rows))

    if not rows:
        return 0

    linkable = _linkable_rate_cards(cutoff)
    if linkable:
        # Refuse rather than retire: the link is the one thing that cannot be
        # rebuilt by re-reading the mailbox.
        logger.error(
            "%d rate card(s) in this window cite an RFQ that HAS a job — "
            "retiring them would drop those links. Let the scan process them "
            "first (or narrow --before). Offending rows: %s",
            len(linkable), linkable,
        )
        return 1

    if not commit:
        logger.info("Dry run. Re-run with --commit to stamp processed_at on these "
                    "%d row(s). Nothing is deleted; set the column back to NULL "
                    "for this window to requeue.", len(rows))
        return 0

    stamp = datetime.now(timezone.utc).isoformat()
    updated = _retire([r["id"] for r in rows], stamp)
    logger.info("Done. Retired %d of %d row(s) at %s. Today's mail is now at the "
                "front of the scan queue.", updated, len(rows), stamp)
    return 0


if __name__ == "__main__":
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", default=None,
                        help="ISO timestamp; default is today 00:00 UTC")
    parser.add_argument("--commit", action="store_true",
                        help="actually write; omit for a dry run")
    args = parser.parse_args()
    raise SystemExit(main(before=args.before, commit=args.commit))
