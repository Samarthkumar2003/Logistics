"""
Row -> dataclass conversion.

Supabase returns partial rows whenever a query selects a subset of columns, and
returns None for any unset column. Every `from_row` has to survive both without
raising, because the alternative is a scan that dies halfway through a batch.
"""

from backend.domain.models import AgentContact, Attachment, Email, RfqJob


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def test_email_from_a_full_row():
    email = Email.from_row({
        "id": "uuid-1",
        "provider_msg_id": "19fe71005df6e2f4",
        "sender": "agent@example.com",
        "subject": "Re: RFQId:20260101-a1b2",
        "body": "rates attached",
        "thread_id": "thread-9",
        "classification": "quotation_rate_card",
        "rfq_reference": "RFQ-20260101-a1b2",
        "has_attachments": True,
    })
    assert email.id == "19fe71005df6e2f4"   # the Gmail id, not the Postgres uuid
    assert email.row_id == "uuid-1"
    assert email.is_rate_card is True
    assert email.is_linked is True


def test_email_from_an_empty_row_does_not_raise():
    email = Email.from_row({})
    assert email.id == ""
    assert email.is_rate_card is False
    assert email.is_linked is False


def test_none_columns_become_empty_strings_not_the_string_none():
    # `str(None)` is "None", which would then be searched for RFQ references and
    # written back to the database. _s exists to stop exactly that.
    email = Email.from_row({"provider_msg_id": None, "subject": None, "body": None})
    assert email.subject == ""
    assert email.body == ""
    assert "None" not in (email.subject + email.body + email.id)


def test_an_unlinked_rate_card_is_the_needs_linking_case():
    email = Email.from_row({
        "classification": "quotation_rate_card", "rfq_reference": None,
    })
    assert email.is_rate_card and not email.is_linked


def test_non_string_columns_are_coerced():
    # A numeric subject is unusual but a numeric id is not.
    assert Email.from_row({"provider_msg_id": 12345}).id == "12345"


# ---------------------------------------------------------------------------
# RfqJob
# ---------------------------------------------------------------------------

def test_rfq_job_maps_the_prefixed_shipment_columns():
    job = RfqJob.from_row({
        "reference": "RFQ-20260101-a1b2",
        "status": "rfqs_sent",
        "agents_contacted": ["Oceanic Freight"],
        "shipment_origin": "Nhava Sheva",
        "shipment_destination": "Rotterdam",
        "shipment_mode": "sea",
        "shipment_weight_kg": 1200.5,
    })
    assert job.origin == "Nhava Sheva"
    assert job.weight_kg == 1200.5
    assert job.agent_name == "Oceanic Freight"
    assert job.is_open is True


def test_null_agents_contacted_becomes_an_empty_list():
    # text[] comes back as None, not [], when never written.
    job = RfqJob.from_row({"reference": "r", "agents_contacted": None})
    assert job.agents_contacted == []
    assert job.agent_name == ""


def test_weight_stays_none_rather_than_becoming_zero():
    """A missing weight and a zero weight are different facts. Defaulting to 0
    was the old behaviour and it silently produced quotes for weightless cargo."""
    assert RfqJob.from_row({"reference": "r"}).weight_kg is None


def test_is_open_covers_exactly_the_statuses_that_can_receive_a_reply():
    assert RfqJob.from_row({"reference": "r", "status": "rfqs_sent"}).is_open
    assert RfqJob.from_row({"reference": "r", "status": "quotes_received"}).is_open
    assert not RfqJob.from_row({"reference": "r", "status": "approved"}).is_open
    assert not RfqJob.from_row({"reference": "r", "status": ""}).is_open


def test_shipment_dict_has_the_keys_the_llm_agents_expect():
    job = RfqJob.from_row({"reference": "r", "shipment_origin": "Mumbai"})
    assert set(job.shipment_dict()) == {
        "origin", "destination", "mode", "weight_kg", "commodity", "size",
    }


# ---------------------------------------------------------------------------
# AgentContact
# ---------------------------------------------------------------------------

def test_agent_email_is_lowercased_for_matching():
    agent = AgentContact.from_row({"agent_name": "Oceanic", "email": "Sales@Oceanic.COM"})
    assert agent.email == "sales@oceanic.com"


def test_agent_identity_is_name_and_email_together():
    """P2-1: one agent has several offices, each its own row with its own email.
    Keying on the name alone merges Singapore's rates into Dubai's job."""
    singapore = AgentContact.from_row({"agent_name": "Oceanic", "email": "sg@oceanic.com"})
    dubai = AgentContact.from_row({"agent_name": "Oceanic", "email": "dxb@oceanic.com"})
    assert singapore.key != dubai.key
    assert singapore.key == ("Oceanic", "sg@oceanic.com")


# ---------------------------------------------------------------------------
# Attachment
# ---------------------------------------------------------------------------

def test_attachment_size_stays_none_when_unknown():
    attachment = Attachment.from_row({"id": "a1", "file_name": "rates.pdf"})
    assert attachment.size_bytes is None
    assert attachment.url == ""   # signed at read time, never stored
