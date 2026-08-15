"""The Send Request flow: agents, extraction, preview, send.

This is the only path by which mail reaches a freight vendor, and every step
here is driven by an explicit operator action.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from backend.agents.intake_agent import ShipmentDetails, run_intake_agent
from backend.app.errors import AppException
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
def preview_rfq(payload: PreviewRFQRequest):
    """One sample draft, sent to nobody."""
    agent = (rfq_service.SelectedAgent(payload.agent.agent_name, payload.agent.email)
             if payload.agent else None)
    try:
        return rfq_service.preview_draft(_shipment(payload), agent)
    except rfq_service.RfqError as e:
        raise AppException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error("Draft generation failed: %s", e)
        raise AppException(status_code=500, detail=f"Draft generation failed: {e}")


@router.post("/send-rfq")
def send_rfq(payload: SendRFQRequest):
    """Send one RFQ per selected agent, each with its own reference."""
    agents = [rfq_service.SelectedAgent(a.agent_name, a.email) for a in payload.agents]
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
            edited_subject=(payload.edited_subject or "").strip(),
            edited_body=(payload.edited_body or "").strip(),
        )
    except rfq_service.RfqError as e:
        raise AppException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error("Send failed: %s", e)
        raise AppException(status_code=500, detail=f"Send failed: {e}")
