"""RFQ jobs: listing, one customer request, replies, and awarding."""

import logging

from fastapi import APIRouter

from backend.app.errors import AppException
from backend.repositories import email_repo, job_repo
from backend.services import reply_service, rfq_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["jobs"])


@router.get("/jobs")
def list_jobs():
    """Recent RFQ jobs, newest first. One row is one agent.

    `reply_count` lets the dashboard show who has come back without a request
    per card — the difference between "we asked 8 agents" and "3 answered" is
    the number a freight desk actually watches.
    """
    try:
        jobs = job_repo.list_recent(limit=20)
        replies = email_repo.count_replies_by_reference(
            [j.reference for j in jobs if j.reference]
        )
    except Exception as e:
        logger.error("Failed to list jobs: %s", e)
        raise AppException(status_code=500, detail=f"Failed to list jobs: {e}")

    return [
        {
            "reference": j.reference,
            "status": j.status,
            "reply_count": replies.get(j.reference, 0),
            "agents_contacted": j.agents_contacted,
            "customer_email_id": j.customer_email_id,
            "customer_email_sender": j.customer_sender,
            "customer_email_subject": j.customer_subject,
            "shipment_origin": j.origin,
            "shipment_destination": j.destination,
            "shipment_mode": j.mode,
            "shipment_commodity": j.commodity,
            "shipment_weight_kg": j.weight_kg,
            "created_at": j.created_at,
        }
        for j in jobs
    ]


@router.get("/jobs/{reference}")
def get_job(reference: str):
    try:
        job = job_repo.get(reference)
    except Exception as e:
        logger.error("Failed to fetch job %s: %s", reference, e)
        raise AppException(status_code=500, detail=f"Failed to fetch job: {e}")

    if job is None:
        raise AppException(status_code=404, detail=f"Job {reference} not found")

    return {
        "reference": job.reference,
        "status": job.status,
        "agents_contacted": job.agents_contacted,
        "customer_email_id": job.customer_email_id,
        "customer_email_sender": job.customer_sender,
        "customer_email_subject": job.customer_subject,
        "shipment_origin": job.origin,
        "shipment_destination": job.destination,
        "shipment_mode": job.mode,
        "shipment_commodity": job.commodity,
        "shipment_weight_kg": job.weight_kg,
        "created_at": job.created_at,
    }


@router.get("/jobs/{reference}/replies")
def list_replies(reference: str):
    """The agent's own reply emails for this RFQ. Rates are not extracted —
    the operator reads the message."""
    try:
        return reply_service.list_for_reference(reference)
    except Exception as e:
        logger.error("Failed to list replies for %s: %s", reference, e)
        raise AppException(status_code=500, detail=f"Failed to list replies: {e}")


@router.get("/customer-request/{customer_email_id}")
def get_customer_request(customer_email_id: str):
    """One enquiry, every RFQ it produced, and every agent reply against them."""
    try:
        data = reply_service.get_customer_request(customer_email_id)
    except Exception as e:
        logger.error("Failed to load customer request %s: %s", customer_email_id, e)
        raise AppException(status_code=500, detail=f"Failed to load customer request: {e}")

    if data is None:
        raise AppException(status_code=404, detail="No request found for this email")
    return data


@router.post("/jobs/{reference}/approve")
def approve_job(reference: str):
    """Award this RFQ to the agent it was sent to.

    No body: one job is one agent, so the reference identifies the winner. Only
    an acceptance is sent — agents who were not chosen are not emailed.
    """
    try:
        return rfq_service.approve(reference)
    except rfq_service.RfqError as e:
        detail = str(e)
        raise AppException(status_code=404 if "not found" in detail else 422, detail=detail)
    except Exception as e:
        logger.error("Approval failed for %s: %s", reference, e)
        raise AppException(status_code=500, detail=f"Approval failed: {e}")
