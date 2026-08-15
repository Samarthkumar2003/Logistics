"""
rfq_service.py
--------------
Drafting, sending, and recording RFQs — and awarding one afterwards.

This is the only path by which mail reaches a freight vendor. It runs solely
from an explicit operator action; nothing here is ever triggered by the
automation loop. See Documentation/01-architecture.md#the-human-gate.
"""

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from backend.agents.rfq_agent import DraftEmail, generate_rfq_drafts
from backend.connectors.email_sender import send_rfq_email, send_rfq_emails_batch
from backend.core.rfq_reference import inject_reference
from backend.domain.models import RfqJob
from backend.repositories import agent_repo, email_repo, job_repo

logger = logging.getLogger(__name__)

MAX_DRAFT_WORKERS = 10


class RfqError(Exception):
    """A caller-visible problem — the HTTP layer maps these to 4xx."""


@dataclass
class SelectedAgent:
    agent_name: str
    email: str


def new_reference() -> str:
    """A per-agent RFQ id. Uniqueness per agent is what lets a reply be traced
    back to exactly one job."""
    return f"RFQ-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:4]}"


def preview_draft(shipment: dict[str, Any], agent: Optional[SelectedAgent]) -> dict[str, Any]:
    """One sample draft, sent to nobody. Lets the operator see what an agent
    would receive before committing."""
    if not shipment.get("origin") or not shipment.get("destination"):
        raise RfqError("Origin and destination ports are required")

    target = agent or SelectedAgent("Sample Agent", "agent@example.com")
    reference = new_reference()

    result = generate_rfq_drafts(
        shipment_data=shipment,
        agents=[{"agent_name": target.agent_name, "email": target.email}],
        reference=reference,
    )
    drafts = result.drafts if hasattr(result, "drafts") else result
    if not drafts:
        raise RfqError("The model returned no draft")

    draft = drafts[0]
    return {
        "reference": reference,
        "vendor_name": draft.vendor_name,
        "subject": draft.subject,
        "body": draft.body,
        "note": "Sample only — each agent gets its own unique reference at send time.",
    }


def _draft_for_agent(
    agent: SelectedAgent,
    shipment: dict[str, Any],
    edited_subject: str,
    edited_body: str,
) -> dict[str, Any]:
    """One agent's draft and its own reference.

    When the operator has edited a draft, that exact text goes out verbatim with
    no model call — only the reference in the subject differs per agent, so
    replies stay attributable.
    """
    reference = new_reference()
    entry: dict[str, Any] = {
        "reference": reference, "agent_name": agent.agent_name,
        "email": agent.email, "draft": None, "error": "",
    }

    if edited_subject and edited_body:
        entry["draft"] = DraftEmail(
            vendor_name=agent.agent_name,
            vendor_email=agent.email,
            subject=inject_reference(edited_subject, reference),
            body=edited_body,
        )
        return entry

    try:
        result = generate_rfq_drafts(
            shipment_data=shipment,
            agents=[{"agent_name": agent.agent_name, "email": agent.email}],
            reference=reference,
        )
        drafts = result.drafts if hasattr(result, "drafts") else result
        if drafts:
            draft = drafts[0]
            # The prompt asks the model for the reference; this guarantees it.
            # Without this the subject was whatever the model returned, and a
            # deviation meant the RFQ went out unmatchable — the reply could
            # never be attributed to a shipment.
            draft.subject = inject_reference(draft.subject, reference)
            entry["draft"] = draft
        else:
            entry["error"] = "LLM returned no draft"
    except Exception as e:
        entry["error"] = f"draft failed: {e}"
    return entry


def send_rfqs(
    shipment: dict[str, Any],
    agents: list[SelectedAgent],
    customer: dict[str, str],
    edited_subject: str = "",
    edited_body: str = "",
) -> dict[str, Any]:
    """Draft, send, and record one RFQ per agent."""
    if not agents:
        raise RfqError("Select at least one agent")
    if not shipment.get("origin") or not shipment.get("destination"):
        raise RfqError("Origin and destination ports are required")

    # Remember hand-entered recipients so they show up as agents next time. A
    # no-op for agents already on file; non-fatal if the DB write fails.
    agent_repo.ensure_agents([{"agent_name": a.agent_name, "email": a.email} for a in agents])

    # Drafts run in parallel so latency is roughly one model call, not N.
    with ThreadPoolExecutor(max_workers=min(len(agents), MAX_DRAFT_WORKERS)) as pool:
        entries = list(pool.map(
            lambda a: _draft_for_agent(a, shipment, edited_subject, edited_body),
            agents,
        ))

    customer_email_id = customer.get("email_id", "")
    thread_id = email_repo.get_thread_id(customer_email_id) if customer_email_id else ""

    to_send = [
        {
            "vendor_name": e["draft"].vendor_name,
            "vendor_email": e["draft"].vendor_email or e["email"],
            "subject": e["draft"].subject,
            "body": e["draft"].body,
        }
        for e in entries if e["draft"] is not None
    ]
    try:
        results = send_rfq_emails_batch(to_send)
    except Exception as e:
        logger.error("Batch send failed: %s", e)
        results = [{"vendor_name": d["vendor_name"], "status": f"batch_error: {e}"}
                   for d in to_send]
    status_by_agent = {r.get("vendor_name", ""): r.get("status", "") for r in results}

    jobs = []
    for e in entries:
        status = e["error"] or status_by_agent.get(e["agent_name"], "unknown")
        if e["draft"] is not None and not e["error"]:
            try:
                job_repo.insert(RfqJob(
                    reference=e["reference"],
                    status="rfqs_sent",
                    agents_contacted=[e["agent_name"]],
                    customer_email_id=customer_email_id or None,
                    customer_thread_id=thread_id or None,
                    customer_sender=customer.get("sender", ""),
                    customer_subject=customer.get("subject", ""),
                    customer_body=customer.get("body", ""),
                    origin=shipment["origin"],
                    destination=shipment["destination"],
                    mode=shipment.get("mode", ""),
                    commodity=shipment.get("commodity", ""),
                    size=shipment.get("size", ""),
                    weight_kg=shipment.get("weight_kg"),
                ))
                logger.info("RFQ %s sent to %s (%s)",
                            e["reference"], e["agent_name"], status)
            except Exception as exc:
                # The mail is already gone; losing the row means losing the
                # ability to match the reply. Loud, and surfaced to the operator.
                logger.error("Sent %s but failed to persist the job: %s",
                             e["reference"], exc)
                status = f"sent but job persist failed: {exc}"
        jobs.append({
            "reference": e["reference"], "agent_name": e["agent_name"],
            "email": e["email"], "status": status,
        })

    return {"jobs": jobs, "shipment": shipment, "total_sent": len(to_send)}


def approve(reference: str) -> dict[str, Any]:
    """Award this RFQ to the agent it was sent to.

    One job is one agent, so the reference alone identifies the winner — there
    is nothing to select. Only an acceptance goes out; agents who were not
    chosen are not emailed.
    """
    job = job_repo.get(reference)
    if job is None:
        raise RfqError(f"Job {reference} not found")

    agent_name = job.agent_name or "the agent"
    agent_email = agent_repo.email_for_name(agent_name)
    if not agent_email:
        raise RfqError(
            f"No single email address on file for '{agent_name}' — cannot send the acceptance"
        )

    body = (
        f"Dear {agent_name},\n\n"
        f"We are pleased to inform you that your quotation for RFQ {reference} "
        f"has been accepted.\n\n"
        f"We look forward to working with you on this shipment. Please proceed "
        f"with the necessary arrangements and confirm the booking at your "
        f"earliest convenience.\n\n"
        f"Best regards,\nLogistics Copilot"
    )

    try:
        result = send_rfq_email(
            to_addr=agent_email,
            subject=f"Re: {reference} | Quotation Accepted",
            body=body,
        )
        acceptance_status = result.get("status", "unknown")
    except Exception as e:
        logger.error("Failed to send acceptance to %s: %s", agent_name, e)
        acceptance_status = f"failed: {e}"

    job_repo.set_status(reference, "approved")
    logger.info("RFQ %s approved — awarded to %s (%s)",
                reference, agent_name, acceptance_status)

    return {
        "reference": reference,
        "status": "approved",
        "agent_name": agent_name,
        "agent_email": agent_email,
        "acceptance_status": acceptance_status,
    }
