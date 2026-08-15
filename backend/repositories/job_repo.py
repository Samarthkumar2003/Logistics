"""
job_repo.py
-----------
Reads and writes over `rfq_jobs` — one row per agent per customer request.

Note the column-name asymmetry that has caught people before:
`rfq_jobs.reference` is the RFQ id, and `emails.rfq_reference` is the link back
to it.
"""

import logging
from typing import Optional

from backend.core.db import get_db
from backend.domain.models import OPEN_JOB_STATUSES, RfqJob

logger = logging.getLogger(__name__)


def list_recent(limit: int = 20) -> list[RfqJob]:
    rows = (
        get_db().table("rfq_jobs").select("*")
        .order("created_at", desc=True).limit(limit).execute().data or []
    )
    return [RfqJob.from_row(r) for r in rows]


def get(reference: str) -> Optional[RfqJob]:
    rows = (
        get_db().table("rfq_jobs").select("*")
        .eq("reference", reference).limit(1).execute().data or []
    )
    return RfqJob.from_row(rows[0]) if rows else None


def list_for_customer_email(customer_email_id: str) -> list[RfqJob]:
    """Every RFQ spawned by one customer enquiry, oldest first."""
    rows = (
        get_db().table("rfq_jobs").select("*")
        .eq("customer_email_id", customer_email_id)
        .order("created_at").execute().data or []
    )
    return [RfqJob.from_row(r) for r in rows]


def insert(job: RfqJob) -> None:
    get_db().table("rfq_jobs").insert({
        "reference": job.reference,
        "customer_email_sender": job.customer_sender,
        "customer_email_subject": job.customer_subject,
        "customer_email_body": job.customer_body,
        "shipment_origin": job.origin,
        "shipment_destination": job.destination,
        "shipment_mode": job.mode,
        "shipment_weight_kg": job.weight_kg,
        "shipment_commodity": job.commodity,
        "shipment_size": job.size,
        "status": job.status or "rfqs_sent",
        "agents_contacted": job.agents_contacted,
        "customer_email_id": job.customer_email_id or None,
        "customer_thread_id": job.customer_thread_id or None,
    }).execute()


def set_status(reference: str, status: str) -> None:
    get_db().table("rfq_jobs").update({"status": status}).eq(
        "reference", reference
    ).execute()


def mark_quotes_received(reference: str, current_status: str) -> None:
    """Advance an open job when a reply lands.

    Guarded so a late reply cannot reopen an approved job — the desk has already
    committed to an agent by then.
    """
    if current_status in OPEN_JOB_STATUSES:
        try:
            set_status(reference, "quotes_received")
        except Exception as e:
            logger.warning("Status update failed for %s: %s", reference, e)
