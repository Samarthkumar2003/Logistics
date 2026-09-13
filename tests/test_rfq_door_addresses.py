"""
Optional door addresses: sending (pickup) and receiving (delivery).

Both are typed by the operator and both are optional, so the thing worth pinning
is the asymmetry - present, they must reach the model AND change what is asked
for; absent, they must leave a port-to-port RFQ completely untouched.

The prompt is asserted on directly rather than through rfq_service, because the
fixture there stubs generate_rfq_drafts out entirely and so cannot see a
shipment field that fails to arrive.
"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.agents import rfq_agent
from backend.app.routes import rfq as rfq_route
from backend.domain.models import SenderIdentity

OPERATOR = SenderIdentity(name="Asha Nair", company="Bhatia Shipping Group")

PICKUP = "Plot 14, MIDC Andheri, Mumbai 400093"
DELIVERY = "Unit 7, Tema Industrial Area, Ghana"


def _capture(monkeypatch, shipment):
    """Run the drafter against a fake client and return both prompts."""
    seen = {}

    class _Completions:
        def parse(self, model, messages, response_format):
            seen["system"] = messages[0]["content"]
            seen["user"] = messages[1]["content"]
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(parsed=rfq_agent.RFQResponse(drafts=[])))])

    monkeypatch.setattr(rfq_agent, "_get_client", lambda: SimpleNamespace(
        beta=SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))))

    base = {"origin": "Nhava Sheva", "destination": "Tema"}
    rfq_agent.generate_rfq_drafts(
        {**base, **shipment},
        [{"agent_name": "Alpha", "email": "alpha@example.com"}],
        "RFQ-20260913-abcd1234",
        OPERATOR,
    )
    return seen


# ---------------------------------------------------------------------------
# Present: the model is told the address and what it means commercially.
# ---------------------------------------------------------------------------

def test_both_addresses_reach_the_shipment_details(monkeypatch):
    seen = _capture(monkeypatch, {"sending_address": PICKUP,
                                  "receiving_address": DELIVERY})
    assert PICKUP in seen["user"]
    assert DELIVERY in seen["user"]


def test_an_address_turns_the_rfq_into_a_door_leg_request(monkeypatch):
    """The commercial point. A door address means the vendor has to price inland
    haulage, and an RFQ that shows the address without asking for that gets back
    a port-to-port rate against a door-to-door enquiry."""
    seen = _capture(monkeypatch, {"sending_address": PICKUP})
    assert "inland haulage" in seen["system"]


def test_the_model_is_forbidden_from_completing_a_partial_address(monkeypatch):
    """Asked to write a professional email around a half-typed address, a model
    will fill in a plausible postcode. That reaches a vendor looking verified."""
    seen = _capture(monkeypatch, {"receiving_address": "Unit 7, Tema"})
    assert "Do NOT invent" in seen["system"]


@pytest.mark.parametrize("shipment", [
    {"sending_address": PICKUP},
    {"receiving_address": DELIVERY},
])
def test_either_address_alone_is_enough_to_trigger_the_clause(monkeypatch, shipment):
    assert "inland haulage" in _capture(monkeypatch, shipment)["system"]


# ---------------------------------------------------------------------------
# Absent: a port-to-port RFQ must not acquire a door clause nobody asked for.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shipment", [
    {},
    {"sending_address": "", "receiving_address": ""},
    {"sending_address": None, "receiving_address": None},
])
def test_no_address_leaves_the_prompt_alone(monkeypatch, shipment):
    seen = _capture(monkeypatch, shipment)
    assert "inland haulage" not in seen["system"]
    assert "address" not in seen["user"].lower()


# ---------------------------------------------------------------------------
# The route: both request models carry the pair, and _shipment funnels it.
# ---------------------------------------------------------------------------

def _preview(**kw):
    return rfq_route.PreviewRFQRequest(origin_port="Nhava Sheva",
                                       destination_port="Tema", **kw)


def _send(**kw):
    return rfq_route.SendRFQRequest(
        origin_port="Nhava Sheva", destination_port="Tema",
        agents=[rfq_route.SelectedAgent(agent_name="Alpha", email="a@x.example",
                                        category="CHA")],
        **kw)


@pytest.mark.parametrize("build", [_preview, _send])
def test_addresses_default_to_empty(build):
    payload = build()
    assert payload.sending_address == ""
    assert payload.receiving_address == ""


@pytest.mark.parametrize("build", [_preview, _send])
def test_the_shipment_funnel_carries_both(build):
    """One funnel feeds preview and send alike, so this is what proves the model
    sees the address on BOTH paths."""
    shipment = rfq_route._shipment(
        build(sending_address=PICKUP, receiving_address=DELIVERY))
    assert shipment["sending_address"] == PICKUP
    assert shipment["receiving_address"] == DELIVERY


@pytest.mark.parametrize("build", [_preview, _send])
def test_surrounding_whitespace_is_stripped(build):
    shipment = rfq_route._shipment(
        build(sending_address=f"  {PICKUP}\n ", receiving_address="   "))
    assert shipment["sending_address"] == PICKUP
    assert shipment["receiving_address"] == "", "blank must stay falsy, not a space"


@pytest.mark.parametrize("build", [_preview, _send])
def test_an_over_long_address_is_refused(build):
    """Capped so a pasted thread cannot crowd the shipment facts out of the
    prompt. Refused rather than truncated: a silently halved address is worse
    than being told to shorten it."""
    with pytest.raises(ValidationError):
        build(sending_address="x" * (rfq_route.MAX_ADDRESS_CHARS + 1))


@pytest.mark.parametrize("build", [_preview, _send])
def test_an_address_at_the_cap_is_accepted(build):
    payload = build(receiving_address="x" * rfq_route.MAX_ADDRESS_CHARS)
    assert len(payload.receiving_address) == rfq_route.MAX_ADDRESS_CHARS
