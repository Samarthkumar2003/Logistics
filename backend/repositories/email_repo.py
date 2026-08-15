"""
email_repo.py
-------------
Reads and writes over the `emails` table (and its attachments).

All email lookups key on `provider_msg_id` — the Gmail message id. It is always
present, unlike the RFC-2822 `message_id` header, and an earlier version of this
system keyed the label cache on the latter, which split the same email across two
id spaces so corrections applied to one never reached the other.
"""

import logging
from typing import Optional

from backend.core.config import settings
from backend.core.db import get_db
from backend.domain.models import Attachment, Email

logger = logging.getLogger(__name__)

_LIST_COLUMNS = (
    "provider_msg_id, sender, subject, received_at, classification, "
    "classification_status, rfq_reference"
)


def list_inbox(limit: int, offset: int, search: str = "") -> tuple[list[Email], int]:
    """A page of the inbox, newest first, with the total for pagination."""
    query = (
        get_db().table("emails")
        .select(_LIST_COLUMNS, count="exact")
        .order("received_at", desc=True)
    )
    if search:
        query = query.ilike("subject", f"%{search}%")
    result = query.range(offset, offset + limit - 1).execute()
    return [Email.from_row(r) for r in (result.data or [])], (result.count or 0)


def get_body(provider_msg_id: str) -> Optional[str]:
    """The stored body, or None when the email isn't persisted yet."""
    rows = (
        get_db().table("emails").select("body")
        .eq("provider_msg_id", provider_msg_id).limit(1).execute().data or []
    )
    return rows[0].get("body") if rows else None


def get_thread_id(provider_msg_id: str) -> str:
    rows = (
        get_db().table("emails").select("thread_id")
        .eq("provider_msg_id", provider_msg_id).limit(1).execute().data or []
    )
    return (rows[0].get("thread_id") or "") if rows else ""


def get_by_id(provider_msg_id: str) -> Optional[Email]:
    rows = (
        get_db().table("emails")
        .select(f"{_LIST_COLUMNS}, body, thread_id")
        .eq("provider_msg_id", provider_msg_id).limit(1).execute().data or []
    )
    return Email.from_row(rows[0]) if rows else None


def list_replies_for(references: list[str]) -> list[Email]:
    """Agent replies linked to any of these RFQ references, oldest first."""
    if not references:
        return []
    rows = (
        get_db().table("emails")
        .select("provider_msg_id, rfq_reference, sender, subject, body, "
                "received_at, has_attachments")
        .in_("rfq_reference", references)
        .order("received_at").execute().data or []
    )
    return [Email.from_row(r) for r in rows]


def list_unlinked_rate_cards(limit: int, offset: int) -> tuple[list[Email], int]:
    """Rate cards that never attached to an RFQ — newest first.

    Bodies are selected because the caller re-derives *why* each one failed, and
    the reference may sit in the quoted original rather than the subject.
    """
    result = (
        get_db().table("emails")
        .select("provider_msg_id, sender, subject, body, received_at", count="exact")
        .eq("classification", "quotation_rate_card")
        .is_("rfq_reference", "null")
        .order("received_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return [Email.from_row(r) for r in (result.data or [])], (result.count or 0)


def count_replies_by_reference(references: list[str]) -> dict[str, int]:
    """How many agent replies are linked to each of these RFQs.

    One query for the whole set rather than one per job — the dashboard asks
    this for every job on screen. References with no reply are simply absent
    from the result; callers default to 0.
    """
    if not references:
        return {}
    counts: dict[str, int] = {}
    for i in range(0, len(references), 100):  # stay under PostgREST IN limits
        chunk = references[i:i + 100]
        try:
            rows = (
                get_db().table("emails").select("rfq_reference")
                .in_("rfq_reference", chunk).execute().data or []
            )
        except Exception as e:
            logger.warning("Reply count lookup failed: %s", e)
            continue
        for r in rows:
            ref = r.get("rfq_reference")
            if ref:
                counts[ref] = counts.get(ref, 0) + 1
    return counts


def set_rfq_reference(provider_msg_id: str, reference: str) -> None:
    get_db().table("emails").update({"rfq_reference": reference}).eq(
        "provider_msg_id", provider_msg_id
    ).execute()


def get_cached_labels(provider_ids: list[str]) -> dict[str, dict]:
    """Human-corrected labels from the classification cache, by email id.
    A correction here outranks the label written at ingest time."""
    if not provider_ids:
        return {}
    rows = (
        get_db().table("email_classifications")
        .select("email_id, label, confidence, method")
        .in_("email_id", provider_ids).execute().data or []
    )
    return {r["email_id"]: r for r in rows}


def list_attachments(provider_msg_id: str) -> list[Attachment]:
    """Attachment metadata plus short-lived signed download URLs.

    A URL that fails to sign comes back empty rather than raising — one broken
    object should not hide the rest of the email's attachments.
    """
    email_rows = (
        get_db().table("emails").select("id")
        .eq("provider_msg_id", provider_msg_id).limit(1).execute().data or []
    )
    if not email_rows:
        return []

    rows = (
        get_db().table("attachments")
        .select("id, file_name, mime_type, size_bytes, storage_path")
        .eq("email_id", email_rows[0]["id"]).execute().data or []
    )

    attachments = []
    for row in rows:
        att = Attachment.from_row(row)
        # storage_path is empty until the background worker downloads the bytes
        # (processing_status='pending'). Skip signing rather than throw — the row
        # still surfaces with no URL yet, and fills in on a later fetch.
        if att.storage_path:
            try:
                signed = get_db().storage.from_(settings.attachment_bucket).create_signed_url(
                    att.storage_path, 300  # 5 minutes
                )
                att.url = signed.get("signedURL") or signed.get("signedUrl") or ""
            except Exception as e:
                logger.warning("Could not sign URL for %s: %s", att.file_name, e)
        attachments.append(att)
    return attachments


def count_pending_classifications() -> int:
    """Emails the classifier never managed to label."""
    try:
        return (
            get_db().table("emails").select("id", count="exact")
            .eq("classification_status", "pending").limit(1).execute().count or 0
        )
    except Exception as e:
        logger.warning("Pending count failed: %s", e)
        return 0
