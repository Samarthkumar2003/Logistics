"""
retire_inline_attachment_backlog.py
-----------------------------------
Retire body furniture from the attachment download queue.

Ingest enqueues attachment metadata and a worker downloads the bytes, 150 rows
every two minutes. `email_store.is_body_furniture` now keeps that budget for
documents: an image carrying a Content-ID and weighing less than
INLINE_IMAGE_MIN_BYTES (20 kB) is a signature logo, an icon, a spacer or a
tracking pixel, and is recorded as `skipped` instead of queued.

Rows enqueued *before* that filter existed have no Content-ID recorded — the
header was not captured then, and re-fetching every message to learn it would
cost more Gmail calls than the downloads it saves. So the backlog uses the
observable proxy for the same population: `mime_type like 'image/%'` and
`size_bytes < 20000`. On 2026-08-19 that was 25,484 of 36,205 pending rows —
70% of the queue for ~3% of its bytes, all of it served *ahead* of vendor rate
cards because the queue drained oldest-first.

Nothing is deleted. This only moves `processing_status` from `pending` to
`skipped`, and no bytes were ever downloaded for these rows, so there is no
bucket object to lose. To requeue the whole population:

    update attachments set processing_status = 'pending'
     where processing_status = 'skipped' and mime_type like 'image/%';

Usage:
    python scripts/retire_inline_attachment_backlog.py            # dry run, the default
    python scripts/retire_inline_attachment_backlog.py --commit
    python scripts/retire_inline_attachment_backlog.py --commit --threshold 50000
"""

import argparse
import logging
import time
from typing import Any

from backend.connectors.email_store import INLINE_IMAGE_MIN_BYTES
from backend.core.db import get_db
from backend.core.logging_config import configure_logging

logger = logging.getLogger(__name__)

# Ids per UPDATE. 1000 overflows the postgrest URL and comes back as
# `400 JSON could not be generated`, so this is a URL-length bound, not a
# performance one.
UPDATE_CHUNK = 200
# Statement timeouts (57014) are expected on a queue this size and are worth
# waiting out; anything else is a real error and is re-raised.
TIMEOUT_RETRIES = 6


def _count(**filters: Any) -> int:
    q = get_db().table("attachments").select("id", count="exact") \
        .eq("processing_status", "pending")
    if filters.get("furniture"):
        q = q.like("mime_type", "image/%").lt("size_bytes", filters["threshold"])
    return q.execute().count or 0


def _next_ids(threshold: int, limit: int = UPDATE_CHUNK) -> list[str]:
    """The next chunk of furniture ids, oldest-first.

    `.order("created_at")` is load-bearing, not cosmetic: it lets the partial
    index `idx_attachments_pending on attachments (created_at) where
    processing_status = 'pending'` drive the scan. Without it the mime/size
    predicates force a sequential scan of the whole table and Supabase cancels
    the statement partway through the sweep.
    """
    for attempt in range(TIMEOUT_RETRIES):
        try:
            rows = (
                get_db().table("attachments").select("id")
                .eq("processing_status", "pending")
                .like("mime_type", "image/%")
                .lt("size_bytes", threshold)
                .order("created_at")
                .limit(limit).execute().data or []
            )
            return [r["id"] for r in rows]
        except Exception as e:
            if "57014" not in str(e) and "timeout" not in str(e).lower():
                raise
            logger.warning("Statement timeout reading the queue, backing off "
                           "(attempt %d/%d)", attempt + 1, TIMEOUT_RETRIES)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("gave up: repeated statement timeouts reading the queue")


def _retire(threshold: int) -> int:
    db, retired = get_db(), 0
    while True:
        ids = _next_ids(threshold)
        if not ids:
            return retired
        db.table("attachments").update({"processing_status": "skipped"}) \
            .in_("id", ids).execute()
        retired += len(ids)
        logger.info("Retired %d so far", retired)


def main(threshold: int, commit: bool) -> int:
    before = _count()
    furniture = _count(furniture=True, threshold=threshold)
    logger.info("Pending: %d, of which %d are images under %d bytes",
                before, furniture, threshold)

    if not furniture:
        logger.info("Nothing to retire.")
        return 0

    if not commit:
        logger.info("Dry run. Re-run with --commit to mark those %d row(s) "
                    "'skipped'. Nothing is deleted and no bytes were ever "
                    "downloaded; the reversal SQL is in this file's docstring.",
                    furniture)
        return 0

    retired = _retire(threshold)
    logger.info("Done. Retired %d row(s); pending %d -> %d. Rate-card documents "
                "are now at the front of the download queue.",
                retired, before, _count())
    return 0


if __name__ == "__main__":
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=int, default=INLINE_IMAGE_MIN_BYTES,
                        help=f"size floor in bytes; default {INLINE_IMAGE_MIN_BYTES} "
                             "(matches the ingest filter)")
    parser.add_argument("--commit", action="store_true",
                        help="actually write; omit for a dry run")
    args = parser.parse_args()
    raise SystemExit(main(threshold=args.threshold, commit=args.commit))
