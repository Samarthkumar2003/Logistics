"""
automation.py
-------------
Full end-to-end automation loop. Runs every 5 minutes via APScheduler
(started from the FastAPI lifespan handler — see backend/app/api.py).

State lives in Supabase, NOT on disk:
  - emails.processed_at      which emails the pipeline has handled. The scan
                             claims a row atomically (UPDATE ... WHERE
                             processed_at IS NULL) so duplicate RFQ sends are
                             impossible across restarts, threads, or workers.
  - automation_state (1 row) enabled flag + last-run stats.

Pipeline per run:
  1. Select unprocessed emails from `emails` (already ingested + classified
     by the ingest job — no Gmail call, no re-classification)
  2. Atomically claim each row; a failure hands the claim back, capped by
     emails.processing_attempts so a poison message cannot loop
  3. customer_requirement -> recorded only. RFQs leave the system exclusively
     through the human-gated POST /send-rfq. Replies in a thread that already
     produced a customer_requirement are downgraded to general.
  4. quotation_rate_card  -> linked to the job whose RFQ reference the reply
       quotes back. Rates are NOT extracted and prices are NOT predicted — the
       operator reads the agent's own email. A reply with no resolvable
       reference is left unlinked and stays visible in the inbox.
"""

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import List

from backend.core.config import settings
from backend.core.logging_context import email_context, scan_context
from backend.domain.models import Email
from backend.services import reply_service

logger = logging.getLogger("logistics_copilot.automation")
SCAN_BATCH = settings.scan_batch

# How many times the scan will retry one email before giving up on it. A row
# that exhausts these stays unprocessed with processing_error set, so it can be
# found and dealt with rather than silently retried forever.
MAX_PROCESS_ATTEMPTS = 3

# Prevents the scheduler tick and POST /automation/run-now from scanning
# concurrently in the same process. Cross-process safety comes from the
# atomic claim on emails.processed_at.
_scan_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@dataclass
class ScanStats:
    # Why this run produced the numbers below. Without it, "another scan is
    # already running" and "automation is switched off" are indistinguishable
    # from a genuine run that found nothing — all three are zeros, and the UI
    # renders the last two as though the inbox were empty.
    status: str = "completed"  # completed | already_running | disabled | error
    run_at: str = ""
    emails_scanned: int = 0
    new_emails: int = 0
    customer_requirements: int = 0
    quotation_rate_cards: int = 0
    general: int = 0
    errors: int = 0
    duration_seconds: float = 0.0
    replies_linked: int = 0
    # Detected customer-requirement emails shown in the dashboard's last-run panel
    customer_emails: List[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# automation_state (single row, id=1)
# ---------------------------------------------------------------------------

def _get_state_row(supabase) -> dict:
    try:
        rows = supabase.table("automation_state").select("*").eq("id", 1).execute().data or []
        if rows:
            return rows[0]
    except Exception as e:
        logger.warning("automation_state read failed (run sql/setup_scan_state.sql?): %s", e)
    return {"enabled": True, "last_stats": None}


def _save_stats(supabase, stats: ScanStats) -> None:
    # Only a run that actually did the work is worth recording. Persisting a
    # skipped or disabled run would overwrite the last real result with zeros.
    if stats.status != "completed":
        return
    try:
        supabase.table("automation_state").upsert({
            "id": 1,
            "last_stats": asdict(stats),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="id").execute()
    except Exception as e:
        logger.warning("Failed to save scan stats: %s", e)


def get_status(supabase) -> dict:
    row = _get_state_row(supabase)
    try:
        processed_total = (
            supabase.table("emails")
            .select("id", count="exact")
            .not_.is_("processed_at", "null")
            .limit(1)
            .execute()
            .count or 0
        )
    except Exception:
        processed_total = 0
    email_redirect = settings.email_redirect
    return {
        "enabled": row.get("enabled", True),
        "schedule": "Every 5 minutes",
        "last_run": row.get("last_stats"),
        "processed_total": processed_total,
        # Safety: when set, ALL outgoing mail goes here instead of vendors
        "email_redirect": email_redirect or None,
        "safe_mode": settings.safe_mode,
    }


def scan_in_progress() -> bool:
    """True while a scan holds the lock in this process. Lets the API reject a
    duplicate trigger with a real answer instead of returning an empty run."""
    return _scan_lock.locked()


def set_enabled(supabase, enabled: bool) -> None:
    try:
        supabase.table("automation_state").upsert({
            "id": 1,
            "enabled": enabled,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="id").execute()
        logger.info("Automation %s", "enabled" if enabled else "disabled")
    except Exception as e:
        logger.error("Failed to toggle automation: %s", e)
        raise


# ---------------------------------------------------------------------------
# Claim + thread helpers
# ---------------------------------------------------------------------------

def _claim_email(supabase, row_id: str) -> bool:
    """Atomically mark one email as processed. Returns True only for the
    caller that wins the claim — everyone else sees zero updated rows."""
    try:
        res = (
            supabase.table("emails")
            .update({"processed_at": datetime.now(timezone.utc).isoformat()})
            .eq("id", row_id)
            .is_("processed_at", "null")
            .execute()
        )
        return bool(res.data)
    except Exception as e:
        logger.error("Failed to claim email %s: %s", row_id, e)
        return False


def _release_claim(supabase, row_id: str, attempts: int, error: str) -> None:
    """Hand a failed email back to the queue.

    The claim is taken before the work, so without this a single exception
    retired the row permanently — the rate card in it was never seen again.
    `processing_attempts` caps the retries so a poison message cannot loop.
    """
    try:
        supabase.table("emails").update({
            "processed_at": None,
            "processing_attempts": attempts + 1,
            "processing_error": error[:500],
        }).eq("id", row_id).execute()
    except Exception as e:
        logger.error("Failed to release claim on email %s: %s", row_id, e)


def _thread_already_customer(supabase, thread_id: str, current_msg_id: str) -> bool:
    """True if another email in this thread was already processed as a
    customer_requirement — replies then get downgraded to general."""
    if not thread_id:
        return False
    try:
        rows = (
            supabase.table("emails")
            .select("id")
            .eq("thread_id", thread_id)
            .eq("classification", "customer_requirement")
            .not_.is_("processed_at", "null")
            .neq("provider_msg_id", current_msg_id)
            .limit(1)
            .execute()
            .data
        )
        return bool(rows)
    except Exception as e:
        logger.warning("Thread lookup failed for %s: %s", thread_id, e)
        return False


# ---------------------------------------------------------------------------
# Rate card -> link the reply to the request it answers
# ---------------------------------------------------------------------------
#
# The rule itself lives in services/reply_service.py. The scan used to carry its
# own copy — two implementations of one rule, which is exactly how they drift.


def _process_rate_card(email: dict, _supabase=None) -> int:
    """Attach an agent's reply to the RFQ it answers. Returns 1 if linked, else 0.

    `_supabase` is accepted and ignored: the service reaches the database through
    core.db, and the scan still threads a client around for its other helpers.
    """
    return 1 if reply_service.link_reply(Email(
        id=email.get("id", ""),
        sender=email.get("from", ""),
        subject=email.get("subject", ""),
        body=email.get("body", ""),
    )) else 0


# ---------------------------------------------------------------------------
# Main scan job
# ---------------------------------------------------------------------------

def _fetch_batch(supabase) -> list[dict]:
    """Unprocessed emails, oldest first, excluding ones the scan has given up on."""
    return (
        supabase.table("emails")
        .select("id, provider_msg_id, thread_id, sender, subject, body, "
                "classification, processing_attempts")
        .is_("processed_at", "null")
        .lt("processing_attempts", MAX_PROCESS_ATTEMPTS)
        .order("received_at", desc=False)  # FIFO
        .limit(SCAN_BATCH)
        .execute()
        .data or []
    )


def _resolve_label(supabase, row: dict, msg_id: str) -> str:
    """The label to act on.

    Ingest classifies at persist time, so this normally just reads the stored
    value. The two exceptions: a row that was never classified, and a reply in a
    thread that already produced a customer requirement — which is downgraded so
    reminders and follow-ups cannot spawn duplicate RFQ jobs.
    """
    label = (row.get("classification") or "").strip()
    if not label:
        try:
            from backend.classifier.email_classifier import classify_email
            label = classify_email(
                subject=row.get("subject", ""), body=row.get("body", ""),
                sender=row.get("sender", ""),
            ).label
        except Exception as e:
            logger.warning("Fallback classification failed: %s", e)
            label = "general"

    if label == "customer_requirement" and _thread_already_customer(
        supabase, row.get("thread_id", ""), msg_id
    ):
        logger.info("Reply in an already-processed customer thread — treating as general")
        label = "general"
    return label


def _handle_email(supabase, row: dict, stats: ScanStats) -> None:
    """Process one claimed email. Releases the claim if anything throws."""
    msg_id = row.get("provider_msg_id", "")
    with email_context(msg_id):
        label = _resolve_label(supabase, row, msg_id)
        email = {
            "id": msg_id,
            "subject": row.get("subject") or "",
            "body": row.get("body") or "",
            "from": row.get("sender") or "",
        }
        try:
            if label == "customer_requirement":
                stats.customer_requirements += 1
                stats.customer_emails.append({
                    "id": msg_id, "subject": email["subject"], "sender": email["from"],
                })
                # NO auto-send. Recorded only, so a human can action it through
                # the gated flow. RFQs leave the system exclusively via
                # POST /send-rfq — never automatically.
            elif label == "quotation_rate_card":
                stats.quotation_rate_cards += 1
                stats.replies_linked += _process_rate_card(email)
            else:
                stats.general += 1
        except Exception as e:
            # Hand the claim back so the next run retries it. Without this a
            # single exception retired the email permanently.
            attempts = row.get("processing_attempts") or 0
            logger.error("Failed to process (%s), attempt %d/%d: %s",
                         label, attempts + 1, MAX_PROCESS_ATTEMPTS, e)
            _release_claim(supabase, row["id"], attempts, str(e))
            stats.errors += 1


def _run_scan_body(supabase, stats: ScanStats) -> None:
    start = time.time()
    logger.info("Starting inbox scan (batch size %d)", SCAN_BATCH)

    try:
        rows = _fetch_batch(supabase)
    except Exception as e:
        logger.error("Scan failed to fetch unprocessed emails: %s", e)
        stats.status = "error"
        stats.errors += 1
        return

    stats.emails_scanned = stats.new_emails = len(rows)
    logger.info("Found %d unprocessed emails", len(rows))

    for row in rows:
        if not _claim_email(supabase, row["id"]):
            continue  # another worker won the claim
        _handle_email(supabase, row, stats)

    stats.duration_seconds = round(time.time() - start, 2)
    logger.info(
        "Scan done in %.1fs — %d new, %d customer, %d rate cards→%d linked, %d errors",
        stats.duration_seconds, stats.new_emails, stats.customer_requirements,
        stats.quotation_rate_cards, stats.replies_linked, stats.errors,
    )
    _save_stats(supabase, stats)


def run_scan(supabase) -> ScanStats:
    """Process unclaimed emails. Called every 5 min by the scheduler and on
    demand via POST /automation/run-now."""
    stats = ScanStats(run_at=datetime.now(timezone.utc).isoformat())

    if not _scan_lock.acquire(blocking=False):
        logger.info("Scan already running — skipping this trigger")
        stats.status = "already_running"
        return stats

    try:
        if not _get_state_row(supabase).get("enabled", True):
            logger.info("Automation disabled — skipping scan")
            stats.status = "disabled"
            return stats
        # Every line this run emits carries the same scan id, so one run can be
        # pulled out of a file that interleaves it with HTTP traffic.
        with scan_context():
            _run_scan_body(supabase, stats)
        return stats
    finally:
        _scan_lock.release()
