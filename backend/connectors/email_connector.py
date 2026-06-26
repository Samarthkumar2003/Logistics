import imaplib
import email
import email.message
from email.header import decode_header
import os
from dotenv import load_dotenv

load_dotenv()

IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.gmail.com")
EMAIL_ACCOUNT = os.getenv("EMAIL_ACCOUNT")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

# EMAIL_PROVIDER options:
#   gmail            — IMAP + App Password (default, our own labsxelta inbox)
#   gmail_workspace  — Google service account + DWD (client's Google Workspace inbox)
#   outlook          — Microsoft Graph API (client's Office 365 inbox)
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "gmail").lower()


def _parse_email_message(msg: email.message.Message, imap_id: str) -> dict | None:
    """Parse a raw email.message.Message into a dict.

    Uses the Message-ID header as the stable deduplication key.
    Falls back to the IMAP numeric id if Message-ID is absent.
    Returns None if the message has no usable body.
    """
    subject_raw, encoding = decode_header(msg.get("Subject", ""))[0]
    if isinstance(subject_raw, bytes):
        subject = subject_raw.decode(encoding or "utf-8", errors="replace")
    else:
        subject = subject_raw or ""

    from_ = msg.get("From", "")

    # Stable dedup key — Message-ID is globally unique per RFC 2822
    message_id = (msg.get("Message-ID") or msg.get("Message-Id") or "").strip()
    stable_id = message_id if message_id else f"imap-{imap_id}"

    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if (
                part.get_content_type() == "text/plain"
                and "attachment" not in str(part.get("Content-Disposition"))
            ):
                try:
                    body = part.get_payload(decode=True).decode(errors="replace")
                    break
                except Exception:
                    pass
    else:
        if msg.get_content_type() == "text/plain":
            try:
                body = msg.get_payload(decode=True).decode(errors="replace")
            except Exception:
                pass

    if not body:
        return None

    return {
        "id": stable_id,
        "from": from_,
        "subject": subject,
        "body": body.strip(),
    }


def fetch_latest_emails(limit: int = 5, offset: int = 0):
    if EMAIL_PROVIDER == "gmail_workspace":
        from backend.connectors.gmail_connector import fetch_latest_emails as _gws_fetch
        return _gws_fetch(limit=limit, offset=offset)
    if EMAIL_PROVIDER == "outlook":
        from backend.connectors.outlook_connector import fetch_latest_emails as _outlook_fetch
        return _outlook_fetch(limit=limit, offset=offset)

    if not EMAIL_ACCOUNT or not EMAIL_PASSWORD:
        raise ValueError("Email credentials are not set in the .env file.")

    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
    mail.select("inbox")

    status, messages = mail.search(None, "ALL")
    if status != "OK" or not messages[0]:
        return {"emails": [], "total": 0}

    email_ids = messages[0].split()
    total = len(email_ids)

    # Reverse so newest first, then apply offset + limit
    reversed_ids = list(reversed(email_ids))
    page_ids = reversed_ids[offset:offset + limit]

    if not page_ids:
        mail.close()
        mail.logout()
        return {"emails": [], "total": total}

    latest_email_ids = page_ids

    extracted_emails = []
    seen_ids: set[str] = set()

    # Batch fetch — one IMAP round-trip for all IDs
    ids_str = b",".join(latest_email_ids)
    _, msg_data = mail.fetch(ids_str, "(BODY.PEEK[])")
    imap_idx = 0
    for response_part in msg_data:
        if isinstance(response_part, tuple):
            raw_id = latest_email_ids[imap_idx] if imap_idx < len(latest_email_ids) else b"0"
            parsed = _parse_email_message(
                email.message_from_bytes(response_part[1]),
                raw_id.decode(),
            )
            imap_idx += 1
            if parsed and parsed["id"] not in seen_ids:
                seen_ids.add(parsed["id"])
                extracted_emails.append(parsed)

    # Return newest-first
    extracted_emails.reverse()
    mail.close()
    mail.logout()
    return {"emails": extracted_emails, "total": total}


def fetch_emails_by_subject(search_term: str, limit: int = 20) -> list[dict]:
    if EMAIL_PROVIDER == "gmail_workspace":
        from backend.connectors.gmail_connector import fetch_emails_by_subject as _gws_fetch
        return _gws_fetch(search_term=search_term, limit=limit)
    if EMAIL_PROVIDER == "outlook":
        from backend.connectors.outlook_connector import fetch_emails_by_subject as _outlook_fetch
        return _outlook_fetch(search_term=search_term, limit=limit)

    if not EMAIL_ACCOUNT or not EMAIL_PASSWORD:
        raise ValueError("Email credentials are not set in the .env file.")

    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
    mail.select("inbox")

    status, messages = mail.search(None, f'SUBJECT "{search_term}"')
    if status != "OK" or not messages[0]:
        mail.close()
        mail.logout()
        return []

    email_ids = messages[0].split()
    latest_email_ids = email_ids[-limit:]

    extracted_emails = []
    seen_ids: set[str] = set()

    # Batch fetch — one IMAP round-trip
    ids_str = b",".join(latest_email_ids)
    _, msg_data = mail.fetch(ids_str, "(BODY.PEEK[])")
    imap_idx = 0
    for response_part in msg_data:
        if isinstance(response_part, tuple):
            raw_id = latest_email_ids[imap_idx] if imap_idx < len(latest_email_ids) else b"0"
            parsed = _parse_email_message(
                email.message_from_bytes(response_part[1]),
                raw_id.decode(),
            )
            imap_idx += 1
            if parsed and parsed["id"] not in seen_ids:
                seen_ids.add(parsed["id"])
                extracted_emails.append(parsed)

    mail.close()
    mail.logout()
    return extracted_emails


def fetch_unseen_emails(limit: int = 20) -> list[dict]:
    if EMAIL_PROVIDER == "gmail_workspace":
        from backend.connectors.gmail_connector import fetch_unseen_emails as _gws_fetch
        return _gws_fetch(limit=limit)
    if EMAIL_PROVIDER == "outlook":
        from backend.connectors.outlook_connector import fetch_unseen_emails as _outlook_fetch
        return _outlook_fetch(limit=limit)

    if not EMAIL_ACCOUNT or not EMAIL_PASSWORD:
        raise ValueError("Email credentials are not set in the .env file.")

    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
    mail.select("inbox")

    status, messages = mail.search(None, "UNSEEN")
    if status != "OK" or not messages[0]:
        mail.close()
        mail.logout()
        return []

    email_ids = messages[0].split()
    latest_email_ids = email_ids[-limit:]

    extracted_emails = []
    seen_ids: set[str] = set()

    # Batch fetch — one IMAP round-trip
    ids_str = b",".join(latest_email_ids)
    _, msg_data = mail.fetch(ids_str, "(BODY.PEEK[])")
    imap_idx = 0
    for response_part in msg_data:
        if isinstance(response_part, tuple):
            raw_id = latest_email_ids[imap_idx] if imap_idx < len(latest_email_ids) else b"0"
            parsed = _parse_email_message(
                email.message_from_bytes(response_part[1]),
                raw_id.decode(),
            )
            imap_idx += 1
            if parsed and parsed["id"] not in seen_ids:
                seen_ids.add(parsed["id"])
                extracted_emails.append(parsed)

    mail.close()
    mail.logout()
    return extracted_emails


if __name__ == "__main__":
    try:
        emails = fetch_latest_emails(limit=2)
        print(f"Fetched {len(emails)} emails.")
        for i, eml in enumerate(emails, 1):
            print(f"\n--- Email {i} ---")
            print(f"Subject: {eml['subject']}")
            print(f"Body: {eml['body'][:200]}...")
    except Exception as e:
        print(f"Error fetching emails: {e}")
