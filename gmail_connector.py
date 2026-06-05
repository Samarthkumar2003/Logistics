"""
Gmail connector using service account + Domain-Wide Delegation.

Uses google.auth + requests directly (no googleapiclient overhead).
AuthorizedSession is thread-safe → enables parallel message fetches.

Requires:
  - service_account.json in the project root
  - GMAIL_MAILBOX env var (e.g. dhaval.shah@bhatiashipping.com)
  - DWD granted in Google Workspace Admin Console
"""
import base64
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.header import decode_header

import requests as _requests
from dotenv import load_dotenv
from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession

load_dotenv()

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
SERVICE_ACCOUNT_FILE = os.path.join(os.path.dirname(__file__), "service_account.json")
GMAIL_MAILBOX = (os.getenv("GMAIL_MAILBOX") or "").strip()

_session: AuthorizedSession | None = None


def _get_session() -> AuthorizedSession:
    global _session
    if _session is not None:
        return _session
    if not GMAIL_MAILBOX:
        raise ValueError("GMAIL_MAILBOX not set in .env")
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(f"service_account.json not found at {SERVICE_ACCOUNT_FILE}")
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    ).with_subject(GMAIL_MAILBOX)
    _session = AuthorizedSession(creds)
    return _session


def _gmail_get(path: str, params: dict | None = None) -> dict:
    """GET request to Gmail API, raises on non-200."""
    session = _get_session()
    resp = session.get(f"{GMAIL_BASE}/{path}", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _decode_header_value(raw: str) -> str:
    parts = decode_header(raw or "")
    result = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            result += part.decode(enc or "utf-8", errors="replace")
        else:
            result += part
    return result


def _extract_body(payload: dict) -> str:
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    if mime.startswith("multipart/"):
        for part in payload.get("parts", []):
            text = _extract_body(part)
            if text:
                return text
    return ""


def _map_message(msg: dict) -> dict:
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    body = _extract_body(msg.get("payload", {})).strip()
    return {
        "id": msg["id"],
        "threadId": msg.get("threadId", msg["id"]),
        "from": headers.get("From", ""),
        "subject": _decode_header_value(headers.get("Subject", "")),
        "body": body or "[No plain-text body — may contain attachments only]",
    }


def _fetch_metadata(msg_id: str) -> dict:
    """Fetch sender+subject for one message. Fast — metadata only, no body."""
    msg = _gmail_get(f"messages/{msg_id}", params={
        "format": "metadata",
        "metadataHeaders": ["From", "Subject"],
        "fields": "id,threadId,snippet,payload/headers",
    })
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return {
        "id": msg["id"],
        "threadId": msg.get("threadId", msg["id"]),
        "from": headers.get("From", ""),
        "subject": _decode_header_value(headers.get("Subject", "")),
        "body": "",
        "snippet": msg.get("snippet", ""),
    }


def fetch_latest_emails(limit: int = 10, offset: int = 0) -> dict:
    """Fetch inbox metadata in parallel. Body loads on-demand via /email-body/<id>."""
    result = _gmail_get("messages", params={
        "maxResults": limit + offset,
        "labelIds": "INBOX",
        "fields": "messages/id,resultSizeEstimate",
    })
    messages = result.get("messages", [])
    total_estimate = result.get("resultSizeEstimate", 0)
    page = messages[offset:offset + limit]

    emails: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(len(page), 10)) as pool:
        future_map = {pool.submit(_fetch_metadata, ref["id"]): ref["id"] for ref in page}
        for future in as_completed(future_map):
            try:
                emails.append(future.result())
            except Exception as e:
                logger.warning("Skipping message %s: %s", future_map[future], e)

    return {"emails": emails, "total": total_estimate}


def fetch_emails_by_subject(search_term: str, limit: int = 20) -> list[dict]:
    result = _gmail_get("messages", params={
        "q": f"subject:{search_term}",
        "maxResults": limit,
        "fields": "messages/id",
    })
    messages = result.get("messages", [])
    emails = []
    for ref in messages:
        try:
            msg = _gmail_get(f"messages/{ref['id']}", params={"format": "full"})
            emails.append(_map_message(msg))
        except Exception as e:
            logger.warning("Skipping message %s: %s", ref["id"], e)
    return emails


def fetch_unseen_emails(limit: int = 20) -> list[dict]:
    result = _gmail_get("messages", params={
        "q": "is:unread",
        "labelIds": "INBOX",
        "maxResults": limit,
        "fields": "messages/id",
    })
    messages = result.get("messages", [])
    emails = []
    for ref in messages:
        try:
            msg = _gmail_get(f"messages/{ref['id']}", params={"format": "full"})
            emails.append(_map_message(msg))
        except Exception as e:
            logger.warning("Skipping message %s: %s", ref["id"], e)
    return emails


def fetch_full_message(msg_id: str) -> dict:
    """Fetch full message body for a single email (on-demand when user expands)."""
    msg = _gmail_get(f"messages/{msg_id}", params={"format": "full"})
    return _map_message(msg)
