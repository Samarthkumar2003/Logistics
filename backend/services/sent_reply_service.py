"""
sent_reply_service.py
---------------------
Replying to an email from the dashboard, and the record of every reply sent.

The rules that matter, because live email is armed:

* **One composed reply is one send.** The browser sends an idempotency key per
  composed reply. A second request with the same key returns the first row and
  sends nothing, so a double click cannot put two replies in a customer's inbox.
* **The row exists before the mail does.** It is written as `sending`, then the
  provider is called, then the outcome is recorded. A crash between the two
  leaves a `sending` row the operator can see, rather than a reply nobody knows
  went out.
* **Everything goes through `send_rfq_email`.** That is the one function where
  EMAIL_REDIRECT is applied, and it drops Cc under redirect, so a reply cannot
  route around safe mode.
* **The operator's text is sent as written.** The service adds the quoted
  original underneath and nothing else; the signature is already in the text,
  prefilled by the composer from GET /rfq-signature, the same rule as RFQs.
"""

import logging
import re
from datetime import datetime
from email.utils import parseaddr
from typing import Any

from backend.connectors.email_sender import send_rfq_email
from backend.domain.models import SenderIdentity
from backend.repositories import email_repo, sent_reply_repo

logger = logging.getLogger(__name__)

MAX_CC = 10
MAX_BODY_CHARS = 20_000
MAX_KEY_CHARS = 100
# How much of the original is quoted under the reply. Enough to show what is
# being answered; a 40 kB forwarded thread does not need to travel back.
QUOTE_CHARS = 4_000

_RE_PREFIX = re.compile(r"^\s*re\s*:", re.IGNORECASE)
_ADDRESS = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


class ReplyError(Exception):
    """A reply the service refused. `status_code` is what the route returns."""

    def __init__(self, detail: str, status_code: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def reply_subject(subject: str) -> str:
    """"Re: " plus the original, never "Re: Re: ". Gmail threads a reply only when
    the subject matches, so this is threading, not cosmetics.

    A blank original gets a bare "Re:", which is what Gmail's own Reply sends.
    The recipient's Gmail strips the prefix before comparing, so "Re:" matches a
    blank subject; "Re: (no subject)" does not, and landed as a new conversation.
    """
    s = (subject or "").strip()
    if not s:
        return "Re:"
    return s if _RE_PREFIX.match(s) else f"Re: {s}"


def _valid_address(addr: str) -> bool:
    return bool(_ADDRESS.match(addr))


def normalise_cc(cc: list[str] | None, to_addr: str) -> list[str]:
    """Bare, lowercased, de-duplicated Cc addresses, without the To address.

    Refuses an unparseable entry instead of dropping it: an operator who typed a
    Cc and was told "sent" would believe that person had been copied.
    """
    out: list[str] = []
    for raw in cc or []:
        if not (raw or "").strip():
            continue
        _, addr = parseaddr(raw)
        addr = (addr or raw).strip().lower()
        if not _valid_address(addr):
            raise ReplyError(f"Not an email address: {raw.strip()!r}")
        if addr != to_addr and addr not in out:
            out.append(addr)
    if len(out) > MAX_CC:
        raise ReplyError(f"At most {MAX_CC} Cc addresses per reply")
    return out


def _when(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d %b %Y, %H:%M UTC")
    except ValueError:
        return iso


def quoted_original(target: dict) -> str:
    """The original message as an "On ..., X wrote:" block, or '' with no body."""
    body = (target.get("body") or "").strip()
    if not body:
        return ""
    clipped = body[:QUOTE_CHARS]
    lines = [f"> {line}" if line else ">" for line in clipped.splitlines()]
    if len(body) > QUOTE_CHARS:
        lines.append("> […]")
    when = _when(target.get("received_at"))
    header = f"On {when}, {target.get('sender', '')} wrote:" if when else f"{target.get('sender', '')} wrote:"
    return header + "\n" + "\n".join(lines)


def _deliver(to_addr: str, subject: str, text: str, cc: list[str], target: dict,
             attachments: list[dict] | None) -> dict:
    quote = quoted_original(target)
    full = f"{text}\n\n{quote}" if quote else text
    try:
        return send_rfq_email(
            to_addr, subject, full, attachments or None,
            cc=cc, thread_id=target.get("thread_id") or "",
            in_reply_to=(target.get("message_id") or "").strip(),
        )
    except Exception as e:  # send_rfq_email reports failures, but never trust that
        logger.exception("Reply send to %s raised", to_addr)
        return {"status": "failed", "error": f"Send failed: {e}"}


def public(row: dict) -> dict[str, Any]:
    """A stored reply as the browser sees it."""
    return {
        "id": row.get("id"),
        "email_id": row.get("email_id"),
        "to_addr": row.get("to_addr", ""),
        "cc": row.get("cc") or [],
        "subject": row.get("subject", ""),
        "body": row.get("body", ""),
        "attachment_names": row.get("attachment_names") or [],
        "status": row.get("status", ""),
        "error": row.get("error"),
        "sent_by_name": row.get("sent_by_name", ""),
        "created_at": row.get("created_at"),
        "sent_at": row.get("sent_at"),
    }


def send_reply(
    email_id: str,
    body: str,
    sender: SenderIdentity,
    idempotency_key: str,
    user_id: str = "",
    cc: list[str] | None = None,
    attachments: list[dict] | None = None,
) -> dict[str, Any]:
    """Send one reply to the sender of `email_id` and return its stored row."""
    key = (idempotency_key or "").strip()
    if not key or len(key) > MAX_KEY_CHARS:
        raise ReplyError("Missing or malformed idempotency key; reload the page")
    text = (body or "").strip()
    if not text:
        raise ReplyError("Write a reply before sending")
    if len(text) > MAX_BODY_CHARS:
        raise ReplyError(f"Reply is over {MAX_BODY_CHARS:,} characters")

    existing = sent_reply_repo.get_by_key(key)
    if existing:
        logger.info("Reply %s already recorded for key %s; not sending again",
                    existing.get("id"), key)
        return public(existing)

    target = email_repo.get_reply_target(email_id)
    if target is None:
        raise ReplyError(f"Email {email_id} not found", status_code=404)
    to_addr = email_repo.sender_address(target.get("sender", ""))
    if not _valid_address(to_addr):
        raise ReplyError("Can't work out a reply address from this email's sender")
    cc_list = normalise_cc(cc, to_addr)
    subject = reply_subject(target.get("subject", ""))

    try:
        row = sent_reply_repo.insert_sending({
            "email_id": email_id,
            "thread_id": target.get("thread_id") or "",
            "to_addr": to_addr,
            "cc": cc_list,
            "subject": subject,
            "body": text,
            "attachment_names": [a.get("filename", "attachment") for a in (attachments or [])],
            "sent_by": user_id or "",
            "sent_by_name": sender.name,
            "idempotency_key": key,
        })
    except Exception:
        # Most likely the unique key: a concurrent request with the same key won
        # the insert. Its row is the answer, and this request must not send.
        raced = sent_reply_repo.get_by_key(key)
        if raced:
            return public(raced)
        raise

    result = _deliver(to_addr, subject, text, cc_list, target, attachments)
    return public(sent_reply_repo.record_outcome(row["id"], result))


def retry(reply_id: str) -> dict[str, Any]:
    """Send a failed reply again, as it was. Same row, so history shows one reply."""
    row = sent_reply_repo.get(reply_id)
    if row is None:
        raise ReplyError(f"Reply {reply_id} not found", status_code=404)
    if row.get("status") != "failed":
        raise ReplyError(f"Only a failed reply can be retried; this one is {row.get('status')}",
                         status_code=409)
    if row.get("attachment_names"):
        raise ReplyError("This reply had attachments, which aren't stored. "
                         "Use Edit & resend and attach them again.", status_code=409)
    target = email_repo.get_reply_target(row["email_id"])
    if target is None:
        raise ReplyError(f"Email {row['email_id']} not found", status_code=404)
    if not sent_reply_repo.claim_retry(reply_id):
        # Someone else flipped it out of 'failed' between our read and now.
        raise ReplyError("This reply is already being retried", status_code=409)

    result = _deliver(row["to_addr"], row["subject"], row["body"], list(row.get("cc") or []),
                      target, None)
    return public(sent_reply_repo.record_outcome(reply_id, result))


def list_for_email(email_id: str) -> list[dict[str, Any]]:
    return [public(r) for r in sent_reply_repo.list_for_email(email_id)]
