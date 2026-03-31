import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import List

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from supabase import create_client

from intake_agent import run_intake_agent, ShipmentDetails
from agents_lookup import lookup_agents, AgentMatch
from rfq_agent import generate_rfq_drafts
from email_connector import fetch_latest_emails, fetch_emails_by_subject
from email_sender import send_rfq_email, send_rfq_emails_batch
from quotation_agent import parse_quotation_email
from price_predictor import predict_price, assess_quotation, PricePrediction
from history_agent import find_similar_shipments
from email_classifier import classify_email, classify_emails_batch, submit_feedback
from automation import run_daily_scan, get_status as automation_get_status, set_enabled as automation_set_enabled

load_dotenv()

logger = logging.getLogger("logistics_copilot")

# ---------------------------------------------------------------------------
# Supabase client
# ---------------------------------------------------------------------------

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# ---------------------------------------------------------------------------
# Custom exception & handlers
# ---------------------------------------------------------------------------

class AppException(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


scheduler = BackgroundScheduler(timezone="UTC")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    scheduler.add_job(run_daily_scan, "cron", hour=7, minute=0, id="daily_scan", replace_existing=True)
    scheduler.start()
    logger.info("Scheduler started — daily scan at 07:00 UTC")
    yield
    scheduler.shutdown()
    logger.info("Scheduler stopped")


app = FastAPI(title="Logistics Copilot API", lifespan=lifespan)


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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/fetch-inbox")
def fetch_inbox(limit: int = 20, offset: int = 0, search: str = ""):
    try:
        if search:
            from email_connector import fetch_emails_by_subject
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
        # Classify each email (rules only — fast, free, no API call unless ambiguous)
        classified = classify_emails_batch([
            {"id": e["id"], "subject": e["subject"], "body": e["body"], "sender": e["from"]}
            for e in emails_list
        ])
        label_map = {c["id"]: c for c in classified}
        return {
            "emails": [
                {
                    "id": e["id"],
                    "sender": e["from"],
                    "subject": e["subject"],
                    "body": e["body"],
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
    from agents_lookup import _load_agents_csv
    all_agents_csv = _load_agents_csv()
    email_to_agent = {}
    for a in all_agents_csv:
        email = a.get("email", "").strip().lower()
        name = a.get("agent_name", "")
        if email and name in agents_contacted:
            email_to_agent[email] = name

    # Fetch emails matching this reference
    try:
        reply_emails = fetch_emails_by_subject(reference)
    except Exception as e:
        logger.error("Failed to fetch emails for %s: %s", reference, e)
        raise AppException(status_code=500, detail=f"Failed to fetch reply emails: {e}")

    # Load already-stored quotations to avoid duplicates
    try:
        existing_result = (
            supabase.table("quotations")
            .select("raw_email_subject")
            .eq("rfq_reference", reference)
            .execute()
        )
        existing_subjects = {row["raw_email_subject"] for row in existing_result.data}
    except Exception as e:
        logger.error("Failed to load existing quotations: %s", e)
        existing_subjects = set()

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

        # Skip if already processed
        if subject in existing_subjects:
            continue

        # Parse the quotation
        try:
            parsed = parse_quotation_email(body, subject)
        except Exception as e:
            logger.warning("Failed to parse quotation from %s: %s", sender_email, e)
            continue

        # Determine the agent name from the sender
        agent_name = email_to_agent.get(sender_email, sender_email)

        # Assess the quotation against the prediction
        assessment = None
        pred_low = None
        pred_high = None
        if prediction and parsed.rate is not None:
            try:
                assessment = assess_quotation(parsed.rate, prediction)
                pred_low = prediction.predicted_low
                pred_high = prediction.predicted_high
            except Exception as e:
                logger.warning("Assessment failed for %s: %s", agent_name, e)

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
            "ai_assessment": assessment,
            "predicted_low": pred_low,
            "predicted_high": pred_high,
            "is_selected": False,
        }

        try:
            supabase.table("quotations").insert(quotation_record).execute()
            new_quotations.append(quotation_record)
        except Exception as e:
            logger.error("Failed to store quotation from %s: %s", agent_name, e)

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
    """Classify a single email using the hybrid classifier (rules → fine-tuned → KNN → few-shot)."""
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

    classified = classify_emails_batch([
        {"id": e["id"], "subject": e["subject"], "body": e["body"], "sender": e["from"]}
        for e in emails
    ])
    return {
        "emails": classified,
        "total": total,
        "has_more": (offset + limit) < total,
    }


@app.post("/feedback")
def feedback_endpoint(payload: FeedbackRequest):
    """Submit a human correction — stores it as feedback and adds it to KNN training data."""
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
        return result
    except Exception as e:
        raise AppException(status_code=500, detail=f"Failed to submit feedback: {e}")


@app.get("/classifier-status")
def classifier_status():
    """Return current classifier configuration — which tier is active."""
    fine_tuned = os.environ.get("CLASSIFIER_MODEL_ID", "")
    try:
        count_result = supabase.table("email_training_data").select("id", count="exact").limit(1).execute()
        training_examples = count_result.count or 0
    except Exception:
        training_examples = 0

    try:
        feedback_result = supabase.table("classification_feedback").select("id", count="exact").limit(1).execute()
        feedback_count = feedback_result.count or 0
    except Exception:
        feedback_count = 0

    return {
        "tiers_active": {
            "tier1_rules": True,
            "tier2_fine_tuned": bool(fine_tuned),
            "tier3_knn": training_examples >= 10,
            "tier4_few_shot_fallback": True,
        },
        "fine_tuned_model": fine_tuned or None,
        "knn_training_examples": training_examples,
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
    """Return scheduler state, last scan stats, and next run time."""
    status = automation_get_status()
    job = scheduler.get_job("daily_scan")
    next_run = job.next_run_time.isoformat() if job and job.next_run_time else None
    return {**status, "next_run": next_run}


@app.post("/automation/run-now")
def automation_run_now():
    """Trigger the daily scan immediately without waiting for 7 AM."""
    try:
        stats = run_daily_scan()
    except Exception as e:
        raise AppException(status_code=500, detail=f"Scan failed: {e}")
    return {
        "message": "Scan complete",
        "emails_scanned": stats.emails_scanned,
        "new_emails": stats.new_emails,
        "customer_requirements": stats.customer_requirements,
        "quotation_rate_cards": stats.quotation_rate_cards,
        "errors": stats.errors,
        "duration_seconds": stats.duration_seconds,
        "customer_emails": stats.customer_emails,
    }


@app.post("/automation/toggle")
def automation_toggle(body: AutomationToggle):
    """Enable or disable the daily scheduler."""
    automation_set_enabled(body.enabled)
    job = scheduler.get_job("daily_scan")
    if job:
        if body.enabled:
            job.resume()
        else:
            job.pause()
    return {"enabled": body.enabled}
