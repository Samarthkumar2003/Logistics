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
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from supabase import create_client

from backend.connectors.gmail_connector import fetch_messages_since, fetch_attachment
from backend.classifier.classification_cache import classify_with_cache

load_dotenv()
logger = logging.getLogger(__name__)

supabase = create_client(
    os.environ.get("SUPABASE_URL", ""),
    os.environ.get("SUPABASE_KEY", ""),
)

ATTACHMENT_BUCKET = os.environ.get("ATTACHMENT_BUCKET", "rate-card-attachments")


# ---------------------------------------------------------------------------
# Watermark
# ---------------------------------------------------------------------------

def _get_watermark(provider: str) -> Optional[datetime]:
    try:
        rows = (
            supabase.table("sync_state")
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
        supabase.table("sync_state").upsert({
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
    """Download an attachment's bytes and persist to the bucket + attachments table.
    Returns the attachment UUID, or None on failure."""
    att_uuid = str(uuid.uuid4())
    filename = meta.get("filename", "")
    ext = os.path.splitext(filename)[1].lstrip(".").lower() or "bin"
    storage_path = f"{att_uuid}.{ext}"
    try:
        data = fetch_attachment(provider_msg_id, meta["attachment_id"])
        if not data:
            return None
        supabase.storage.from_(ATTACHMENT_BUCKET).upload(
            storage_path, data,
            {"content-type": meta.get("mime_type", "application/octet-stream")},
        )
        supabase.table("attachments").insert({
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


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

def _already_ingested(message_id: str) -> Optional[str]:
    """Return the email UUID if this message_id is already stored, else None."""
    if not message_id:
        return None
    try:
        rows = (
            supabase.table("emails").select("id").eq("message_id", message_id).limit(1)
            .execute().data or []
        )
        return rows[0]["id"] if rows else None
    except Exception:
        return None


def ingest_new_emails(provider: str = "gmail", max_results: int = 500) -> dict:
    """Fetch mail newer than the watermark, persist each once (idempotent on
    message_id), classify, store attachments, advance the watermark.
    Returns {fetched, new, skipped, attachments}."""
    if provider != "gmail":
        raise ValueError(f"ingest_new_emails: provider '{provider}' not yet supported")

    watermark = _get_watermark(provider)
    after_epoch = int(watermark.timestamp()) if watermark else None
    records = fetch_messages_since(after_epoch_s=after_epoch, max_results=max_results)
    logger.info("Ingest %s: fetched %d records since %s", provider, len(records), watermark)

    # Classify only the genuinely-new ones (idempotent gate before any LLM cost).
    def _key(r: dict) -> str:
        return r.get("message_id") or r["provider_msg_id"]

    fresh = [r for r in records if not _already_ingested(r.get("message_id", ""))]
    fresh_keys = {_key(r) for r in fresh}
    skipped = len(records) - len(fresh)
    labels = classify_with_cache([
        {"id": _key(r), "subject": r["subject"], "body": r["body"], "sender": r["sender"]}
        for r in fresh
    ]) if fresh else {}

    new_count = att_count = 0
    newest_seen = watermark
    for r in records:
        recv = _parse_dt(r.get("received_at"))
        if recv and (newest_seen is None or recv > newest_seen):
            newest_seen = recv
        key = _key(r)
        if key not in fresh_keys:  # already persisted — skip (idempotent)
            continue
        lab = labels.get(key, {})
        try:
            inserted = supabase.table("emails").insert({
                "message_id": r.get("message_id") or None,
                "provider": r["provider"],
                "provider_msg_id": r["provider_msg_id"],
                "thread_id": r["thread_id"],
                "sender": r["sender"],
                "subject": r["subject"],
                "body": r["body"],
                "has_attachments": r["has_attachments"],
                "received_at": r.get("received_at"),
                "classification": lab.get("label", ""),
                "classification_status": "classified" if lab else "pending",
            }).execute()
            email_id = inserted.data[0]["id"]
            new_count += 1
        except Exception as e:
            logger.warning("Failed to persist email %s: %s", key, e)
            continue
        for meta in r.get("attachments", []):
            if store_attachment(email_id, r["provider_msg_id"], meta):
                att_count += 1

    if newest_seen and newest_seen != watermark:
        _advance_watermark(provider, newest_seen)

    stats = {"fetched": len(records), "new": new_count, "skipped": skipped, "attachments": att_count}
    logger.info("Ingest %s done: %s", provider, stats)
    return stats


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
