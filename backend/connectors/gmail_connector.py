"""
Gmail connector. Two auth paths, preferred first:

  1. User OAuth — a refresh token granted by the mailbox owner themselves.
     Set GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET /
     GMAIL_REFRESH_TOKEN. No admin involvement. See google_oauth.py and
     backend/scripts/authorize_gmail.py.

  2. Service account + Domain-Wide Delegation — service_account.json plus DWD
     granted in the Workspace Admin Console. Legacy, being retired: the client
     is revoking admin delegation.

Both target users/me, so the whole API surface below is identical either way.
Uses google.auth + requests directly (no googleapiclient overhead).
AuthorizedSession is thread-safe → enables parallel message fetches.
"""
import base64
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from google.auth.exceptions import RefreshError
from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession

from backend.connectors.google_oauth import (
    REAUTH_HINT,
    SCOPES,
    GmailReauthRequired,
    build_user_credentials,
    oauth_configured,
)

logger = logging.getLogger(__name__)

from backend.core.config import settings
from backend.core.logging_context import carry_context
from backend.core.paths import SERVICE_ACCOUNT_FILE

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
GMAIL_MAILBOX = settings.gmail_mailbox

_session: AuthorizedSession | None = None


def _service_account_credentials() -> service_account.Credentials:
    """Legacy DWD path — impersonates GMAIL_MAILBOX via admin-granted delegation."""
    if not GMAIL_MAILBOX:
        raise ValueError("GMAIL_MAILBOX not set in .env")
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(f"service_account.json not found at {SERVICE_ACCOUNT_FILE}")
    return service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    ).with_subject(GMAIL_MAILBOX)


def _verify_mailbox(session: AuthorizedSession) -> None:
    """Confirm the token actually opens GMAIL_MAILBOX.

    Under user OAuth the mailbox is whoever consented, not whoever .env names —
    a mismatch would otherwise silently ingest the wrong inbox. Fail loudly at
    startup instead.
    """
    if not GMAIL_MAILBOX:
        return
    resp = session.get(f"{GMAIL_BASE}/profile", timeout=15)
    resp.raise_for_status()
    actual = (resp.json().get("emailAddress") or "").strip()
    if actual.lower() != GMAIL_MAILBOX.lower():
        raise ValueError(
            f"Gmail token belongs to {actual!r}, but GMAIL_MAILBOX is "
            f"{GMAIL_MAILBOX!r}. Re-run authorize_gmail signed in as {GMAIL_MAILBOX}."
        )


def _get_session() -> AuthorizedSession:
    global _session
    if _session is not None:
        return _session
    if oauth_configured():
        logger.info("Gmail auth: user OAuth (%s)", GMAIL_MAILBOX or "mailbox unset")
        session = AuthorizedSession(build_user_credentials())
        try:
            _verify_mailbox(session)
        except RefreshError as e:
            logger.error(REAUTH_HINT)
            raise GmailReauthRequired(REAUTH_HINT) from e
    else:
        logger.warning(
            "Gmail auth: falling back to service account + Domain-Wide Delegation. "
            "This path dies when the client revokes admin delegation — set "
            "GMAIL_REFRESH_TOKEN to migrate."
        )
        session = AuthorizedSession(_service_account_credentials())
    _session = session
    return _session


def _gmail_get(path: str, params: dict | None = None) -> dict:
    """GET request to Gmail API, raises on non-200.

    A dead refresh token surfaces as GmailReauthRequired rather than a generic
    fetch failure — retrying never fixes it, a human has to re-consent.
    """
    session = _get_session()
    try:
        resp = session.get(f"{GMAIL_BASE}/{path}", params=params, timeout=15)
    except RefreshError as e:
        logger.error(REAUTH_HINT)
        raise GmailReauthRequired(REAUTH_HINT) from e
    resp.raise_for_status()
    return resp.json()


def _gmail_post(path: str, payload: dict) -> dict:
    """POST request to Gmail API, raises on non-2xx.

    Same reauth contract as _gmail_get: a dead refresh token is a human problem,
    not a retryable one.
    """
    session = _get_session()
    try:
        resp = session.post(f"{GMAIL_BASE}/{path}", json=payload, timeout=30)
    except RefreshError as e:
        logger.error(REAUTH_HINT)
        raise GmailReauthRequired(REAUTH_HINT) from e
    resp.raise_for_status()
    return resp.json()


def send_message(to_addr: str, subject: str, body: str) -> str:
    """Send a plain-text message as the authenticated mailbox. Returns the message id.

    No From header is set on purpose: Gmail stamps it with the token owner, i.e.
    GMAIL_MAILBOX. That is the whole point of this path — SMTP sends as
    EMAIL_ACCOUNT, a different identity from the mailbox the ingest reads, so
    vendor replies landed where nothing was watching. Uses the gmail.send scope
    already granted on the same refresh token as the read path.
    """
    msg = MIMEMultipart()
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return _gmail_post("messages/send", {"raw": raw}).get("id", "")


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
    fetch_one = carry_context(_fetch_metadata)
    with ThreadPoolExecutor(max_workers=min(len(page), 10)) as pool:
        future_map = {pool.submit(fetch_one, ref["id"]): ref["id"] for ref in page}
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


# ---------------------------------------------------------------------------
# Attachments + incremental ingestion (Phase 1: persist emails + originals)
# ---------------------------------------------------------------------------

def _collect_attachments(payload: dict) -> list[dict]:
    """Walk the MIME tree and return metadata for every real attachment part
    (anything with a filename + an attachmentId). Bytes are fetched lazily via
    fetch_attachment(); this only returns {filename, mime_type, attachment_id, size}."""
    found: list[dict] = []
    filename = payload.get("filename") or ""
    body = payload.get("body", {})
    att_id = body.get("attachmentId")
    if filename and att_id:
        found.append({
            "filename": filename,
            "mime_type": payload.get("mimeType", ""),
            "attachment_id": att_id,
            "size_bytes": body.get("size"),
        })
    for part in payload.get("parts", []) or []:
        found.extend(_collect_attachments(part))
    return found


def fetch_attachment(msg_id: str, attachment_id: str) -> bytes:
    """Download one attachment's raw bytes from Gmail."""
    resp = _gmail_get(f"messages/{msg_id}/attachments/{attachment_id}")
    data = resp.get("data", "")
    return base64.urlsafe_b64decode(data + "==") if data else b""


def _map_full_record(msg: dict) -> dict:
    """Map a format=full message into a persistence record: stable Message-ID,
    received timestamp, body, thread, and attachment metadata."""
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    body = _extract_body(msg.get("payload", {})).strip()
    attachments = _collect_attachments(msg.get("payload", {}))
    internal = msg.get("internalDate")  # epoch millis, as string
    received_at = None
    if internal:
        # ISO 8601 UTC; let the DB/driver coerce. epoch millis -> seconds.
        from datetime import datetime, timezone
        received_at = datetime.fromtimestamp(int(internal) / 1000, tz=timezone.utc).isoformat()
    return {
        "provider": "gmail",
        "provider_msg_id": msg["id"],
        "message_id": (headers.get("Message-ID") or headers.get("Message-Id") or "").strip(),
        "thread_id": msg.get("threadId", msg["id"]),
        "sender": headers.get("From", ""),
        "subject": _decode_header_value(headers.get("Subject", "")),
        "body": body,
        "has_attachments": bool(attachments),
        "attachments": attachments,
        "received_at": received_at,
    }


MAX_INCREMENTAL_FETCH = 5000  # safety ceiling for a very stale watermark


def iter_message_id_pages(after_epoch_s: int | None = None, page_size: int = 500,
                          query: str | None = None):
    """Yield successive pages (lists) of INBOX message ids, newest-first.
    Pages through everything — the caller decides when to stop (e.g. once a page
    is entirely already-ingested). `after_epoch_s` sets a lower-bound floor;
    `query` overrides it with a raw Gmail search string (e.g. an after/before window)."""
    q = query if query is not None else (f"after:{after_epoch_s}" if after_epoch_s is not None else None)
    params = {"labelIds": "INBOX", "maxResults": min(page_size, 500),
              "fields": "messages/id,nextPageToken"}
    if q:
        params["q"] = q
    page_token = None
    while True:
        if page_token:
            params["pageToken"] = page_token
        else:
            params.pop("pageToken", None)
        result = _gmail_get("messages", params=params)
        yield [ref["id"] for ref in result.get("messages", [])]
        page_token = result.get("nextPageToken")
        if not page_token:
            break


def count_inbox_messages(after_epoch_s: int | None = None,
                         before_epoch_s: int | None = None) -> int:
    """Count INBOX messages in a time window (ids only, full pagination).
    Used by the sync-gap audit to compare Gmail's truth against the DB."""
    parts = []
    if after_epoch_s is not None:
        parts.append(f"after:{after_epoch_s}")
    if before_epoch_s is not None:
        parts.append(f"before:{before_epoch_s}")
    query = " ".join(parts) or None
    return sum(len(page) for page in iter_message_id_pages(query=query))


def fetch_full_records(ids: list[str]) -> list[dict]:
    """Fetch full (format=full) records for the given message ids, in parallel.
    Returns _map_full_record dicts (with attachment metadata); skips ids that error."""
    def _fetch_one(msg_id: str) -> dict | None:
        try:
            msg = _gmail_get(f"messages/{msg_id}", params={"format": "full"})
            return _map_full_record(msg)
        except Exception as e:
            logger.warning("Skipping message %s during fetch: %s", msg_id, e)
            return None

    records: list[dict] = []
    fetch = carry_context(_fetch_one)
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(fetch, mid): mid for mid in ids}
        done = 0
        for future in as_completed(futures):
            done += 1
            result = future.result()
            if result:
                records.append(result)
            if done % 50 == 0:
                logger.info("Fetched %d/%d full messages", done, len(ids))
    return records


def fetch_messages_since(after_epoch_s: int | None = None, max_results: int = 500) -> list[dict]:
    """Fetch full records for INBOX messages newer than after_epoch_s
    (Gmail `after:` query, seconds).

    Incremental mode (after_epoch_s set): pages through ALL matching messages.
    Gmail returns ids newest-first, so keeping only the newest `max_results`
    silently drops older mail — that is exactly how the Jul 10-19 gap happened.
    A safety ceiling guards against a runaway backlog from a very stale watermark.

    Initial mode (after_epoch_s=None): returns the most recent `max_results`."""
    incremental = after_epoch_s is not None
    soft_cap = MAX_INCREMENTAL_FETCH if incremental else max_results
    ids: list[str] = []
    for page in iter_message_id_pages(after_epoch_s):
        ids.extend(page)
        if len(ids) >= soft_cap:
            break
    if incremental and len(ids) >= MAX_INCREMENTAL_FETCH:
        logger.warning("Incremental fetch hit safety ceiling %d — watermark may be too "
                       "old; some older mail could still be beyond this window", MAX_INCREMENTAL_FETCH)

    target = ids if incremental else ids[:max_results]
    return fetch_full_records(target)
