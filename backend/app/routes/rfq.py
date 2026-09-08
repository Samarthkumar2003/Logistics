"""The Send Request flow: agents, extraction, preview, send.

This is the only path by which mail reaches a freight vendor, and every step
here is driven by an explicit operator action.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

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


class SelectedAgent(BaseModel):
    agent_name: str
    email: str


class AttachmentInput(BaseModel):
    filename: str
    content_type: str = "application/octet-stream"
    # Raw file bytes, base64-encoded — same encoding the browser's FileReader
    # produces, so the frontend needs no extra transform.
    data_base64: str


class PreviewRFQRequest(BaseModel):
    origin_port: str
    destination_port: str
    size: str = ""
    commodity: str = ""
    mode: str = "sea_freight"
    weight_kg: Optional[float] = None
    agent: Optional[SelectedAgent] = None


class SendRFQRequest(BaseModel):
    origin_port: str
    destination_port: str
    size: str = ""
    commodity: str = ""
    mode: str = "sea_freight"
    weight_kg: Optional[float] = None
    agents: List[SelectedAgent]
    # The originating customer email, kept on the job for traceability.
    customer_sender: str = ""
    customer_subject: str = ""
    customer_body: str = ""
    # provider_msg_id of that email — what groups this request's per-agent RFQs.
    customer_email_id: str = ""
    # One shared, operator-edited draft. When both are present every agent is
    # sent THIS text verbatim; only the reference in the subject differs.
    edited_subject: Optional[str] = None
    edited_body: Optional[str] = None
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
    agents = [rfq_service.SelectedAgent(a.agent_name, a.email) for a in payload.agents]

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
            edited_subject=(payload.edited_subject or "").strip(),
            edited_body=(payload.edited_body or "").strip(),
            attachments=[a.model_dump() for a in payload.attachments],
        )
    except rfq_service.RfqError as e:
        raise AppException(status_code=422, detail=str(e))
    except Exception as e:
        logger.exception("Send failed: %s", e)
        raise AppException(status_code=500, detail=f"Send failed: {e}")
