"""Reading the mailbox as Gmail has it: the Sent folder and whole threads.

Live Gmail reads, never the store, because ingest only reads INBOX (see the
read-only section of gmail_connector). Nothing here sends, labels or modifies
anything, so none of it is affected by live email being armed.
"""

import logging
from typing import Callable, TypeVar

from fastapi import APIRouter, Query

from backend.app.errors import AppException
from backend.core.rfq_reference import extract_rfq_reference
from backend.repositories import email_repo, sent_reply_repo

logger = logging.getLogger(__name__)
router = APIRouter(tags=["mailbox"])

T = TypeVar("T")
SENT_PAGE_MAX = 50


def _gmail(what: str, call: Callable[[], T]) -> T:
    """Run a Gmail read with the same error contract as /email-body: re-consent
    needed is a 502 that says so, Gmail's own 404 is a 404, anything else 502."""
    # Lazy, like inbox.py: keeps Google's client libraries off the import path.
    from backend.connectors.google_oauth import GmailReauthRequired
    try:
        return call()
    except GmailReauthRequired as e:
        logger.error("%s needs Gmail re-consent: %s", what, e)
        raise AppException(status_code=502, detail=f"Gmail needs re-authorisation: {e}")
    except Exception as e:
        response = getattr(e, "response", None)
        if getattr(response, "status_code", None) == 404:
            raise AppException(status_code=404, detail=f"{what}: not found in Gmail, it may have been deleted")
        logger.exception("%s failed: %s", what, e)
        raise AppException(status_code=502, detail=f"Mail provider unavailable: {e}")


@router.get("/sent")
def list_sent(limit: int = Query(20, ge=1, le=SENT_PAGE_MAX), page_token: str = ""):
    """One page of the mailbox's Sent folder, newest first.

    `app_reply` marks replies this app sent; `rfq_reference` marks RFQs (their
    sends store no Gmail id, but every one carries its reference in the subject).
    """
    from backend.connectors import gmail_connector
    page = _gmail("Sent folder", lambda: gmail_connector.list_sent(limit=limit, page_token=page_token))
    mine = sent_reply_repo.app_sent_ids([i["id"] for i in page["items"]])
    for item in page["items"]:
        item["app_reply"] = item["id"] in mine
        item["rfq_reference"] = extract_rfq_reference(item.get("subject", ""))
    return page


@router.get("/sent/count")
def sent_count():
    from backend.connectors import gmail_connector
    return {"total": _gmail("Sent count", gmail_connector.sent_total)}


@router.get("/sent/{msg_id}")
def get_sent_message(msg_id: str):
    """One sent message in full, for opening a Sent row."""
    from backend.connectors import gmail_connector
    return _gmail(f"Message {msg_id}", lambda: gmail_connector.fetch_view_message(msg_id))


@router.get("/emails/{email_id}/thread")
def get_thread(email_id: str):
    """The whole conversation an email belongs to, both directions, oldest first.

    Works for any Gmail message id: an inbox email (thread id from the store) or
    a Sent row (not stored, so asked of Gmail).
    """
    from backend.connectors import gmail_connector
    try:
        thread_id = email_repo.get_thread_id(email_id)
    except Exception as e:  # noqa: BLE001 - the Gmail lookup below covers it
        logger.warning("Thread id lookup failed for %s: %s", email_id, e)
        thread_id = ""
    if thread_id:
        def stored():
            try:
                return gmail_connector.fetch_thread(thread_id)
            except Exception as e:
                if getattr(getattr(e, "response", None), "status_code", None) == 404:
                    return None  # stale stored id: ask Gmail for the current one below
                raise
        thread = _gmail(f"Thread {thread_id}", stored)
        if thread is not None:
            return thread
        logger.warning("Stored thread %s for %s is gone; asking Gmail", thread_id, email_id)
    thread_id = _gmail(f"Message {email_id}", lambda: gmail_connector.message_thread_id(email_id))
    return _gmail(f"Thread {thread_id}", lambda: gmail_connector.fetch_thread(thread_id))
