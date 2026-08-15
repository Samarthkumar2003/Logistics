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


def get_inbox_page(limit: int, offset: int, search: str = "") -> dict[str, Any]:
    emails, total = email_repo.list_inbox(limit=limit, offset=offset, search=search)
    cached = email_repo.get_cached_labels([e.id for e in emails])

    items = []
    for e in emails:
        label = _effective_label(e, cached)
        meta = cached.get(e.id, {})
        items.append({
            "id": e.id,
            "sender": e.sender,
            "subject": e.subject,
            "body": "",  # loaded on demand via GET /email-body/<id>
            "label": label,
            "label_pending": label == "pending",
            "label_confidence": meta.get("confidence", 0.0),
            "label_method": meta.get("method", ""),
            "received_at": e.received_at,
        })

    return {"emails": items, "total": total, "has_more": (offset + limit) < total}


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
