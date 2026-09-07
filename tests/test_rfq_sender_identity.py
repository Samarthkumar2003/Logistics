"""
Who an RFQ is signed by, and what happens when nobody knows.

The defect these cover: the drafting prompt described the job, the shipment and
the vendors but never said who was writing it. Asked for a professional email
with nobody to sign it as, the model closed with `[Your Name]` — and on the path
where the operator skips the preview, that went to a real freight agent with no
human in between.

Two independent guards, because the prompt is advice and the append is not:
  * the prompt names the operator and forbids a sign-off  (removes the cause)
  * the caller appends the signature itself               (removes the choice)
"""

from types import SimpleNamespace

import jwt
import pytest

from backend.app.errors import AppException
from backend.app.routes import rfq as rfq_route
from backend.core import security
from backend.domain.models import SenderIdentity
from backend.services import rfq_service

OPERATOR = SenderIdentity(name="Asha Nair", company="Bhatia Shipping Group")


# ---------------------------------------------------------------------------
# The claim on the token
# ---------------------------------------------------------------------------

def test_the_token_carries_the_operators_display_name():
    token, _ = security.mint_token("u1", "asha@example.com", "operator", "Asha Nair")
    assert security.decode_token(token).name == "Asha Nair"


def test_a_token_minted_before_the_name_claim_still_decodes():
    """Backward compatibility, and the reason `name` is not in decode's `require`.

    Rejecting a nameless token would log every operator out on deploy and turn a
    profile gap into an authentication failure. It decodes blank and is refused
    later, by the layer that can say something useful about it.
    """
    legacy = jwt.encode(
        {"sub": "u1", "email": "asha@example.com", "exp": 9_999_999_999},
        security.settings.jwt_secret,
        algorithm=security.settings.jwt_algorithm,
    )
    assert security.decode_token(legacy).name == ""


def test_login_signs_the_token_with_the_full_name_on_the_row(monkeypatch):
    from backend.domain.models import AppUser
    from backend.services import auth_service

    row = AppUser(id="u1", email="asha@example.com",
                  password_hash=security.hash_password("correct-horse-battery"),
                  full_name="Asha Nair")
    monkeypatch.setattr(auth_service.user_repo, "get_by_email", lambda _e: row)
    monkeypatch.setattr(auth_service.user_repo, "touch_last_login", lambda _i: None)

    token, _, _ = auth_service.login("asha@example.com", "correct-horse-battery")
    assert security.decode_token(token).name == "Asha Nair"


# ---------------------------------------------------------------------------
# Failing closed: no name, no draft
# ---------------------------------------------------------------------------

def _request(claims=...):
    """A stand-in for the parts of Request that _sender_or_422 touches.

    `claims=...` omits the attribute entirely rather than setting it to None —
    that is the AUTH_ENABLED=0 shape, where the middleware returns before ever
    assigning it, and it is the case a plain `request.state.claims` would turn
    into a 500.
    """
    state = SimpleNamespace() if claims is ... else SimpleNamespace(claims=claims)
    return SimpleNamespace(state=state)


@pytest.fixture(autouse=True)
def company(monkeypatch):
    monkeypatch.setattr(rfq_route, "settings",
                        SimpleNamespace(company_name="Bhatia Shipping Group"))


def test_a_named_operator_resolves_to_a_sender():
    sender = rfq_route._sender_or_422(_request(SimpleNamespace(name="Asha Nair")))
    assert sender == SenderIdentity(name="Asha Nair", company="Bhatia Shipping Group")


@pytest.mark.parametrize("name", ["", "   "])
def test_a_blank_display_name_is_refused_not_drafted(name):
    """The regression. A blank name is exactly what produced `[Your Name]`."""
    with pytest.raises(AppException) as exc:
        rfq_route._sender_or_422(_request(SimpleNamespace(name=name)))
    assert exc.value.status_code == 422
    assert "display name" in exc.value.detail


def test_missing_claims_are_a_422_not_a_500():
    """AUTH_ENABLED=0 sets no claims at all. That must not read as a crash."""
    with pytest.raises(AppException) as exc:
        rfq_route._sender_or_422(_request())
    assert exc.value.status_code == 422


# ---------------------------------------------------------------------------
# The signature is appended, not requested
# ---------------------------------------------------------------------------

def test_the_signature_is_appended_to_the_model_body():
    signed = rfq_service._signed("Please quote FCL Nhava Sheva to Hamburg.", OPERATOR)
    assert signed.endswith("Asha Nair\nBhatia Shipping Group\n")


def test_the_signature_is_name_only_when_no_company_is_configured():
    signed = rfq_service._signed("Body.", SenderIdentity(name="Asha Nair"))
    assert signed.endswith("Asha Nair\n")
    # No trailing blank line where the company would have been.
    assert not signed.endswith("\n\n")


def test_signing_does_not_strand_whitespace_from_the_model():
    signed = rfq_service._signed("Body.\n\n\n", OPERATOR)
    assert signed == "Body.\n\nAsha Nair\nBhatia Shipping Group\n"


# ---------------------------------------------------------------------------
# Which branch signs
# ---------------------------------------------------------------------------

def _fake_model(monkeypatch, body="Model body."):
    """Stand in for the LLM and capture the prompt it was given."""
    seen = {}

    def _generate(shipment_data, agents, reference, sender):
        seen["sender"] = sender
        return SimpleNamespace(drafts=[SimpleNamespace(
            vendor_name=agents[0]["agent_name"],
            vendor_email=agents[0]["email"],
            subject="RFQ", body=body)])

    monkeypatch.setattr(rfq_service, "generate_rfq_drafts", _generate)
    return seen


def test_the_model_branch_signs_the_body(monkeypatch):
    _fake_model(monkeypatch)
    entry = rfq_service._draft_for_agent(
        rfq_service.SelectedAgent("Alpha", "alpha@example.com"),
        {"origin": "A", "destination": "B"}, "", "", OPERATOR,
    )
    assert entry["draft"].body.endswith("Asha Nair\nBhatia Shipping Group\n")


def test_the_edited_branch_is_not_signed_twice(monkeypatch):
    """Edited text came back from preview_draft already signed, and the operator
    may have adjusted it. Appending again would print the name twice."""
    _fake_model(monkeypatch)
    edited = "My own wording.\n\nAsha Nair\nBhatia Shipping Group\n"
    entry = rfq_service._draft_for_agent(
        rfq_service.SelectedAgent("Alpha", "alpha@example.com"),
        {"origin": "A", "destination": "B"}, "Subject RFQ-1", edited, OPERATOR,
    )
    assert entry["draft"].body == edited
    assert entry["draft"].body.count("Asha Nair") == 1


def test_the_sender_reaches_the_model_through_the_thread_pool(monkeypatch):
    """send_rfqs drafts in a ThreadPoolExecutor. The identity has to survive it."""
    seen = _fake_model(monkeypatch)
    monkeypatch.setattr(rfq_service.agent_repo, "ensure_agents", lambda _a: None)
    monkeypatch.setattr(rfq_service.email_repo, "get_thread_id", lambda _e: "")
    monkeypatch.setattr(rfq_service.job_repo, "insert_many", lambda _j: None)
    monkeypatch.setattr(rfq_service.job_repo, "set_status_if",
                        lambda _r, _s, _e: True)
    monkeypatch.setattr(rfq_service, "send_rfq_emails_batch",
                        lambda drafts: [{"vendor_name": d["vendor_name"],
                                         "status": "sent"} for d in drafts])

    rfq_service.send_rfqs(
        {"origin": "A", "destination": "B"},
        [rfq_service.SelectedAgent("Alpha", "alpha@example.com")],
        {"email_id": "", "sender": "x@y.com", "subject": "Rates?"},
        OPERATOR,
    )
    assert seen["sender"] == OPERATOR


# ---------------------------------------------------------------------------
# The prompt itself — the guard on the root cause
# ---------------------------------------------------------------------------

def test_the_prompt_names_the_sender_and_forbids_a_sign_off(monkeypatch):
    """Without this the whole fix is one careless prompt edit from regressing."""
    from backend.agents import rfq_agent

    captured = {}

    class _Completions:
        def parse(self, model, messages, response_format):
            captured["system"] = messages[0]["content"]
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(parsed=rfq_agent.RFQResponse(drafts=[])))])

    monkeypatch.setattr(rfq_agent, "_get_client", lambda: SimpleNamespace(
        beta=SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))))

    rfq_agent.generate_rfq_drafts(
        {"origin": "Nhava Sheva", "destination": "Hamburg"},
        [{"agent_name": "Alpha", "email": "alpha@example.com"}],
        "RFQ-20260908-abcd1234",
        OPERATOR,
    )

    system = captured["system"]
    assert "Asha Nair" in system
    assert "Bhatia Shipping Group" in system
    assert "signature" in system.lower()
    assert "[Your Name]" in system      # named as the thing never to emit
