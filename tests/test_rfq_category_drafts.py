"""
One reviewed draft per kind of vendor, and what happens when one is missing.

The send form used to hold a single edited subject/body that every recipient
received. It now holds one per category, so a customs agent can be sent different
wording from a shipping line. The routing key is `SelectedAgent.category`.

The rule these tests exist to pin is the refusal, not the routing. When the
operator has supplied drafts, a recipient whose category has no usable text must
abort the send - never fall through to the model. Falling through is the
dangerous shape because it is invisible: the vendor receives a plausible RFQ, the
job row records a clean send, and nothing on screen distinguishes it from text a
human read and approved. `_draft_for_agent`'s switch is therefore
`drafts is not None`, not a truthiness test on the text itself.
"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.app.routes import rfq as rfq_route
from backend.domain.models import SenderIdentity
from backend.services import rfq_service
from backend.services.rfq_service import SelectedAgent

SHIPMENT = {"origin": "Mundra", "destination": "Tema", "mode": "sea_freight"}
CUSTOMER = {"email_id": "", "sender": "shipper@example.com", "subject": "Rates?"}
OPERATOR = SenderIdentity(name="Test Operator", company="Test Freight Co")

CHA = SelectedAgent("Jeena", "ops@jeena.example", "CHA")
FWD = SelectedAgent("Kuehne", "rfq@kn.example", "FREIGHT_FORWARDER")
CAR = SelectedAgent("Maersk", "quotes@maersk.example", "CARRIER")


def _text(label):
    """A distinguishable draft, so a mis-route shows up as the wrong body."""
    return {"subject": f"Quote request for {label}", "body": f"Body written for {label}."}


ALL_THREE = {
    "CHA": _text("CHA"),
    "FREIGHT_FORWARDER": _text("forwarders"),
    "CARRIER": _text("carriers"),
}


@pytest.fixture
def wired(monkeypatch):
    """Everything a send reaches out to, replaced by a recorder.

    Returns the mail handed to the sender, the calls that reached the model, and
    the recipients handed to `ensure_agents` - the three things these tests need
    to tell a routed send from a substituted one.
    """
    sent: list[dict] = []
    model_calls: list[dict] = []
    remembered: list[list[dict]] = []

    class _Draft:
        def __init__(self, name, email):
            self.vendor_name = name
            self.vendor_email = email
            self.subject = "MODEL SUBJECT"
            self.body = "MODEL BODY"

    class _Result:
        def __init__(self, agents):
            self.drafts = [_Draft(a["agent_name"], a.get("email", "")) for a in agents]

    def _generate(*, shipment_data, agents, reference, sender):
        model_calls.append({"agents": agents, "reference": reference})
        return _Result(agents)

    def _batch(drafts):
        sent.extend(drafts)
        return [{"vendor_name": d["vendor_name"], "status": "sent"} for d in drafts]

    monkeypatch.setattr(rfq_service, "generate_rfq_drafts", _generate)
    monkeypatch.setattr(rfq_service, "send_rfq_emails_batch", _batch)
    monkeypatch.setattr(rfq_service.job_repo, "insert_many", lambda _jobs: None)
    monkeypatch.setattr(rfq_service.job_repo, "set_status_if", lambda _r, _s, _e: True)
    monkeypatch.setattr(rfq_service.agent_repo, "ensure_agents", remembered.append)
    monkeypatch.setattr(rfq_service.email_repo, "get_thread_id", lambda _e: "")
    return {"sent": sent, "model": model_calls, "remembered": remembered}


def _bodies_by_email(sent):
    return {d["vendor_email"]: d["body"] for d in sent}


# ---------------------------------------------------------------------------
# Routing: each category's text reaches that category's agents
# ---------------------------------------------------------------------------

def test_each_category_receives_its_own_text(wired):
    rfq_service.send_rfqs(SHIPMENT, [CHA, FWD, CAR], CUSTOMER, OPERATOR,
                          drafts=ALL_THREE)
    bodies = _bodies_by_email(wired["sent"])
    assert bodies[CHA.email] == ALL_THREE["CHA"]["body"]
    assert bodies[FWD.email] == ALL_THREE["FREIGHT_FORWARDER"]["body"]
    assert bodies[CAR.email] == ALL_THREE["CARRIER"]["body"]


def test_the_model_is_never_called_when_drafts_are_supplied(wired):
    """The operator reviewed this text. Regenerating any part of it would send a
    vendor words nobody read, which is the whole failure this design avoids."""
    rfq_service.send_rfqs(SHIPMENT, [CHA, FWD, CAR], CUSTOMER, OPERATOR,
                          drafts=ALL_THREE)
    assert wired["model"] == []


def test_an_edited_category_does_not_disturb_the_others(wired):
    """The reported requirement: edit the CHA panel, and the other two still send
    the draft they were seeded with."""
    seed = _text("everyone")
    drafts = {
        "CHA": {"subject": "Hand-written for customs", "body": "My own wording."},
        "FREIGHT_FORWARDER": seed,
        "CARRIER": seed,
    }
    rfq_service.send_rfqs(SHIPMENT, [CHA, FWD, CAR], CUSTOMER, OPERATOR, drafts=drafts)
    bodies = _bodies_by_email(wired["sent"])
    assert bodies[CHA.email] == "My own wording."
    assert bodies[FWD.email] == seed["body"]
    assert bodies[CAR.email] == seed["body"]


def test_two_agents_in_one_category_share_text_but_not_the_reference(wired):
    """Shared wording, separate references - otherwise two vendors' replies could
    not be told apart, which is what the per-agent reference is for."""
    other = SelectedAgent("DB Schenker", "rfq@dbs.example", "FREIGHT_FORWARDER")
    rfq_service.send_rfqs(SHIPMENT, [FWD, other], CUSTOMER, OPERATOR, drafts=ALL_THREE)
    bodies = [d["body"] for d in wired["sent"]]
    subjects = [d["subject"] for d in wired["sent"]]
    assert bodies[0] == bodies[1] == ALL_THREE["FREIGHT_FORWARDER"]["body"]
    assert subjects[0] != subjects[1], "each agent needs its own reference"


def test_a_draft_for_a_category_with_no_recipients_is_simply_unused(wired):
    """The panel stack is derived from the recipient list, but a stale key in the
    payload must not conjure an extra send."""
    rfq_service.send_rfqs(SHIPMENT, [CHA], CUSTOMER, OPERATOR, drafts=ALL_THREE)
    assert [d["vendor_email"] for d in wired["sent"]] == [CHA.email]


def test_ensure_agents_is_told_each_recipients_own_category(wired):
    """The reason four addresses were stranded under a MANUAL category nothing
    lists: this used to be one hardcoded value for the whole batch."""
    rfq_service.send_rfqs(SHIPMENT, [CHA, CAR], CUSTOMER, OPERATOR, drafts=ALL_THREE)
    assert wired["remembered"] == [[
        {"agent_name": "Jeena", "email": CHA.email, "category": "CHA"},
        {"agent_name": "Maersk", "email": CAR.email, "category": "CARRIER"},
    ]]


# ---------------------------------------------------------------------------
# The refusal. Substituting model text for a missing draft is the failure that
# cannot be seen, so every one of these asserts that NOTHING was sent.
# ---------------------------------------------------------------------------

def test_a_recipient_whose_category_has_no_draft_aborts_the_whole_send(wired):
    """Not "skip that agent" - abort. A partial send reports success for everyone
    else, so the operator has no way to notice one category went nowhere."""
    with pytest.raises(rfq_service.RfqError) as excinfo:
        rfq_service.send_rfqs(SHIPMENT, [CHA, CAR], CUSTOMER, OPERATOR,
                              drafts={"CHA": _text("CHA")})
    assert "CARRIER" in str(excinfo.value)
    assert wired["sent"] == []
    assert wired["model"] == []
    assert wired["remembered"] == [], "aborted before anything was written down"


@pytest.mark.parametrize("broken", [
    {"subject": "Has a subject", "body": ""},
    {"subject": "Has a subject", "body": "   \n  "},
    {"subject": "", "body": "Has a body"},
    {"subject": "   ", "body": "Has a body"},
])
def test_a_blank_draft_is_refused_rather_than_model_drafted(wired, broken):
    """The load-bearing case. Make the switch in _draft_for_agent a truthiness
    test on the text and this is the test that fails - every other test in this
    file still passes, because a blank panel is the only input that tells the two
    behaviours apart."""
    with pytest.raises(rfq_service.RfqError):
        rfq_service.send_rfqs(SHIPMENT, [CHA], CUSTOMER, OPERATOR,
                              drafts={"CHA": broken})
    assert wired["sent"] == []
    assert wired["model"] == []


def test_a_recipient_with_no_category_at_all_is_refused(wired):
    """SelectedAgent.category defaults to empty for callers with nothing to route.
    That default is only safe because it cannot be sent when drafts exist."""
    with pytest.raises(rfq_service.RfqError):
        rfq_service.send_rfqs(SHIPMENT, [SelectedAgent("Nobody", "n@x.example")],
                              CUSTOMER, OPERATOR, drafts=ALL_THREE)
    assert wired["sent"] == []


def test_draft_for_agent_refuses_on_its_own(wired):
    """The backstop, for any caller that skips the guards above. It records an
    error on the entry and produces no draft, so no row is reserved and no mail
    is addressed - rather than silently calling the model."""
    entry = rfq_service._draft_for_agent(
        CAR, "RFQ-001042", SHIPMENT, {"CHA": _text("CHA")}, OPERATOR)
    assert entry["draft"] is None
    assert "CARRIER" in entry["error"]
    assert wired["model"] == []


# ---------------------------------------------------------------------------
# The older flow, still supported: never opened the draft editor
# ---------------------------------------------------------------------------

def test_no_drafts_at_all_still_lets_the_model_write_one_per_agent(wired):
    """Pressing Send without previewing is a real, deliberate path. `drafts=None`
    is what distinguishes it from a payload whose panels came back empty."""
    rfq_service.send_rfqs(SHIPMENT, [CHA, CAR], CUSTOMER, OPERATOR, drafts=None)
    assert len(wired["model"]) == 2, "one call per agent"
    assert all(d["body"].startswith("MODEL BODY") for d in wired["sent"])


def test_only_the_model_branch_appends_the_signature(wired):
    """The two branches sign differently and must keep doing so. Model text was
    told to omit a sign-off, so it is signed here. Operator text arrives already
    signed - preview_draft appends it, and a hand-composed draft is built in the
    browser from GET /rfq-signature - so signing it again would print the name
    twice, which is what reached vendors before."""
    rfq_service.send_rfqs(SHIPMENT, [CHA], CUSTOMER, OPERATOR, drafts=None)
    from_model = wired["sent"][0]["body"]
    assert from_model.endswith("Test Operator\nTest Freight Co\n")

    wired["sent"].clear()
    rfq_service.send_rfqs(SHIPMENT, [CHA], CUSTOMER, OPERATOR,
                          drafts={"CHA": _text("CHA")})
    from_operator = wired["sent"][0]["body"]
    assert from_operator == _text("CHA")["body"], "not signed a second time"
    assert "Test Operator" not in from_operator


# ---------------------------------------------------------------------------
# The route's own guard, unit-tested.
#
# Not over HTTP: `_sender_or_422` runs first and, with no verified token in the
# test environment, answers 422 for a missing display name - which would mask the
# category check with a same-status refusal for an unrelated reason.
# ---------------------------------------------------------------------------

def _payload(agents, drafts=None):
    return rfq_route.SendRFQRequest(
        origin_port="Mundra",
        destination_port="Tema",
        agents=[rfq_route.SelectedAgent(agent_name=a.agent_name, email=a.email,
                                        category=a.category) for a in agents],
        drafts=({k: rfq_route.DraftText(**v) for k, v in drafts.items()}
                if drafts is not None else None),
    )


def test_the_route_accepts_a_complete_bundle():
    rfq_route._check_categories(_payload([CHA, FWD, CAR], ALL_THREE))


def test_the_route_accepts_no_drafts_at_all():
    rfq_route._check_categories(_payload([CHA, FWD, CAR]))


def test_an_unrecognised_category_is_a_422():
    """What a browser tab left open across a deploy posts. Refusing beats guessing:
    the alternative is sending this vendor another category's wording."""
    stale = SelectedAgent("Old", "old@x.example", "MANUAL")
    with pytest.raises(rfq_route.AppException) as excinfo:
        rfq_route._check_categories(_payload([stale], ALL_THREE))
    assert excinfo.value.status_code == 422
    assert "MANUAL" in excinfo.value.detail
    assert "FREIGHT_FORWARDER" in excinfo.value.detail, "say what was expected"


def test_a_draft_for_an_unknown_category_is_a_422():
    drafts = dict(ALL_THREE, WAREHOUSE=_text("warehouse"))
    with pytest.raises(rfq_route.AppException) as excinfo:
        rfq_route._check_categories(_payload([CHA], drafts))
    assert excinfo.value.status_code == 422
    assert "WAREHOUSE" in excinfo.value.detail


def test_a_present_category_with_no_draft_is_a_422_before_anything_sends():
    with pytest.raises(rfq_route.AppException) as excinfo:
        rfq_route._check_categories(_payload([CHA, CAR], {"CHA": _text("CHA")}))
    assert excinfo.value.status_code == 422
    assert "CARRIER" in excinfo.value.detail


def test_a_blank_draft_is_refused_at_the_route_too():
    """The same refusal as in the service, one layer earlier, so the operator gets
    a message naming the panel instead of a generic failure."""
    with pytest.raises(rfq_route.AppException) as excinfo:
        rfq_route._check_categories(
            _payload([CHA], {"CHA": {"subject": "Subject", "body": "  "}}))
    assert excinfo.value.status_code == 422
    assert "CHA" in excinfo.value.detail


# ---------------------------------------------------------------------------
# Remembering a hand-entered address under the category the operator chose
# ---------------------------------------------------------------------------

class _FakeTable:
    def __init__(self, existing, inserted):
        self._existing, self._inserted = existing, inserted

    def select(self, *_cols):
        return self

    def in_(self, _column, _values):
        return self

    def insert(self, rows):
        self._inserted.extend(rows)
        return self

    def execute(self):
        # Only the select path reads this; insert().execute() ignores the return.
        return SimpleNamespace(data=[{"email": e} for e in self._existing])


class _FakeDb:
    def __init__(self, existing=()):
        self.inserted: list[dict] = []
        self._existing = list(existing)

    def table(self, name):
        assert name == "agents", name
        return _FakeTable(self._existing, self.inserted)


@pytest.fixture
def fake_agents_table(monkeypatch):
    from backend.repositories import agent_repo
    db = _FakeDb()
    monkeypatch.setattr(agent_repo, "get_db", lambda: db)
    return agent_repo, db


def test_a_remembered_agent_keeps_the_category_the_operator_chose(fake_agents_table):
    """The bug this closes: the category was one hardcoded "MANUAL" for the whole
    batch, and the send form's three dropdowns filter on the three real ones - so
    every address this remembered was invisible from the moment it was saved."""
    agent_repo, db = fake_agents_table
    added = agent_repo.ensure_agents([
        {"agent_name": "New CHA", "email": "a@x.example", "category": "CHA"},
        {"agent_name": "New Line", "email": "b@x.example", "category": "CARRIER"},
    ])
    assert added == 2
    assert [(r["email"], r["category"]) for r in db.inserted] == [
        ("a@x.example", "CHA"), ("b@x.example", "CARRIER"),
    ]


@pytest.mark.parametrize("bad", ["MANUAL", "", "   ", "cha", None])
def test_a_recipient_with_no_usable_category_is_not_remembered(fake_agents_table, bad):
    """Skipped, not defaulted. Writing the row anyway would trip the CHECK
    constraint on agents.category, and a rejected insert loses the whole batch
    rather than the one bad row."""
    agent_repo, db = fake_agents_table
    added = agent_repo.ensure_agents([
        {"agent_name": "Bad", "email": "bad@x.example", "category": bad},
        {"agent_name": "Good", "email": "good@x.example", "category": "CHA"},
    ])
    assert added == 1
    assert [r["email"] for r in db.inserted] == ["good@x.example"]


# ---------------------------------------------------------------------------
# Where `category` is actually required: send, not preview.
#
# The gap this closes: /preview-rfq had no test at all, and reused SelectedAgent
# for its optional `agent`. That made a field required which the handler then
# discarded, so every draft request answered 422
# `body -> agent -> category: Field required` before the handler ran. The
# frontend caught it, assumed the model had failed, and told the operator
# "AI draft unavailable" - blaming the LLM for a schema mismatch.
# ---------------------------------------------------------------------------

def test_preview_accepts_an_agent_with_no_category():
    """Preview discards the field, so it must not demand it."""
    payload = rfq_route.PreviewRFQRequest(
        origin_port="Mundra",
        destination_port="Tema",
        agent={"agent_name": "Acme Lines", "email": "rates@acme.example"},
    )
    assert payload.agent is not None
    assert payload.agent.email == "rates@acme.example"
    assert not hasattr(payload.agent, "category"), (
        "a preview agent must carry no routing key - having one invites a caller "
        "to think preview honours it, which it does not"
    )


def test_preview_still_accepts_no_agent_at_all():
    """`agent` is optional and stays that way: the drafting prompt uses it only
    for flavour, so a shipment with no recipient chosen yet still previews."""
    payload = rfq_route.PreviewRFQRequest(origin_port="Mundra", destination_port="Tema")
    assert payload.agent is None


def test_send_still_refuses_an_agent_with_no_category():
    """The strictness has to survive the split. This is the path that reaches a
    real freight vendor, where defaulting the category would send somebody
    another category's wording."""
    with pytest.raises(ValidationError):
        rfq_route.SendRFQRequest(
            origin_port="Mundra",
            destination_port="Tema",
            agents=[{"agent_name": "Acme Lines", "email": "rates@acme.example"}],
        )
