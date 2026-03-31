"""
automation.py
-------------
Daily email scan automation.

- Fetches the latest batch of emails from IMAP
- Classifies each one using the 4-tier classifier
- Tracks which emails have already been processed (avoids re-classifying)
- Persists state in automation_state.json between runs
- Called by APScheduler every day at 7 AM, or triggered manually via API
"""

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from email_classifier import classify_email
from email_connector import fetch_latest_emails

logger = logging.getLogger("logistics_copilot.automation")

STATE_FILE = Path(__file__).parent / "automation_state.json"
SCAN_BATCH = 100  # how many recent emails to check per run


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CustomerEmail:
    email_id: str
    subject: str
    sender: str
    confidence: float
    method: str


@dataclass
class ScanStats:
    run_at: str = ""
    emails_scanned: int = 0
    new_emails: int = 0
    customer_requirements: int = 0
    quotation_rate_cards: int = 0
    general: int = 0
    errors: int = 0
    duration_seconds: float = 0.0
    customer_emails: List[dict] = field(default_factory=list)


@dataclass
class AutomationState:
    enabled: bool = True
    schedule_hour: int = 7
    last_stats: Optional[dict] = None
    processed_ids: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _load_state() -> AutomationState:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return AutomationState(**{k: v for k, v in data.items() if k in AutomationState.__dataclass_fields__})
        except Exception as e:
            logger.warning("Failed to load automation state: %s — using defaults", e)
    return AutomationState()


def _save_state(state: AutomationState) -> None:
    STATE_FILE.write_text(json.dumps(asdict(state), indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Core scan job
# ---------------------------------------------------------------------------

def run_daily_scan() -> ScanStats:
    """Fetch recent emails, classify new ones, update state. Called at 7 AM."""
    state = _load_state()
    stats = ScanStats(run_at=datetime.now(timezone.utc).isoformat())
    start = time.time()

    logger.info("Starting daily email scan...")

    try:
        result = fetch_latest_emails(limit=SCAN_BATCH, offset=0)
        emails = result.get("emails", [])
        stats.emails_scanned = len(emails)
        processed_set = set(state.processed_ids)

        for em in emails:
            email_id = em.get("id", "")
            if email_id in processed_set:
                continue

            stats.new_emails += 1
            subject = em.get("subject", "")
            body = em.get("body", "")
            sender = em.get("sender", em.get("from", ""))

            try:
                clf = classify_email(subject, body, sender)
                if clf.label == "customer_requirement":
                    stats.customer_requirements += 1
                    stats.customer_emails.append({
                        "id": email_id,
                        "subject": subject,
                        "sender": sender,
                        "confidence": round(clf.confidence, 3),
                        "method": clf.method,
                    })
                elif clf.label == "quotation_rate_card":
                    stats.quotation_rate_cards += 1
                else:
                    stats.general += 1

                processed_set.add(email_id)

            except Exception as e:
                logger.warning("Failed to classify email %s: %s", email_id, e)
                stats.errors += 1

        # Persist — keep last 10,000 seen IDs to avoid memory growth
        state.processed_ids = list(processed_set)[-10_000:]
        state.last_stats = asdict(stats)
        _save_state(state)

    except Exception as e:
        logger.error("Daily scan failed: %s", e)
        stats.errors += 1

    stats.duration_seconds = round(time.time() - start, 2)
    logger.info(
        "Scan done in %.1fs — %d scanned, %d new, %d customer, %d rate cards, %d errors",
        stats.duration_seconds, stats.emails_scanned, stats.new_emails,
        stats.customer_requirements, stats.quotation_rate_cards, stats.errors,
    )
    return stats


# ---------------------------------------------------------------------------
# Status / control
# ---------------------------------------------------------------------------

def get_status() -> dict:
    state = _load_state()
    return {
        "enabled": state.enabled,
        "schedule": f"Daily at {state.schedule_hour:02d}:00 UTC",
        "last_run": state.last_stats,
        "processed_total": len(state.processed_ids),
    }


def set_enabled(enabled: bool) -> None:
    state = _load_state()
    state.enabled = enabled
    _save_state(state)
    logger.info("Automation %s", "enabled" if enabled else "disabled")
