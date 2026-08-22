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
from backend.core.logging_context import carry_context
from backend.core.rfq_reference import inject_reference
from backend.domain.models import STATUS_SENDING, RfqJob
from backend.repositories import agent_repo, email_repo, job_repo

logger = logging.getLogger(__name__)

MAX_DRAFT_WORKERS = 10

# The one sender status that means the mail actually left. Everything else —
# "failed", "skipped", a batch error, an uncorrelatable result — is not a send,
# and must not be recorded as one.
SENT_STATUS = "sent"


class RfqError(Exception):
    """A caller-visible problem — the HTTP layer maps these to 4xx."""


@dataclass
class SelectedAgent:
    agent_name: str
    email: str


def new_reference() -> str:
    """A per-agent RFQ id. Uniqueness per agent is what lets a reply be traced
    back to exactly one job.

    8 hex characters, not 4: the suffix is scoped to one day, and 4 gave only
    65,536 values — a ~16% chance per day at ~150 RFQs of colliding with a
    reference already issued. The column is unique, so a collision fails the
    insert; the send now reserves the row first, which turns that from a sent RFQ
    stranded with no job row into an aborted send the operator can simply retry.
    """
    return f"RFQ-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:8]}"


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


def _attach_send_status(
    sendable: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> None:
    """Record each draft's delivery outcome on its own entry, by position.

    By position, not by `vendor_name`: that name is produced by the model
    (`DraftEmail.vendor_name`), so it need not equal the agent we selected, and
    multi-office agents share one name (BUGS.md P2-1) — a name-keyed dict
    silently collapses two agents' outcomes into one. Both senders append exactly
    one result per draft, in order.
    """
    if len(results) != len(sendable):
        # Positions can no longer be trusted, and guessing would mean labelling
        # one agent's RFQ with another's outcome.
        logger.error(
            "Sender returned %d results for %d drafts — cannot correlate; "
            "treating every send as unconfirmed", len(results), len(sendable),
        )
        for entry in sendable:
            entry["send_status"] = "unconfirmed"
        return

    for entry, result in zip(sendable, results):
        entry["send_status"] = result.get("status", "unknown")


def _reserve_jobs(
    sendable: list[dict[str, Any]],
    shipment: dict[str, Any],
    customer: dict[str, str],
    customer_email_id: str,
    thread_id: str,
) -> None:
    """Phase one: commit a job row per draft *before* any mail goes out.

    Every reference about to reach a vendor gets its row first, at
    `STATUS_SENDING` — a record of intent, not a claim of delivery. Writing
    afterwards left a window in which a reference existed in the wild with
    nothing to attach a reply to: an auto-responder that replies in under a
    second, a crash mid-batch, or a reference collision against the unique index
    all produced an RFQ whose reply could never be filed.

    Raises RfqError if the reservation fails, which aborts before the send. That
    is the safe direction to fail: an un-sent RFQ can be re-sent, whereas a sent
    one with no row is a quote that silently never arrives.

    The drafted subject and body go in too, so a send abandoned mid-flight can be
    resent verbatim instead of re-drafted into different words. Pre-redirect
    text: the sender applies EMAIL_REDIRECT, so a retry picks up whatever redirect
    is configured then rather than replaying a stale test address.
    """
    try:
        job_repo.insert_many([
            RfqJob(
                reference=e["reference"],
                status=STATUS_SENDING,
                agents_contacted=[e["agent_name"]],
                draft_subject=e["draft"].subject,
                draft_body=e["draft"].body,
                # The same resolution the sender uses below, so the stored
                # recipient is the one actually addressed.
                draft_to=e["draft"].vendor_email or e["email"],
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
            )
            for e in sendable
        ])
    except Exception as exc:
        logger.exception("Reserving %d job row(s) failed, nothing sent: %s",
                         len(sendable), exc)
        raise RfqError(
            f"Could not record the RFQ before sending, so nothing was sent: {exc}"
        ) from exc


def _record_outcome(entry: dict[str, Any]) -> str:
    """Phase two: move a reserved row to its delivery outcome. Returns the
    operator-facing status.

    The stored status is what the sender actually reported, not the fact that a
    draft was produced. Hardcoding `rfqs_sent` meant a dead SMTP login filled the
    dashboard with jobs apparently awaiting replies from agents who were never
    contacted — and because attribution is by reference alone, no reply would
    ever arrive to contradict the record. It reads as an unresponsive vendor.

    `send_failed` rows are kept, not deleted, because a failure can be ambiguous:
    a timeout may have delivered. Keeping the row keeps a reply attributable,
    where dropping it would strand the reply.

    The update is guarded on the row still being `sending`, so a reply that has
    already landed and advanced the job to `quotes_received` is not overwritten —
    a real quote outranks our record of how the send went.
    """
    reference = entry["reference"]
    send_status = entry.get("send_status", "unknown")
    delivered = send_status == SENT_STATUS
    final = "rfqs_sent" if delivered else "send_failed"

    try:
        moved = job_repo.set_status_if(reference, final, STATUS_SENDING)
    except Exception as exc:
        # The row exists and holds the reference, so a reply is still
        # attributable; only the status is stale. Loud, and surfaced.
        logger.exception("RFQ %s (%s) failed to update its job status: %s",
                         reference, send_status, exc)
        return f"{send_status} but job status update failed: {exc}"

    if not moved:
        # Left `sending` by someone else — in practice a reply that beat us here.
        logger.info("RFQ %s left %s before its %s update; leaving it alone",
                    reference, STATUS_SENDING, final)
    if delivered:
        logger.info("RFQ %s sent to %s", reference, entry["agent_name"])
    else:
        logger.error("RFQ %s to %s not confirmed sent (%s) — recorded as %s",
                     reference, entry["agent_name"], send_status, final)
    return send_status


def _dedupe_recipients(agents: list[SelectedAgent]) -> list[SelectedAgent]:
    """One address, one RFQ. Keeps the first spelling of each.

    The recipient list is whatever the browser posted, and one address twice means
    a vendor gets the same enquiry twice under two references — unrecallable, and it
    reads to them as either a mistake or a duplicate booking attempt. It has already
    happened: a state updater in the send form appended each hand-typed address
    twice, and since the copies shared a render key the page showed one recipient
    while the payload carried two. That is fixed in the form, but a browser tab left
    open keeps posting the old bundle, so the guard that counts is this one.
    """
    seen: set[str] = set()
    unique: list[SelectedAgent] = []
    for agent in agents:
        key = (agent.email or "").strip().lower()
        if not key:
            logger.warning("Dropping recipient %r with no email address", agent.agent_name)
            continue
        if key in seen:
            logger.warning("Dropping duplicate recipient %s (%s) — already sending to it once",
                           agent.email, agent.agent_name)
            continue
        seen.add(key)
        unique.append(agent)
    return unique


def send_rfqs(
    shipment: dict[str, Any],
    agents: list[SelectedAgent],
    customer: dict[str, str],
    edited_subject: str = "",
    edited_body: str = "",
) -> dict[str, Any]:
    """Draft, send, and record one RFQ per agent."""
    # Before the empty check, so a list of nothing but blanks is refused, not sent.
    agents = _dedupe_recipients(agents)
    if not agents:
        raise RfqError("Select at least one agent")
    if not shipment.get("origin") or not shipment.get("destination"):
        raise RfqError("Origin and destination ports are required")

    # Remember hand-entered recipients so they show up as agents next time. A
    # no-op for agents already on file; non-fatal if the DB write fails.
    agent_repo.ensure_agents([{"agent_name": a.agent_name, "email": a.email} for a in agents])

    # Drafts run in parallel so latency is roughly one model call, not N.
    # `carry_context` keeps the operator's request id on every line the drafting
    # emits — this is the money path, and a pool worker would otherwise log the
    # model's failures with no way back to the request that caused them.
    draft_one = carry_context(
        lambda a: _draft_for_agent(a, shipment, edited_subject, edited_body)
    )
    with ThreadPoolExecutor(max_workers=min(len(agents), MAX_DRAFT_WORKERS)) as pool:
        entries = list(pool.map(draft_one, agents))

    customer_email_id = customer.get("email_id", "")
    thread_id = email_repo.get_thread_id(customer_email_id) if customer_email_id else ""

    sendable = [e for e in entries if e["draft"] is not None]
    to_send = [
        {
            "vendor_name": e["draft"].vendor_name,
            "vendor_email": e["draft"].vendor_email or e["email"],
            "subject": e["draft"].subject,
            "body": e["draft"].body,
        }
        for e in sendable
    ]
    # Reserve every row before the first mail leaves. Raises on failure, which
    # aborts the send — see _reserve_jobs.
    _reserve_jobs(sendable, shipment, customer, customer_email_id, thread_id)

    try:
        results = send_rfq_emails_batch(to_send)
    except Exception as exc:
        logger.exception("Batch send failed: %s", exc)
        results = [{"status": f"batch_error: {exc}"} for _ in to_send]
    _attach_send_status(sendable, results)

    jobs = []
    for e in entries:
        # Drafting failed: nothing was addressed and nothing was sent, so there
        # is no reference in anyone's hands and no row was reserved.
        status = e["error"] or _record_outcome(e)
        jobs.append({
            "reference": e["reference"], "agent_name": e["agent_name"],
            "email": e["email"], "status": status,
        })

    # Confirmed sends, not drafts handed to the sender.
    total_sent = sum(1 for e in sendable if e.get("send_status") == SENT_STATUS)
    return {"jobs": jobs, "shipment": shipment, "total_sent": total_sent}


def approve(reference: str) -> dict[str, Any]:
    """Award this RFQ to the agent it was sent to.

    One job is one agent, so the reference alone identifies the winner — there
    is nothing to select. Only an acceptance goes out; agents who were not
    chosen are not emailed.

    The award is recorded only once the acceptance has actually been sent.
    `set_status(reference, "approved")` used to run unconditionally, outside the
    try, and the response hardcoded `"status": "approved"` — so a dead SMTP login
    marked the job awarded, told the operator it was awarded, and left the agent
    who won it never informed. `send_rfq_email` returns `{"status": "failed"}`
    rather than raising, so the ordinary failure did not even reach the handler.
    This is the same defect `_record_outcome` above was fixed for, one function
    later, on the path that commits the desk to a vendor.

    A failure raises instead of recording a distinct status, for two reasons.
    Job statuses are enumerated in five places — `OPEN_JOB_STATUSES`,
    `metrics_service.JOB_STATUSES`, the metrics snapshot columns, and two
    frontend status maps — so a sixth value would be silently uncounted and
    render as unknown. And leaving the job in its current (open) status is
    already the truthful record: nobody has been told anything, and a reply can
    still land. `approve` is therefore safe to call again once the sender is
    fixed, which is exactly what the operator needs to do.
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
        logger.exception("Failed to send acceptance to %s: %s", agent_name, e)
        acceptance_status = f"failed: {e}"

    if acceptance_status != SENT_STATUS:
        logger.error(
            "RFQ %s NOT awarded — acceptance to %s did not send (%s); "
            "status left at %s", reference, agent_name, acceptance_status, job.status,
        )
        raise RfqError(
            f"The acceptance email to {agent_name} did not send "
            f"({acceptance_status}). {reference} is unchanged — fix the sender, "
            f"then approve again."
        )

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
