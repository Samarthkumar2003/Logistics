"""
sent_reply_repo.py
------------------
Reads and writes over `sent_replies`: what the desk sent from the app, and how
each send ended. See sql/setup_sent_replies.sql for why the table exists.

Not to be confused with `reply_service`, which is about the INBOUND replies
freight agents send to our RFQs. These are outbound.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from backend.core.db import get_db

logger = logging.getLogger(__name__)

TABLE = "sent_replies"

_COLUMNS = (
    "id, email_id, thread_id, to_addr, cc, subject, body, attachment_names, "
    "status, error, provider_msg_id, sent_by_name, created_at, sent_at"
)


@dataclass
class ReplySummary:
    """What an inbox row shows about the replies sent to it, without opening it."""
    sent: int = 0
    failed: int = 0
    last_at: str | None = None
    last_status: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"sent": self.sent, "failed": self.failed,
                "last_at": self.last_at, "last_status": self.last_status}


def get_by_key(idempotency_key: str) -> dict | None:
    rows = (
        get_db().table(TABLE).select(_COLUMNS)
        .eq("idempotency_key", idempotency_key).limit(1).execute().data or []
    )
    return rows[0] if rows else None


def get(reply_id: str) -> dict | None:
    rows = (
        get_db().table(TABLE).select(_COLUMNS)
        .eq("id", reply_id).limit(1).execute().data or []
    )
    return rows[0] if rows else None


def insert_sending(row: dict) -> dict:
    """Record a send that is about to start. Returns the stored row."""
    data = get_db().table(TABLE).insert({**row, "status": "sending"}).execute().data or []
    if not data:
        raise RuntimeError("sent_replies insert returned no row")
    return data[0]


def claim_retry(reply_id: str) -> bool:
    """Move a failed reply back to 'sending', only if it is still failed.

    Conditional on the current status so two retry clicks cannot both send: the
    second finds no 'failed' row to update and gets False.
    """
    data = (
        get_db().table(TABLE).update({"status": "sending", "error": None})
        .eq("id", reply_id).eq("status", "failed").execute().data or []
    )
    return bool(data)


def outcome_update(result: dict) -> dict:
    """The columns a provider's answer sets.

    `unknown` is a failure the provider could not vouch for (timeout, 5xx; see
    email_sender._outcome_unknown). It is kept apart from `failed` because only a
    `failed` reply may be retried, and retrying one that did arrive mails the
    customer twice.
    """
    sent = result.get("status") == "sent"
    if sent:
        status = "sent"
    elif result.get("outcome_unknown"):
        status = "unknown"
    else:
        status = "failed"
    return {
        "status": status,
        "error": None if sent else (result.get("error") or "Send failed"),
        "provider_msg_id": result.get("provider_msg_id") or None,
        "sent_at": datetime.now(timezone.utc).isoformat() if sent else None,
    }


def record_outcome(reply_id: str, result: dict) -> dict:
    """Write the provider's answer onto the row and return the updated row."""
    update = outcome_update(result)
    data = get_db().table(TABLE).update(update).eq("id", reply_id).execute().data or []
    return data[0] if data else {"id": reply_id, **update}


def list_for_email(email_id: str, limit: int = 50) -> list[dict]:
    """Every reply sent to this email, newest first. Bounded: a card shows them all
    and fifty replies to one email is already a conversation that belongs in Gmail."""
    return (
        get_db().table(TABLE).select(_COLUMNS)
        .eq("email_id", email_id).order("created_at", desc=True)
        .limit(limit).execute().data or []
    )


def summaries(email_ids: list[str]) -> dict[str, ReplySummary]:
    """Per-email reply counts for one page of the inbox, in a single query.

    Reads only the three columns it needs and caps the rows, because this runs on
    every inbox page load: a page is at most 200 emails, and a cap of 2000 replies
    across them is far beyond anything a desk sends in one screen of mail.
    """
    ids = [i for i in dict.fromkeys(email_ids) if i]
    if not ids:
        return {}
    rows = (
        get_db().table(TABLE).select("email_id, status, created_at")
        .in_("email_id", ids).order("created_at", desc=True)
        .limit(2000).execute().data or []
    )
    out: dict[str, ReplySummary] = {}
    for r in rows:  # newest first, so the first row seen per email is its latest
        s = out.setdefault(r["email_id"], ReplySummary())
        if s.last_at is None:
            s.last_at, s.last_status = r.get("created_at"), r.get("status", "")
        if r.get("status") == "sent":
            s.sent += 1
        elif r.get("status") == "failed":
            s.failed += 1
    return out


def summaries_or_empty(email_ids: list[str]) -> dict[str, ReplySummary]:
    """`summaries`, but an inbox page must never fail because of it.

    The inbox is the app's front door and this is decoration on it. In particular
    it makes deploy order safe: code that reaches prod before
    setup_sent_replies.sql has been run still serves the inbox, just without
    the Replied chips.
    """
    try:
        return summaries(email_ids)
    except Exception as e:  # noqa: BLE001 - deliberate: decoration must not break the inbox
        logger.warning("Reply summaries unavailable: %s", e)
        return {}


def app_sent_ids(provider_msg_ids: list[str]) -> set[str]:
    """Which of these Gmail ids are replies this app sent. For the "Sent from app"
    chip on the Sent folder: decoration, so a failure reads as "none", never as
    a broken folder."""
    ids = [i for i in dict.fromkeys(provider_msg_ids) if i]
    if not ids:
        return set()
    try:
        rows = (get_db().table(TABLE).select("provider_msg_id")
                .in_("provider_msg_id", ids).execute().data or [])
    except Exception as e:  # noqa: BLE001 - deliberate: decoration must not break the folder
        logger.warning("App-sent lookup unavailable: %s", e)
        return set()
    return {r["provider_msg_id"] for r in rows if r.get("provider_msg_id")}
