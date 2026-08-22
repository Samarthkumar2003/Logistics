"""Pulling new mail from the provider on demand.

Distinct from /automation/run-now, which classifies mail that has ALREADY been
ingested. Nothing in the HTTP surface used to reach the Gmail sweep at all: the
inbox endpoints read Supabase only, so "Refresh" could not surface a message the
5-minute scheduler had not yet pulled.
"""

import logging

from fastapi import APIRouter

from backend.app.errors import AppException
from backend.app.lifespan import run_ingest_in_background
from backend.connectors.email_store import ingest_in_progress

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ingest", tags=["ingest"])


@router.post("/run-now", status_code=202)
def ingest_run_now():
    """Kick off a Gmail sweep and return immediately.

    A sweep is bounded by the watermark but still lists every id in the window
    and fetches full bodies for the new ones, so it can outlast an HTTP timeout.
    Poll GET /fetch-inbox — new rows appear as the sweep commits them, in chunks,
    rather than all at the end.
    """
    if ingest_in_progress():
        raise AppException(status_code=409, detail="An ingest is already running")
    run_ingest_in_background()
    return {"status": "started"}


@router.get("/status")
def ingest_status():
    """Whether a sweep is running right now. Cheap: no DB or provider call."""
    return {"running": ingest_in_progress()}
