"""
reply_service.py
----------------
Attaching an agent's reply to the RFQ it answers, and reading those replies back.

Attribution is by RFQ reference and nothing else. Rates are not extracted and
prices are not predicted — the operator reads the agent's own email. See
Documentation/01-architecture.md for why that trade was made.
"""

import logging
from typing import Any, Optional

from backend.core.rfq_reference import extract_rfq_reference
from backend.domain.models import Email
from backend.repositories import email_repo, job_repo

logger = logging.getLogger(__name__)

# How far into the body to look for a reference. Some clients drop the subject
# prefix on reply but keep the quoted original underneath. Bounded deliberately:
# searching the whole body risks matching an unrelated reference from a stale
# thread further down.
_BODY_SCAN_CHARS = 2000


def find_reference(subject: str, body: str) -> Optional[str]:
    """The RFQ reference this reply is quoting back, if any."""
    return (extract_rfq_reference(subject)
            or extract_rfq_reference((body or "")[:_BODY_SCAN_CHARS]))


def link_reply(email: Email) -> bool:
    """Attach a rate-card reply to its job. True when linked.

    Never guesses. A reply with no resolvable reference is left unlinked and
    stays visible in the inbox — and in the dashboard's "Needs Linking" tab —
    rather than being filed against a plausible-looking job.
    """
    reference = find_reference(email.subject, email.body)
    if not reference:
        logger.info("Rate card %s carries no RFQ reference — left unlinked", email.id)
        return False

    job = job_repo.get(reference)
    if job is None:
        logger.warning("Rate card %s cites %s but no such job exists", email.id, reference)
        return False

    email_repo.set_rfq_reference(email.id, reference)
    job_repo.mark_quotes_received(reference, job.status)
    logger.info("Rate card %s linked to %s", email.id, reference)
    return True


def _as_dict(e: Email, agent_name: str = "") -> dict[str, Any]:
    return {
        "id": e.id,
        "rfq_reference": e.rfq_reference,
        "agent_name": agent_name,
        "sender": e.sender,
        "subject": e.subject,
        "body": e.body,
        "received_at": e.received_at,
        "has_attachments": e.has_attachments,
    }


def list_for_reference(reference: str) -> list[dict[str, Any]]:
    return [_as_dict(e) for e in email_repo.list_replies_for([reference])]


def list_unlinked(limit: int, offset: int) -> dict[str, Any]:
    """Rate cards that never attached, with the reason worked out per row.

    The reason is derived here rather than stored, because the two cases want
    different responses: a reply that cites an unresolvable reference suggests a
    typo or a deleted job, while one that cites nothing is routine.
    """
    emails, total = email_repo.list_unlinked_rate_cards(limit=limit, offset=offset)

    items = []
    for e in emails:
        cited = find_reference(e.subject, e.body)
        items.append({
            "id": e.id,
            "sender": e.sender,
            "subject": e.subject,
            "body": "",  # loaded on demand
            "label": "quotation_rate_card",
            "received_at": e.received_at,
            "cited_reference": cited,
            "reason": (f"Cites {cited}, which matches no job" if cited
                       else "No RFQ reference in the reply"),
        })

    return {"emails": items, "total": total, "has_more": (offset + limit) < total}


def get_customer_request(customer_email_id: str) -> Optional[dict[str, Any]]:
    """One customer enquiry, its RFQs, and every agent reply against them.
    None when neither the email nor any job exists."""
    email = email_repo.get_by_id(customer_email_id)
    jobs = job_repo.list_for_customer_email(customer_email_id)
    if email is None and not jobs:
        return None

    references = [j.reference for j in jobs if j.reference]
    agent_by_reference = {j.reference: j.agent_name for j in jobs if j.reference}
    replies = [
        _as_dict(r, agent_by_reference.get(r.rfq_reference or "", ""))
        for r in email_repo.list_replies_for(references)
    ]

    return {
        "customer_email_id": customer_email_id,
        "customer_email": {
            "provider_msg_id": email.id,
            "sender": email.sender,
            "subject": email.subject,
            "body": email.body,
            "received_at": email.received_at,
        } if email else None,
        "jobs": [
            {
                "reference": j.reference,
                "status": j.status,
                "agents_contacted": j.agents_contacted,
                "created_at": j.created_at,
            }
            for j in jobs
        ],
        "replies": replies,
        "agents_contacted": sorted({a for j in jobs for a in j.agents_contacted}),
        "counts": {"agents": len(jobs), "replies": len(replies)},
    }
