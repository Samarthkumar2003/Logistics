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
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.header import decode_header
from html import unescape as html_unescape
from html.parser import HTMLParser
from email.mime.application import MIMEApplication
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


def send_message(to_addr: str, subject: str, body: str, attachments: list[dict] | None = None,
                 cc: list[str] | None = None, thread_id: str = "", in_reply_to: str = "") -> str:
    """Send a plain-text message as the authenticated mailbox. Returns the message id.

    No From header is set on purpose: Gmail stamps it with the token owner, i.e.
    GMAIL_MAILBOX. That is the whole point of this path — SMTP sends as
    EMAIL_ACCOUNT, a different identity from the mailbox the ingest reads, so
    vendor replies landed where nothing was watching. Uses the gmail.send scope
    already granted on the same refresh token as the read path.

    attachments : each dict {"filename": str, "content_type": str, "data_base64": str}.

    Replying (all three optional, and an RFQ passes none of them):
      cc          extra visible recipients.
      thread_id   the Gmail thread to file the sent message under, so it sits in
                  the same conversation as the mail it answers in this mailbox.
      in_reply_to the RFC 2822 Message-ID being answered. Set as In-Reply-To and
                  References, which is what threads it in the RECIPIENT's client;
                  `threadId` only affects our own mailbox. Gmail also needs the
                  subject to match, which the caller guarantees with "Re: ".
    """
    msg = MIMEMultipart()
    msg["To"] = to_addr
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.attach(MIMEText(body, "plain"))
    for att in (attachments or []):
        try:
            part = MIMEApplication(base64.b64decode(att["data_base64"]), Name=att.get("filename", "attachment"))
            part["Content-Disposition"] = f'attachment; filename="{att.get("filename", "attachment")}"'
            msg.attach(part)
        except Exception:
            logger.exception("Skipping malformed attachment %r", att.get("filename"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    return _gmail_post("messages/send", payload).get("id", "")


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
    fetch_attachment(); this only returns
    {filename, mime_type, attachment_id, size_bytes, content_id}.

    `content_id` is what separates a document someone attached on purpose from an
    image embedded in the message body. Measured across the queue: every
    deliberately attached file carries `Content-Disposition: attachment` and **no**
    Content-ID, while every body-embedded image carries one. It costs nothing to
    read — the headers arrive in the same `format=full` response — and 94% of the
    parts we were downloading were embedded signature furniture.
    """
    found: list[dict] = []
    filename = payload.get("filename") or ""
    body = payload.get("body", {})
    att_id = body.get("attachmentId")
    if filename and att_id:
        headers = {h.get("name", "").lower(): h.get("value", "")
                   for h in payload.get("headers", []) or []}
        found.append({
            "filename": filename,
            "mime_type": payload.get("mimeType", ""),
            "attachment_id": att_id,
            "size_bytes": body.get("size"),
            "content_id": headers.get("content-id", ""),
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


def sent_contains(phrase: str, after_epoch_s: int, before_epoch_s: int) -> bool:
    """Is there a message in SENT matching `phrase` within this window?

    A point lookup, deliberately unlike everything else in this module. The
    retry sweep asks this per stuck RFQ to find out whether the mail actually
    left, so the answer is one boolean and the cost must not scale with mailbox
    size:

      * `labelIds: SENT` — never INBOX, so this cannot perturb the scan queue.
      * `maxResults: 1` — a hit or nothing; the id itself is not wanted.
      * no pagination. `iter_message_id_pages` is the function that enumerated
        25,500 ids when its `after:` filter went missing; a sweep must never
        borrow that behaviour.
      * `after:`/`before:` are required, not optional, for the same reason. The
        caller knows within seconds when the mail would have been sent, so an
        unbounded search is never the right question.

    Raises on transport or auth failure rather than returning False: "no evidence
    of a send" and "could not look" lead to opposite decisions, and a resend is
    not something to do on a failed lookup.
    """
    result = _gmail_get("messages", params={
        "labelIds": "SENT",
        "q": f'"{phrase}" after:{after_epoch_s} before:{before_epoch_s}',
        "maxResults": 1,
        "fields": "messages/id",
    })
    return bool(result.get("messages"))


def count_inbox_messages(after_epoch_s: int, before_epoch_s: int) -> int:
    """Count INBOX messages in the window [after, before) — ids only, but FULL
    pagination. Used by the sync-gap audit to compare Gmail's truth to the DB.

    Both bounds are REQUIRED, and None raises rather than widening the search.
    They used to default to None, where None meant "omit that side of the filter":
    calling this with no arguments built no `q` at all and counted the entire
    mailbox, 500 ids per request, with no ceiling of any kind.

    That is the exact shape of the incident this codebase already paid for — a
    lower bound that went missing turned a routine sweep into a 25,500-id
    enumeration. It would be worse here than there: `fetch_messages_since` at
    least stops at MAX_INCREMENTAL_FETCH, whereas a counter that only sums page
    lengths has nothing to stop it. At 195k messages that is ~390 sequential
    requests to produce a number nobody needed to be exact.

    Both callers (the gap audit and the heal loop) always know their day window,
    so an unbounded count is never the question being asked. Failing loudly is
    what keeps it that way.
    """
    if after_epoch_s is None or before_epoch_s is None:
        raise ValueError(
            "count_inbox_messages requires both after_epoch_s and before_epoch_s — "
            "an unbounded count pages the entire mailbox with no ceiling"
        )
    query = f"after:{after_epoch_s} before:{before_epoch_s}"
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


# ---------------------------------------------------------------------------
# Read-only mailbox views: the Sent folder and whole threads
# ---------------------------------------------------------------------------
#
# Live from Gmail rather than from `emails`, because ingest reads INBOX only:
# mail sent from this app, or by the desk straight from Gmail, never reaches the
# store. A Sent folder or a thread built from the store would be missing exactly
# the half of the conversation these views exist to show.

VIEW_BODY_CHARS = 20_000      # per message; a thread is many of them
THREAD_MAX_MESSAGES = 50      # newest kept; the rest are counted, not dropped silently


class _HTMLText(HTMLParser):
    """HTML to readable plain text: block tags become line breaks, script and
    style vanish, entities are decoded. Enough to read an HTML-only email, not a
    renderer."""

    _BLOCK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
              "blockquote", "table", "hr"}
    _SKIP = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK:
            self.out.append("\n")

    def handle_startendtag(self, tag, attrs):
        # <br/> and <hr/> are one break, not the start+end pair the default gives.
        if tag in self._BLOCK and not self._skip:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    parser = _HTMLText()
    parser.feed(html or "")
    parser.close()
    lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in "".join(parser.out).splitlines()]
    # At most one blank line in a row.
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return text.strip()


def _find_part(payload: dict, mime: str) -> str:
    if payload.get("mimeType", "") == mime:
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        found = _find_part(part, mime)
        if found:
            return found
    return ""


def extract_text(payload: dict) -> str:
    """The plain-text part, else the HTML part as text. `_extract_body` (ingest)
    stays plain-only on purpose; this is for reading, where a blank message is
    worse than an approximate one. Line endings come back as LF only: mail is
    CRLF on the wire, and the view folds quoted history line by line."""
    plain = _extract_body(payload).replace("\r\n", "\n").replace("\r", "\n").strip()
    if plain:
        return plain
    return html_to_text(_find_part(payload, "text/html")).replace("\r", "")


def _iso(internal_ms: str | None) -> str | None:
    if not internal_ms:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(int(internal_ms) / 1000, tz=timezone.utc).isoformat()


def view_message(msg: dict) -> dict:
    """One message as the thread and Sent views show it."""
    payload = msg.get("payload", {}) or {}
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", []) or []}
    labels = msg.get("labelIds", []) or []
    body = extract_text(payload)
    clipped = len(body) > VIEW_BODY_CHARS
    return {
        "id": msg["id"],
        "thread_id": msg.get("threadId", msg["id"]),
        "from": _decode_header_value(headers.get("from", "")),
        "to": _decode_header_value(headers.get("to", "")),
        "cc": _decode_header_value(headers.get("cc", "")),
        "subject": _decode_header_value(headers.get("subject", "")),
        "date": _iso(msg.get("internalDate")),
        "direction": "sent" if "SENT" in labels else "received",
        "body": body[:VIEW_BODY_CHARS],
        "body_clipped": clipped,
        # Same rule as ingest: a part with a Content-ID is embedded furniture
        # (signature logos), not a document someone attached.
        "attachments": [
            {"filename": a["filename"], "mime_type": a["mime_type"], "size_bytes": a["size_bytes"]}
            for a in _collect_attachments(payload) if not a.get("content_id")
        ],
    }


def fetch_view_message(msg_id: str) -> dict:
    return view_message(_gmail_get(f"messages/{msg_id}", params={"format": "full"}))


def message_thread_id(msg_id: str) -> str:
    msg = _gmail_get(f"messages/{msg_id}", params={"format": "minimal", "fields": "id,threadId"})
    return msg.get("threadId") or msg_id


def fetch_thread(thread_id: str) -> dict:
    """Every message in a thread, oldest first, drafts left out.

    Capped at the newest THREAD_MAX_MESSAGES; `omitted` says how many older ones
    were left off so the view can say so rather than look complete.
    """
    data = _gmail_get(f"threads/{thread_id}", params={"format": "full"})
    msgs = [m for m in data.get("messages", []) or [] if "DRAFT" not in (m.get("labelIds") or [])]
    omitted = max(0, len(msgs) - THREAD_MAX_MESSAGES)
    return {"thread_id": data.get("id", thread_id), "omitted": omitted,
            "messages": [view_message(m) for m in msgs[omitted:]]}


def _fetch_sent_row(msg_id: str) -> dict:
    msg = _gmail_get(f"messages/{msg_id}", params={
        "format": "metadata",
        "metadataHeaders": ["To", "Cc", "Subject"],
        "fields": "id,threadId,snippet,internalDate,payload/headers",
    })
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", []) or []}
    return {
        "id": msg["id"],
        "thread_id": msg.get("threadId", msg["id"]),
        "to": _decode_header_value(headers.get("to", "")),
        "cc": _decode_header_value(headers.get("cc", "")),
        "subject": _decode_header_value(headers.get("subject", "")),
        "snippet": html_unescape(msg.get("snippet", "")),
        "date": _iso(msg.get("internalDate")),
    }


def list_sent(limit: int = 20, page_token: str = "") -> dict:
    """One page of the Sent folder, newest first, metadata only (bodies load on
    open). Same parallel metadata fetch as fetch_latest_emails."""
    params = {"labelIds": "SENT", "maxResults": limit,
              "fields": "messages/id,nextPageToken"}
    if page_token:
        params["pageToken"] = page_token
    result = _gmail_get("messages", params=params)
    ids = [m["id"] for m in result.get("messages", []) or []]
    rows: dict[str, dict] = {}
    if ids:
        fetch_one = carry_context(_fetch_sent_row)
        with ThreadPoolExecutor(max_workers=min(len(ids), 10)) as pool:
            future_map = {pool.submit(fetch_one, i): i for i in ids}
            for future in as_completed(future_map):
                try:
                    rows[future_map[future]] = future.result()
                except Exception as e:
                    logger.warning("Skipping sent message %s: %s", future_map[future], e)
    # Gmail's order (newest first), not completion order.
    return {"items": [rows[i] for i in ids if i in rows],
            "next_page_token": result.get("nextPageToken", "")}


def sent_total() -> int:
    """Messages in Sent, from the label's own counter: one cheap call."""
    return int(_gmail_get("labels/SENT", params={"fields": "messagesTotal"}).get("messagesTotal", 0))
