"""
inbox_service.py
----------------
Assembling the inbox view: paging, and working out which label to show.
"""

import logging
from typing import Any

from backend.domain.models import Email
from backend.repositories import email_repo

logger = logging.getLogger(__name__)


def _effective_label(email: Email, cached: dict[str, dict]) -> str:
    """Which label the operator should see.

    Order matters. A human correction in the cache outranks everything. Failing
    that, an email whose classification never succeeded reports `pending` rather
    than the `general` fallback stored alongside it — an unclassified enquiry
    that looks like a confident "general" is an RFQ nobody sends and nobody
    notices.
    """
    corrected = cached.get(email.id, {}).get("label")
    if corrected:
        return corrected
    if email.classification_status == "pending":
        return "pending"
    return email.classification or "general"


LABELS = ("customer_requirement", "quotation_rate_card", "general", "pending")


def _row(email: Email, effective: str, meta: dict) -> dict[str, Any]:
    """One inbox row as the dashboard consumes it."""
    return {
        "id": email.id,
        "sender": email.sender,
        "subject": email.subject,
        "body": "",  # loaded on demand via GET /email-body/<id>
        "label": effective,
        "label_pending": effective == "pending",
        "label_confidence": meta.get("confidence", 0.0),
        "label_method": meta.get("method", ""),
        "received_at": email.received_at,
    }


# How many database reads one page request may make while topping up. Four reads
# of `limit` rows fills a page of fifteen unless well over half the matches in
# that stretch are stale, which no measured stretch of this inbox is.
_SCAN_ROUNDS = 4


def _fill_page(limit: int, offset: int, search: str,
               label: str) -> tuple[list[dict], int, bool]:
    """Rows displaying as `label`, the offset to resume from, and whether the
    filtered set ran out.

    The database filters on the *stored* classification; the view shows
    `_effective_label`, where a cached human correction outranks it. Those two
    disagree for older mail — thread-rule relabels once wrote only the cache — so
    filtering the page again here left short pages: fifteen rows asked for, seven
    displayed. Reading on until the page is full is what makes a page of fifteen
    fifteen requests. The returned offset counts rows *scanned*, not kept, which
    is why the caller pages by it rather than by adding `limit` itself.
    """
    kept: list[dict] = []
    scanned = offset
    for _ in range(_SCAN_ROUNDS):
        emails, _total = email_repo.list_inbox(
            limit=limit, offset=scanned, search=search, label=label
        )
        if not emails:
            return kept, scanned, True
        cached = email_repo.get_cached_labels([e.id for e in emails])
        for e in emails:
            scanned += 1
            effective = _effective_label(e, cached)
            if effective != label:
                continue
            kept.append(_row(e, effective, cached.get(e.id, {})))
            if len(kept) == limit:
                return kept, scanned, False
        if len(emails) < limit:  # last stretch of the filtered set
            return kept, scanned, True
    return kept, scanned, False


def get_inbox_page(limit: int, offset: int, search: str = "",
                   label: str = "") -> dict[str, Any]:
    """A page of the inbox, optionally of one label only.

    `next_offset` is the cursor for the following page. Under a label filter it
    advances by rows scanned rather than rows returned, so callers must use it
    instead of `offset + limit`, or they re-serve rows this page already showed.

    `total` under a filter counts the label as *displayed* (the classification
    cache), not as stored, because that is what the tab lists: 1054 customer
    requests rather than the stored column's 1086.

    One gap survives, and needs a database write to close. The rows the page
    *reads* are chosen by the stored column, so an email the cache moves **into**
    a label cannot appear under it however far the top-up reads. Today that is 34
    emails stored as `customer_requirement` and displayed as `general` (legacy
    thread-rule relabels, written to the cache only) — missing from the General
    tab, whose total therefore overstates by the same 34. Nothing is promoted into
    the request or rate-card tabs, so those two are complete. `total` also counts a
    handful of cache rows whose email is no longer stored, which is why end-of-list
    comes from `has_more` and not from comparing counts.
    """
    if not label:
        emails, total = email_repo.list_inbox(
            limit=limit, offset=offset, search=search
        )
        cached = email_repo.get_cached_labels([e.id for e in emails])
        items = [_row(e, _effective_label(e, cached), cached.get(e.id, {}))
                 for e in emails]
        next_offset = offset + len(emails)
        return {"emails": items, "total": total, "next_offset": next_offset,
                "has_more": next_offset < total, "label": label}

    items, next_offset, exhausted = _fill_page(limit, offset, search, label)
    total = email_repo.count_displayed_label(label, search=search)
    return {"emails": items, "total": total, "next_offset": next_offset,
            # Not `next_offset < total`: the cursor counts scanned rows and the
            # total counts matches, so they are not on the same scale.
            "has_more": not exhausted, "label": label}


def get_attachments(provider_msg_id: str) -> list[dict[str, Any]]:
    return [
        {
            "id": a.id,
            "file_name": a.file_name,
            "mime_type": a.mime_type,
            "size_bytes": a.size_bytes,
            "url": a.url,
        }
        for a in email_repo.list_attachments(provider_msg_id)
    ]
