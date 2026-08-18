"""
Which identity an RFQ actually goes out as.

The desk reads one mailbox (GMAIL_MAILBOX) and used to send from another
(EMAIL_ACCOUNT, over SMTP). Every vendor reply therefore arrived somewhere the
ingest never looks, so an RFQ could be "sent" and its answer be invisible
forever — attribution is by RFQ reference alone, and nothing ever contradicts a
job row that says the mail went out. These tests pin the identity and the safe-
mode redirect on both send paths, because a redirect that applies to one path
and not the other mails a real vendor during a test run.
"""

import base64
from email import message_from_bytes

import pytest

from backend.connectors import email_sender, gmail_connector

TO = "vendor@example.com"


@pytest.fixture
def gmail_api_sends(monkeypatch):
    """Route through the Gmail API path and capture what send_message got."""
    monkeypatch.setattr(email_sender, "EMAIL_PROVIDER", "gmail_workspace")
    calls = []

    def _send(to_addr, subject, body):
        calls.append({"to": to_addr, "subject": subject, "body": body})
        return "msg-123"

    monkeypatch.setattr(gmail_connector, "send_message", _send)
    return calls


@pytest.fixture
def smtp_sends(monkeypatch):
    """Route through SMTP and capture the envelope, without opening a socket."""
    monkeypatch.setattr(email_sender, "EMAIL_PROVIDER", "gmail")
    monkeypatch.setattr(email_sender, "EMAIL_ACCOUNT", "sender@example.com")
    monkeypatch.setattr(email_sender, "EMAIL_PASSWORD", "app-password")
    calls = []

    class _FakeSMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def ehlo(self):
            pass

        def starttls(self, context=None):
            pass

        def login(self, *_args):
            pass

        def sendmail(self, from_addr, to_addr, raw):
            calls.append({"from": from_addr, "to": to_addr, "raw": raw})

    monkeypatch.setattr(email_sender.smtplib, "SMTP", _FakeSMTP)
    return calls


# ---------------------------------------------------------------------------
# Routing: gmail_workspace must not fall through to SMTP
# ---------------------------------------------------------------------------

def test_gmail_workspace_sends_over_the_api_and_never_touches_smtp(
    gmail_api_sends, monkeypatch,
):
    """The original bug: only "outlook" was branched on, so gmail_workspace fell
    through to SMTP and went out as EMAIL_ACCOUNT."""
    def _boom(*_args, **_kwargs):
        raise AssertionError("gmail_workspace must not reach SMTP")

    monkeypatch.setattr(email_sender.smtplib, "SMTP", _boom)
    monkeypatch.setattr(email_sender, "EMAIL_REDIRECT", "")

    assert email_sender.send_rfq_email(TO, "RFQ", "body") == {"status": "sent", "to": TO}
    assert gmail_api_sends == [{"to": TO, "subject": "RFQ", "body": "body"}]


def test_the_default_provider_still_sends_over_smtp(smtp_sends, monkeypatch):
    """The legacy path stays reachable for anyone without a refresh token."""
    monkeypatch.setattr(email_sender, "EMAIL_REDIRECT", "")

    assert email_sender.send_rfq_email(TO, "RFQ", "body")["status"] == "sent"
    assert [c["to"] for c in smtp_sends] == [TO]
    assert smtp_sends[0]["from"] == "sender@example.com"


# ---------------------------------------------------------------------------
# Identity: the API path leaves From to Gmail, which stamps the token owner
# ---------------------------------------------------------------------------

def test_send_message_sets_no_from_header_so_gmail_stamps_the_monitored_mailbox(
    monkeypatch,
):
    """Setting From here would either be ignored or rejected; omitting it is what
    makes the mail go out as GMAIL_MAILBOX."""
    posted = {}

    def _post(path, payload):
        posted["path"] = path
        posted["payload"] = payload
        return {"id": "msg-123"}

    monkeypatch.setattr(gmail_connector, "_gmail_post", _post)

    assert gmail_connector.send_message(TO, "RFQ", "body") == "msg-123"
    assert posted["path"] == "messages/send"

    mime = message_from_bytes(base64.urlsafe_b64decode(posted["payload"]["raw"]))
    assert mime["From"] is None
    assert mime["To"] == TO
    assert mime["Subject"] == "RFQ"


def test_send_message_returns_empty_id_when_gmail_omits_one(monkeypatch):
    """The id is only ever logged, so a surprising response must not raise and
    turn a delivered mail into a recorded failure."""
    monkeypatch.setattr(gmail_connector, "_gmail_post", lambda _p, _b: {})
    assert gmail_connector.send_message(TO, "RFQ", "body") == ""


# ---------------------------------------------------------------------------
# Safe mode applies identically on both paths
# ---------------------------------------------------------------------------

def test_the_api_path_honours_email_redirect(gmail_api_sends, monkeypatch):
    monkeypatch.setattr(email_sender, "EMAIL_REDIRECT", "tester@example.com")

    result = email_sender.send_rfq_email(TO, "RFQ", "body")

    assert gmail_api_sends[0]["to"] == "tester@example.com"
    assert gmail_api_sends[0]["subject"] == f"[TEST → {TO}] RFQ"
    # The caller is told the intended vendor, not the redirect target — job rows
    # and reply attribution key off it.
    assert result == {"status": "sent", "to": TO}


def test_both_paths_rewrite_recipient_and_subject_the_same_way(
    gmail_api_sends, monkeypatch,
):
    monkeypatch.setattr(email_sender, "EMAIL_REDIRECT", "tester@example.com")
    assert email_sender._apply_redirect(TO, "RFQ") == (
        "tester@example.com", f"[TEST → {TO}] RFQ",
    )


# ---------------------------------------------------------------------------
# Failure contract: rfq_service compares status against "sent" by position
# ---------------------------------------------------------------------------

def test_an_api_failure_is_reported_as_failed_against_the_vendor_address(monkeypatch):
    monkeypatch.setattr(email_sender, "EMAIL_PROVIDER", "gmail_workspace")
    monkeypatch.setattr(email_sender, "EMAIL_REDIRECT", "")

    def _boom(**_kwargs):
        raise RuntimeError("403 insufficient scope")

    monkeypatch.setattr(gmail_connector, "send_message", _boom)

    result = email_sender.send_rfq_email(TO, "RFQ", "body")

    assert result["status"] == "failed"
    assert result["to"] == TO
    assert "403 insufficient scope" in result["error"]


def test_a_reauth_requirement_fails_the_send_rather_than_crashing_the_batch(monkeypatch):
    """A dead refresh token must degrade to send_failed rows, not abort the run
    and leave the remaining vendors with no job row at all."""
    monkeypatch.setattr(email_sender, "EMAIL_PROVIDER", "gmail_workspace")
    monkeypatch.setattr(email_sender, "EMAIL_REDIRECT", "")

    def _boom(**_kwargs):
        raise gmail_connector.GmailReauthRequired("re-consent needed")

    monkeypatch.setattr(gmail_connector, "send_message", _boom)

    drafts = [{"vendor_name": "Alpha", "vendor_email": TO, "subject": "RFQ", "body": "b"},
              {"vendor_name": "Beta", "vendor_email": "b@example.com",
               "subject": "RFQ", "body": "b"}]
    results = email_sender.send_rfq_emails_batch(drafts)

    assert [r["status"] for r in results] == ["failed", "failed"]
    assert [r["vendor_name"] for r in results] == ["Alpha", "Beta"]
