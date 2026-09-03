"""
outlook_connector.py
--------------------
Fetches emails from an Office 365 mailbox via Microsoft Graph API.

Implements the same public interface as email_connector.py:
  - fetch_latest_emails(limit, offset) -> dict
  - fetch_emails_by_subject(search_term, limit) -> list[dict]
  - fetch_unseen_emails(limit) -> list[dict]

Each returned email dict has the shape:
  {"id": str, "from": str, "subject": str, "body": str}

Note on message IDs:
  Graph message IDs are opaque base64 strings (e.g. "AAMkAGI..."),
  not IMAP UIDs. Callers (api.py, automation.py) treat IDs as opaque
  de-duplication keys, so Graph IDs work transparently.

Note on $skip depth:
  Graph Exchange Online supports $skip reliably up to ~1 000 items.
  For deeper pagination, switch to @odata.nextLink chaining. The current
  UI fetches small pages (limit=20, offset<100), so $skip is safe.
"""

import logging
import os
import re

import requests
from dotenv import load_dotenv

from backend.connectors.graph_auth import get_graph_token

load_dotenv()

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Required: UPN or email address of the shared mailbox to read.
OUTLOOK_MAILBOX = os.getenv("OUTLOOK_MAILBOX", "")

_COMMON_HEADERS = {
    # Ask Graph to return plain-text bodies wherever possible.
    "Prefer": 'outlook.body-content-type="text"',
}

_SELECT = "id,from,subject,body,receivedDateTime"


def _mailbox() -> str:
    if not OUTLOOK_MAILBOX:
        raise ValueError(
            "OUTLOOK_MAILBOX env var must be set when EMAIL_PROVIDER=outlook"
        )
    return OUTLOOK_MAILBOX


def _auth_headers() -> dict[str, str]:
    return {**_COMMON_HEADERS, "Authorization": f"Bearer {get_graph_token()}"}


def _extract_body(body_obj: dict) -> str:
    """Return plain text from a Graph body object.

    Graph returns {"contentType": "text"|"html", "content": "..."}.
    The Prefer header requests plain text, but falls back to HTML for
    messages that have no plain-text alternative. In that case we strip
    HTML tags with a lightweight regex — intentionally minimal to avoid
    adding beautifulsoup4 as a dependency.
    """
    content = body_obj.get("content", "")
    content_type = body_obj.get("contentType", "text")

    if content_type == "html":
        # Strip HTML tags.
        content = re.sub(r"<[^>]+>", " ", content)
        # Collapse excessive whitespace / blank lines.
        content = re.sub(r"[ \t]+", " ", content)
        content = re.sub(r"\n{3,}", "\n\n", content)

    return content.strip()


def _map_message(msg: dict) -> dict:
    """Map a Graph message object to the standard email dict shape."""
    from_obj = msg.get("from") or {}
    email_address = from_obj.get("emailAddress") or {}
    sender = email_address.get("address", "")

    return {
        "id": msg["id"],
        "from": sender,
        "subject": msg.get("subject") or "",
        "body": _extract_body(msg.get("body") or {}),
    }


def _get_message_count() -> int:
    """Return total message count for the configured mailbox inbox."""
    url = f"{GRAPH_BASE}/users/{_mailbox()}/mailFolders/inbox/messages/$count"
    resp = requests.get(
        url,
        headers={**_auth_headers(), "ConsistencyLevel": "eventual"},
        timeout=30,
    )
    if resp.status_code == 200:
        try:
            return int(resp.text)
        except ValueError:
            return 0
    logger.warning("Graph $count returned %d: %s", resp.status_code, resp.text[:200])
    return 0


def _raise_for_graph_error(resp: requests.Response, context: str) -> None:
    if resp.status_code == 401:
        raise RuntimeError(
            f"Graph 401 Unauthorized during {context}. "
            "Check that admin consent has been granted and credentials are correct."
        )
    if resp.status_code == 403:
        raise RuntimeError(
            f"Graph 403 Forbidden during {context}. "
            f"The app may lack Mail.Read permission on mailbox '{OUTLOOK_MAILBOX}'."
        )
    if not resp.ok:
        raise RuntimeError(
            f"Graph API error {resp.status_code} during {context}: {resp.text[:300]}"
        )


def fetch_latest_emails(limit: int = 5, offset: int = 0) -> dict:
    """Fetch the most recent emails from the inbox, newest first.

    Parameters
    ----------
    limit:  Number of emails per page (default 5).
    offset: Zero-based page offset (default 0).

    Returns
    -------
    dict with keys:
      "emails" : list of email dicts
      "total"  : total number of messages in the inbox
    """
    mailbox = _mailbox()
    url = f"{GRAPH_BASE}/users/{mailbox}/mailFolders/inbox/messages"
    params = {
        "$top": limit,
        "$skip": offset,
        "$orderby": "receivedDateTime desc",
        "$select": _SELECT,
    }

    resp = requests.get(url, headers=_auth_headers(), params=params, timeout=30)
    _raise_for_graph_error(resp, "fetch_latest_emails")

    data = resp.json()
    emails = [_map_message(m) for m in data.get("value", [])]
    total = _get_message_count()

    return {"emails": emails, "total": total}


def fetch_emails_by_subject(search_term: str, limit: int = 20) -> list[dict]:
    """Fetch emails whose subject contains search_term.

    Uses Graph KQL $search — does not support $skip, so only the most
    recent `limit` matching messages are returned.

    Note: If search_term contains KQL special characters (:, (, ), *)
    they may affect the query. The term is wrapped in double quotes for
    safety, which handles spaces and most special characters.
    """
    mailbox = _mailbox()
    url = f"{GRAPH_BASE}/users/{mailbox}/messages"
    params = {
        "$search": f'"subject:{search_term}"',
        "$top": limit,
        "$select": _SELECT,
    }

    resp = requests.get(
        url,
        headers={**_auth_headers(), "ConsistencyLevel": "eventual"},
        params=params,
        timeout=30,
    )
    _raise_for_graph_error(resp, "fetch_emails_by_subject")

    data = resp.json()
    return [_map_message(m) for m in data.get("value", [])]


def fetch_unseen_emails(limit: int = 20) -> list[dict]:
    """Fetch unread emails from the inbox, newest first."""
    mailbox = _mailbox()
    url = f"{GRAPH_BASE}/users/{mailbox}/mailFolders/inbox/messages"
    params = {
        "$filter": "isRead eq false",
        "$top": limit,
        "$orderby": "receivedDateTime desc",
        "$select": _SELECT,
    }

    resp = requests.get(url, headers=_auth_headers(), params=params, timeout=30)
    _raise_for_graph_error(resp, "fetch_unseen_emails")

    data = resp.json()
    return [_map_message(m) for m in data.get("value", [])]
