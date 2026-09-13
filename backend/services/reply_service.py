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
from backend.repositories import agent_repo, email_repo, job_repo

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


def _as_dict(e: Email, agent_name: str = "", linked: bool = True) -> dict[str, Any]:
    return {
        "id": e.id,
        "rfq_reference": e.rfq_reference,
        "agent_name": agent_name,
        "sender": e.sender,
        "subject": e.subject,
        "body": e.body,
        "received_at": e.received_at,
        "has_attachments": e.has_attachments,
        "thread_id": e.thread_id,
        # False for a message that sits in a linked reply's thread but carries no
        # reference of its own. It is shown for context; it is not attribution.
        "linked": linked,
    }


def list_for_reference(reference: str) -> list[dict[str, Any]]:
    """Everything the agent has sent on this RFQ, newest first — the linked
    replies plus the rest of their thread.

    Attribution is still reference-only: nothing here writes `rfq_reference`, and
    a message is marked `linked: false` unless it cited the reference itself. But
    a *reading* panel that only shows linked messages loses the follow-up. Agents
    reply twice — a correction, an omitted surcharge, a revised validity — and the
    second message routinely drops the token, because "Re:" chains get retyped and
    some clients rewrite the subject. The thread id comes from Gmail and is not a
    guess, so widening the view to the thread costs nothing and is the difference
    between reading the agent's latest word and reading their first one.
    """
    linked = email_repo.list_replies_for([reference])
    if not linked:
        return []

    linked_ids = {e.id for e in linked}
    siblings = [
        e for e in email_repo.list_thread_messages([e.thread_id for e in linked])
        # A sibling already linked to a *different* RFQ belongs on that RFQ's
        # panel, not this one. Same thread is context; it is not a claim.
        if e.id not in linked_ids and (e.rfq_reference or reference) == reference
    ]

    combined = [_as_dict(e) for e in linked] + [_as_dict(e, linked=False) for e in siblings]
    combined.sort(key=lambda r: r.get("received_at") or "", reverse=True)
    return combined


def _unlinked_reason(cited: Optional[str], job_exists: bool, queued: bool) -> str:
    """Why one rate card is still unlinked.

    Derived rather than stored, because the cases want different responses. The
    reason used to be `f"Cites {cited}, which matches no job"` for every row that
    cited anything at all — no job lookup ran, so the sentence was an assertion
    the code had not checked. It was routinely wrong: linking happens in the
    scan, and the scan is FIFO over `emails.processed_at`, so a reply that landed
    minutes ago sits behind every older unprocessed email. A reply whose job was
    sitting in `rfq_jobs` the whole time was reported as citing a reference that
    matched nothing — which reads as data loss and sends you looking for a bug in
    attribution instead of at a queue that has not drained.
    """
    if not cited:
        return "No RFQ reference in the reply"
    if not job_exists:
        return f"Cites {cited}, which matches no job"
    if queued:
        return (f"Cites {cited} — its job exists; waiting for the inbox scan to "
                f"link it")
    return (f"Cites {cited} — its job exists but linking did not run; "
            f"needs a retry")


def list_unlinked(limit: int, offset: int) -> dict[str, Any]:
    """Rate cards that never attached, with the reason worked out per row."""
    emails, total = email_repo.list_unlinked_rate_cards(limit=limit, offset=offset)

    cited_by_id = {e.id: find_reference(e.subject, e.body) for e in emails}
    # Two batch queries for the page, not two per row.
    jobs_present = job_repo.existing_references(
        [ref for ref in cited_by_id.values() if ref]
    )
    queued = email_repo.pending_scan_ids(list(cited_by_id))

    items = []
    for e in emails:
        cited = cited_by_id[e.id]
        items.append({
            "id": e.id,
            "sender": e.sender,
            "subject": e.subject,
            "body": "",  # loaded on demand
            "label": "quotation_rate_card",
            "received_at": e.received_at,
            "cited_reference": cited,
            "reason": _unlinked_reason(cited, cited in jobs_present, e.id in queued),
        })

    return {"emails": items, "total": total, "has_more": (offset + limit) < total}


def _widen_to_threads(
    references: list[str],
    agent_by_reference: dict[str, str],
) -> tuple[list[dict[str, Any]], int]:
    """Every message these RFQs drew, linked replies plus the rest of their threads.

    Same widening `list_for_reference` does for a single RFQ, and for the same
    reason: agents reply twice — a correction, an omitted surcharge, a revised
    validity — and the second message routinely drops the reference token, because
    "Re:" chains get retyped and some clients rewrite the subject. A panel showing
    only linked mail shows the agent's first word and hides their latest.

    Attribution does not widen with it. Nothing here writes `rfq_reference`, and a
    message that did not cite one is returned with `linked: false` so the UI can
    label it as context. A sibling already linked to a *different* RFQ is dropped
    outright: it belongs on that RFQ's panel and repeating it here would be a claim.

    Returns the messages newest-first and how many of them are context-only.
    """
    linked = email_repo.list_replies_for(references)
    if not linked:
        return [], 0

    linked_ids = {e.id for e in linked}
    reference_set = set(references)
    # Which RFQ each thread belongs to, so a follow-up that dropped the token is
    # still credited to the agent whose thread it is rather than to nobody.
    reference_by_thread = {e.thread_id: e.rfq_reference for e in linked if e.thread_id}

    siblings = [
        e for e in email_repo.list_thread_messages([e.thread_id for e in linked])
        if e.id not in linked_ids
        # Unlinked: context, keep it. Linked to one of our own references: already
        # in `linked` above. Linked to somebody else's RFQ: not ours to show.
        and (not e.rfq_reference or e.rfq_reference in reference_set)
    ]

    def agent_for(e: Email, linked_msg: bool) -> str:
        reference = e.rfq_reference if linked_msg else reference_by_thread.get(e.thread_id)
        return agent_by_reference.get(reference or "", "")

    messages = (
        [_as_dict(e, agent_for(e, True)) for e in linked]
        + [_as_dict(e, agent_for(e, False), linked=False) for e in siblings]
    )
    messages.sort(key=lambda r: r.get("received_at") or "", reverse=True)
    return messages, len(siblings)


def get_customer_request(customer_email_id: str) -> Optional[dict[str, Any]]:
    """One customer enquiry, its RFQs, and every agent reply against them.
    None when neither the email nor any job exists.

    This is the shipment detail view: the enquiry that came in, a row per vendor we
    asked with that vendor's own status, and the mail threads. It is the only place
    a shipment can be awarded, because awarding means choosing between vendors and
    that is a comparison you cannot make from a single vendor's card.
    """
    email = email_repo.get_by_id(customer_email_id)
    jobs = job_repo.list_for_customer_email(customer_email_id)
    if email is None and not jobs:
        return None

    references = [j.reference for j in jobs if j.reference]
    agent_by_reference = {j.reference: j.agent_name for j in jobs if j.reference}
    replies, thread_only = _widen_to_threads(references, agent_by_reference)

    # Batched: one reply-count read and one roster read for the whole page, not one
    # per vendor row.
    stats = email_repo.reply_stats_by_reference(references)
    categories = agent_repo.category_index()
    _no_stats = email_repo.ReplyStats(messages=0, agents=0)

    return {
        "customer_email_id": customer_email_id,
        "customer_email": {
            "provider_msg_id": email.id,
            "sender": email.sender,
            "subject": email.subject,
            "body": email.body,
            "received_at": email.received_at,
        } if email else None,
        # Shipment-level facts, so the detail page can show the route it is about
        # without a second call to /jobs. Every row came from one `_shipment()`
        # dict, so these agree across the group.
        "shipment": jobs[0].shipment_dict() if jobs else None,
        "jobs": [
            {
                "reference": j.reference,
                "status": j.status,
                "agents_contacted": j.agents_contacted,
                "agent_name": j.agent_name,
                "agent_email": j.draft_to,
                # "" when the roster cannot say. Rendered as unknown, never as a
                # default category — see agent_repo.CategoryIndex.
                "agent_category": categories.category_for(j.agent_name, j.draft_to),
                "reply_count": stats.get(j.reference, _no_stats).messages,
                "replied": stats.get(j.reference, _no_stats).messages > 0,
                "created_at": j.created_at,
            }
            for j in jobs
        ],
        "replies": replies,
        "agents_contacted": sorted({a for j in jobs for a in j.agents_contacted}),
        "counts": {
            "agents": len(jobs),
            # Linked replies only, so this keeps meaning what it always meant. The
            # widened thread adds context messages, and counting those here would
            # inflate "replies received" with mail that answered nothing.
            "replies": len(replies) - thread_only,
            # Context messages pulled in by thread. Shown as its own number rather
            # than hidden inside the one above.
            "thread_only": thread_only,
            # Distinct mailboxes that answered, not messages. Two replies from one
            # agent is one response; counting messages here would say the enquiry
            # has two quotes to compare when it has one.
            "agents_replied": len({
                addr for r in replies
                if r["linked"] and (addr := email_repo.sender_address(r["sender"]))
            }),
        },
    }
