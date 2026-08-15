"""The scan: status, manual trigger, on/off."""

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from backend.app.errors import AppException
from backend.app.lifespan import run_scan_in_background
from backend.automation.automation import get_status, scan_in_progress, set_enabled
from backend.core.db import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/automation", tags=["automation"])


class AutomationToggle(BaseModel):
    enabled: bool


@router.get("/status")
def automation_status():
    try:
        return get_status(get_db())
    except Exception as e:
        raise AppException(status_code=500, detail=str(e))


@router.post("/run-now", status_code=202)
def automation_run_now():
    """Kick off a scan and return immediately.

    A scan is a batch of work over up to SCAN_BATCH emails; running it inline
    held an HTTP worker for minutes and gave the browser nothing meanwhile. Poll
    GET /automation/status — `last_run.run_at` changes when this finishes.
    """
    if scan_in_progress():
        raise AppException(status_code=409, detail="A scan is already running")
    run_scan_in_background()
    return {"status": "started"}


@router.post("/toggle")
def automation_toggle(payload: AutomationToggle):
    try:
        set_enabled(get_db(), payload.enabled)
        return {"enabled": payload.enabled}
    except Exception as e:
        raise AppException(status_code=500, detail=str(e))
