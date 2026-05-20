"""
Gmail connector using service account + Domain-Wide Delegation.

Requires:
  - service_account.json in the project root (downloaded from GCP)
  - GMAIL_MAILBOX env var set to the client's mailbox (e.g. logistics@client.com)
  - DWD granted in client's Google Admin Console for our service account client_id
"""
import base64
import logging
import os
from email.header import decode_header

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

SERVICE_ACCOUNT_FILE = os.path.join(os.path.dirname(__file__), "service_account.json")
GMAIL_MAILBOX = (os.getenv("GMAIL_MAILBOX") or "").strip()


def _get_service():
    if not GMAIL_MAILBOX:
        raise ValueError("GMAIL_MAILBOX not set in .env (e.g. logistics@client.com)")
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(
            f"service_account.json not found at {SERVICE_ACCOUNT_FILE}. "
            "Download it from GCP Console → IAM → Service Accounts → Keys."
        )
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    ).with_subject(GMAIL_MAILBOX)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


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
    """Recursively extract plain-text body from Gmail message payload."""
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


def _map_message(msg: dict) -> dict | None:
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    body = _extract_body(msg.get("payload", {})).strip()
    # Keep emails even with no plain-text body (e.g. attachments-only) — show subject
    return {
        "id": msg["id"],
        "threadId": msg.get("threadId", msg["id"]),
        "from": headers.get("From", ""),
        "subject": _decode_header_value(headers.get("Subject", "")),
        "body": body or "[No plain-text body — may contain attachments only]",
    }


def fetch_latest_emails(limit: int = 5, offset: int = 0) -> dict:
    service = _get_service()
    try:
        result = service.users().messages().list(
            userId="me",
            maxResults=limit + offset,
            labelIds=["INBOX"],
        ).execute()
        messages = result.get("messages", [])
        total_estimate = result.get("resultSizeEstimate", 0)
        page = messages[offset:offset + limit]
        emails = []
        for ref in page:
            try:
                msg = service.users().messages().get(
                    userId="me", id=ref["id"], format="full"
                ).execute()
                parsed = _map_message(msg)
                if parsed:
                    emails.append(parsed)
            except HttpError as e:
                logger.warning("Skipping message %s: %s", ref["id"], e)
        return {"emails": emails, "total": total_estimate}
    except HttpError as e:
        raise RuntimeError(f"Gmail API error: {e}") from e


def fetch_emails_by_subject(search_term: str, limit: int = 20) -> list[dict]:
    service = _get_service()
    try:
        result = service.users().messages().list(
            userId="me",
            q=f"subject:{search_term}",
            maxResults=limit,
        ).execute()
        messages = result.get("messages", [])
        emails = []
        for ref in messages:
            try:
                msg = service.users().messages().get(
                    userId="me", id=ref["id"], format="full"
                ).execute()
                parsed = _map_message(msg)
                if parsed:
                    emails.append(parsed)
            except HttpError as e:
                logger.warning("Skipping message %s: %s", ref["id"], e)
        return emails
    except HttpError as e:
        raise RuntimeError(f"Gmail API error: {e}") from e


def fetch_unseen_emails(limit: int = 20) -> list[dict]:
    service = _get_service()
    try:
        result = service.users().messages().list(
            userId="me",
            q="is:unread",
            labelIds=["INBOX"],
            maxResults=limit,
        ).execute()
        messages = result.get("messages", [])
        emails = []
        for ref in messages:
            try:
                msg = service.users().messages().get(
                    userId="me", id=ref["id"], format="full"
                ).execute()
                parsed = _map_message(msg)
                if parsed:
                    emails.append(parsed)
            except HttpError as e:
                logger.warning("Skipping message %s: %s", ref["id"], e)
        return emails
    except HttpError as e:
        raise RuntimeError(f"Gmail API error: {e}") from e
