"""The Sent folder and whole-thread views: live Gmail reads, nothing sent.

Gmail is stubbed at `_gmail_get`, so these exercise the real parsing, ordering
and error mapping without a network.
"""

import base64

import pytest

from backend.app.errors import AppException
from backend.app.routes import mailbox
from backend.connectors import gmail_connector as g
from backend.connectors.google_oauth import GmailReauthRequired
from backend.repositories import email_repo, sent_reply_repo


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _msg(mid, labels, frm="A <a@x.example>", to="b@x.example", subject="Rates",
         plain=None, html=None, attachments=(), internal="1760600000000", thread="t-1"):
    parts = []
    if plain is not None:
        parts.append({"mimeType": "text/plain", "body": {"data": _b64(plain)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": _b64(html)}})
    for name, cid in attachments:
        headers = [{"name": "Content-ID", "value": cid}] if cid else []
        parts.append({"mimeType": "application/pdf", "filename": name, "headers": headers,
                      "body": {"attachmentId": f"att-{name}", "size": 1234}})
    return {
        "id": mid, "threadId": thread, "labelIds": labels, "internalDate": internal,
        "payload": {"mimeType": "multipart/mixed", "parts": parts, "headers": [
            {"name": "From", "value": frm}, {"name": "To", "value": to},
            {"name": "Subject", "value": subject},
        ]},
    }


class _HttpError(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.response = type("R", (), {"status_code": code})()


# ── HTML-only mail still reads as text ─────────────────────────────────────

def test_html_to_text_keeps_lines_and_drops_markup():
    html = ("<html><head><style>p{x:1}</style></head><body><p>Dear team,</p>"
            "<p>Rate is &pound;120&nbsp;/kg<br>Valid 7 days</p><script>x()</script></body></html>")
    assert g.html_to_text(html) == "Dear team,\n\nRate is £120 /kg\nValid 7 days"


def test_self_closing_br_is_one_line_break():
    assert g.html_to_text("<div>Line one<br/>Line two<br />Line three</div>") == "Line one\nLine two\nLine three"


def test_plain_text_is_preferred_and_html_is_the_fallback():
    both = _msg("m", ["INBOX"], plain="plain wins", html="<p>html</p>")["payload"]
    only_html = _msg("m", ["INBOX"], html="<div>Only <b>html</b></div>")["payload"]
    assert g.extract_text(both) == "plain wins"
    assert g.extract_text(only_html) == "Only html"


def test_crlf_bodies_come_back_with_plain_newlines():
    # Mail is CRLF on the wire; the view folds quoted history line by line.
    crlf = _msg("m", ["SENT"], plain="Dear Pooja\r\n\r\nShare weight\r\nOn Mon, X wrote:\r\n> hi")
    assert g.extract_text(crlf["payload"]) == "Dear Pooja\n\nShare weight\nOn Mon, X wrote:\n> hi"


# ── One message as the views show it ───────────────────────────────────────

def test_view_message_marks_direction_and_hides_signature_images():
    m = g.view_message(_msg("s1", ["SENT"], plain="hi",
                            attachments=[("quote.pdf", ""), ("logo.png", "<logo@x>")]))
    assert m["direction"] == "sent"
    assert [a["filename"] for a in m["attachments"]] == ["quote.pdf"]
    assert m["date"].startswith("2025-10-16")
    assert g.view_message(_msg("r1", ["INBOX"], plain="hi"))["direction"] == "received"


def test_a_long_body_is_clipped_and_says_so(monkeypatch):
    monkeypatch.setattr(g, "VIEW_BODY_CHARS", 10)
    m = g.view_message(_msg("m", ["INBOX"], plain="x" * 25))
    assert len(m["body"]) == 10 and m["body_clipped"] is True


# ── Threads ────────────────────────────────────────────────────────────────

def test_thread_is_oldest_first_without_drafts(monkeypatch):
    msgs = [_msg("1", ["INBOX"], plain="q"), _msg("2", ["SENT"], plain="a"),
            _msg("3", ["DRAFT"], plain="unsent")]
    monkeypatch.setattr(g, "_gmail_get", lambda path, params=None: {"id": "t-1", "messages": msgs})
    t = g.fetch_thread("t-1")
    assert [m["id"] for m in t["messages"]] == ["1", "2"] and t["omitted"] == 0


def test_a_huge_thread_keeps_the_newest_and_counts_the_rest(monkeypatch):
    monkeypatch.setattr(g, "THREAD_MAX_MESSAGES", 2)
    msgs = [_msg(str(i), ["INBOX"], plain=str(i)) for i in range(5)]
    monkeypatch.setattr(g, "_gmail_get", lambda path, params=None: {"id": "t", "messages": msgs})
    t = g.fetch_thread("t")
    assert [m["id"] for m in t["messages"]] == ["3", "4"] and t["omitted"] == 3


def test_thread_route_uses_the_stored_thread_id(monkeypatch):
    seen = []
    monkeypatch.setattr(email_repo, "get_thread_id", lambda mid: "t-stored")
    monkeypatch.setattr(g, "fetch_thread", lambda tid: seen.append(tid) or {"messages": []})
    monkeypatch.setattr(g, "message_thread_id", lambda mid: pytest.fail("asked Gmail needlessly"))
    mailbox.get_thread("m-1")
    assert seen == ["t-stored"]


def test_thread_route_asks_gmail_for_a_message_not_in_the_store(monkeypatch):
    # Sent rows are never stored, so this is the normal path for them.
    monkeypatch.setattr(email_repo, "get_thread_id", lambda mid: "")
    monkeypatch.setattr(g, "message_thread_id", lambda mid: "t-gmail")
    monkeypatch.setattr(g, "fetch_thread", lambda tid: {"thread_id": tid, "messages": []})
    assert mailbox.get_thread("s-9")["thread_id"] == "t-gmail"


def test_a_stale_stored_thread_id_falls_back_to_gmail(monkeypatch):
    monkeypatch.setattr(email_repo, "get_thread_id", lambda mid: "t-old")
    monkeypatch.setattr(g, "message_thread_id", lambda mid: "t-new")

    def fetch(tid):
        if tid == "t-old":
            raise _HttpError(404)
        return {"thread_id": tid, "messages": []}
    monkeypatch.setattr(g, "fetch_thread", fetch)
    assert mailbox.get_thread("m-1")["thread_id"] == "t-new"


def test_a_stored_thread_failing_otherwise_is_a_502(monkeypatch):
    monkeypatch.setattr(email_repo, "get_thread_id", lambda mid: "t-1")
    monkeypatch.setattr(g, "message_thread_id", lambda mid: pytest.fail("not a stale id"))

    def fetch(tid):
        raise _HttpError(500)
    monkeypatch.setattr(g, "fetch_thread", fetch)
    with pytest.raises(AppException) as e:
        mailbox.get_thread("m-1")
    assert e.value.status_code == 502


def test_a_store_outage_falls_back_to_gmail(monkeypatch):
    def boom(_mid):
        raise RuntimeError("db down")
    monkeypatch.setattr(email_repo, "get_thread_id", boom)
    monkeypatch.setattr(g, "message_thread_id", lambda mid: "t-gmail")
    monkeypatch.setattr(g, "fetch_thread", lambda tid: {"thread_id": tid, "messages": []})
    assert mailbox.get_thread("m-1")["thread_id"] == "t-gmail"


# ── Sent folder ────────────────────────────────────────────────────────────

def test_sent_page_keeps_gmail_order_and_marks_app_mail(monkeypatch):
    def fake_get(path, params=None):
        if path == "messages":
            assert params["labelIds"] == "SENT"
            return {"messages": [{"id": "s3"}, {"id": "s2"}, {"id": "s1"}], "nextPageToken": "p2"}
        mid = path.split("/")[-1]
        subject = {"s3": "Re: rates", "s2": "RFQId:001042 Air freight BLR-FRA", "s1": "Lunch"}[mid]
        return {"id": mid, "threadId": f"t-{mid}", "snippet": "a &amp; b", "internalDate": "1760600000000",
                "payload": {"headers": [{"name": "To", "value": "c@x.example"},
                                        {"name": "Subject", "value": subject}]}}
    monkeypatch.setattr(g, "_gmail_get", fake_get)
    monkeypatch.setattr(sent_reply_repo, "app_sent_ids", lambda ids: {"s3"})
    page = mailbox.list_sent(limit=3, page_token="")
    assert [i["id"] for i in page["items"]] == ["s3", "s2", "s1"]
    assert page["next_page_token"] == "p2"
    assert [i["app_reply"] for i in page["items"]] == [True, False, False]
    assert page["items"][1]["rfq_reference"] == "RFQ-001042"
    assert page["items"][0]["rfq_reference"] is None
    assert page["items"][0]["snippet"] == "a & b"


def test_one_unreadable_sent_row_is_skipped_not_fatal(monkeypatch):
    def fake_get(path, params=None):
        if path == "messages":
            return {"messages": [{"id": "ok"}, {"id": "bad"}]}
        if path.endswith("bad"):
            raise RuntimeError("boom")
        return {"id": "ok", "payload": {"headers": []}}
    monkeypatch.setattr(g, "_gmail_get", fake_get)
    monkeypatch.setattr(sent_reply_repo, "app_sent_ids", lambda ids: set())
    assert [i["id"] for i in mailbox.list_sent(limit=2, page_token="")["items"]] == ["ok"]


def test_sent_count_reads_the_label_counter(monkeypatch):
    monkeypatch.setattr(g, "_gmail_get", lambda path, params=None: {"messagesTotal": 4021})
    assert mailbox.sent_count() == {"total": 4021}


def test_app_sent_lookup_never_breaks_the_folder(monkeypatch):
    def boom():
        raise RuntimeError("table missing")
    monkeypatch.setattr(sent_reply_repo, "get_db", boom)
    assert sent_reply_repo.app_sent_ids(["a"]) == set()
    assert sent_reply_repo.app_sent_ids([]) == set()


# ── Error contract, same as /email-body ────────────────────────────────────

@pytest.mark.parametrize("exc, code, text", [
    (_HttpError(404), 404, "deleted"),
    (GmailReauthRequired("token revoked"), 502, "re-authorisation"),
    (_HttpError(500), 502, "unavailable"),
])
def test_gmail_failures_map_to_readable_errors(monkeypatch, exc, code, text):
    def boom(_mid):
        raise exc
    monkeypatch.setattr(g, "fetch_view_message", boom)
    with pytest.raises(AppException) as e:
        mailbox.get_sent_message("s-1")
    assert e.value.status_code == code and text in e.value.detail
