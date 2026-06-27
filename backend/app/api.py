import logging
import os
import socket
import uuid
from datetime import datetime
from typing import List

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from supabase import create_client

from backend.agents.intake_agent import run_intake_agent, ShipmentDetails
from backend.agents.agents_lookup import lookup_agents, AgentMatch
from backend.agents.rfq_agent import generate_rfq_drafts
from backend.connectors.email_connector import fetch_latest_emails, fetch_emails_by_subject, fetch_unseen_emails
from backend.connectors.email_sender import send_rfq_email, send_rfq_emails_batch
from backend.agents.quotation_agent import parse_quotation_email
from backend.classifier.price_predictor import predict_price, assess_quotation, PricePrediction
from backend.agents.history_agent import find_similar_shipments
from backend.classifier.email_classifier import classify_email, classify_emails_batch, submit_feedback
from backend.classifier.classification_cache import classify_with_cache, update_label as cache_update_label
from backend.automation.automation import run_scan, get_status as automation_get_status, set_enabled as automation_set_enabled
from backend.core.paths import PROJECT_ROOT

load_dotenv(PROJECT_ROOT / ".env")

# Ensure app INFO logs reach the console under uvicorn (root defaults to WARNING).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

logger = logging.getLogger("logistics_copilot")

# ---------------------------------------------------------------------------
# Supabase client
# ---------------------------------------------------------------------------

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip()
SUPABASE_KEY = (os.environ.get("SUPABASE_KEY") or "").strip()


def _validate_supabase_config() -> None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.error("SUPABASE_URL and SUPABASE_KEY must be set in .env")
        return
    host = SUPABASE_URL.replace("https://", "").replace("http://", "").split("/")[0]
    try:
        socket.getaddrinfo(host, 443)
    except socket.gaierror:
        logger.error(
            "Supabase host %r does not resolve (DNS NXDOMAIN). "
            "Project may be deleted or SUPABASE_URL is wrong — copy Project URL + "
            "service_role key from Supabase Dashboard → Settings → API.",
            host,
        )


_validate_supabase_config()
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# ---------------------------------------------------------------------------
# Custom exception & handlers
# ---------------------------------------------------------------------------

class AppException(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


def _warmup():
    try:
        from backend.classifier.llm_provider import get_provider
        provider = get_provider()  # construct the active LLM client once at startup
        logger.info("LLM classifier provider warmed up: %s", provider.name)
    except Exception as e:
        logger.warning("LLM provider warmup failed: %s", e)

def _run_scan_job():
    try:
        run_scan(supabase)
    except Exception as e:
        logger.error("Scheduled scan error: %s", e)

import threading as _t
_t.Thread(target=_warmup, daemon=True).start()

_scheduler = BackgroundScheduler(timezone="UTC")
_scheduler.add_job(_run_scan_job, "interval", minutes=5, id="inbox_scan", replace_existing=True)
_scheduler.start()
logger.info("Automation scheduler started — scanning every 5 minutes")

app = FastAPI(title="Logistics Copilot API")


@app.exception_handler(AppException)
async def app_exception_handler(_request: Request, exc: AppException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request: Request, exc: RequestValidationError):
    errors = exc.errors()
    messages = []
    for err in errors:
        loc = " -> ".join(str(l) for l in err.get("loc", []))
        messages.append(f"{loc}: {err.get('msg', 'invalid')}")
    return JSONResponse(status_code=422, content={"detail": "; ".join(messages)})


@app.exception_handler(Exception)
async def catch_all_handler(_request: Request, exc: Exception):
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class EmailInput(BaseModel):
    sender: str = ""
    subject: str = ""
    body: str


class InboxEmail(BaseModel):
    id: str
    sender: str
    subject: str
    body: str


class ApproveRequest(BaseModel):
    selected_agent: str


class ClassifyRequest(BaseModel):
    subject: str = ""
    body: str
    sender: str = ""


class FeedbackRequest(BaseModel):
    email_subject: str = ""
    email_body: str
    email_sender: str = ""
    predicted_label: str
    corrected_label: str
    confidence: float = 0.0
    email_id: str = ""


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/fetch-inbox")
def fetch_inbox(limit: int = 20, offset: int = 0, search: str = ""):
    try:
        if search:
            from backend.connectors.email_connector import fetch_emails_by_subject
            raw_emails = fetch_emails_by_subject(search, limit=limit)
            # fetch_emails_by_subject returns a plain list
            emails_list = raw_emails if isinstance(raw_emails, list) else raw_emails
            return {
                "emails": [
                    {"id": e["id"], "sender": e["from"], "subject": e["subject"], "body": e["body"]}
                    for e in emails_list
                ],
                "total": len(emails_list),
                "has_more": False,
            }
        result = fetch_latest_emails(limit=limit, offset=offset)
        emails_list = result["emails"]
        total = result["total"]
        # Cache-aware classification — LLM is called only for emails not yet
        # classified; refreshes read labels from Supabase. Snippet is the body
        # proxy here (metadata-only fetch).
        label_map = classify_with_cache(
            [{"id": e["id"], "subject": e["subject"],
              "body": e.get("snippet", "") or e.get("body", ""),
              "sender": e["from"]}
             for e in emails_list],
        )
        return {
            "emails": [
                {
                    "id": e["id"],
                    "sender": e["from"],
                    "subject": e["subject"],
                    "body": e.get("snippet", ""),
                    "label": label_map.get(e["id"], {}).get("label", "general"),
                    "label_confidence": label_map.get(e["id"], {}).get("confidence", 0.0),
                    "label_method": label_map.get(e["id"], {}).get("method", ""),
                }
                for e in emails_list
            ],
            "total": total,
            "has_more": (offset + limit) < total,
        }
    except Exception as e:
        raise AppException(status_code=500, detail=f"Failed to fetch inbox: {e}")


@app.get("/email-body/{message_id}")
def get_email_body(message_id: str):
    """Fetch full body of a single email on-demand (when user expands the email card)."""
    try:
        from backend.connectors.gmail_connector import fetch_full_message
        msg = fetch_full_message(message_id)
        return {"body": msg["body"]}
    except Exception as e:
        raise AppException(status_code=500, detail=f"Failed to fetch email body: {e}")


@app.post("/process-email")
def process_email(payload: EmailInput):
    """Process a customer email: extract shipment details, find agents, generate
    and immediately send RFQ emails, then persist the job in Supabase."""

    full_content = f"Subject: {payload.subject}\n\nBody:\n{payload.body}"

    # 1. Extract shipment details
    try:
        shipment: ShipmentDetails = run_intake_agent(full_content)
    except Exception as e:
        raise AppException(status_code=422, detail=f"Intake agent failed: {e}")

    # 2. Look up forwarding agents
    try:
        agents: List[AgentMatch] = lookup_agents(
            destination=shipment.destination,
            destination_country=shipment.destination_country,
            mode=shipment.mode,
            commodity_desc=shipment.commodity,
            origin=shipment.origin,
        )
    except Exception as e:
        raise AppException(status_code=500, detail=f"Agent lookup failed: {e}")

    if not agents:
        raise AppException(status_code=404, detail="No matching forwarding agents found")

    # 3. Generate RFQ reference
    reference = f"RFQ-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:4]}"

    # 4. Generate RFQ drafts
    agents_as_dicts = [
        {
            "agent_name": a.agent_name,
            "email": a.email,
            "specialty": a.specialty,
            "historical_rate": a.historical_rate,
            "historical_transit_days": a.historical_transit_days,
        }
        for a in agents
    ]

    try:
        drafts_result = generate_rfq_drafts(
            shipment_data=shipment.model_dump(),
            agents=agents_as_dicts,
            reference=reference,
        )
        drafts_list = drafts_result.drafts if hasattr(drafts_result, "drafts") else drafts_result
    except Exception as e:
        raise AppException(status_code=500, detail=f"RFQ draft generation failed: {e}")

    # 5. Convert drafts to dicts for the batch sender
    drafts_as_dicts = [
        {
            "vendor_name": d.vendor_name,
            "vendor_email": d.vendor_email,
            "subject": d.subject,
            "body": d.body,
        }
        for d in (drafts_list if isinstance(drafts_list, list) else [])
    ]

    # 6. Auto-send all drafted emails
    try:
        send_results = send_rfq_emails_batch(drafts_as_dicts)
    except Exception as e:
        logger.error("Batch send failed: %s", e)
        send_results = [{"vendor_name": "unknown", "status": f"batch_error: {e}"}]

    # 7. Store job in Supabase (match rfq_jobs table schema)
    agents_contacted_names = [a.agent_name for a in agents]
    agents_contacted_info = [
        {"agent_name": a.agent_name, "email": a.email, "source": a.source}
        for a in agents
    ]

    job_record = {
        "reference": reference,
        "customer_email_sender": payload.sender,
        "customer_email_subject": payload.subject,
        "customer_email_body": payload.body,
        "shipment_origin": shipment.origin,
        "shipment_destination": shipment.destination,
        "shipment_mode": shipment.mode,
        "shipment_weight_kg": float(shipment.weight_kg),
        "shipment_commodity": shipment.commodity,
        "status": "rfqs_sent",
        "agents_contacted": agents_contacted_names,
    }

    try:
        supabase.table("rfq_jobs").insert(job_record).execute()
    except Exception as e:
        logger.error("Failed to store job in Supabase: %s", e)
        raise AppException(status_code=500, detail=f"Failed to persist job: {e}")

    # 8. Return result
    return {
        "reference": reference,
        "shipment": shipment.model_dump(),
        "agents_contacted": agents_contacted_info,
        "send_results": send_results,
    }


@app.get("/jobs")
def list_jobs():
    """List all RFQ jobs from Supabase, most recent first."""
    try:
        result = (
            supabase.table("rfq_jobs")
            .select("*")
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        )
        return result.data
    except Exception as e:
        logger.error("Failed to list jobs: %s", e)
        raise AppException(status_code=500, detail=f"Failed to list jobs: {e}")


@app.get("/jobs/{reference}")
def get_job(reference: str):
    """Get a single job by its RFQ reference."""
    try:
        result = (
            supabase.table("rfq_jobs")
            .select("*")
            .eq("reference", reference)
            .execute()
        )
    except Exception as e:
        logger.error("Failed to fetch job %s: %s", reference, e)
        raise AppException(status_code=500, detail=f"Failed to fetch job: {e}")

    if not result.data:
        raise AppException(status_code=404, detail=f"Job {reference} not found")

    return result.data[0]


@app.post("/jobs/{reference}/check-quotations")
def check_quotations(reference: str):
    """Check the inbox for quotation replies matching this RFQ reference,
    parse new quotations, run price prediction and assessment, and store them."""

    # Load the job
    try:
        job_result = (
            supabase.table("rfq_jobs")
            .select("*")
            .eq("reference", reference)
            .execute()
        )
    except Exception as e:
        raise AppException(status_code=500, detail=f"Failed to fetch job: {e}")

    if not job_result.data:
        raise AppException(status_code=404, detail=f"Job {reference} not found")

    job = job_result.data[0]
    # Build shipment dict from individual columns
    shipment = {
        "origin": job.get("shipment_origin", ""),
        "destination": job.get("shipment_destination", ""),
        "mode": job.get("shipment_mode", ""),
        "weight_kg": job.get("shipment_weight_kg", 0),
        "commodity": job.get("shipment_commodity", ""),
    }
    agents_contacted = job.get("agents_contacted", [])  # text[] of agent names

    # Build a lookup from email address to agent name using the agents CSV
    from backend.agents.agents_lookup import _load_agents_csv
    all_agents_csv = _load_agents_csv()
    email_to_agent = {}
    for a in all_agents_csv:
        email = a.get("email", "").strip().lower()
        name = a.get("agent_name", "")
        if email and name in agents_contacted:
            email_to_agent[email] = name

    # Fetch emails matching this reference (exact subject search)
    try:
        reply_emails_by_ref = fetch_emails_by_subject(reference)
    except Exception as e:
        logger.error("Failed to fetch emails for %s: %s", reference, e)
        raise AppException(status_code=500, detail=f"Failed to fetch reply emails: {e}")

    # Fuzzy fallback: also check unseen emails from contacted agent addresses.
    # Vendors often reply without the RFQ reference in the subject line.
    contacted_addresses = set(email_to_agent.keys())
    fuzzy_emails: list[dict] = []
    if contacted_addresses:
        try:
            unseen = fetch_unseen_emails(limit=50)
            fuzzy_emails = [
                e for e in unseen
                if e.get("from", "").lower() in contacted_addresses
            ]
        except Exception as e:
            logger.warning("Fuzzy email fetch failed: %s", e)

    # Merge by email id — subject-match emails take priority
    seen_email_ids: set[str] = {e["id"] for e in reply_emails_by_ref}
    reply_emails = list(reply_emails_by_ref)
    for e in fuzzy_emails:
        if e["id"] not in seen_email_ids:
            seen_email_ids.add(e["id"])
            reply_emails.append(e)

    # Load already-stored quotations to avoid duplicates
    # Dedup key: (agent_email, rate, rate_label) — handles multi-rate emails correctly
    try:
        existing_result = (
            supabase.table("quotations")
            .select("agent_email, rate, rate_label")
            .eq("rfq_reference", reference)
            .execute()
        )
        existing_keys: set[tuple] = {
            (row["agent_email"], row["rate"], row.get("rate_label", ""))
            for row in existing_result.data
        }
    except Exception as e:
        logger.error("Failed to load existing quotations: %s", e)
        existing_keys = set()

    # Get price prediction for assessment
    try:
        history = find_similar_shipments(
            origin=shipment.get("origin", ""),
            destination=shipment.get("destination", ""),
            mode=shipment.get("mode", ""),
            commodity_desc=shipment.get("commodity", ""),
        )
    except Exception:
        history = []

    try:
        prediction: PricePrediction = predict_price(shipment, history)
    except Exception as e:
        logger.warning("Price prediction failed: %s", e)
        prediction = None

    new_quotations = []
    for email in reply_emails:
        subject = email.get("subject", "")
        body = email.get("body", "")
        sender_email = email.get("from", "").lower()

        # Parse — returns list[QuotationDetails], one per rate line
        try:
            parsed_rates = parse_quotation_email(body, subject)
        except Exception as e:
            logger.warning("Failed to parse quotation from %s: %s", sender_email, e)
            continue

        if not parsed_rates:
            logger.warning("No rates extracted from email by %s", sender_email)
            continue

        # Determine the agent name from the sender
        agent_name = email_to_agent.get(sender_email, sender_email)

        for parsed in parsed_rates:
            dedup_key = (sender_email, parsed.rate, parsed.rate_label)
            if dedup_key in existing_keys:
                continue
            existing_keys.add(dedup_key)

            # Assess each rate line against the prediction
            assessment = None
            pred_low = None
            pred_high = None
            if prediction and parsed.rate is not None:
                try:
                    assessment = assess_quotation(parsed.rate, prediction)
                    pred_low = prediction.predicted_low
                    pred_high = prediction.predicted_high
                except Exception as e:
                    logger.warning("Assessment failed for %s [%s]: %s", agent_name, parsed.rate_label, e)

            quotation_record = {
                "rfq_reference": reference,
                "agent_name": agent_name,
                "agent_email": sender_email,
                "raw_email_subject": subject,
                "raw_email_body": body,
                "rate": parsed.rate,
                "currency": parsed.currency,
                "transit_time_days": parsed.transit_time_days,
                "validity": parsed.validity,
                "terms": parsed.terms,
                "rate_label": parsed.rate_label,
                "ai_assessment": assessment,
                "predicted_low": pred_low,
                "predicted_high": pred_high,
                "is_selected": False,
            }

            try:
                supabase.table("quotations").insert(quotation_record).execute()
                new_quotations.append(quotation_record)
            except Exception as e:
                logger.error("Failed to store quotation from %s [%s]: %s", agent_name, parsed.rate_label, e)

    # Update job status if new quotations were found
    if new_quotations:
        try:
            supabase.table("rfq_jobs").update({"status": "quotes_received"}).eq("reference", reference).execute()
        except Exception as e:
            logger.error("Failed to update job status for %s: %s", reference, e)

    # Return all quotations for this job
    try:
        all_quotations_result = (
            supabase.table("quotations")
            .select("*")
            .eq("rfq_reference", reference)
            .execute()
        )
        all_quotations = all_quotations_result.data
    except Exception as e:
        logger.error("Failed to load all quotations for %s: %s", reference, e)
        all_quotations = new_quotations

    return {
        "reference": reference,
        "new_quotations_found": len(new_quotations),
        "total_quotations": len(all_quotations),
        "quotations": all_quotations,
        "prediction": prediction.model_dump() if prediction and hasattr(prediction, "model_dump") else None,
    }


@app.get("/jobs/{reference}/quotations")
def list_quotations(reference: str):
    """List all quotations for a given RFQ job."""
    try:
        result = (
            supabase.table("quotations")
            .select("*")
            .eq("rfq_reference", reference)
            .execute()
        )
        return result.data
    except Exception as e:
        logger.error("Failed to list quotations for %s: %s", reference, e)
        raise AppException(status_code=500, detail=f"Failed to list quotations: {e}")


@app.get("/jobs/{reference}/prediction")
def get_prediction(reference: str):
    """Get an AI price prediction for this job based on historical shipments."""

    # Load the job to get shipment details
    try:
        job_result = (
            supabase.table("rfq_jobs")
            .select("*")
            .eq("reference", reference)
            .execute()
        )
    except Exception as e:
        raise AppException(status_code=500, detail=f"Failed to fetch job: {e}")

    if not job_result.data:
        raise AppException(status_code=404, detail=f"Job {reference} not found")

    job = job_result.data[0]
    shipment = {
        "origin": job.get("shipment_origin", ""),
        "destination": job.get("shipment_destination", ""),
        "mode": job.get("shipment_mode", ""),
        "weight_kg": job.get("shipment_weight_kg", 0),
        "commodity": job.get("shipment_commodity", ""),
    }

    # Find similar historical shipments
    try:
        history = find_similar_shipments(
            origin=shipment.get("origin", ""),
            destination=shipment.get("destination", ""),
            mode=shipment.get("mode", ""),
            commodity_desc=shipment.get("commodity", ""),
        )
    except Exception as e:
        logger.warning("History lookup failed: %s", e)
        history = []

    # Run price prediction
    try:
        prediction: PricePrediction = predict_price(shipment, history)
    except Exception as e:
        raise AppException(status_code=500, detail=f"Price prediction failed: {e}")

    return {
        "reference": reference,
        "prediction": prediction.model_dump() if hasattr(prediction, "model_dump") else prediction,
        "history_matches_used": len(history),
    }


@app.post("/jobs/{reference}/approve")
def approve_quotation(reference: str, payload: ApproveRequest):
    """Approve a quotation: mark it as selected, send acceptance to the winner,
    and send polite rejection emails to the other agents."""

    selected_agent = payload.selected_agent

    # Load all quotations for this job
    try:
        quot_result = (
            supabase.table("quotations")
            .select("*")
            .eq("rfq_reference", reference)
            .execute()
        )
    except Exception as e:
        raise AppException(status_code=500, detail=f"Failed to fetch quotations: {e}")

    if not quot_result.data:
        raise AppException(status_code=404, detail=f"No quotations found for {reference}")

    quotations = quot_result.data
    selected = None
    rejected = []

    for q in quotations:
        if q["agent_name"] == selected_agent:
            selected = q
        else:
            rejected.append(q)

    if selected is None:
        raise AppException(
            status_code=404,
            detail=f"No quotation from agent '{selected_agent}' found for {reference}",
        )

    # 1. Mark the selected quotation
    try:
        supabase.table("quotations").update({"is_selected": True}).eq("rfq_reference", reference).eq("agent_name", selected_agent).execute()
    except Exception as e:
        logger.error("Failed to mark quotation as selected: %s", e)
        raise AppException(status_code=500, detail=f"Failed to update quotation: {e}")

    # 2. Send acceptance email to selected agent
    acceptance_subject = f"Re: {reference} | Quotation Accepted"
    acceptance_body = (
        f"Dear {selected['agent_name']},\n\n"
        f"We are pleased to inform you that your quotation for RFQ {reference} has been accepted.\n\n"
        f"We look forward to working with you on this shipment. "
        f"Please proceed with the necessary arrangements and confirm the booking at your earliest convenience.\n\n"
        f"Best regards,\nLogistics Copilot"
    )

    sent_results = []
    try:
        send_rfq_email(
            to_addr=selected["agent_email"],
            subject=acceptance_subject,
            body=acceptance_body,
        )
        sent_results.append({"agent_name": selected["agent_name"], "type": "acceptance", "status": "sent"})
    except Exception as e:
        logger.error("Failed to send acceptance email to %s: %s", selected["agent_name"], e)
        sent_results.append({"agent_name": selected["agent_name"], "type": "acceptance", "status": f"failed: {e}"})

    # 3. Send polite rejection emails to other agents
    for q in rejected:
        rejection_subject = f"Re: {reference} | Thank You for Your Quotation"
        rejection_body = (
            f"Dear {q['agent_name']},\n\n"
            f"Thank you for submitting your quotation for RFQ {reference}.\n\n"
            f"After careful consideration, we have decided to proceed with another provider for this shipment. "
            f"We truly appreciate your time and effort, and we hope to collaborate on future opportunities.\n\n"
            f"Best regards,\nLogistics Copilot"
        )

        try:
            send_rfq_email(
                to_addr=q["agent_email"],
                subject=rejection_subject,
                body=rejection_body,
            )
            sent_results.append({"agent_name": q["agent_name"], "type": "rejection", "status": "sent"})
        except Exception as e:
            logger.error("Failed to send rejection email to %s: %s", q["agent_name"], e)
            sent_results.append({"agent_name": q["agent_name"], "type": "rejection", "status": f"failed: {e}"})

    # 4. Update job status to approved
    try:
        supabase.table("rfq_jobs").update({"status": "approved"}).eq("reference", reference).execute()
    except Exception as e:
        logger.error("Failed to update job status to approved for %s: %s", reference, e)

    return {
        "reference": reference,
        "status": "approved",
        "selected_agent": selected_agent,
        "email_results": sent_results,
    }


@app.post("/classify-email")
def classify_email_endpoint(payload: ClassifyRequest):
    """Classify a single email with one LLM call through the active provider (LLM_PROVIDER)."""
    try:
        result = classify_email(
            subject=payload.subject,
            body=payload.body,
            sender=payload.sender,
        )
        return {
            "label": result.label,
            "confidence": result.confidence,
            "method": result.method,
            "details": result.details,
        }
    except Exception as e:
        raise AppException(status_code=500, detail=f"Classification failed: {e}")


@app.post("/classify-inbox")
def classify_inbox_endpoint(limit: int = 20, offset: int = 0):
    """Fetch inbox emails and classify each one. Returns emails with labels."""
    try:
        result = fetch_latest_emails(limit=limit, offset=offset)
        emails = result.get("emails", [])
        total = result.get("total", 0)
    except Exception as e:
        raise AppException(status_code=500, detail=f"Failed to fetch inbox: {e}")

    label_map = classify_with_cache([
        {"id": e["id"], "subject": e["subject"], "body": e["body"], "sender": e["from"]}
        for e in emails
    ])
    classified = [
        {"id": e["id"], "subject": e["subject"],
         "label": label_map.get(e["id"], {}).get("label", "general"),
         "confidence": label_map.get(e["id"], {}).get("confidence", 0.0),
         "method": label_map.get(e["id"], {}).get("method", "")}
        for e in emails
    ]
    return {
        "emails": classified,
        "total": total,
        "has_more": (offset + limit) < total,
    }


@app.post("/feedback")
def feedback_endpoint(payload: FeedbackRequest):
    """Submit a human correction — stores it as feedback and adds it to SVM training data."""
    valid_labels = {"customer_requirement", "quotation_rate_card", "general"}
    if payload.corrected_label not in valid_labels:
        raise AppException(
            status_code=422,
            detail=f"Invalid label '{payload.corrected_label}'. Must be one of: {valid_labels}",
        )
    try:
        result = submit_feedback(
            email_subject=payload.email_subject,
            email_body=payload.email_body,
            email_sender=payload.email_sender,
            predicted_label=payload.predicted_label,
            corrected_label=payload.corrected_label,
            confidence=payload.confidence,
        )
        # Sync the correction into the cache so the fix sticks on next refresh.
        if payload.email_id:
            cache_update_label(payload.email_id, payload.corrected_label)
        return result
    except Exception as e:
        raise AppException(status_code=500, detail=f"Failed to submit feedback: {e}")


@app.get("/classifier-status")
def classifier_status():
    """Return current classifier configuration — active LLM provider + model."""
    from backend.classifier.llm_provider import available_providers

    active = os.environ.get("LLM_PROVIDER", "openai").strip().lower()
    model_by_provider = {
        "openai": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        "gemini": os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite"),
    }

    try:
        feedback_result = supabase.table("classification_feedback").select("id", count="exact").limit(1).execute()
        feedback_count = feedback_result.count or 0
    except Exception:
        feedback_count = 0

    return {
        "classifier": "llm",
        "active_provider": active,
        "active_model": model_by_provider.get(active),
        "available_providers": available_providers(),
        "feedback_corrections": feedback_count,
    }


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Automation endpoints
# ---------------------------------------------------------------------------

class AutomationToggle(BaseModel):
    enabled: bool


@app.get("/automation/status")
def automation_status():
    try:
        return automation_get_status()
    except Exception as e:
        raise AppException(status_code=500, detail=str(e))


@app.post("/automation/run-now")
def automation_run_now():
    try:
        stats = run_scan(supabase)
        return stats
    except Exception as e:
        raise AppException(status_code=500, detail=str(e))


@app.post("/automation/toggle")
def automation_toggle(payload: AutomationToggle):
    try:
        automation_set_enabled(payload.enabled)
        return {"enabled": payload.enabled}
    except Exception as e:
        raise AppException(status_code=500, detail=str(e))
