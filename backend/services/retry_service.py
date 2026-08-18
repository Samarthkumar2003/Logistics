"""
retry_service.py
----------------
Recover RFQ job rows abandoned at `sending`.

A send is two-phase (see rfq_service): reserve a row at `sending`, hand the mail
to the provider, then advance the row to `rfqs_sent` or `send_failed`. If the
process dies between the reserve and the advance, the row is stuck at `sending`
and — crucially — we no longer know whether the mail actually left. From the row
alone, "never sent" and "sent, then crashed" are indistinguishable.

So this sweep never resends on a guess. For each stuck row it asks the Sent
folder whether that exact RFQ left, and acts on the answer:

    sent      -> advance to rfqs_sent. Send nothing; only the bookkeeping died.
    not sent  -> resend the SAME drafted mail, then advance on the outcome.
    can't tell -> mark send_failed and log. Send nothing; a human decides.

The work-list is the database, never the mailbox: "which rows are at `sending`?"
In steady state that is zero; after a crash it is the handful of RFQs one
operator click reserved. It cannot grow with mailbox size. A swept row always
LEAVES `sending`, so nothing is ever retried twice — retry-once, no counter.
"""

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.connectors.email_sender import send_rfq_email, was_sent
from backend.core.config import settings
from backend.core.rfq_reference import subject_token
from backend.domain.models import STATUS_SENDING, RfqJob
from backend.repositories import job_repo

logger = logging.getLogger(__name__)

# One sweep at a time. Overlapping ticks must not both search and both resend the
# same row — mirrors automation._scan_lock.
_sweep_lock = threading.Lock()

# Most rows one crash can strand is the size of a single operator's send. This
# cap is a backstop against a pathological pile-up turning one tick into a long
# run of Gmail searches and sends; anything above it is logged, not dropped
# silently, and the next tick takes the rest.
MAX_PER_SWEEP = 20

# How wide a Sent-folder window to search around when the row was reserved. The
# mail, if it left, left within seconds; a day each side absorbs clock skew
# between this host and Gmail without ever approaching an unbounded search.
SEARCH_PAD = timedelta(days=1)

SENT_STATUS = "sent"


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def _reconcile_one(job: RfqJob) -> str:
    """Resolve a single stuck row. Returns a short outcome label for the tally.

    Reads whether the RFQ is in the Sent folder, then takes exactly one of the
    three branches. Every status write is guarded on the row still being
    `sending`, so a reply that already advanced the job to `quotes_received`
    (an auto-responder can beat this sweep) is never overwritten.
    """
    reference = job.reference
    reserved = _parse_dt(job.created_at)
    if reserved is None:
        # No timestamp to bound a search on. Refuse to resend blind; flag it.
        logger.error("RFQ %s at sending has no created_at — cannot verify; "
                     "flagging send_failed", reference)
        job_repo.set_status_if(reference, "send_failed", STATUS_SENDING)
        return "flagged"

    phrase = subject_token(reference)
    evidence = was_sent(phrase, _epoch(reserved - SEARCH_PAD),
                        _epoch(reserved + SEARCH_PAD))

    if evidence is True:
        # It went out; only our record of it died. No mail.
        moved = job_repo.set_status_if(reference, "rfqs_sent", STATUS_SENDING)
        logger.info("RFQ %s found in Sent — reconciled to rfqs_sent%s", reference,
                    "" if moved else " (already moved on)")
        return "reconciled"

    if evidence is None:
        # Could not look. "No evidence" is not "no send" — never resend here.
        job_repo.set_status_if(reference, "send_failed", STATUS_SENDING)
        logger.warning("RFQ %s: no delivery evidence available — flagged "
                       "send_failed for a human, not resent", reference)
        return "flagged"

    # evidence is False: proven never sent.
    return _resend_one(job)


def _resend_one(job: RfqJob) -> str:
    """Resend an RFQ proven absent from the Sent folder, from stored draft text.

    Uses the exact subject/body/recipient recorded when the row was reserved, so
    the vendor receives the same mail the crash interrupted rather than freshly
    invented words. If the draft was not stored (a row from before this feature,
    or a partial reserve), there is nothing safe to send — flag it.
    """
    reference = job.reference
    if not (settings.auto_retry_stuck_sends):
        job_repo.set_status_if(reference, "send_failed", STATUS_SENDING)
        logger.info("RFQ %s proven unsent, but AUTO_RETRY_STUCK_SENDS is off — "
                    "flagged send_failed for the operator", reference)
        return "flagged"

    if not (job.draft_to and job.draft_subject and job.draft_body):
        job_repo.set_status_if(reference, "send_failed", STATUS_SENDING)
        logger.error("RFQ %s proven unsent but its draft was not stored — cannot "
                     "resend faithfully; flagged send_failed", reference)
        return "flagged"

    logger.info("RFQ %s proven unsent — resending stored draft to %s",
                reference, job.draft_to)
    result = send_rfq_email(to_addr=job.draft_to, subject=job.draft_subject,
                            body=job.draft_body)
    delivered = result.get("status") == SENT_STATUS
    final = "rfqs_sent" if delivered else "send_failed"
    job_repo.set_status_if(reference, final, STATUS_SENDING)
    if delivered:
        logger.info("RFQ %s resent successfully", reference)
        return "resent"
    logger.error("RFQ %s resend failed (%s) — recorded send_failed",
                 reference, result.get("error") or result.get("status"))
    return "flagged"


def sweep_stuck_sends() -> dict[str, Any]:
    """Reconcile every job stranded at `sending`. Safe to call on a timer.

    Single-flight: a slow tick (a run of Gmail searches) must not overlap the
    next. Returns a tally the caller can log or return over HTTP.
    """
    if not _sweep_lock.acquire(blocking=False):
        logger.info("Stuck-send sweep already running — skipping this trigger")
        return {"status": "already_running"}

    try:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(minutes=settings.stale_sending_minutes)).isoformat()
        stuck = job_repo.list_stale_sending(cutoff)
        if not stuck:
            return {"status": "ok", "scanned": 0, "reconciled": 0,
                    "resent": 0, "flagged": 0, "deferred": 0}

        deferred = 0
        if len(stuck) > MAX_PER_SWEEP:
            deferred = len(stuck) - MAX_PER_SWEEP
            logger.warning("%d rows stuck at sending; handling %d this sweep, "
                           "%d deferred to the next", len(stuck), MAX_PER_SWEEP,
                           deferred)
            stuck = stuck[:MAX_PER_SWEEP]

        tally = {"reconciled": 0, "resent": 0, "flagged": 0}
        for job in stuck:
            try:
                tally[_reconcile_one(job)] += 1
            except Exception as e:
                # One row's failure must not abort the rest of the sweep. The row
                # stays at `sending` and the next tick retries it.
                logger.exception("Sweep failed on RFQ %s: %s", job.reference, e)

        logger.info("Stuck-send sweep: scanned %d, reconciled %d, resent %d, "
                    "flagged %d, deferred %d", len(stuck), tally["reconciled"],
                    tally["resent"], tally["flagged"], deferred)
        return {"status": "ok", "scanned": len(stuck), **tally,
                "deferred": deferred}
    finally:
        _sweep_lock.release()


def _parse_dt(value: Any) -> Any:
    """Parse an ISO timestamp to an aware datetime, or None. Tolerant of the
    trailing 'Z' Supabase sometimes returns."""
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00") if isinstance(value, str) else value
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None
