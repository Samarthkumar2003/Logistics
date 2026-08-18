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
from backend.domain.models import OPEN_JOB_STATUSES, STATUS_SENDING, RfqJob

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


def existing_references(references: list[str]) -> set[str]:
    """Which of these references have a job row. One query per 100, not one per
    reference — the unlinked list asks this for every row on screen.

    A lookup that fails returns the chunk as absent rather than raising: the
    caller uses this to word a diagnosis, and a missing diagnosis beats a 500.
    """
    if not references:
        return set()
    found: set[str] = set()
    for i in range(0, len(references), 100):  # stay under PostgREST IN limits
        chunk = references[i:i + 100]
        try:
            rows = (
                get_db().table("rfq_jobs").select("reference")
                .in_("reference", chunk).execute().data or []
            )
        except Exception as e:
            logger.warning("Job existence lookup failed: %s", e)
            continue
        found.update(r["reference"] for r in rows if r.get("reference"))
    return found


def list_for_customer_email(customer_email_id: str) -> list[RfqJob]:
    """Every RFQ spawned by one customer enquiry, oldest first."""
    rows = (
        get_db().table("rfq_jobs").select("*")
        .eq("customer_email_id", customer_email_id)
        .order("created_at").execute().data or []
    )
    return [RfqJob.from_row(r) for r in rows]


def _row(job: RfqJob) -> dict:
    return {
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
        # The drafted mail, kept so a send abandoned mid-flight can be resent
        # verbatim. See sql/add_rfq_draft_columns.sql.
        "draft_subject": job.draft_subject or None,
        "draft_body": job.draft_body or None,
        "draft_to": job.draft_to or None,
    }


def insert(job: RfqJob) -> None:
    get_db().table("rfq_jobs").insert(_row(job)).execute()


def insert_many(jobs: list[RfqJob]) -> None:
    """Reserve several references in one statement.

    One round trip for a whole multi-agent send, because this now sits on the
    operator's blocking path before any mail goes out. Either every row lands or
    the statement raises and none do — which is the point: nothing has been sent
    yet, so aborting is free.
    """
    if not jobs:
        return
    get_db().table("rfq_jobs").insert([_row(j) for j in jobs]).execute()


def set_status(reference: str, status: str) -> None:
    get_db().table("rfq_jobs").update({"status": status}).eq(
        "reference", reference
    ).execute()


def set_status_if(reference: str, status: str, expected: str) -> bool:
    """Advance a job only while it still holds `expected`. True if it moved.

    The send path needs this rather than `set_status`: a vendor auto-reply can
    link and advance a job to `quotes_received` in the window between the mail
    leaving and this process recording the outcome. A blind update would then
    overwrite a real reply with `rfqs_sent`, hiding a quote that had already
    arrived. Not returning False on a no-op would hide that it happened.
    """
    result = (
        get_db().table("rfq_jobs").update({"status": status})
        .eq("reference", reference).eq("status", expected).execute()
    )
    return bool(result.data)


def list_stale_sending(before: str) -> list[RfqJob]:
    """Jobs still `sending` since before `before` — an ISO timestamp.

    A row reaches this list only if the process died between reserving the
    reference and recording the send outcome. It needs a human: "never sent" and
    "sent, then crashed before the update" are indistinguishable from here, and
    re-sending an RFQ that did go out is not something you can take back.
    """
    rows = (
        get_db().table("rfq_jobs").select("*")
        .eq("status", STATUS_SENDING)
        .lt("created_at", before)
        .order("created_at").execute().data or []
    )
    return [RfqJob.from_row(r) for r in rows]


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
