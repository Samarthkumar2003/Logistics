"""
shipment_service.py
-------------------
The shipment-first read of `rfq_jobs`.

`rfq_jobs` is one row per *agent*, which is right for sending and wrong for
reading: twenty-one rows on file are seven pieces of work, so a list of rows
shows one enquiry seven times and never shows the thing an operator is deciding
about. This module groups those rows back into the enquiry they came from and
answers the two questions a freight desk actually asks — how many vendors have
come back, and is this shipment awarded yet.

Nothing here writes. Aggregation is the whole job, and the honesty of the
aggregate is the whole design: see `headline_status` for why one chip is never
allowed to be the only status on screen.
"""

import logging
from typing import Any, Iterable

from backend.domain.models import RfqJob
from backend.repositories import agent_repo, email_repo, job_repo

logger = logging.getLogger(__name__)

# Best to worst, and the order a shipment travels. A shipment showing `approved`
# has been awarded; one showing `rfqs_sent` is still waiting on everybody.
STATUS_LADDER = ("approved", "quotes_received", "rfqs_sent", "sending")

# Deliberately not on the ladder. A failed send is not a stage of progress, it is
# an RFQ that never left, and it has to be able to coexist with a shipment that is
# otherwise doing fine. See `headline_status`.
STATUS_SEND_FAILED = "send_failed"


def headline_status(statuses: Iterable[str]) -> str:
    """The single best status in a shipment — the chip the card leads with.

    Highest rung wins, because that is what the shipment has achieved: one
    approved RFQ means the shipment is awarded even though its five losing RFQs
    still read `rfqs_sent`.

    This is lossy on purpose and must never travel alone. A shipment sitting at
    `{approved: 1, rfqs_sent: 2}` is not "approved" in any complete sense, so
    callers ship `statuses` (the full breakdown) alongside it and the card renders
    both. A chip on its own would quietly turn six unanswered vendors into a
    finished job.

    `send_failed` can only be the headline when it is *all* there is. Anything
    else outranks it, so a shipment whose second RFQ bounced still leads with the
    progress it made — and the caller surfaces the failure as its own badge, which
    is the one thing an "Approved" headline is not allowed to hide.
    """
    present = set(statuses)
    for status in STATUS_LADDER:
        if status in present:
            return status
    if STATUS_SEND_FAILED in present:
        return STATUS_SEND_FAILED
    # An unrecognised status is shown as itself rather than mapped to a default:
    # a new status nobody taught this function about should look wrong on screen.
    return sorted(present)[0] if present else ""


def status_counts(jobs: Iterable[RfqJob]) -> dict[str, int]:
    """How many RFQs sit at each status. The breakdown that rides with the chip."""
    counts: dict[str, int] = {}
    for job in jobs:
        key = job.status or ""
        counts[key] = counts.get(key, 0) + 1
    return counts


def _newest(group: list[RfqJob], attr: str) -> Any:
    """A shipment-level field, taken from the most recent RFQ that has one.

    Every row in a group was built from one `_shipment()` dict, so these are
    normally identical and the choice does not matter. It matters when a second
    batch goes out for the same enquiry with a corrected weight: the later value
    is the current one. Scanning newest-first and skipping blanks means a
    correction wins and an empty field never overwrites a populated one.
    """
    for job in reversed(group):
        value = getattr(job, attr)
        if value not in (None, ""):
            return value
    return getattr(group[-1], attr)


def _shape(
    group: list[RfqJob],
    stats: dict[str, email_repo.ReplyStats],
    categories: agent_repo.CategoryIndex,
) -> dict[str, Any]:
    """One shipment card's worth of data."""
    replied_refs = {
        j.reference for j in group
        if stats.get(j.reference, email_repo.ReplyStats(0, 0)).messages > 0
    }
    # Sent, still undecided, and nobody has written back.
    #
    # Two exclusions, both learned from the real table. `send_failed` is not
    # awaiting a reply because nothing was delivered to reply to, and counting it
    # as such is how a broken send hides inside an ordinary-looking wait.
    # `approved` is not awaiting one either: the shipment has been awarded to that
    # vendor, so the desk is not still hoping to hear from them. The Chennai
    # enquiry on file is exactly this shape — awarded, with no reply ever linked
    # against the winning RFQ — and it read as "3 awaiting" on a finished job.
    awaiting = [
        j for j in group
        if j.agents_contacted
        and j.status not in (STATUS_SEND_FAILED, "approved")
        and j.reference not in replied_refs
    ]

    rfqs = []
    type_counts: dict[str, int] = {}
    for job in group:
        stat = stats.get(job.reference, email_repo.ReplyStats(0, 0))
        category = categories.category_for(job.agent_name, job.draft_to)
        type_counts[category] = type_counts.get(category, 0) + 1
        rfqs.append({
            "reference": job.reference,
            "status": job.status,
            "agent_name": job.agent_name,
            "agent_email": job.draft_to,
            # "" means the roster cannot tell us. The frontend must render that as
            # unknown, never as a default category.
            "agent_category": category,
            "reply_count": stat.messages,
            "replied": stat.messages > 0,
            "created_at": job.created_at,
        })

    head = group[0]
    return {
        # The detail page's key. Null for an orphan send with no source email,
        # which therefore has no request page to open.
        "customer_email_id": head.customer_email_id,
        "customer_thread_id": head.customer_thread_id,
        "customer_email_sender": _newest(group, "customer_sender"),
        "customer_email_subject": _newest(group, "customer_subject"),
        "shipment_origin": _newest(group, "origin"),
        "shipment_destination": _newest(group, "destination"),
        "shipment_mode": _newest(group, "mode"),
        "shipment_commodity": _newest(group, "commodity"),
        "shipment_weight_kg": _newest(group, "weight_kg"),
        "shipment_size": _newest(group, "size"),
        "status": headline_status(j.status for j in group),
        "statuses": status_counts(group),
        # Its own field, never folded into `statuses` for display purposes, because
        # the card has to be able to show it next to a healthy headline.
        "send_failed": sum(1 for j in group if j.status == STATUS_SEND_FAILED),
        "rfq_count": len(group),
        # One RFQ is one agent, so "references that got a reply" *is* "vendors who
        # came back". Counting messages instead would report an agent's correction
        # as a second vendor competing.
        "agents_replied": len(replied_refs),
        "reply_count": sum(
            stats.get(j.reference, email_repo.ReplyStats(0, 0)).messages for j in group
        ),
        "awaiting": len(awaiting),
        "agents_contacted": sorted({a for j in group for a in j.agents_contacted}),
        "agent_types": type_counts,
        "rfqs": rfqs,
        "first_sent_at": head.created_at,
        "last_sent_at": group[-1].created_at,
    }


def list_shipments(limit: int = 20, offset: int = 0) -> dict[str, Any]:
    """A page of shipments, most recently active first.

    Paged by shipment on the server. Grouping a page of *rows* in the browser
    instead would silently under-report the RFQ count of any shipment whose rows
    straddle the row limit — and the seven-RFQ shipment on file is already a third
    of one twenty-row page.
    """
    page = job_repo.list_shipment_groups(limit=limit, offset=offset)
    references = [j.reference for g in page.groups for j in g if j.reference]

    # Two batched reads for the whole page, not per shipment and not per RFQ.
    stats = email_repo.reply_stats_by_reference(references)
    categories = agent_repo.category_index()

    return {
        "shipments": [_shape(g, stats, categories) for g in page.groups],
        "total": page.total,
        # True when the grouping window filled, which makes `total` a floor. The
        # caller must word it as "at least N" rather than printing it as a count.
        "truncated": page.truncated,
        # `or page.truncated`, because `total` is only a floor once the window has
        # filled. Comparing against it alone would report "no more shipments" at the
        # edge of a truncated scan and stop a paging caller early, on the one table
        # size where there is most certainly more to see.
        "has_more": (offset + limit) < page.total or page.truncated,
    }
