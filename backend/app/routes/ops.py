"""Health, and the classifier feedback loop."""

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from backend.app.errors import AppException
from backend.automation.automation import scan_in_progress
from backend.classifier.classification_cache import update_label as cache_update_label
from backend.classifier.email_classifier import submit_feedback
from backend.core.config import settings
from backend.core.db import get_db
from backend.repositories import email_repo
from backend.services.metrics_service import collect_metrics
from backend.services.retry_service import sweep_stuck_sends

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ops"])

VALID_LABELS = {"customer_requirement", "quotation_rate_card", "general"}


class FeedbackRequest(BaseModel):
    corrected_label: str
    email_id: str = ""
    predicted_label: str = ""
    confidence: float = 0.0
    # Optional client-side overrides. The stored copy wins when email_id is
    # given, because the inbox list ships body:"" by design — a client cannot
    # send a body it was never given.
    email_subject: str = ""
    email_body: str = ""
    email_sender: str = ""


@router.get("/health")
def health():
    """Readiness probe. 200 when the database is reachable, 503 when it is not,
    so a monitor can alert on the status code alone."""
    try:
        get_db().table("automation_state").select("id").limit(1).execute()
    except Exception as e:
        logger.warning("Health check: database unreachable: %s", e)
        raise AppException(status_code=503, detail=f"Database unreachable: {e}")

    return {
        "status": "ok",
        "database": "ok",
        "scheduler": settings.run_scheduler,
        "scan_running": scan_in_progress(),
        "safe_mode": settings.safe_mode,
    }


@router.get("/metrics")
def metrics():
    """Operational counts — the numbers worth alerting on.

    Every one of these was something I had to query by hand while debugging;
    that is the argument for exposing them. The counts live in
    services/metrics_service so the hourly snapshot job records exactly what
    this endpoint reports, rather than a second implementation that drifts.

    Not Prometheus format: nothing here scrapes yet, and JSON is readable with
    curl. Swapping the rendering later is a formatting change, not a data one.
    """
    return collect_metrics()


@router.post("/admin/retry-stuck-sends")
def retry_stuck_sends():
    """Run the stuck-send recovery now, instead of waiting for the 15-minute
    sweep. Returns the tally: how many rows were reconciled (found already sent),
    resent, or flagged for a human.

    Bounded and synchronous, unlike the scan/ingest run-now endpoints: the sweep
    caps itself at a handful of rows, so it finishes in seconds and the operator
    gets the outcome in the response rather than having to poll. The same
    single-flight lock as the scheduled sweep means a manual run during a
    scheduled one simply reports already_running rather than doubling up.
    """
    try:
        return sweep_stuck_sends()
    except Exception as e:
        raise AppException(status_code=500, detail=f"Stuck-send sweep failed: {e}")


@router.post("/feedback")
def feedback_endpoint(payload: FeedbackRequest):
    """Record a human correction and make it stick on the next inbox refresh."""
    if payload.corrected_label not in VALID_LABELS:
        raise AppException(
            status_code=422,
            detail=f"Invalid label '{payload.corrected_label}'. "
                   f"Must be one of: {sorted(VALID_LABELS)}",
        )

    subject, body, sender = payload.email_subject, payload.email_body, payload.email_sender
    if payload.email_id and not body:
        stored = email_repo.get_by_id(payload.email_id)
        if stored:
            subject = subject or stored.subject
            body = stored.body
            sender = sender or stored.sender

    if not body:
        raise AppException(
            status_code=422,
            detail="email_body is empty and email_id did not resolve to a stored email",
        )

    try:
        result = submit_feedback(
            email_subject=subject,
            email_body=body,
            email_sender=sender,
            predicted_label=payload.predicted_label or "unknown",
            corrected_label=payload.corrected_label,
            confidence=payload.confidence,
        )
        # The label the operator sees comes from the cache, so the correction
        # only "sticks" once this runs.
        if payload.email_id:
            cache_update_label(payload.email_id, payload.corrected_label)
        logger.info("Label corrected for %s: %s -> %s",
                    payload.email_id or "(no id)",
                    payload.predicted_label, payload.corrected_label)
        return result
    except Exception as e:
        raise AppException(status_code=500, detail=f"Failed to submit feedback: {e}")
