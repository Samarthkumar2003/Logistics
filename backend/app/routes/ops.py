"""Health, and the classifier feedback loop."""

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel

from backend.app.errors import AppException
from backend.automation.automation import scan_in_progress
from backend.classifier.classification_cache import update_label as cache_update_label
from backend.core.config import settings
from backend.core.db import get_db
from backend.repositories import label_review_repo
from backend.services.metrics_service import collect_metrics
from backend.services.retry_service import sweep_stuck_sends

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ops"])

VALID_LABELS = {"customer_requirement", "quotation_rate_card", "general"}


class FeedbackRequest(BaseModel):
    """One operator judgement on one classifier label.

    The email's subject, body and sender used to be accepted here and copied
    into storage. They are gone: `email_id` joins to `emails` and
    `email_classifications`, so duplicating the customer's message into a
    second table bought nothing and put up to 5000 characters of it somewhere
    with no retention policy.
    """

    corrected_label: str
    email_id: str
    predicted_label: str = ""
    confidence: float = 0.0


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


def _reviewer(request: Request) -> str:
    """Who is judging this label, from the verified token.

    Best effort, unlike rfq.py's `_sender_or_422`. A review with no operator
    attached is still worth keeping, whereas an RFQ signed by nobody is not, so
    this returns "" rather than refusing. Empty under AUTH_ENABLED=0, where the
    middleware returns before setting claims at all, hence getattr.
    """
    claims = getattr(request.state, "claims", None)
    return (getattr(claims, "email", "") or "").strip()


@router.post("/feedback")
def feedback_endpoint(payload: FeedbackRequest, request: Request):
    """Record a human review of a classifier label, and make it stick.

    Confirmations are stored as well as corrections. `predicted_label ==
    corrected_label` is the operator agreeing, and keeping only the
    disagreements would leave a numerator with no denominator, which is not
    enough to state an accuracy figure.
    """
    if payload.corrected_label not in VALID_LABELS:
        raise AppException(
            status_code=422,
            detail=f"Invalid label '{payload.corrected_label}'. "
                   f"Must be one of: {sorted(VALID_LABELS)}",
        )

    # Required, where it used to default to "". It is the join key to `emails`
    # and `email_classifications`, so a review without one can be neither
    # analysed nor applied, and the cache update below was already skipped
    # without it. Such a call therefore reported success while changing no
    # label at all. A 422 says so instead.
    email_id = payload.email_id.strip()
    if not email_id:
        raise AppException(
            status_code=422,
            detail="email_id is required: a review that cannot be traced back "
                   "to an email can be neither analysed nor applied",
        )

    predicted = payload.predicted_label or "unknown"

    # The review is written before the cache update so that a failure means
    # nothing happened and the operator can just retry. The other order leaves
    # the label changed but the judgement unrecorded, which can only be
    # reported as a 200 the browser renders as fully saved, and silently losing
    # these rows is the defect this endpoint is being fixed for. Both writes go
    # through one Supabase client, so "an analytics table outage blocks a
    # correction" is not a failure mode that occurs on its own; revisit this
    # order if the reviews ever move to separate storage.
    try:
        label_review_repo.record(
            email_id=email_id,
            predicted_label=predicted,
            corrected_label=payload.corrected_label,
            confidence=payload.confidence,
            reviewed_by=_reviewer(request),
        )
    except Exception as e:
        logger.exception("Label review NOT stored for %s: %s", email_id, e)
        raise AppException(status_code=500,
                           detail=f"Could not store the review: {e}")

    # The label the operator sees comes from the cache, so the correction only
    # takes effect once this runs.
    cache_update_label(email_id, payload.corrected_label)

    agreed = predicted == payload.corrected_label
    logger.info("Label %s for %s: %s -> %s",
                "confirmed" if agreed else "corrected",
                email_id, predicted, payload.corrected_label)
    return {
        "status": "ok",
        "agreed": agreed,
        "detail": f"Review stored as '{payload.corrected_label}'",
    }
