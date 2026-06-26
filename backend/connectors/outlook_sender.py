"""
outlook_sender.py
-----------------
Sends emails via Microsoft Graph API (Office 365 / Outlook).

Implements the same public interface as email_sender.py:
  - send_rfq_email(to_addr, subject, body) -> dict
  - send_rfq_emails_batch(drafts) -> list[dict]

Return shapes are identical to the Gmail implementation so that
api.py and automation.py require no changes.
"""

import logging
import os

import requests
from dotenv import load_dotenv

from backend.connectors.graph_auth import get_graph_token

load_dotenv()

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

OUTLOOK_MAILBOX = os.getenv("OUTLOOK_MAILBOX", "")


def _mailbox() -> str:
    if not OUTLOOK_MAILBOX:
        raise ValueError(
            "OUTLOOK_MAILBOX env var must be set when EMAIL_PROVIDER=outlook"
        )
    return OUTLOOK_MAILBOX


def send_rfq_email(to_addr: str, subject: str, body: str) -> dict:
    """Send a single email via Graph API sendMail endpoint.

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
    try:
        mailbox = _mailbox()
    except ValueError as exc:
        logger.error(str(exc))
        return {"status": "failed", "to": to_addr, "error": str(exc)}

    url = f"{GRAPH_BASE}/users/{mailbox}/sendMail"
    payload = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": "Text",
                "content": body,
            },
            "toRecipients": [
                {"emailAddress": {"address": to_addr}}
            ],
        },
        "saveToSentItems": True,
    }

    try:
        token = get_graph_token()
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )

        # Graph returns 202 Accepted with no body on success.
        if resp.status_code == 202:
            logger.info("Email sent via Graph API to %s", to_addr)
            return {"status": "sent", "to": to_addr}

        error_msg = f"Graph API {resp.status_code}: {resp.text[:200]}"
        logger.error("Failed to send email to %s: %s", to_addr, error_msg)
        return {"status": "failed", "to": to_addr, "error": error_msg}

    except Exception as exc:
        error_msg = f"Unexpected error sending email via Graph: {exc}"
        logger.error(error_msg)
        return {"status": "failed", "to": to_addr, "error": error_msg}


def send_rfq_emails_batch(drafts: list[dict]) -> list[dict]:
    """Send a batch of RFQ emails via Graph API.

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
