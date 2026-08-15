"""
email_store.py
--------------
Phase 1 of the shipment-history plan: persist raw emails + attachments and make
ingestion idempotent.

- Incremental fetch via a date high-water mark (`sync_state.last_received_at`),
  NOT a fragile provider cursor. `UNIQUE(message_id)` makes re-fetch a no-op, so
  classification/extraction runs once per email ever.
- Attachment BYTES go to a Supabase Storage bucket keyed by UUID; the DB row
  holds metadata + storage_path only.

Tables + bucket: see sql/setup_email_store.sql.
"""

import os
import uuid
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Optional


from backend.connectors.gmail_connector import (
    fetch_messages_since, fetch_attachment, iter_message_id_pages, fetch_full_records,
    count_inbox_messages,
)
from backend.classifier.classification_cache import classify_with_cache
from backend.core.config import settings
from backend.core.db import get_db

logger = logging.getLogger(__name__)

ATTACHMENT_BUCKET = settings.attachment_bucket

# Safety ceiling on how many new mails one ingest sweep will pull (guards against
# a very stale watermark dragging in an unbounded backlog in a single pass).
MAX_INGEST_BATCH = 5000

# How far back the watermark is held from the newest mail persisted. The sweep
# re-lists this window every run (cheap, ids only) so late/out-of-order arrivals
# and rows from a chunk that failed to commit still get picked up.
WATERMARK_LOOKBACK = timedelta(days=1)

# Serialize ingestion. Two callers fire it: the startup thread and the 5-min
# scheduler job. A backlog sweep can outlast the 5-min interval, so without this
# the two overlap on the same window — duplicate Gmail fetches, re-classification,
# re-uploaded attachments. Non-blocking: a second caller skips this run rather
# than queueing behind the first (the sweep is idempotent, the next tick retries).
_ingest_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Watermark
# ---------------------------------------------------------------------------

def _get_watermark(provider: str) -> Optional[datetime]:
    try:
        rows = (
            get_db().table("sync_state")
            .select("last_received_at")
            .eq("provider", provider)
            .execute()
            .data
            or []
        )
        if rows and rows[0].get("last_received_at"):
            return datetime.fromisoformat(rows[0]["last_received_at"].replace("Z", "+00:00"))
    except Exception as e:
        logger.warning("Watermark read failed for %s: %s", provider, e)
    return None


def _advance_watermark(provider: str, newest: datetime) -> None:
    try:
        get_db().table("sync_state").upsert({
            "provider": provider,
            "last_received_at": newest.isoformat(),
            "updated_at": "now()",
        }, on_conflict="provider").execute()
    except Exception as e:
        logger.warning("Watermark advance failed for %s: %s", provider, e)


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

def store_attachment(email_id: str, provider_msg_id: str, meta: dict) -> Optional[str]:
    """Download an attachment's bytes and persist to the bucket + attachments table,
    synchronously. Returns the attachment UUID, or None on failure.

    Kept for the manual backfill script (scripts/ingest_window.py). The live ingest
    path uses enqueue_attachment + the background worker instead, so it is not
    blocked on Gmail/Storage I/O — see process_pending_attachments."""
    att_uuid = str(uuid.uuid4())
    filename = meta.get("filename", "")
    ext = os.path.splitext(filename)[1].lstrip(".").lower() or "bin"
    storage_path = f"{att_uuid}.{ext}"
    try:
        data = fetch_attachment(provider_msg_id, meta["attachment_id"])
        if not data:
            return None
        get_db().storage.from_(ATTACHMENT_BUCKET).upload(
            storage_path, data,
            {"content-type": meta.get("mime_type", "application/octet-stream")},
        )
        get_db().table("attachments").insert({
            "id": att_uuid,
            "email_id": email_id,
            "file_name": filename,
            "mime_type": meta.get("mime_type", ""),
            "storage_path": storage_path,
            "size_bytes": meta.get("size_bytes"),
            "processing_status": "stored",
        }).execute()
        return att_uuid
    except Exception as e:
        logger.warning("Failed to store attachment %s (email %s): %s", filename, email_id, e)
        return None


# Background attachment download/upload. Keeping this off the ingest hot path is
# what lets a backlog of mail land in minutes: ingest writes metadata-only rows
# (pending), this worker fetches the bytes afterwards.
_attach_lock = threading.Lock()
MAX_ATTACHMENT_ATTEMPTS = 5  # give up after this many transient failures


def enqueue_attachment(email_id: str, provider_msg_id: str, meta: dict) -> bool:
    """Record attachment metadata only — no bytes fetched. The background worker
    (process_pending_attachments) downloads + uploads later. Fast: one insert,
    no Gmail/Storage round-trip. Returns True if the row was queued."""
    if not meta.get("attachment_id"):
        return False
    try:
        get_db().table("attachments").insert({
            "id": str(uuid.uuid4()),
            "email_id": email_id,
            "file_name": meta.get("filename", ""),
            "mime_type": meta.get("mime_type", ""),
            "size_bytes": meta.get("size_bytes"),
            "provider_msg_id": provider_msg_id,
            "attachment_id": meta["attachment_id"],
            "storage_path": "",
            "processing_status": "pending",
        }).execute()
        return True
    except Exception as e:
        logger.warning("Failed to enqueue attachment %s (email %s): %s",
                       meta.get("filename", ""), email_id, e)
        return False


def _download_pending_attachment(row: dict) -> str:
    """Fetch one queued attachment's bytes, upload to the bucket, mark it stored.
    Returns 'stored' | 'failed' | 'retry'. Transient errors return 'retry' (left
    pending) until MAX_ATTACHMENT_ATTEMPTS, then 'failed' so it stops looping."""
    att_id = row["id"]
    attempts = (row.get("attempts") or 0) + 1
    ext = os.path.splitext(row.get("file_name") or "")[1].lstrip(".").lower() or "bin"
    storage_path = f"{att_id}.{ext}"
    try:
        data = fetch_attachment(row["provider_msg_id"], row["attachment_id"])
        if not data:
            # No bytes is a permanent condition, not a transient one — fail now.
            get_db().table("attachments").update(
                {"processing_status": "failed", "attempts": attempts}
            ).eq("id", att_id).execute()
            return "failed"
        get_db().storage.from_(ATTACHMENT_BUCKET).upload(
            storage_path, data,
            # upsert so a retry after a half-done attempt overwrites rather than 409s.
            {"content-type": row.get("mime_type") or "application/octet-stream",
             "upsert": "true"},
        )
        get_db().table("attachments").update(
            {"storage_path": storage_path, "processing_status": "stored", "attempts": attempts}
        ).eq("id", att_id).execute()
        return "stored"
    except Exception as e:
        terminal = attempts >= MAX_ATTACHMENT_ATTEMPTS
        logger.warning("Attachment download failed for %s (attempt %d%s): %s",
                       att_id, attempts, "/giving up" if terminal else "", e)
        try:
            get_db().table("attachments").update(
                {"attempts": attempts,
                 "processing_status": "failed" if terminal else "pending"}
            ).eq("id", att_id).execute()
        except Exception:
            pass
        return "failed" if terminal else "retry"


def process_pending_attachments(max_atts: int = 150, workers: int = 4) -> dict:
    """Drain queued (pending) attachments: download bytes from Gmail, upload to the
    bucket, mark stored. Parallel across `workers`. Non-blocking lock — a second
    caller skips rather than double-processing the same rows.
    Returns {pending, stored, failed, retry}.

    Deliberately throttled (4 workers × 150/tick). Higher settings saturate the
    shared Supabase project — bulk Storage uploads starve the ingest inserts and
    the dashboard reads (observed: dropped connections, 20s+ API latency). The
    bytes are not time-critical, so this trails in gently rather than at full tilt.

    Yields to ingest: while an ingest is running the two would fight over the same
    shared Gmail session and Supabase client (dropped connections, chunks slowing
    from seconds to minutes). Email recovery is the priority, so during a sweep
    this stands down and drains only once ingest is idle. Best-effort: ingest never
    waits on attachments, so email recovery always wins the connections."""
    if _ingest_lock.locked():
        logger.info("Attachment worker: ingest in progress — yielding this run")
        return {"pending": 0, "stored": 0, "failed": 0, "retry": 0, "skipped_ingest_busy": True}
    if not _attach_lock.acquire(blocking=False):
        logger.info("Attachment worker: already running — skipping this run")
        return {"pending": 0, "stored": 0, "failed": 0, "retry": 0, "skipped_locked": True}
    try:
        return _process_pending_attachments(max_atts=max_atts, workers=workers)
    finally:
        _attach_lock.release()


def _process_pending_attachments(max_atts: int, workers: int) -> dict:
    try:
        rows = (
            get_db().table("attachments")
            .select("id, provider_msg_id, attachment_id, file_name, mime_type, attempts")
            .eq("processing_status", "pending")
            .order("created_at")
            .limit(max_atts)
            .execute().data or []
        )
    except Exception as e:
        logger.warning("Attachment worker: could not read queue: %s", e)
        return {"pending": 0, "stored": 0, "failed": 0, "retry": 0}
    if not rows:
        return {"pending": 0, "stored": 0, "failed": 0, "retry": 0}

    logger.info("Attachment worker: %d pending", len(rows))
    outcomes = {"stored": 0, "failed": 0, "retry": 0}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_download_pending_attachment, r): r["id"] for r in rows}
        for fut in as_completed(futures):
            try:
                outcomes[fut.result()] += 1
            except Exception as e:
                logger.warning("Attachment worker task crashed for %s: %s",
                               futures[fut], e)
                outcomes["retry"] += 1
    stats = {"pending": len(rows), **outcomes}
    logger.info("Attachment worker done: %s", stats)
    return stats


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

def _thread_has_customer_requirement(thread_id: str) -> bool:
    """True if this thread already contains a customer_requirement email.
    Replies in such a thread must be labeled general — otherwise every
    'kind reminder' in the chain spawns a duplicate RFQ job."""
    if not thread_id:
        return False
    try:
        rows = (
            get_db().table("emails")
            .select("id")
            .eq("thread_id", thread_id)
            .eq("classification", "customer_requirement")
            .limit(1)
            .execute()
            .data or []
        )
        return bool(rows)
    except Exception as e:
        logger.warning("Thread lookup failed for %s: %s", thread_id, e)
        return False


def _already_ingested(provider_msg_id: str) -> Optional[str]:
    """Return the email UUID if this provider_msg_id is already stored, else None.
    Keyed on provider_msg_id (always present) — not message_id, which can be null
    and would split the same email across two id spaces (cache vs dashboard)."""
    if not provider_msg_id:
        return None
    try:
        rows = (
            get_db().table("emails").select("id").eq("provider_msg_id", provider_msg_id).limit(1)
            .execute().data or []
        )
        return rows[0]["id"] if rows else None
    except Exception:
        return None


def _max_received_at() -> Optional[datetime]:
    """Newest received_at currently stored. Only meaningful as a watermark once a
    sweep has verified completeness over the window — see ingest_new_emails."""
    try:
        rows = (
            get_db().table("emails").select("received_at")
            .order("received_at", desc=True).limit(1).execute().data or []
        )
        return _parse_dt(rows[0]["received_at"]) if rows else None
    except Exception as e:
        logger.warning("max received_at lookup failed: %s", e)
        return None


def _existing_provider_ids(ids: list[str]) -> set[str]:
    """Return the subset of provider_msg_ids already stored, batched to stay under
    Supabase's IN-list limits. Lets the ingest sweep skip mail we already have."""
    found: set[str] = set()
    for i in range(0, len(ids), 200):
        chunk = [x for x in ids[i:i + 200] if x]
        if not chunk:
            continue
        try:
            rows = (
                get_db().table("emails").select("provider_msg_id")
                .in_("provider_msg_id", chunk).execute().data or []
            )
            found.update(r["provider_msg_id"] for r in rows)
        except Exception as e:
            logger.warning("existing-ids lookup failed: %s", e)
    return found


def ingest_new_emails(provider: str = "gmail", max_results: int = 500) -> dict:
    """Fetch mail newer than the watermark, persist each once (idempotent on
    provider_msg_id), classify, store attachments, advance the watermark.
    Returns {fetched, new, skipped, attachments}.

    Serialized via a non-blocking lock: if another ingest is already running,
    this call returns immediately with skipped=True instead of duplicating it."""
    if not _ingest_lock.acquire(blocking=False):
        logger.info("Ingest %s: another ingest in progress — skipping this run", provider)
        return {"swept": 0, "fetched": 0, "new": 0, "skipped": 0,
                "attachments": 0, "skipped_locked": True}
    try:
        return _ingest_new_emails(provider=provider, max_results=max_results)
    finally:
        _ingest_lock.release()


def _ingest_new_emails(provider: str = "gmail", max_results: int = 500) -> dict:
    """Ingest worker — always call ingest_new_emails() (holds the lock) instead."""
    if provider != "gmail":
        raise ValueError(f"ingest_new_emails: provider '{provider}' not yet supported")

    watermark = _get_watermark(provider)
    after_epoch = int(watermark.timestamp()) if watermark else None

    # Sweep the whole after-watermark window (bounded), collecting ids we do not
    # already have. We page through EVERY page rather than stopping at the first
    # already-known page: a gap can hide *below* already-ingested mail (exactly
    # the Jul 10-19 case), so an early stop would miss it. Full bodies are fetched
    # only for the unknown ids — mail we already have is skipped for free.
    seen = 0
    truncated = False
    unknown_ids: list[str] = []
    for page in iter_message_id_pages(after_epoch):
        if not page:
            break
        seen += len(page)
        have = _existing_provider_ids(page)
        unknown_ids.extend(pid for pid in page if pid not in have)
        if len(unknown_ids) >= MAX_INGEST_BATCH:
            logger.warning("Ingest sweep hit ceiling %d new ids; stopping early", MAX_INGEST_BATCH)
            truncated = True
            break

    # Gmail lists ids newest-first; reverse so we process oldest-first. That
    # ordering matters for the thread rule (a thread's original request must land
    # before its replies) and makes the watermark a true contiguity point.
    unknown_ids.reverse()
    total_new = len(unknown_ids)
    logger.info("Ingest %s: swept %d ids since %s, %d new to ingest",
                provider, seen, watermark, total_new)

    # Key on provider_msg_id everywhere — it is always present, so the cache and
    # the dashboard (which reads by provider_msg_id) never split into two rows.
    def _key(r: dict) -> str:
        return r["provider_msg_id"]

    from backend.classifier.classification_cache import update_label
    new_count = att_count = fetched = 0
    newest_seen = watermark
    # Fetch + classify + insert one CHUNK at a time. Chunking the *fetch* too
    # (not just the insert) is what makes mail show up in the UI in parts: the
    # first rows land within seconds instead of after every body is downloaded.
    # A crash also keeps everything already committed — the next run's DB-based
    # sweep simply re-finds whatever is still missing.
    CHUNK = 50
    for i in range(0, total_new, CHUNK):
        records = fetch_full_records(unknown_ids[i:i + CHUNK])
        fetched += len(records)
        records.sort(key=lambda r: r.get("received_at") or "")  # oldest-first within chunk
        labels = classify_with_cache([
            {"id": _key(r), "subject": r["subject"], "body": r["body"], "sender": r["sender"]}
            for r in records
        ])
        for r in records:
            recv = _parse_dt(r.get("received_at"))
            if recv and (newest_seen is None or recv > newest_seen):
                newest_seen = recv
            key = _key(r)
            lab = labels.get(key, {})
            # A failed LLM call returns label="general" with method="error". That
            # fallback must NOT be recorded as a confident verdict — otherwise an
            # API hiccup is indistinguishable from a real "general" and an RFQ gets
            # silently buried. Mark it pending so a retry pass reclassifies it.
            classify_failed = lab.get("method") == "error"
            label = lab.get("label", "")

            # Thread rule: once a thread has produced a customer_requirement,
            # every later email in it is general — reminders and follow-ups must
            # not spawn duplicate RFQ jobs or show a second Send RFQs button.
            if label == "customer_requirement" and _thread_has_customer_requirement(r.get("thread_id", "")):
                logger.info(
                    "Email %s is a reply in customer thread %s — labeling general",
                    key, r.get("thread_id"),
                )
                label = "general"
                try:
                    update_label(key, "general", method="thread_rule")
                except Exception as e:
                    logger.warning("Failed to sync thread-rule label for %s: %s", key, e)

            try:
                inserted = get_db().table("emails").insert({
                    "message_id": r.get("message_id") or None,
                    "provider": r["provider"],
                    "provider_msg_id": r["provider_msg_id"],
                    "thread_id": r["thread_id"],
                    "sender": r["sender"],
                    "subject": r["subject"],
                    "body": r["body"],
                    "has_attachments": r["has_attachments"],
                    "received_at": r.get("received_at"),
                    "classification": label,
                    "classification_status": "pending" if (not lab or classify_failed) else "classified",
                }).execute()
                email_id = inserted.data[0]["id"]
                new_count += 1
            except Exception as e:
                logger.warning("Failed to persist email %s: %s", key, e)
                continue
            # Queue attachments as metadata only — the background worker fetches
            # the bytes. Keeping Gmail/Storage I/O off this loop is what lets a
            # mail backlog land in minutes instead of hours.
            for meta in r.get("attachments", []):
                if enqueue_attachment(email_id, r["provider_msg_id"], meta):
                    att_count += 1
        logger.info("Ingest %s: committed %d/%d new mails so far", provider, new_count, total_new)

    # newest_seen only covers mail this run inserted, so a fully-caught-up run
    # (0 new) would never advance and the sweep window would grow without bound.
    # When the sweep ran to COMPLETION it has just verified DB-completeness across
    # the whole window, which makes the newest stored mail a genuine contiguity
    # point — safe in this position precisely because it was verified. (Using
    # MAX(received_at) as a blind floor stays unsafe: "newest stored" alone says
    # nothing about completeness beneath it. The verified sweep is what earns it.)
    if not truncated:
        newest_stored = _max_received_at()
        if newest_stored and (newest_seen is None or newest_stored > newest_seen):
            newest_seen = newest_stored

    # Advance the watermark, minus a lookback buffer. The sweep is DB-diff based,
    # so the watermark is only a performance bound, never the correctness
    # mechanism — being conservative costs one extra page of ids, while being
    # aggressive risks stranding mail that arrived out of order or sat in a chunk
    # that failed to commit.
    if newest_seen:
        safe = newest_seen - WATERMARK_LOOKBACK
        if watermark is None or safe > watermark:
            _advance_watermark(provider, safe)

    stats = {"swept": seen, "fetched": fetched, "new": new_count,
             "skipped": seen - fetched, "attachments": att_count}
    logger.info("Ingest %s done: %s", provider, stats)
    return stats


def audit_sync_gaps(days: int = 14) -> list[dict]:
    """Per-day reconciliation: compare Gmail's INBOX count to stored rows over the
    last `days`. Returns the days where Gmail has more than the DB (a gap), e.g.
    [{'day': '2026-07-15', 'gmail': 180, 'db': 0, 'missing': 180}]. Empty = in sync.
    This is how the Jul 10-19 hole would have been flagged automatically."""
    import calendar
    from datetime import timedelta
    today = datetime.now(tz=timezone.utc).date()
    gaps: list[dict] = []
    for n in range(days, 0, -1):
        d0 = today - timedelta(days=n)
        d1 = d0 + timedelta(days=1)
        gmail_n = count_inbox_messages(
            after_epoch_s=calendar.timegm(d0.timetuple()),
            before_epoch_s=calendar.timegm(d1.timetuple()),
        )
        try:
            db_n = (
                get_db().table("emails").select("id", count="exact")
                .gte("received_at", f"{d0.isoformat()}T00:00:00+00:00")
                .lt("received_at", f"{d1.isoformat()}T00:00:00+00:00")
                .execute().count or 0
            )
        except Exception as e:
            logger.warning("Gap audit DB count failed for %s: %s", d0, e)
            continue
        if gmail_n > db_n:
            gaps.append({"day": d0.isoformat(), "gmail": gmail_n, "db": db_n,
                         "missing": gmail_n - db_n})
    if gaps:
        logger.warning("Sync-gap audit found %d day(s) missing mail: %s",
                       len(gaps), ", ".join(f"{g['day']} (-{g['missing']})" for g in gaps))
    else:
        logger.info("Sync-gap audit: no gaps over last %d days", days)
    return gaps


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def retry_pending_classifications(batch_size: int = 20, max_emails: int = 500) -> dict:
    """Retry queue for classifications that never succeeded.

    The queue is simply `classification_status='pending'`, set at ingest whenever
    the LLM call failed (quota exhausted, API outage, rate limit). A successful
    result is written back to emails.classification and the row is marked
    classified; a failure leaves it pending, so an outage self-heals on a later
    pass instead of silently freezing an RFQ as 'general'.
    """
    try:
        rows = (
            get_db().table("emails")
            .select("provider_msg_id, subject, body, sender")
            .eq("classification_status", "pending")
            .order("received_at", desc=True)
            .limit(max_emails).execute().data or []
        )
    except Exception as e:
        logger.error("Retry queue: fetch failed: %s", e)
        return {"error": str(e)}

    if not rows:
        logger.info("Retry queue: nothing pending")
        return {"pending": 0, "classified": 0, "still_pending": 0}

    logger.info("Retry queue: %d pending classification(s)", len(rows))
    classified = 0
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        out = classify_with_cache([
            {"id": r["provider_msg_id"], "subject": r["subject"],
             "body": r["body"], "sender": r["sender"]} for r in chunk
        ])
        chunk_ok = 0
        for r in chunk:
            pid = r["provider_msg_id"]
            res = out.get(pid, {})
            if res.get("method") == "error" or not res.get("label"):
                continue  # stays pending for the next pass
            try:
                get_db().table("emails").update({
                    "classification": res["label"],
                    "classification_status": "classified",
                }).eq("provider_msg_id", pid).execute()
                classified += 1
                chunk_ok += 1
            except Exception as e:
                logger.warning("Retry queue: label write failed for %s: %s", pid, e)
        if chunk_ok == 0:
            # Whole batch failed — provider is down or out of quota. Stop rather
            # than grinding the rest; they stay queued for the next run.
            logger.error("Retry queue: whole batch failed (provider down/quota) — "
                         "stopping, %d still pending", len(rows) - classified)
            break

    stats = {"pending": len(rows), "classified": classified,
             "still_pending": len(rows) - classified}
    logger.info("Retry queue done: %s", stats)
    return stats


def backfill_classifications(batch_size: int = 20) -> dict:
    """Classify emails in `emails` table that have no entry in `email_classifications`.
    Called on startup to recover after a cache wipe or first run."""
    try:
        # Fetch emails not yet in email_classifications
        rows = (
            get_db().table("emails")
            .select("provider_msg_id, subject, body, sender")
            .order("received_at", desc=True)
            .limit(500)
            .execute()
            .data or []
        )
    except Exception as e:
        logger.error("Backfill: failed to fetch emails: %s", e)
        return {"error": str(e)}

    if not rows:
        return {"backfilled": 0}

    # Get already-classified ids
    ids = [r["provider_msg_id"] for r in rows]
    try:
        cached_ids = {
            r["email_id"]
            for r in (
                get_db().table("email_classifications")
                .select("email_id")
                .in_("email_id", ids)
                .neq("method", "error")
                .execute()
                .data or []
            )
        }
    except Exception as e:
        logger.error("Backfill: failed to fetch cached ids: %s", e)
        return {"error": str(e)}

    missing = [r for r in rows if r["provider_msg_id"] not in cached_ids]
    if not missing:
        logger.info("Backfill: all %d emails already classified", len(rows))
        return {"backfilled": 0}

    logger.info("Backfill: classifying %d emails without cache entries", len(missing))
    total = 0
    for i in range(0, len(missing), batch_size):
        chunk = missing[i:i + batch_size]
        classify_with_cache([
            {"id": r["provider_msg_id"], "subject": r["subject"],
             "body": r["body"], "sender": r["sender"]}
            for r in chunk
        ])
        total += len(chunk)
        logger.info("Backfill: done %d/%d", total, len(missing))

    return {"backfilled": total}
