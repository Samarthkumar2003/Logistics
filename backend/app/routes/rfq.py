"""The Send Request flow: agents, extraction, preview, send.

This is the only path by which mail reaches a freight vendor, and every step
here is driven by an explicit operator action.
"""

import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from backend.agents.intake_agent import ShipmentDetails, run_intake_agent
from backend.app.errors import AppException
from backend.core.config import settings
from backend.domain.models import SenderIdentity
from backend.repositories import agent_repo
from backend.services import rfq_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["rfq"])


class EmailInput(BaseModel):
    sender: str = ""
    subject: str = ""
    body: str


# The three kinds of vendor, mirrored in frontend/src/app/send-request/categories.ts
# and pinned by a CHECK constraint on agents.category.
#
# This used to be presentation only - a way to group three dropdowns. It is now
# the routing key that decides WHICH DRAFT a recipient is sent, so an
# unrecognised value is a refusal rather than an "OTHER" bucket.
CATEGORY_KEYS = ("CHA", "FREIGHT_FORWARDER", "CARRIER")

# Door addresses are free text, so they are capped. Uncapped, a pasted signature
# block or a whole quoted thread would crowd the shipment facts out of the
# drafting prompt - the model reads position and volume, not just content.
# 500 characters is several lines of a real address with room to spare.
MAX_ADDRESS_CHARS = 500


class SelectedAgent(BaseModel):
    agent_name: str
    email: str
    # Required, deliberately. A browser tab left open across a deploy posts the
    # old bundle with no category at all; a 422 telling it to reload is the safe
    # answer, where a default would send this vendor another category's wording.
    # Same fail-closed reasoning as _sender_or_422 below.
    category: str


class DraftText(BaseModel):
    """One category's reviewed subject and body, sent verbatim."""

    subject: str
    body: str


class AttachmentInput(BaseModel):
    filename: str
    content_type: str = "application/octet-stream"
    # Raw file bytes, base64-encoded — same encoding the browser's FileReader
    # produces, so the frontend needs no extra transform.
    data_base64: str


class PreviewAgent(BaseModel):
    """Just enough to address a sample draft.

    Deliberately NOT SelectedAgent. That model requires `category` because it is
    the routing key deciding which reviewed draft a vendor is sent, and defaulting
    it would mail somebody another category's wording. None of that applies here:
    preview_rfq discards the field (it builds a two-argument
    rfq_service.SelectedAgent) and the draft is shown to the operator, not sent.
    Sharing the stricter model cost every operator AI drafting with a 422 reading
    `body -> agent -> category: Field required`, which the frontend surfaced as
    "AI draft unavailable" and blamed on the model.
    """

    agent_name: str
    email: str


class PreviewRFQRequest(BaseModel):
    origin_port: str
    destination_port: str
    size: str = ""
    commodity: str = ""
    mode: str = "sea_freight"
    weight_kg: Optional[float] = None
    # Optional door addresses, typed by the operator - never extracted from the
    # customer email, for the same reason Size is not: a guessed address is worse
    # than a blank one. Reaches the model only when non-empty (see rfq_agent).
    # Must stay in step with the same pair on the other request model below.
    sending_address: str = Field(default="", max_length=MAX_ADDRESS_CHARS)
    receiving_address: str = Field(default="", max_length=MAX_ADDRESS_CHARS)
    agent: Optional[PreviewAgent] = None


class SendRFQRequest(BaseModel):
    origin_port: str
    destination_port: str
    size: str = ""
    commodity: str = ""
    mode: str = "sea_freight"
    weight_kg: Optional[float] = None
    # Optional door addresses, typed by the operator - never extracted from the
    # customer email, for the same reason Size is not: a guessed address is worse
    # than a blank one. Reaches the model only when non-empty (see rfq_agent).
    # Must stay in step with the same pair on the other request model below.
    sending_address: str = Field(default="", max_length=MAX_ADDRESS_CHARS)
    receiving_address: str = Field(default="", max_length=MAX_ADDRESS_CHARS)
    agents: List[SelectedAgent]
    # The originating customer email, kept on the job for traceability.
    customer_sender: str = ""
    customer_subject: str = ""
    customer_body: str = ""
    # provider_msg_id of that email — what groups this request's per-agent RFQs.
    customer_email_id: str = ""
    # The reviewed drafts, keyed by category. When present, each agent is sent
    # the text for THEIR category verbatim; only the reference in the subject
    # differs. When absent - the operator never opened the draft editor - every
    # agent is drafted by the model instead, which is the older flow and still
    # supported. There is no middle ground: a partially filled map is refused,
    # because silently model-drafting the gap swaps text nobody read for text
    # somebody did, on the only path that reaches a real freight vendor.
    drafts: Optional[Dict[str, DraftText]] = None
    # Same files go to every selected agent.
    attachments: List[AttachmentInput] = []


NO_SENDER_NAME = (
    "Your operator profile has no display name, so an RFQ cannot be signed. "
    "Set full_name on your app_users row and log in again."
)


def _sender_or_422(request: Request) -> SenderIdentity:
    """Who this RFQ will be signed by, or a refusal.

    Reads the name from the verified token rather than the database: the bearer
    middleware has already put the claims on `request.state`, so this costs
    nothing, and app/auth.py notes that identity is exactly what they were put
    there for.

    Fails closed, and that is the whole point of the function. A blank name is
    not cosmetic — it is what let the model sign RFQs to real freight agents as
    `[Your Name]`. Three ways to get one, all handled here:

      * a token minted before the `name` claim existed — blank, refused;
      * an app_users row whose full_name was never filled in (the column
        defaults to '') — blank, refused;
      * AUTH_ENABLED=0, where the middleware returns before setting claims at
        all, so `request.state.claims` does not exist — hence getattr, not
        attribute access, or this would be a 500 instead of a 422.

    The last case means the drafting endpoints do not work with auth disabled.
    That is correct: with no token there is no operator, and an unsigned RFQ to a
    vendor is worse than a refused one. The offline suite drives rfq_service
    directly and passes its own SenderIdentity, so nothing there depends on this.
    """
    claims = getattr(request.state, "claims", None)
    name = (getattr(claims, "name", "") or "").strip()
    if not name:
        raise AppException(status_code=422, detail=NO_SENDER_NAME)
    return SenderIdentity(name=name, company=settings.company_name.strip())


def _shipment(payload) -> dict:
    return {
        "origin": payload.origin_port.strip(),
        "destination": payload.destination_port.strip(),
        "mode": payload.mode,
        "weight_kg": payload.weight_kg,
        "commodity": payload.commodity.strip(),
        "size": payload.size.strip(),
        "sending_address": payload.sending_address.strip(),
        "receiving_address": payload.receiving_address.strip(),
    }


@router.get("/agents")
def list_agents():
    """All agents, plus a grouping by category for the three dropdowns."""
    try:
        agents = agent_repo.list_all_raw()
    except Exception as e:
        raise AppException(status_code=500, detail=f"Failed to fetch agents: {e}")

    grouped: dict[str, list] = {}
    for a in agents:
        grouped.setdefault(a.get("category", "OTHER"), []).append(a)
    return {"agents": agents, "by_category": grouped, "total": len(agents)}


def _usable(draft: Optional[DraftText]) -> bool:
    return bool(draft and draft.subject.strip() and draft.body.strip())


def _check_categories(payload: SendRFQRequest) -> None:
    """Refuse any send the three draft panels cannot fully cover.

    Every branch here fails the whole request rather than dropping or
    substituting for one recipient. A recipient quietly skipped, or quietly given
    text the operator never read, is indistinguishable on screen from a clean
    send: the response still reports success for everyone else.
    """
    known = ", ".join(CATEGORY_KEYS)
    unknown = sorted({a.category for a in payload.agents if a.category not in CATEGORY_KEYS})
    if unknown:
        raise AppException(
            status_code=422,
            detail=f"Recipient category not recognised: {', '.join(repr(u) for u in unknown)}. "
                   f"Expected one of {known}. Reload the page and pick the recipients again.",
        )
    if payload.drafts is None:
        return

    stray = sorted(set(payload.drafts) - set(CATEGORY_KEYS))
    if stray:
        raise AppException(
            status_code=422,
            detail=f"Draft supplied for unknown category: {', '.join(stray)}. Expected {known}.",
        )
    needed = {a.category for a in payload.agents}
    missing = [c for c in CATEGORY_KEYS if c in needed and not _usable(payload.drafts.get(c))]
    if missing:
        raise AppException(
            status_code=422,
            detail=f"No reviewed draft for {', '.join(missing)}, so nothing was sent. "
                   f"Draft those panels or remove their recipients.",
        )


@router.get("/rfq-signature")
def rfq_signature(request: Request):
    """The sign-off this operator's RFQs will carry.

    Exists so the browser can build a hand-composed draft that already ends in
    the real signature. Every draft then arrives here signed, which keeps one
    rule instead of a per-draft "is this signed yet" flag: rfq_service signs the
    model branch only, and never touches operator-supplied text.

    It also moves the missing-display-name refusal to page load. The same 422
    used to surface only when the operator pressed Draft or Send, having already
    filled the form in.
    """
    sender = _sender_or_422(request)
    return {"name": sender.name, "signature": sender.signature}


@router.post("/extract-details")
def extract_details(payload: EmailInput):
    """Read a customer email into shipment fields, sending nothing. Prefills the
    Send Request form."""
    content = f"Subject: {payload.subject}\n\nBody:\n{payload.body}"
    try:
        shipment: ShipmentDetails = run_intake_agent(content)
    except Exception as e:
        raise AppException(status_code=422, detail=f"Extraction failed: {e}")
    return {"shipment": shipment.model_dump()}


@router.post("/preview-rfq")
def preview_rfq(payload: PreviewRFQRequest, request: Request):
    """One sample draft, sent to nobody."""
    # Checked on preview as well as send, so a missing name is discovered while
    # composing rather than at the moment the operator tries to send.
    sender = _sender_or_422(request)
    agent = (rfq_service.SelectedAgent(payload.agent.agent_name, payload.agent.email)
             if payload.agent else None)
    try:
        return rfq_service.preview_draft(_shipment(payload), agent, sender)
    except rfq_service.RfqError as e:
        raise AppException(status_code=422, detail=str(e))
    except Exception as e:
        logger.exception("Draft generation failed: %s", e)
        raise AppException(status_code=500, detail=f"Draft generation failed: {e}")


# Gmail's messages.send accepts a base64 JSON body up to ~35MB; Graph's plain
# sendMail (no upload session) caps attachments around 3MB each / a few MB
# total. Capping here keeps the failure a clear 413 instead of a provider
# rejection an agent never sees explained.
MAX_ATTACHMENT_TOTAL_BYTES = 15 * 1024 * 1024


@router.post("/send-rfq")
def send_rfq(payload: SendRFQRequest, request: Request):
    """Send one RFQ per selected agent, each with its own reference."""
    sender = _sender_or_422(request)
    _check_categories(payload)
    agents = [rfq_service.SelectedAgent(a.agent_name, a.email, a.category)
              for a in payload.agents]
    drafts = (
        {key: {"subject": d.subject, "body": d.body} for key, d in payload.drafts.items()}
        if payload.drafts is not None else None
    )

    total_bytes = sum(len(a.data_base64) for a in payload.attachments) * 3 // 4
    if total_bytes > MAX_ATTACHMENT_TOTAL_BYTES:
        raise AppException(
            status_code=413,
            detail=f"Attachments total {total_bytes // 1024}KB, over the "
                   f"{MAX_ATTACHMENT_TOTAL_BYTES // 1024 // 1024}MB limit",
        )

    try:
        return rfq_service.send_rfqs(
            shipment=_shipment(payload),
            agents=agents,
            customer={
                "sender": payload.customer_sender,
                "subject": payload.customer_subject,
                "body": payload.customer_body,
                "email_id": payload.customer_email_id,
            },
            sender=sender,
            drafts=drafts,
            attachments=[a.model_dump() for a in payload.attachments],
        )
    except rfq_service.RfqError as e:
        raise AppException(status_code=422, detail=str(e))
    except Exception as e:
        logger.exception("Send failed: %s", e)
        raise AppException(status_code=500, detail=f"Send failed: {e}")
