"""
lifespan.py
-----------
Background work owned by the API process: four one-shot startup jobs and six
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
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI

from backend.automation.automation import run_scan
from backend.classifier.llm_provider import get_provider
from backend.connectors.email_store import (
    backfill_classifications,
    heal_sync_gaps,
    ingest_new_emails,
    process_pending_attachments,
    retry_pending_classifications,
)
from backend.core.config import settings
from backend.core.db import check_connectivity, get_db
from backend.core.logging_context import carry_context, job_context
from backend.services.metrics_service import snapshot_metrics

logger = logging.getLogger(__name__)


def _job(name: str, work: Callable[[], Any]) -> Callable[[], None]:
    """Wrap a scheduled job: its own correlation id, and failures with a traceback.

    Both were previously per-job and neither was consistent. Only the scan
    established an id (and only inside `run_scan`, so the handler below it did
    not get one), which meant ingest's lines, the attachment worker's, and a
    concurrent scan's all arrived with an empty `ctx` — indistinguishable in a
    file they share with HTTP traffic. At two- and five-minute cadences that
    uncorrelated output is the bulk of the log.

    Every handler also logged `str(e)` alone. That is a bare key name for a
    KeyError and an empty string for a bare RuntimeError, which is not enough to
    act on a job that failed unattended at 3am.

    Wrapping here means a job added later cannot forget either.
    """
    def run() -> None:
        with job_context(name):
            try:
                work()
            except Exception as e:
                logger.exception("%s job failed: %s", name, e)

    # Both, not just `__name__`: APScheduler's `get_callable_name` returns
    # `__qualname__` for a plain function, so setting only `__name__` left every
    # wrapped job displaying as `_job.<locals>.run`. Its own lines ("Added job
    # …", "Running job …", "Job … executed successfully") are emitted from the
    # scheduler thread, outside `job_context`, so they carry no `ctx` — the name
    # is the only thing on them that identifies the job, and one shared name
    # makes them useless. Before this wrapper existed the bare functions gave
    # distinct names for free.
    run.__name__ = f"{name}_job"
    run.__qualname__ = f"{name}_job"
    return run


def _warmup() -> None:
    with job_context("warmup"):
        try:
            logger.info("LLM provider warmed up: %s", get_provider().name)
        except Exception as e:
            # Degraded, not broken: the provider is retried on the first real
            # call. WARNING per the level contract, and no traceback wanted.
            logger.warning("LLM provider warmup failed: %s", e)


def _scan() -> None:
    run_scan(get_db())


def _ingest() -> None:
    logger.info("Scheduled ingest done: %s", ingest_new_emails())


def _backfill() -> None:
    logger.info("Backfill done: %s", backfill_classifications())


def _retry_pending() -> None:
    stats = retry_pending_classifications()
    if stats.get("classified"):
        logger.info("Retry queue: reclassified %d pending email(s)", stats["classified"])


def _attachment_worker() -> None:
    stats = process_pending_attachments()
    if stats.get("stored") or stats.get("failed"):
        logger.info("Attachment worker: stored %d, failed %d",
                    stats.get("stored", 0), stats.get("failed", 0))


def _metrics_snapshot() -> None:
    snapshot_metrics()


def _gap_heal() -> None:
    # Self-healing: audit → auto-backfill each gap day within guardrails →
    # re-audit → email an alert for anything still short.
    stats = heal_sync_gaps(days=14)
    if stats.get("gaps_found"):
        logger.info("Sync-gap heal: found %d, healed %d, unhealed %d",
                    stats["gaps_found"], stats["healed"], len(stats["unhealed"]))


# The name in each id is what makes a line greppable — `job=ingest:3f9a1c22`
# answers "which job?" before you have looked anything up.
_scan_job = _job("scan", _scan)
_ingest_job = _job("ingest", _ingest)
_backfill_job = _job("backfill", _backfill)
_retry_pending_job = _job("retry_pending", _retry_pending)
_attachment_worker_job = _job("attachment_worker", _attachment_worker)
_metrics_snapshot_job = _job("metrics_snapshot", _metrics_snapshot)
_gap_audit_job = _job("gap_heal", _gap_heal)


def run_scan_in_background() -> None:
    """Used by POST /automation/run-now, which returns 202 rather than holding a
    worker for the length of a scan.

    `carry_context` puts the triggering request's id on every line the scan
    emits. A new thread starts with an empty context, so without it nothing
    linked the operator's click to the work it started: the 202 carried an
    X-Request-ID that appeared in no log line, leaving wall-clock time as the
    only way to match a complaint to a scan.
    """
    threading.Thread(target=carry_context(_scan_job), daemon=True).start()


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
