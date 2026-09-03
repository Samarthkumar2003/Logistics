"""Reading the inbox: list, body, attachments, and the unlinked rate cards."""

import logging
from typing import Annotated

from fastapi import APIRouter, Query

from backend.app.errors import AppException
from backend.repositories import email_repo
from backend.services import inbox_service, reply_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["inbox"])


@router.get("/fetch-inbox")
def fetch_inbox(
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    search: str = "",
    label: str = "",
):
    """A page of the inbox from Supabase — no Gmail call, so ordering and
    pagination are stable.

    `label` narrows the page to one classification, so a caller after customer
    requests gets a page of customer requests and a total of how many exist.
    Unfiltered is the whole inbox, as before.

    `limit` is capped (BUGS.md P2-7): it went straight into `.range(offset,
    offset + limit - 1)`, so `?limit=1000000` was a full-table read one typo
    away. 422 on an out-of-range value is the right answer — a silently clamped
    limit makes a paging caller loop forever on a page size it never asked for.
    """
    if label and label not in inbox_service.LABELS:
        # 422 rather than an empty page: an unknown label is a caller bug, and
        # "no customer requests" is the wrong thing to tell an operator about a
        # typo in a query string.
        raise AppException(
            status_code=422,
            detail=f"Unknown label {label!r}. Expected one of: "
                   f"{', '.join(inbox_service.LABELS)}",
        )
    try:
        return inbox_service.get_inbox_page(
            limit=limit, offset=offset, search=search, label=label
        )
    except Exception as e:
        logger.exception("Failed to fetch inbox: %s", e)
        raise AppException(status_code=500, detail=f"Failed to fetch inbox: {e}")


@router.get("/email-body/{message_id}")
def get_email_body(message_id: str):
    """Full body from the store, falling back to a live Gmail fetch.

    A message Gmail itself reports as gone is a 404 — deleted or moved mail is an
    ordinary outcome and the UI should say so. Any *other* provider failure is a
    502, because "it may have been deleted" is a dead end for an operator whose
    real problem is an expired token.
    """
    try:
        stored = email_repo.get_body(message_id)
    except Exception as e:
        logger.exception("Body lookup failed for %s: %s", message_id, e)
        raise AppException(status_code=500, detail=f"Failed to fetch email body: {e}")

    if stored:
        return {"body": stored}

    # Imported lazily, like the connector below: keeps Google's client libraries
    # off the import path of a route that usually answers from the store.
    from backend.connectors.google_oauth import GmailReauthRequired

    try:
        from backend.connectors.gmail_connector import fetch_full_message
        return {"body": fetch_full_message(message_id).get("body", "")}
    except GmailReauthRequired as e:
        # The most likely failure here by far, and the only one a human can act
        # on. Must not read as "the email is missing".
        logger.error("Body fetch for %s needs Gmail re-consent: %s", message_id, e)
        raise AppException(status_code=502, detail=f"Gmail needs re-authorisation: {e}")
    except Exception as e:
        # Only Gmail's own 404 means the message is gone. Reaching this handler
        # tells us nothing on its own — `stored` is None for every message not
        # yet persisted, which is the normal case for this fallback.
        response = getattr(e, "response", None)
        if getattr(response, "status_code", None) == 404:
            raise AppException(
                status_code=404,
                detail=f"No message {message_id} — it may have been deleted or moved",
            )
        logger.exception("Gmail body fetch failed for %s: %s", message_id, e)
        raise AppException(status_code=502, detail=f"Mail provider unavailable: {e}")


@router.get("/email-attachments/{message_id}")
def get_email_attachments(message_id: str):
    """Attachment metadata plus short-lived signed download URLs."""
    try:
        return {"attachments": inbox_service.get_attachments(message_id)}
    except Exception as e:
        logger.exception("Failed to fetch attachments for %s: %s", message_id, e)
        raise AppException(status_code=500, detail=f"Failed to fetch attachments: {e}")


@router.get("/rate-cards/unlinked")
def list_unlinked_rate_cards(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Rate cards that never attached to an RFQ, with the reason for each.

    Nothing is lost — these are ordinary inbox emails — but they are absent from
    the request page, so they need somewhere an operator will actually look.

    Capped at 100 rather than 200 (BUGS.md P2-7) because this query selects
    message **bodies**, so the same page count costs an order of magnitude more
    bytes than `/fetch-inbox` — and egress is metered on Railway.
    """
    try:
        return reply_service.list_unlinked(limit=limit, offset=offset)
    except Exception as e:
        logger.exception("Failed to list unlinked rate cards: %s", e)
        raise AppException(status_code=500, detail=f"Failed to list unlinked rate cards: {e}")
