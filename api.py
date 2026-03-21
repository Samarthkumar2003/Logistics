from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import uuid

from intake_agent import run_intake_agent, ShipmentDetails
from history_agent import find_similar_shipments
from rfq_agent import generate_rfq_drafts
from email_connector import fetch_latest_emails

app = FastAPI(title="Logistics Copilot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001"],
    allow_methods=["*"],
    allow_headers=["*"],
)

pending_drafts: dict = {}


class EmailInput(BaseModel):
    sender: str = ""
    subject: str = ""
    body: str

class InboxEmail(BaseModel):
    id: str
    sender: str
    subject: str
    body: str

class HistoryMatch(BaseModel):
    commodity: str
    agent_used: str
    rate_paid: float
    transit_time_days: int
    similarity: float

class DraftEmailOut(BaseModel):
    vendor_name: str
    subject: str
    body: str

class ProcessResult(BaseModel):
    job_id: str
    shipment: ShipmentDetails
    history_matches: List[HistoryMatch]
    drafts: List[DraftEmailOut]

class ApproveRequest(BaseModel):
    job_id: str
    vendor_names: Optional[List[str]] = None


@app.get("/fetch-inbox", response_model=List[InboxEmail])
def fetch_inbox(limit: int = 5):
    try:
        emails = fetch_latest_emails(limit=limit)
        return [
            InboxEmail(id=e["id"], sender=e["from"], subject=e["subject"], body=e["body"])
            for e in emails
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch inbox: {e}")


@app.post("/process-email", response_model=ProcessResult)
def process_email(payload: EmailInput):
    full_content = f"Subject: {payload.subject}\n\nBody:\n{payload.body}"

    try:
        shipment: ShipmentDetails = run_intake_agent(full_content)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        matches = find_similar_shipments(
            origin=shipment.origin,
            destination=shipment.destination,
            mode=shipment.mode,
            commodity_desc=shipment.commodity,
        )
    except Exception:
        matches = []

    try:
        rfq = generate_rfq_drafts(shipment_data=shipment.model_dump(), history_matches=matches)
        drafts = rfq.drafts
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    job_id = str(uuid.uuid4())
    pending_drafts[job_id] = {"shipment": shipment, "drafts": drafts}

    return ProcessResult(
        job_id=job_id,
        shipment=shipment,
        history_matches=[HistoryMatch(**m) for m in matches],
        drafts=[DraftEmailOut(vendor_name=d.vendor_name, subject=d.subject, body=d.body) for d in drafts],
    )


@app.get("/drafts")
def list_drafts():
    return [
        {"job_id": jid, "shipment": d["shipment"].model_dump(),
         "drafts": [{"vendor_name": x.vendor_name, "subject": x.subject, "body": x.body} for x in d["drafts"]]}
        for jid, d in pending_drafts.items()
    ]


@app.post("/approve")
def approve_drafts(payload: ApproveRequest):
    if payload.job_id not in pending_drafts:
        raise HTTPException(status_code=404, detail="Job not found")
    job = pending_drafts.pop(payload.job_id)
    approved = [d.vendor_name for d in job["drafts"]
                if payload.vendor_names is None or d.vendor_name in payload.vendor_names]
    return {"status": "approved", "job_id": payload.job_id, "approved_vendors": approved}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/test-openai")
def test_openai():
    """Minimal test: call openai directly, no agents involved."""
    from openai import OpenAI
    c = OpenAI()
    try:
        result = c.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Reply with just the word OK"}],
            max_tokens=5,
        )
        return {"ok": True, "reply": result.choices[0].message.content}
    except Exception as e:
        return {"ok": False, "error": str(e), "type": type(e).__name__}
