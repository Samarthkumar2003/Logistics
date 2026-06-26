"""
classification_cache.py
------------------------
Persistent cache for email classifications, keyed by the stable email id
(Message-ID, or imap-<n> fallback). Lets the inbox be re-fetched/refreshed
without re-calling the LLM on every email — the model runs once per email,
the label is read from Supabase thereafter.

Table: email_classifications (see setup_classification_cache.sql)
"""

import os
import logging
from typing import Optional

from dotenv import load_dotenv
from supabase import create_client

from backend.classifier.email_classifier import classify_emails_batch

load_dotenv()
logger = logging.getLogger(__name__)

supabase = create_client(
    os.environ.get("SUPABASE_URL", ""),
    os.environ.get("SUPABASE_KEY", ""),
)

_TABLE = "email_classifications"


def get_cached(email_ids: list[str]) -> dict[str, dict]:
    """Return {email_id: {label, confidence, method}} for ids already classified.
    Missing ids are simply absent from the result."""
    if not email_ids:
        return {}
    try:
        rows = (
            supabase.table(_TABLE)
            .select("email_id, label, confidence, method")
            .in_("email_id", email_ids)
            .execute()
            .data
            or []
        )
        return {r["email_id"]: r for r in rows}
    except Exception as e:
        logger.warning("Classification cache read failed: %s", e)
        return {}


def store(email_id: str, subject: str, sender: str, label: str,
          confidence: float, method: str) -> None:
    """Upsert one classification. Upsert (not insert) so a re-classify or a
    human correction overwrites the prior entry."""
    if not email_id:
        return
    try:
        supabase.table(_TABLE).upsert({
            "email_id": email_id,
            "subject": (subject or "")[:500],
            "sender": (sender or "")[:300],
            "label": label,
            "confidence": confidence,
            "method": method,
            "updated_at": "now()",
        }, on_conflict="email_id").execute()
    except Exception as e:
        logger.warning("Classification cache write failed for %s: %s", email_id, e)


def update_label(email_id: str, label: str, method: str = "feedback") -> None:
    """Override a cached label (used by the human-correction feedback loop)."""
    if not email_id:
        return
    try:
        supabase.table(_TABLE).upsert({
            "email_id": email_id,
            "label": label,
            "confidence": 1.0,
            "method": method,
            "updated_at": "now()",
        }, on_conflict="email_id").execute()
    except Exception as e:
        logger.warning("Classification cache update failed for %s: %s", email_id, e)


def classify_with_cache(emails: list[dict]) -> dict[str, dict]:
    """Classify a list of emails, calling the LLM only for cache misses.

    Each email dict needs: id, subject, body, sender (sender may be 'from').
    Returns {email_id: {label, confidence, method}} for every input email.
    Newly classified emails are written back to the cache.
    """
    ids = [e["id"] for e in emails if e.get("id")]
    cached = get_cached(ids)

    # Per-email visibility: which ids are served from cache (no API call) vs
    # which will trigger a live LLM request.
    misses = [e for e in emails if e.get("id") and e["id"] not in cached]
    for eid in ids:
        if eid in cached:
            logger.info("CACHE HIT  — email_id=%s label=%s (no API call)", eid, cached[eid]["label"])
        else:
            logger.info("CACHE MISS — email_id=%s (API call needed)", eid)

    logger.info("Classification batch: %d total, %d cache hits, %d API calls",
                len(ids), len(cached), len(misses))

    if misses:
        miss_by_id = {e["id"]: e for e in misses}
        for eid in miss_by_id:
            logger.info("API CALL   — email_id=%s -> LLM classify request", eid)
        fresh = classify_emails_batch([
            {"id": e["id"], "subject": e.get("subject", ""),
             "body": e.get("body", ""), "sender": e.get("sender", e.get("from", ""))}
            for e in misses
        ])
        for c in fresh:
            eid = c["id"]
            cached[eid] = {"label": c["label"], "confidence": c["confidence"], "method": c["method"]}
            src = miss_by_id.get(eid, {})
            store(eid, src.get("subject", ""), src.get("sender", src.get("from", "")),
                  c["label"], c["confidence"], c["method"])
            logger.info("API RESULT — email_id=%s label=%s conf=%.2f method=%s (cached)",
                        eid, c["label"], c["confidence"], c["method"])

    return cached
