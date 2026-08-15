"""
metrics_service.py
------------------
The operational counts, in one place, for two consumers:

    GET /metrics                  "is anything stuck right now?"
    the hourly snapshot job       "has it been getting worse?"

`/metrics` on its own answers only the present tense. That was enough to notice
the unlinked rate-card backlog moving 248 -> 266, but only because someone
queried it twice by hand a day apart. A snapshot table turns that into a
question you can ask of the past.

Every count is a cheap COUNT with a WHERE — no scans, no joins.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from backend.automation.automation import MAX_PROCESS_ATTEMPTS, get_status
from backend.core.config import settings
from backend.core.db import get_db

logger = logging.getLogger(__name__)

# A failed query reports this instead of 0, so a broken metric is never
# mistaken for a healthy zero.
UNAVAILABLE = -1

JOB_STATUSES = ("rfqs_sent", "quotes_received", "approved")


def _count(db, table: str, build: Callable) -> int:
    try:
        query = build(db.table(table).select("id", count="exact").limit(1))
        return query.execute().count or 0
    except Exception as e:
        logger.warning("Metric query failed on %s: %s", table, e)
        return UNAVAILABLE


def collect_metrics() -> dict[str, Any]:
    """Current operational state. Never raises — a metric that fails reports
    UNAVAILABLE rather than taking down the endpoint that carries the others."""
    db = get_db()

    emails = {
        # Waiting for the scan. A number that only grows means the scan is stuck.
        "scan_backlog": _count(db, "emails", lambda q: q.is_("processed_at", "null")
                               .lt("processing_attempts", MAX_PROCESS_ATTEMPTS)),
        # Retried to the cap and abandoned — these need a human.
        "processing_failed": _count(db, "emails", lambda q: q.is_("processed_at", "null")
                                    .gte("processing_attempts", MAX_PROCESS_ATTEMPTS)),
        # Classifier never succeeded; the retry job should be draining these.
        "pending_classification": _count(db, "emails",
                                         lambda q: q.eq("classification_status", "pending")),
        # Rate cards that never attached to an RFQ.
        "rate_cards_unlinked": _count(db, "emails",
                                      lambda q: q.eq("classification", "quotation_rate_card")
                                      .is_("rfq_reference", "null")),
    }
    jobs = {
        status: _count(db, "rfq_jobs", lambda q, s=status: q.eq("status", s))
        for status in JOB_STATUSES
    }

    last_run: dict = {}
    try:
        last_run = get_status(db).get("last_run") or {}
    except Exception as e:
        logger.warning("Metrics could not read last scan: %s", e)

    return {
        "emails": emails,
        "jobs": jobs,
        "last_scan": {
            "run_at": last_run.get("run_at"),
            "status": last_run.get("status"),
            "errors": last_run.get("errors"),
            "duration_seconds": last_run.get("duration_seconds"),
        },
        "safe_mode": settings.safe_mode,
    }


def snapshot_metrics() -> bool:
    """Write one row of history. Returns True if a row was stored.

    Skips the write when any count came back UNAVAILABLE. A partial row would
    put a fabricated number in a trend, and a wrong data point is worse than a
    missing one — a gap is at least honest about being a gap.
    """
    metrics = collect_metrics()
    emails, jobs = metrics["emails"], metrics["jobs"]

    unavailable = [k for k, v in {**emails, **jobs}.items() if v == UNAVAILABLE]
    if unavailable:
        logger.warning("Skipping metrics snapshot — unavailable: %s", ", ".join(unavailable))
        return False

    row = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "scan_backlog": emails["scan_backlog"],
        "processing_failed": emails["processing_failed"],
        "pending_classification": emails["pending_classification"],
        "rate_cards_unlinked": emails["rate_cards_unlinked"],
        "jobs_rfqs_sent": jobs["rfqs_sent"],
        "jobs_quotes_received": jobs["quotes_received"],
        "jobs_approved": jobs["approved"],
        "safe_mode": metrics["safe_mode"],
    }
    try:
        get_db().table("metrics_snapshots").insert(row).execute()
    except Exception as e:
        # Never fatal. Losing an hour of trend data must not disturb the process
        # that is doing the actual work.
        logger.warning("Metrics snapshot failed (run sql/setup_metrics_snapshots.sql?): %s", e)
        return False

    logger.info("Metrics snapshot — backlog=%d unlinked=%d failed=%d",
                emails["scan_backlog"], emails["rate_cards_unlinked"],
                emails["processing_failed"])
    return True
