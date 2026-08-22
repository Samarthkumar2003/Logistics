"""
email_sender.py
---------------
Sends RFQ emails. EMAIL_PROVIDER picks the path:

  gmail_workspace (or gmail_api) — Gmail API as GMAIL_MAILBOX. Preferred: mail
      goes out as the same mailbox the ingest reads, so replies come back in-band.
  outlook — Microsoft Graph API.
  gmail (default) — Gmail SMTP as EMAIL_ACCOUNT. Legacy: EMAIL_ACCOUNT is a
      different identity from GMAIL_MAILBOX, so replies land in a mailbox nothing
      ingests and the RFQ thread dead-ends.
"""

import os
import ssl
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from backend.core.config import settings

logger = logging.getLogger(__name__)

EMAIL_ACCOUNT = settings.email_account
EMAIL_PASSWORD = settings.email_password
SMTP_SERVER = settings.smtp_server
SMTP_PORT = settings.smtp_port

EMAIL_PROVIDER = settings.email_provider
EMAIL_REDIRECT = settings.email_redirect
GMAIL_MAILBOX = settings.gmail_mailbox

# Providers that send through the Gmail API as GMAIL_MAILBOX rather than SMTP.
GMAIL_API_PROVIDERS = ("gmail_workspace", "gmail_api")


def _apply_redirect(to_addr: str, subject: str) -> tuple[str, str]:
    """Resolve the real recipient and subject under EMAIL_REDIRECT safe mode.

    Shared by every send path so the redirect can never apply to one and not
    another — a half-applied redirect mails a real vendor from a test run.
    """
    if not EMAIL_REDIRECT:
        return to_addr, subject
    logger.info("EMAIL_REDIRECT active — sending to %s instead of %s",
                EMAIL_REDIRECT, to_addr)
    return EMAIL_REDIRECT, f"[TEST → {to_addr}] {subject}"


def _send_via_gmail_api(to_addr: str, subject: str, body: str) -> dict:
    """Send as GMAIL_MAILBOX over the Gmail API. Same status dict as SMTP."""
    from backend.connectors.gmail_connector import send_message

    actual_to, actual_subject = _apply_redirect(to_addr, subject)
    try:
        msg_id = send_message(to_addr=actual_to, subject=actual_subject, body=body)
        logger.info("Email sent via Gmail API as %s to %s (id=%s)",
                    GMAIL_MAILBOX or "authenticated mailbox", actual_to, msg_id)
        return {"status": "sent", "to": to_addr}
    except Exception as exc:
        error_msg = f"Gmail API send failed: {exc}"
        logger.exception(error_msg)
        return {"status": "failed", "to": to_addr, "error": error_msg}


def send_rfq_email(to_addr: str, subject: str, body: str) -> dict:
    """Send a single RFQ email and return a status dict.

    Parameters
    ----------
    to_addr : str
        Recipient email address.
    subject : str
        Email subject line.
    body : str
        Plain-text email body.

    Returns
    -------
    dict
        {"status": "sent", "to": to_addr} on success, or
        {"status": "failed", "to": to_addr, "error": "<message>"} on failure.
    """
    if EMAIL_PROVIDER == "outlook":
        from backend.connectors.outlook_sender import send_rfq_email as _outlook_send
        return _outlook_send(to_addr=to_addr, subject=subject, body=body)

    if EMAIL_PROVIDER in GMAIL_API_PROVIDERS:
        return _send_via_gmail_api(to_addr=to_addr, subject=subject, body=body)

    if not EMAIL_ACCOUNT or not EMAIL_PASSWORD:
        error_msg = "EMAIL_ACCOUNT or EMAIL_PASSWORD not set in environment"
        logger.error(error_msg)
        return {"status": "failed", "to": to_addr, "error": error_msg}

    actual_to, actual_subject = _apply_redirect(to_addr, subject)

    msg = MIMEMultipart()
    msg["From"] = EMAIL_ACCOUNT
    msg["To"] = actual_to
    msg["Subject"] = actual_subject
    msg.attach(MIMEText(body, "plain"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
            server.sendmail(EMAIL_ACCOUNT, actual_to, msg.as_string())
        logger.info("Email sent successfully to %s", actual_to)
        return {"status": "sent", "to": to_addr}
    except smtplib.SMTPAuthenticationError as exc:
        error_msg = f"SMTP authentication failed: {exc}"
        logger.error(error_msg)
        return {"status": "failed", "to": to_addr, "error": error_msg}
    except smtplib.SMTPException as exc:
        error_msg = f"SMTP error: {exc}"
        logger.exception(error_msg)
        return {"status": "failed", "to": to_addr, "error": error_msg}
    except Exception as exc:
        error_msg = f"Unexpected error sending email: {exc}"
        logger.exception(error_msg)
        return {"status": "failed", "to": to_addr, "error": error_msg}


def was_sent(phrase: str, after_epoch_s: int, before_epoch_s: int) -> Optional[bool]:
    """Did a message containing `phrase` leave this mailbox in that window?

    True / False / None, and the None matters as much as the other two. The retry
    sweep uses this to decide whether an RFQ abandoned mid-send actually went
    out, and only a definite False may lead to a resend:

        True   the mail left. Fix the bookkeeping, send nothing.
        False  it provably never left. Safe to send.
        None   cannot tell — no Sent access on this provider, or the lookup
               failed. Send NOTHING and let a human decide.

    Only the Gmail API path can answer. SMTP has no Sent folder to read (mail
    sent via smtplib is never stored anywhere we can query), and the Outlook path
    has no equivalent implemented, so both return None rather than a guess. That
    makes the conservative answer the default for every provider that cannot
    prove anything.
    """
    if EMAIL_PROVIDER not in GMAIL_API_PROVIDERS:
        logger.warning(
            "EMAIL_PROVIDER=%s cannot prove whether a message was sent — "
            "no Sent-folder lookup available, so no retry can be authorised",
            EMAIL_PROVIDER,
        )
        return None

    from backend.connectors.gmail_connector import sent_contains

    try:
        return sent_contains(phrase, after_epoch_s, before_epoch_s)
    except Exception as exc:
        # Deliberately not False. A failed lookup that read as "never sent"
        # would authorise a duplicate RFQ to a real vendor.
        logger.warning("Sent-folder lookup for %r failed: %s", phrase, exc)
        return None


def send_rfq_emails_batch(drafts: list[dict]) -> list[dict]:
    """Send a batch of RFQ emails.

    Parameters
    ----------
    drafts : list[dict]
        Each dict should contain:
        - vendor_name (str): Name of the vendor.
        - subject (str): Email subject line.
        - body (str): Plain-text email body.
        - vendor_email (str, optional): Recipient address. If missing the
          draft is skipped.

    Returns
    -------
    list[dict]
        One result dict per draft with status "sent", "failed", or "skipped".
    """
    if EMAIL_PROVIDER == "outlook":
        from backend.connectors.outlook_sender import send_rfq_emails_batch as _outlook_batch
        return _outlook_batch(drafts)

    results: list[dict] = []

    for draft in drafts:
        vendor_name = draft.get("vendor_name", "Unknown")
        vendor_email = draft.get("vendor_email")

        if not vendor_email:
            logger.warning(
                "No vendor_email for vendor '%s' -- skipping", vendor_name
            )
            results.append({
                "status": "skipped",
                "vendor_name": vendor_name,
                "reason": "vendor_email not provided",
            })
            continue

        subject = draft.get("subject", "")
        body = draft.get("body", "")

        result = send_rfq_email(to_addr=vendor_email, subject=subject, body=body)
        result["vendor_name"] = vendor_name
        results.append(result)

    sent = sum(1 for r in results if r["status"] == "sent")
    failed = sum(1 for r in results if r["status"] == "failed")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    logger.info(
        "Batch complete: %d sent, %d failed, %d skipped", sent, failed, skipped
    )

    return results
