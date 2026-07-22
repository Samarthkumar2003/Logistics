"""
ingest_window.py
----------------
Targeted ingest for an explicit date window, independent of the watermark.

Why this exists: the incremental ingest sweeps everything after the watermark and
inserts oldest-first, so when a large historical backlog is pending, today's mail
sits at the back of the queue for hours. This pulls a bounded window straight in.

`--skip-attachments` inserts the mail rows only (the has_attachments flag is still
recorded); attachment blobs are what make bulk ingest slow, and they can be
backfilled separately afterwards.

Usage:
    python -m backend.scripts.ingest_window --after 2026-07-20 --before 2026-07-23
    python -m backend.scripts.ingest_window --after 2026-07-22 --skip-attachments
"""
import calendar
import logging
import sys
from datetime import date, timedelta

from backend.classifier.classification_cache import classify_with_cache, update_label
from backend.connectors.email_store import (
    supabase, _existing_provider_ids, _thread_has_customer_requirement, store_attachment,
)
from backend.connectors.gmail_connector import iter_message_id_pages, fetch_full_records

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _arg(name: str, default=None):
    return next((sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == name), default)


def _epoch(d: date) -> int:
    return calendar.timegm(d.timetuple())


def ingest_window(after: date, before: date, skip_attachments: bool = False) -> dict:
    query = f"after:{_epoch(after)} before:{_epoch(before)}"
    ids: list[str] = []
    for page in iter_message_id_pages(query=query):
        ids.extend(page)
    logger.info("Window %s..%s: %d ids in Gmail", after, before, len(ids))

    have = _existing_provider_ids(ids)          # one batched lookup, not one per id
    unknown = [i for i in ids if i not in have]
    logger.info("%d already stored, %d to ingest", len(ids) - len(unknown), len(unknown))
    if not unknown:
        return {"gmail": len(ids), "new": 0, "attachments": 0}

    records = fetch_full_records(unknown)
    records.sort(key=lambda r: r.get("received_at") or "")  # oldest first (thread rule)

    new_count = att_count = 0
    CHUNK = 50
    for i in range(0, len(records), CHUNK):
        chunk = records[i:i + CHUNK]
        labels = classify_with_cache([
            {"id": r["provider_msg_id"], "subject": r["subject"],
             "body": r["body"], "sender": r["sender"]} for r in chunk
        ])
        for r in chunk:
            key = r["provider_msg_id"]
            lab = labels.get(key, {})
            label = lab.get("label", "")
            if label == "customer_requirement" and _thread_has_customer_requirement(r.get("thread_id", "")):
                label = "general"
                try:
                    update_label(key, "general", method="thread_rule")
                except Exception as e:
                    logger.warning("thread-rule sync failed for %s: %s", key, e)
            try:
                inserted = supabase.table("emails").insert({
                    "message_id": r.get("message_id") or None,
                    "provider": r["provider"],
                    "provider_msg_id": key,
                    "thread_id": r["thread_id"],
                    "sender": r["sender"],
                    "subject": r["subject"],
                    "body": r["body"],
                    "has_attachments": r["has_attachments"],
                    "received_at": r.get("received_at"),
                    "classification": label,
                    "classification_status": "classified" if lab else "pending",
                }).execute()
                new_count += 1
            except Exception as e:
                logger.warning("persist failed for %s: %s", key, e)
                continue
            if not skip_attachments:
                for meta in r.get("attachments", []):
                    if store_attachment(inserted.data[0]["id"], key, meta):
                        att_count += 1
        logger.info("committed %d/%d", new_count, len(records))

    return {"gmail": len(ids), "new": new_count, "attachments": att_count}


def main() -> None:
    after = _arg("--after")
    before = _arg("--before")
    if not after:
        raise SystemExit("--after YYYY-MM-DD is required")
    a = date.fromisoformat(after)
    b = date.fromisoformat(before) if before else a + timedelta(days=1)
    stats = ingest_window(a, b, skip_attachments="--skip-attachments" in sys.argv)
    print("DONE:", stats)


if __name__ == "__main__":
    main()
