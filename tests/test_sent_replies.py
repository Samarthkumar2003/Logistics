"""
Replying to an email from the dashboard.

Live email is armed, so the properties under test are the ones that decide what
lands in a real customer's inbox: one composed reply is one send, a reply goes
into the thread it answers, Cc never survives safe mode, and a failed send is
recorded rather than lost.
"""

import base64
from email import message_from_bytes
from types import SimpleNamespace

import pytest

from backend.app import sender as sender_mod
from backend.app.errors import AppException
from backend.app.routes import replies as routes
from backend.connectors import email_sender, gmail_connector
from backend.domain.models import SenderIdentity
from backend.repositories import email_repo, sent_reply_repo
from backend.services import sent_reply_service as svc
from backend.services.sent_reply_service import ReplyError

OPERATOR = SenderIdentity(name="Priya Shah", company="Harbourline Freight")

TARGET = {
    "provider_msg_id": "m-1",
    "sender": "Ravi Kumar <Ops@Sunrise.example>",
    "subject": "Air freight quote needed",
    "body": "Need a quote for 340 kg.\nBLR to FRA.",
    "received_at": "2026-10-11T09:15:00+00:00",
    "thread_id": "t-1",
    "message_id": "<abc@mail.sunrise.example>",
}


class FakeStore:
    """sent_replies in memory, behind the same functions the service calls."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.fail_insert = False

    def get_by_key(self, key):
        return next((r for r in self.rows.values() if r["idempotency_key"] == key), None)

    def get(self, rid):
        return self.rows.get(rid)

    def insert_sending(self, row):
        if self.fail_insert:
            raise RuntimeError("duplicate key value violates unique constraint")
        rid = f"r-{len(self.rows) + 1}"
        self.rows[rid] = {**row, "id": rid, "status": "sending", "error": None,
                          "created_at": "2026-10-11T10:00:00+00:00", "sent_at": None}
        return self.rows[rid]

    def claim_retry(self, rid):
        r = self.rows.get(rid)
        if not r or r["status"] != "failed":
            return False
        r.update(status="sending", error=None)
        return True

    def record_outcome(self, rid, result):
        # The real column mapping, so the fake cannot drift from it.
        self.rows[rid].update(sent_reply_repo.outcome_update(result))
        return self.rows[rid]

    def list_for_email(self, email_id, limit=50):
        return [r for r in self.rows.values() if r["email_id"] == email_id][::-1]


@pytest.fixture
def store(monkeypatch):
    s = FakeStore()
    for name in ("get_by_key", "get", "insert_sending", "claim_retry",
                 "record_outcome", "list_for_email"):
        monkeypatch.setattr(sent_reply_repo, name, getattr(s, name))
    monkeypatch.setattr(email_repo, "get_reply_target",
                        lambda eid: dict(TARGET) if eid == "m-1" else None)
    return s


@pytest.fixture
def sends(monkeypatch):
    """Capture every outbound send; `outcome` sets what the provider answers."""
    calls: list[dict] = []
    outcome = {"status": "sent", "provider_msg_id": "g-9"}

    def _send(to_addr, subject, body, attachments=None, **kw):
        calls.append({"to": to_addr, "subject": subject, "body": body,
                      "attachments": attachments, **kw})
        return dict(outcome)

    monkeypatch.setattr(svc, "send_rfq_email", _send)
    return SimpleNamespace(calls=calls, outcome=outcome)


# --------------------------------------------------------------------------- subject / cc

@pytest.mark.parametrize("subject, expected", [
    ("Air freight quote", "Re: Air freight quote"),
    ("Re: Air freight quote", "Re: Air freight quote"),
    ("RE:Air freight quote", "RE:Air freight quote"),
    ("  re : spaced", "re : spaced"),
    ("", "Re:"),
    ("   ", "Re:"),
])
def test_reply_subject_never_doubles_the_prefix(subject, expected):
    assert svc.reply_subject(subject) == expected


def test_cc_is_normalised_deduplicated_and_never_repeats_the_to_address():
    cc = ["Anita <ANITA@x.example>", "anita@x.example", "ops@sunrise.example", " ", "b@y.example"]
    assert svc.normalise_cc(cc, "ops@sunrise.example") == ["anita@x.example", "b@y.example"]


@pytest.mark.parametrize("bad", ["not-an-address", "a@b", "two@a.example, three@b.example"])
def test_a_bad_cc_is_refused_not_dropped(bad):
    with pytest.raises(ReplyError, match="Not an email address"):
        svc.normalise_cc([bad], "ops@sunrise.example")


def test_cc_is_capped():
    with pytest.raises(ReplyError, match="At most"):
        svc.normalise_cc([f"p{i}@x.example" for i in range(svc.MAX_CC + 1)], "ops@sunrise.example")


# --------------------------------------------------------------------------- sending

def test_a_reply_goes_to_the_sender_in_the_same_thread(store, sends):
    row = svc.send_reply("m-1", "  Hi Ravi,\nOn it.\n\nPriya Shah  ", OPERATOR, "k-1",
                         user_id="u1", cc=["anita@x.example"])

    (call,) = sends.calls
    assert call["to"] == "ops@sunrise.example"
    assert call["subject"] == "Re: Air freight quote needed"
    assert call["cc"] == ["anita@x.example"]
    assert call["thread_id"] == "t-1"
    assert call["in_reply_to"] == "<abc@mail.sunrise.example>"
    # Operator text first, as written (trimmed), then the quoted original.
    assert call["body"].startswith("Hi Ravi,\nOn it.\n\nPriya Shah\n\nOn 11 Oct 2026, 09:15 UTC,")
    assert "> Need a quote for 340 kg.\n> BLR to FRA." in call["body"]

    assert row["status"] == "sent"
    stored = store.rows[row["id"]]
    assert stored["body"] == "Hi Ravi,\nOn it.\n\nPriya Shah"   # history shows the reply, not the quote
    assert stored["sent_by"] == "u1" and stored["sent_by_name"] == "Priya Shah"


def test_the_same_key_twice_sends_once(store, sends):
    first = svc.send_reply("m-1", "Hello", OPERATOR, "k-dup")
    second = svc.send_reply("m-1", "Hello", OPERATOR, "k-dup")
    assert len(sends.calls) == 1
    assert first["id"] == second["id"]


def test_a_concurrent_request_that_lost_the_insert_does_not_send(store, sends, monkeypatch):
    svc.send_reply("m-1", "Hello", OPERATOR, "k-race")
    store.fail_insert = True
    # Simulate the race: the pre-check misses, the insert then hits the unique key.
    real_get = store.get_by_key
    seen = {"n": 0}

    def flaky_get(key):
        seen["n"] += 1
        return None if seen["n"] == 1 else real_get(key)

    monkeypatch.setattr(sent_reply_repo, "get_by_key", flaky_get)
    again = svc.send_reply("m-1", "Hello", OPERATOR, "k-race")
    assert len(sends.calls) == 1
    assert again["status"] == "sent"


def test_a_provider_failure_is_recorded_not_raised(store, sends):
    sends.outcome.clear()
    sends.outcome.update(status="failed", error="Gmail API send failed: 429")
    row = svc.send_reply("m-1", "Hello", OPERATOR, "k-fail")
    assert row["status"] == "failed"
    assert row["error"] == "Gmail API send failed: 429"


def test_the_row_is_written_before_the_provider_is_called(store, monkeypatch):
    seen_status = []

    def _send(*_a, **_k):
        seen_status.append([r["status"] for r in store.rows.values()])
        return {"status": "sent"}

    monkeypatch.setattr(svc, "send_rfq_email", _send)
    svc.send_reply("m-1", "Hello", OPERATOR, "k-order")
    assert seen_status == [["sending"]]


@pytest.mark.parametrize("body", ["", "   \n  "])
def test_an_empty_reply_is_refused(store, sends, body):
    with pytest.raises(ReplyError, match="Write a reply"):
        svc.send_reply("m-1", body, OPERATOR, "k-empty")
    assert sends.calls == []


def test_an_unknown_email_is_a_404(store, sends):
    with pytest.raises(ReplyError) as e:
        svc.send_reply("nope", "Hello", OPERATOR, "k-404")
    assert e.value.status_code == 404
    assert sends.calls == [] and store.rows == {}


def test_attachment_names_are_kept_and_bytes_are_passed_through(store, sends):
    att = [{"filename": "BL.pdf", "content_type": "application/pdf", "data_base64": "QUJD"}]
    row = svc.send_reply("m-1", "Attached", OPERATOR, "k-att", attachments=att)
    assert row["attachment_names"] == ["BL.pdf"]
    assert sends.calls[0]["attachments"] == att


# --------------------------------------------------------------------------- retry

def _failed(store, sends, key="k-r", attachments=None):
    sends.outcome.clear()
    sends.outcome.update(status="failed", error="boom")
    row = svc.send_reply("m-1", "Hello", OPERATOR, key, attachments=attachments)
    sends.outcome.clear()
    sends.outcome.update(status="sent")
    return row


def test_retry_resends_the_same_row(store, sends):
    row = _failed(store, sends)
    again = svc.retry(row["id"])
    assert again["id"] == row["id"] and again["status"] == "sent"
    assert len(sends.calls) == 2
    assert sends.calls[1]["body"].startswith("Hello\n\nOn ")


def test_retry_refuses_anything_not_failed(store, sends):
    row = svc.send_reply("m-1", "Hello", OPERATOR, "k-ok")
    with pytest.raises(ReplyError) as e:
        svc.retry(row["id"])
    assert e.value.status_code == 409


def test_retry_refuses_a_reply_whose_attachments_are_not_stored(store, sends):
    row = _failed(store, sends, attachments=[{"filename": "x.pdf", "data_base64": "QQ=="}])
    with pytest.raises(ReplyError, match="Edit & resend"):
        svc.retry(row["id"])


def test_two_retries_cannot_both_send(store, sends, monkeypatch):
    row = _failed(store, sends)
    monkeypatch.setattr(sent_reply_repo, "claim_retry", lambda _rid: False)
    with pytest.raises(ReplyError, match="already being retried"):
        svc.retry(row["id"])
    assert len(sends.calls) == 1


# --------------------------------------------------------------------------- summaries

def test_summaries_count_per_email_and_report_the_latest(monkeypatch):
    rows = [
        {"email_id": "a", "status": "failed", "created_at": "2026-10-11T11:24:00"},
        {"email_id": "a", "status": "sent", "created_at": "2026-10-11T11:20:00"},
        {"email_id": "a", "status": "sent", "created_at": "2026-10-11T10:58:00"},
        {"email_id": "b", "status": "sending", "created_at": "2026-10-11T09:00:00"},
    ]

    class Q:
        def __getattr__(self, _name):
            return lambda *a, **k: self

        def execute(self):
            return SimpleNamespace(data=rows)

    monkeypatch.setattr(sent_reply_repo, "get_db", lambda: SimpleNamespace(table=lambda _n: Q()))
    out = sent_reply_repo.summaries(["a", "b", "c"])
    assert out["a"].as_dict() == {"sent": 2, "failed": 1, "last_at": "2026-10-11T11:24:00",
                                  "last_status": "failed"}
    assert out["b"].last_status == "sending" and "c" not in out


def test_the_inbox_survives_a_missing_sent_replies_table(monkeypatch):
    def boom():
        raise RuntimeError('relation "sent_replies" does not exist')
    monkeypatch.setattr(sent_reply_repo, "get_db", boom)
    assert sent_reply_repo.summaries_or_empty(["a"]) == {}


# --------------------------------------------------------------------------- send path

@pytest.fixture
def gmail_api(monkeypatch):
    monkeypatch.setattr(email_sender, "EMAIL_PROVIDER", "gmail_workspace")
    monkeypatch.setattr(email_sender, "EMAIL_REDIRECT", "")
    calls = []

    def _send(to_addr, subject, body, attachments=None, cc=None, thread_id="", in_reply_to=""):
        calls.append({"to": to_addr, "subject": subject, "cc": cc,
                      "thread_id": thread_id, "in_reply_to": in_reply_to})
        return "g-1"

    monkeypatch.setattr(gmail_connector, "send_message", _send)
    return calls


def test_reply_arguments_reach_the_gmail_api(gmail_api):
    r = email_sender.send_rfq_email("c@x.example", "Re: s", "b", cc=["d@x.example"],
                                    thread_id="t-1", in_reply_to="<m@x>")
    assert r == {"status": "sent", "to": "c@x.example", "provider_msg_id": "g-1"}
    assert gmail_api[0]["cc"] == ["d@x.example"]
    assert gmail_api[0]["thread_id"] == "t-1" and gmail_api[0]["in_reply_to"] == "<m@x>"


def test_safe_mode_drops_cc_and_threading(gmail_api, monkeypatch):
    monkeypatch.setattr(email_sender, "EMAIL_REDIRECT", "test@desk.example")
    email_sender.send_rfq_email("c@x.example", "Re: s", "b", cc=["real@customer.example"],
                                thread_id="t-1", in_reply_to="<m@x>")
    (call,) = gmail_api
    assert call["to"] == "test@desk.example"
    assert call["cc"] is None and call["thread_id"] == "" and call["in_reply_to"] == ""


def test_outlook_refuses_a_cc_rather_than_dropping_it(monkeypatch):
    monkeypatch.setattr(email_sender, "EMAIL_PROVIDER", "outlook")
    monkeypatch.setattr(email_sender, "EMAIL_REDIRECT", "")
    r = email_sender.send_rfq_email("c@x.example", "Re: s", "b", cc=["d@x.example"])
    assert r["status"] == "failed" and "Cc" in r["error"]


def test_send_message_sets_the_threading_headers(monkeypatch):
    posted = {}
    monkeypatch.setattr(gmail_connector, "_gmail_post",
                        lambda path, payload: posted.update(path=path, **payload) or {"id": "g-2"})
    assert gmail_connector.send_message("c@x.example", "Re: s", "body", cc=["d@x.example"],
                                        thread_id="t-7", in_reply_to="<m@x>") == "g-2"
    assert posted["threadId"] == "t-7"
    msg = message_from_bytes(base64.urlsafe_b64decode(posted["raw"]))
    assert msg["In-Reply-To"] == "<m@x>" and msg["References"] == "<m@x>"
    assert msg["Cc"] == "d@x.example" and msg["To"] == "c@x.example"


def test_an_rfq_send_message_payload_is_unchanged(monkeypatch):
    posted = {}
    monkeypatch.setattr(gmail_connector, "_gmail_post",
                        lambda path, payload: posted.update(payload) or {"id": "g-3"})
    gmail_connector.send_message("v@x.example", "RFQ", "body")
    assert set(posted) == {"raw"}
    msg = message_from_bytes(base64.urlsafe_b64decode(posted["raw"]))
    assert msg["In-Reply-To"] is None and msg["Cc"] is None


# --------------------------------------------------------------------------- routes

def _request(name="Priya Shah"):
    return SimpleNamespace(state=SimpleNamespace(claims=SimpleNamespace(name=name, user_id="u1")))


@pytest.fixture(autouse=True)
def no_company_lookup(monkeypatch):
    monkeypatch.setattr(sender_mod.user_repo, "get_by_id", lambda _uid: None)


def test_route_refuses_an_unsigned_operator(store, sends):
    with pytest.raises(AppException) as e:
        routes.send_reply("m-1", routes.ReplyRequest(body="Hi", idempotency_key="k"), _request(name=""))
    assert e.value.status_code == 422 and sends.calls == []


def test_route_refuses_oversized_attachments_before_sending(store, sends, monkeypatch):
    monkeypatch.setattr(routes, "MAX_ATTACHMENT_TOTAL_BYTES", 3)
    payload = routes.ReplyRequest(body="Hi", idempotency_key="k",
                                  attachments=[{"filename": "a", "data_base64": "QUJDREVG"}])
    with pytest.raises(AppException) as e:
        routes.send_reply("m-1", payload, _request())
    assert e.value.status_code == 413 and sends.calls == []


def test_route_maps_service_refusals_to_their_status(store, sends):
    with pytest.raises(AppException) as e:
        routes.send_reply("nope", routes.ReplyRequest(body="Hi", idempotency_key="k"), _request())
    assert e.value.status_code == 404


def test_route_returns_the_row_and_lists_it(store, sends):
    row = routes.send_reply("m-1", routes.ReplyRequest(body="Hi", idempotency_key="k"), _request())
    assert row["status"] == "sent" and row["sent_by_name"] == "Priya Shah"
    assert [r["id"] for r in routes.list_sent_replies("m-1")["replies"]] == [row["id"]]


# ── Ambiguous provider failures are "unknown", and never retried ──────────────

def _http_error(code):
    import requests
    resp = requests.Response()
    resp.status_code = code
    return requests.exceptions.HTTPError(f"{code}", response=resp)


@pytest.mark.parametrize("make, unknown", [
    (lambda: __import__("requests").exceptions.ReadTimeout("read timed out"), True),
    (lambda: __import__("requests").exceptions.ConnectionError("reset"), True),
    (lambda: _http_error(503), True),
    (lambda: _http_error(500), True),
    (lambda: __import__("requests").exceptions.ConnectTimeout("no route"), False),
    (lambda: _http_error(400), False),
    (lambda: _http_error(429), False),
    (lambda: RuntimeError("reauth"), False),
])
def test_gmail_failure_is_tagged_unknown_only_when_it_may_have_sent(monkeypatch, make, unknown):
    from backend.connectors import email_sender, gmail_connector

    def boom(**_k):
        raise make()

    monkeypatch.setattr(gmail_connector, "send_message", boom)
    r = email_sender._send_via_gmail_api("c@x.example", "s", "b", intended_to="c@x.example")
    # RFQ callers see exactly what they always did.
    assert r["status"] == "failed" and r["error"].startswith("Gmail API send failed")
    assert r.get("outcome_unknown", False) is unknown


def test_an_ambiguous_failure_is_stored_unknown_and_cannot_be_retried(store, sends):
    sends.outcome.clear()
    sends.outcome.update(status="failed", error="Gmail API send failed: Read timed out",
                         outcome_unknown=True)
    row = svc.send_reply("m-1", "Hello", OPERATOR, "k-unknown")
    assert row["status"] == "unknown"
    assert "timed out" in row["error"]
    with pytest.raises(svc.ReplyError) as e:
        svc.retry(row["id"])
    assert e.value.status_code == 409
    assert len(sends.calls) == 1


def test_outcome_update_maps_each_result():
    m = sent_reply_repo.outcome_update
    assert m({"status": "sent", "provider_msg_id": "g"})["status"] == "sent"
    assert m({"status": "failed", "error": "x"})["status"] == "failed"
    assert m({"status": "failed", "error": "x", "outcome_unknown": True})["status"] == "unknown"
    assert m({"status": "failed"})["error"] == "Send failed"


def test_a_blank_subject_email_gets_a_reply_gmail_will_thread(store, monkeypatch):
    """The 2026-10-13 prod test: a blank-subject email, answered as
    "Re: (no subject)", threaded in our mailbox but arrived as a new conversation
    for the customer, because their Gmail compares subjects with "Re:" stripped.
    End to end through the real sender and connector, only the HTTP post stubbed."""
    monkeypatch.setattr(email_repo, "get_reply_target",
                        lambda eid: {**TARGET, "subject": ""} if eid == "m-1" else None)
    monkeypatch.setattr(email_sender, "EMAIL_PROVIDER", sorted(email_sender.GMAIL_API_PROVIDERS)[0])
    monkeypatch.setattr(email_sender, "EMAIL_REDIRECT", "")
    posted = {}
    monkeypatch.setattr(gmail_connector, "_gmail_post",
                        lambda path, payload: posted.update(payload) or {"id": "g-3"})
    row = svc.send_reply("m-1", "Hello", OPERATOR, "k-blank")
    assert row["status"] == "sent" and row["subject"] == "Re:"
    msg = message_from_bytes(base64.urlsafe_b64decode(posted["raw"]))
    assert msg["Subject"] == "Re:"
    assert posted["threadId"] == "t-1"
    assert msg["In-Reply-To"] == "<abc@mail.sunrise.example>"
