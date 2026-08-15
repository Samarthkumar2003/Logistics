"""
email_connector.py
------------------
IMAP inbox access, and the provider switch that routes to Gmail API or Microsoft
Graph instead.

Legacy: the running service reads mail from the `emails` table, populated by
`email_store`. Only `backend/app/main.py` (the debug CLI) still calls in here.

Connections are handled by `_imap_inbox()`, which always closes. Before that,
every fetcher closed on its happy path only — so any exception mid-fetch leaked
the socket, and one of the early-return branches leaked even on success. Gmail
caps IMAP at 15 concurrent connections, so leaked sockets eventually lock the
account out of IMAP entirely.
"""

import email
import email.message
import imaplib
import logging
from contextlib import contextmanager
from email.header import decode_header

from backend.core.config import settings

logger = logging.getLogger(__name__)

IMAP_SERVER = settings.imap_server
EMAIL_ACCOUNT = settings.email_account
EMAIL_PASSWORD = settings.email_password

# EMAIL_PROVIDER options:
#   gmail            — IMAP + App Password (default, our own labsxelta inbox)
#   gmail_workspace  — Google service account / user OAuth (client's Workspace inbox)
#   outlook          — Microsoft Graph API (client's Office 365 inbox)
EMAIL_PROVIDER = settings.email_provider


@contextmanager
def _imap_inbox():
    """An authenticated INBOX connection that is always released.

    close() raises when no mailbox is selected, and logout() raises on an
    already-dead socket. Both are swallowed: cleanup failing must not replace
    the original exception with a confusing one from the teardown path.
    """
    if not EMAIL_ACCOUNT or not EMAIL_PASSWORD:
        raise ValueError("Email credentials are not set in the .env file.")

    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    try:
        mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
        mail.select("inbox")
        yield mail
    finally:
        for step in (mail.close, mail.logout):
            try:
                step()
            except Exception as e:  # noqa: BLE001 — teardown must never raise
                logger.debug("IMAP teardown step failed (ignored): %s", e)


def _parse_email_message(msg: email.message.Message, imap_id: str) -> dict | None:
    """Parse a raw message into a dict, or None when it has no usable body.

    Keyed on Message-ID, which is globally unique per RFC 2822; falls back to
    the IMAP sequence number, which is only unique within this session.
    """
    subject_raw, encoding = decode_header(msg.get("Subject", ""))[0]
    if isinstance(subject_raw, bytes):
        subject = subject_raw.decode(encoding or "utf-8", errors="replace")
    else:
        subject = subject_raw or ""

    message_id = (msg.get("Message-ID") or msg.get("Message-Id") or "").strip()

    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if (part.get_content_type() == "text/plain"
                    and "attachment" not in str(part.get("Content-Disposition"))):
                try:
                    body = part.get_payload(decode=True).decode(errors="replace")
                    break
                except Exception:
                    pass
    elif msg.get_content_type() == "text/plain":
        try:
            body = msg.get_payload(decode=True).decode(errors="replace")
        except Exception:
            pass

    if not body:
        return None

    return {
        "id": message_id or f"imap-{imap_id}",
        "from": msg.get("From", ""),
        "subject": subject,
        "body": body.strip(),
    }


def _fetch_and_parse(mail, ids: list[bytes]) -> list[dict]:
    """Batch-fetch the given ids in one round-trip and parse them.

    Drops messages with no plain-text body, and de-duplicates by Message-ID —
    the same message can appear twice when a mailbox shifts between calls.
    """
    if not ids:
        return []

    _, msg_data = mail.fetch(b",".join(ids), "(BODY.PEEK[])")
    parsed_emails: list[dict] = []
    seen: set[str] = set()
    index = 0

    for part in msg_data:
        if not isinstance(part, tuple):
            continue
        raw_id = ids[index] if index < len(ids) else b"0"
        index += 1
        parsed = _parse_email_message(email.message_from_bytes(part[1]), raw_id.decode())
        if parsed and parsed["id"] not in seen:
            seen.add(parsed["id"])
            parsed_emails.append(parsed)

    return parsed_emails


def fetch_latest_emails(limit: int = 5, offset: int = 0) -> dict:
    """A page of the inbox, newest first. Returns {"emails": [...], "total": n}."""
    if EMAIL_PROVIDER == "gmail_workspace":
        from backend.connectors.gmail_connector import fetch_latest_emails as _gws_fetch
        return _gws_fetch(limit=limit, offset=offset)
    if EMAIL_PROVIDER == "outlook":
        from backend.connectors.outlook_connector import fetch_latest_emails as _outlook_fetch
        return _outlook_fetch(limit=limit, offset=offset)

    with _imap_inbox() as mail:
        status, messages = mail.search(None, "ALL")
        if status != "OK" or not messages[0]:
            return {"emails": [], "total": 0}

        email_ids = messages[0].split()
        page = list(reversed(email_ids))[offset:offset + limit]  # newest first
        emails = _fetch_and_parse(mail, page)
        emails.reverse()
        return {"emails": emails, "total": len(email_ids)}


def fetch_emails_by_subject(search_term: str, limit: int = 20) -> list[dict]:
    if EMAIL_PROVIDER == "gmail_workspace":
        from backend.connectors.gmail_connector import fetch_emails_by_subject as _gws_fetch
        return _gws_fetch(search_term=search_term, limit=limit)
    if EMAIL_PROVIDER == "outlook":
        from backend.connectors.outlook_connector import fetch_emails_by_subject as _outlook_fetch
        return _outlook_fetch(search_term=search_term, limit=limit)

    with _imap_inbox() as mail:
        status, messages = mail.search(None, f'SUBJECT "{search_term}"')
        if status != "OK" or not messages[0]:
            return []
        return _fetch_and_parse(mail, messages[0].split()[-limit:])


def fetch_unseen_emails(limit: int = 20) -> list[dict]:
    if EMAIL_PROVIDER == "gmail_workspace":
        from backend.connectors.gmail_connector import fetch_unseen_emails as _gws_fetch
        return _gws_fetch(limit=limit)
    if EMAIL_PROVIDER == "outlook":
        from backend.connectors.outlook_connector import fetch_unseen_emails as _outlook_fetch
        return _outlook_fetch(limit=limit)

    with _imap_inbox() as mail:
        status, messages = mail.search(None, "UNSEEN")
        if status != "OK" or not messages[0]:
            return []
        return _fetch_and_parse(mail, messages[0].split()[-limit:])


if __name__ == "__main__":
    from backend.core.logging_config import configure_logging
    configure_logging()
    try:
        result = fetch_latest_emails(limit=2)
        found = result["emails"]
        print(f"Fetched {len(found)} of {result['total']} emails.")
        for i, eml in enumerate(found, 1):
            print(f"\n--- Email {i} ---")
            print(f"Subject: {eml['subject']}")
            print(f"Body: {eml['body'][:200]}...")
    except Exception as e:
        print(f"Error fetching emails: {e}")
