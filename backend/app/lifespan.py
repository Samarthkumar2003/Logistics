"""
lifespan.py
-----------
Background work owned by the API process: three one-shot startup jobs and four
repeating ones.

The scheduler starts here rather than at import so `--reload` and multi-worker
deployments do not each spawn their own. When scaling out, set RUN_SCHEDULER=0
on every instance but one — the atomic claim in the scan makes duplicate
processing safe, but you would be paying for the same LLM calls twice.

Splitting this into its own process is Phase 5 of the hardening plan; while it
lives in-process, a long scan competes with HTTP requests for the interpreter.
"""

import logging
import threading
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI

from backend.automation.automation import run_scan
from backend.classifier.llm_provider import get_provider
from backend.connectors.email_store import (
    audit_sync_gaps,
    backfill_classifications,
    ingest_new_emails,
    process_pending_attachments,
    retry_pending_classifications,
)
from backend.core.config import settings
from backend.core.db import check_connectivity, get_db
from backend.services.metrics_service import snapshot_metrics

logger = logging.getLogger(__name__)


def _warmup() -> None:
    try:
        logger.info("LLM provider warmed up: %s", get_provider().name)
    except Exception as e:
        logger.warning("LLM provider warmup failed: %s", e)


def _scan_job() -> None:
    try:
        run_scan(get_db())
    except Exception as e:
        logger.error("Scheduled scan error: %s", e)


def _ingest_job() -> None:
    try:
        logger.info("Scheduled ingest done: %s", ingest_new_emails())
    except Exception as e:
        logger.error("Scheduled ingest error: %s", e)


def _backfill_job() -> None:
    try:
        logger.info("Backfill done: %s", backfill_classifications())
    except Exception as e:
        logger.error("Backfill error: %s", e)


def _retry_pending_job() -> None:
    try:
        stats = retry_pending_classifications()
        if stats.get("classified"):
            logger.info("Retry queue: reclassified %d pending email(s)", stats["classified"])
    except Exception as e:
        logger.error("Retry queue error: %s", e)


def _attachment_worker_job() -> None:
    try:
        stats = process_pending_attachments()
        if stats.get("stored") or stats.get("failed"):
            logger.info("Attachment worker: stored %d, failed %d",
                        stats.get("stored", 0), stats.get("failed", 0))
    except Exception as e:
        logger.error("Attachment worker error: %s", e)


def _metrics_snapshot_job() -> None:
    try:
        snapshot_metrics()
    except Exception as e:
        logger.error("Metrics snapshot error: %s", e)


def _gap_audit_job() -> None:
    try:
        audit_sync_gaps(days=14)  # WARNs with any day Gmail has and we don't
    except Exception as e:
        logger.error("Sync-gap audit error: %s", e)


def run_scan_in_background() -> None:
    """Used by POST /automation/run-now, which returns 202 rather than holding a
    worker for the length of a scan."""
    threading.Thread(target=_scan_job, daemon=True).start()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    scheduler = None
    check_connectivity()  # diagnose credentials/DNS once, non-fatally

    if settings.run_scheduler:
        for job in (_warmup, _ingest_job, _backfill_job, _attachment_worker_job):
            threading.Thread(target=job, daemon=True).start()

        scheduler = BackgroundScheduler(timezone="UTC")
        scheduler.add_job(_scan_job, "interval", minutes=5,
                          id="inbox_scan", replace_existing=True)
        scheduler.add_job(_ingest_job, "interval", minutes=5,
                          id="email_ingest", replace_existing=True)
        scheduler.add_job(_gap_audit_job, "interval", hours=24,
                          id="sync_gap_audit", replace_existing=True)
        # Self-heals once the LLM provider recovers (quota restored, outage over).
        scheduler.add_job(_retry_pending_job, "interval", minutes=15,
                          id="retry_pending_classifications", replace_existing=True)
        # Drains queued attachments (bytes not fetched at ingest time). Frequent
        # because it is the tail latency between an email landing and its PDF being
        # downloadable — and it self-heals a backlog after any outage.
        scheduler.add_job(_attachment_worker_job, "interval", minutes=2,
                          id="attachment_worker", replace_existing=True)
        # Trend history for /metrics, which is otherwise present-tense only.
        # Hourly: the numbers it tracks move over days, and 24 rows a day stays
        # queryable by eye for years.
        scheduler.add_job(_metrics_snapshot_job, "interval", hours=1,
                          id="metrics_snapshot", replace_existing=True)
        scheduler.start()
        logger.info("Scheduler started — scanning every 5 minutes")
    else:
        logger.info("RUN_SCHEDULER=0 — no background jobs in this process")

    yield

    if scheduler is not None:
        scheduler.shutdown(wait=False)
