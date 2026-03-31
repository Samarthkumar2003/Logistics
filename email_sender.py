"""
email_sender.py
---------------
Sends approved RFQ emails via Gmail SMTP using credentials from .env.
"""

import os
import ssl
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

EMAIL_ACCOUNT = os.getenv("EMAIL_ACCOUNT")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))


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
    if not EMAIL_ACCOUNT or not EMAIL_PASSWORD:
        error_msg = "EMAIL_ACCOUNT or EMAIL_PASSWORD not set in environment"
        logger.error(error_msg)
        return {"status": "failed", "to": to_addr, "error": error_msg}

    msg = MIMEMultipart()
    msg["From"] = EMAIL_ACCOUNT
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
            server.sendmail(EMAIL_ACCOUNT, to_addr, msg.as_string())
        logger.info("Email sent successfully to %s", to_addr)
        return {"status": "sent", "to": to_addr}
    except smtplib.SMTPAuthenticationError as exc:
        error_msg = f"SMTP authentication failed: {exc}"
        logger.error(error_msg)
        return {"status": "failed", "to": to_addr, "error": error_msg}
    except smtplib.SMTPException as exc:
        error_msg = f"SMTP error: {exc}"
        logger.error(error_msg)
        return {"status": "failed", "to": to_addr, "error": error_msg}
    except Exception as exc:
        error_msg = f"Unexpected error sending email: {exc}"
        logger.error(error_msg)
        return {"status": "failed", "to": to_addr, "error": error_msg}


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
