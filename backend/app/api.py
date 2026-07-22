import logging
import os
import socket
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import List, Optional

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
from backend.classifier.email_classifier import classify_email, submit_feedback
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


def _run_ingest_job():
    try:
        from backend.connectors.email_store import ingest_new_emails
        stats = ingest_new_emails()
        logger.info("Scheduled ingest done: %s", stats)
    except Exception as e:
        logger.error("Scheduled ingest error: %s", e)


def _run_backfill():
    try:
        from backend.connectors.email_store import backfill_classifications
        stats = backfill_classifications()
        logger.info("Backfill done: %s", stats)
    except Exception as e:
        logger.error("Backfill error: %s", e)


def _run_retry_pending():
    try:
        from backend.connectors.email_store import retry_pending_classifications
        stats = retry_pending_classifications()
        if stats.get("classified"):
            logger.info("Retry queue: reclassified %d pending email(s)", stats["classified"])
    except Exception as e:
        logger.error("Retry queue error: %s", e)


def _run_gap_audit():
    try:
        from backend.connectors.email_store import audit_sync_gaps
        audit_sync_gaps(days=14)  # logs a WARNING listing any days Gmail has but we don't
    except Exception as e:
        logger.error("Sync-gap audit error: %s", e)


# Scheduler ownership: started in the lifespan handler (NOT at import) so
# --reload and multi-worker deployments don't each spawn one. When scaling
# out, set RUN_SCHEDULER=0 on all but one instance.
RUN_SCHEDULER = os.environ.get("RUN_SCHEDULER", "1").strip() == "1"


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    import threading as _t
    scheduler = None
    if RUN_SCHEDULER:
        _t.Thread(target=_warmup, daemon=True).start()
        _t.Thread(target=_run_ingest_job, daemon=True).start()  # ingest on startup
        _t.Thread(target=_run_backfill, daemon=True).start()    # classify existing unclassified emails
        scheduler = BackgroundScheduler(timezone="UTC")
        scheduler.add_job(_run_scan_job, "interval", minutes=5, id="inbox_scan", replace_existing=True)
        scheduler.add_job(_run_ingest_job, "interval", minutes=5, id="email_ingest", replace_existing=True)
        scheduler.add_job(_run_gap_audit, "interval", hours=24, id="sync_gap_audit", replace_existing=True)
        # Retry queue for failed classifications — self-heals once the LLM
        # provider recovers (quota restored / outage over).
        scheduler.add_job(_run_retry_pending, "interval", minutes=15,
                          id="retry_pending_classifications", replace_existing=True)
        scheduler.start()
        logger.info("Automation scheduler started — scanning every 5 minutes")
    else:
        logger.info("RUN_SCHEDULER=0 — scheduler not started in this process")
    yield
    if scheduler is not None:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Logistics Copilot API", lifespan=_lifespan)


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



class ApproveRequest(BaseModel):
    selected_agent: str



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
    """Read emails from the Supabase `emails` table (populated by the ingest job).
    Labels come from `email_classifications` (feedback overrides have priority)
    with fallback to the `classification` column written at ingest time.
    No Gmail API call is made here — ordering and pagination are stable."""
    try:
        query = (
            supabase.table("emails")
            .select(
                "provider_msg_id, sender, subject, received_at, classification, classification_status",
                count="exact",
            )
            .order("received_at", desc=True)
        )
        if search:
            query = query.ilike("subject", f"%{search}%")
        query = query.range(offset, offset + limit - 1)
        result = query.execute()
        emails_list = result.data or []
        total = result.count or 0

        # Pull any human-corrected labels from the classification cache.
        provider_ids = [e["provider_msg_id"] for e in emails_list if e.get("provider_msg_id")]
        label_map: dict[str, dict] = {}
        if provider_ids:
            cache_rows = (
                supabase.table("email_classifications")
                .select("email_id, label, confidence, method")
                .in_("email_id", provider_ids)
                .execute()
                .data or []
            )
            label_map = {r["email_id"]: r for r in cache_rows}

        def _label(e: dict) -> str:
            """Cached label wins. Otherwise, if classification never succeeded
            (quota/API failure) report 'pending' rather than the 'general'
            fallback — an unclassified RFQ must not look like a real verdict."""
            cached = label_map.get(e["provider_msg_id"], {}).get("label")
            if cached:
                return cached
            if e.get("classification_status") == "pending":
                return "pending"
            return e.get("classification") or "general"

        return {
            "emails": [
                {
                    "id": e["provider_msg_id"],
                    "sender": e["sender"],
                    "subject": e["subject"],
                    "body": "",  # loaded on-demand via GET /email-body/<id>
                    "label": _label(e),
                    "label_pending": _label(e) == "pending",
                    "label_confidence": label_map.get(e["provider_msg_id"], {}).get("confidence", 0.0),
                    "label_method": label_map.get(e["provider_msg_id"], {}).get("method", ""),
                    "received_at": e.get("received_at"),
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
    """Return full body from Supabase (stored at ingest). Falls back to Gmail API if not found."""
    try:
        rows = (
            supabase.table("emails")
            .select("body")
            .eq("provider_msg_id", message_id)
            .limit(1)
            .execute()
            .data or []
        )
        if rows and rows[0].get("body"):
            return {"body": rows[0]["body"]}
        from backend.connectors.gmail_connector import fetch_full_message
        msg = fetch_full_message(message_id)
        return {"body": msg["body"]}
    except Exception as e:
        raise AppException(status_code=500, detail=f"Failed to fetch email body: {e}")


@app.get("/email-attachments/{message_id}")
def get_email_attachments(message_id: str):
    """Return attachment metadata + signed download URLs for one email."""
    try:
        email_rows = (
            supabase.table("emails")
            .select("id")
            .eq("provider_msg_id", message_id)
            .limit(1)
            .execute()
            .data or []
        )
        if not email_rows:
            return {"attachments": []}

        att_rows = (
            supabase.table("attachments")
            .select("id, file_name, mime_type, size_bytes, storage_path")
            .eq("email_id", email_rows[0]["id"])
            .execute()
            .data or []
        )

        result = []
        for att in att_rows:
            try:
                signed = supabase.storage.from_(
                    os.environ.get("ATTACHMENT_BUCKET", "rate-card-attachments")
                ).create_signed_url(att["storage_path"], 300)  # 5-min expiry
                url = signed.get("signedURL") or signed.get("signedUrl") or ""
            except Exception:
                url = ""
            result.append({
                "id": att["id"],
                "file_name": att["file_name"],
                "mime_type": att["mime_type"],
                "size_bytes": att["size_bytes"],
                "url": url,
            })
        return {"attachments": result}
    except Exception as e:
        raise AppException(status_code=500, detail=f"Failed to fetch attachments: {e}")


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

    # All extraction fields are optional, but this endpoint auto-sends RFQs —
    # a route is the minimum needed to draft one.
    if not shipment.origin or not shipment.destination:
        raise AppException(
            status_code=422,
            detail="Email does not state both origin and destination — use the Send Request dashboard to fill them in manually",
        )

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
        "shipment_weight_kg": float(shipment.weight_kg) if shipment.weight_kg is not None else None,
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



# ---------------------------------------------------------------------------
# Automation endpoints
# ---------------------------------------------------------------------------

class AutomationToggle(BaseModel):
    enabled: bool


@app.get("/automation/status")
def automation_status():
    try:
        return automation_get_status(supabase)
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
        automation_set_enabled(supabase, payload.enabled)
        return {"enabled": payload.enabled}
    except Exception as e:
        raise AppException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Send Request dashboard endpoints
# ---------------------------------------------------------------------------

class SelectedAgent(BaseModel):
    agent_name: str
    email: str


class SendRFQRequest(BaseModel):
    origin_port: str
    destination_port: str
    size: str = ""
    commodity: str = ""
    mode: str = "sea_freight"
    weight_kg: Optional[float] = None
    agents: List[SelectedAgent]
    # Original customer email, stored on the job for traceability
    customer_sender: str = ""
    customer_subject: str = ""
    customer_body: str = ""


@app.get("/agents")
def list_agents():
    """List all agents from the Supabase `agents` table, grouped by category.
    Feeds the three multi-select dropdowns on the Send Request dashboard."""
    try:
        result = supabase.table("agents").select("*").order("agent_name").execute()
        agents = result.data or []
    except Exception as e:
        raise AppException(status_code=500, detail=f"Failed to fetch agents: {e}")

    grouped: dict[str, list] = {}
    for a in agents:
        grouped.setdefault(a.get("category", "OTHER"), []).append(a)

    return {"agents": agents, "by_category": grouped, "total": len(agents)}


@app.post("/extract-details")
def extract_details(payload: EmailInput):
    """Run the intake agent on a customer email and return the extracted
    shipment fields WITHOUT sending anything. Used to prefill the Send
    Request dashboard form."""
    full_content = f"Subject: {payload.subject}\n\nBody:\n{payload.body}"
    try:
        shipment: ShipmentDetails = run_intake_agent(full_content)
    except Exception as e:
        raise AppException(status_code=422, detail=f"Extraction failed: {e}")
    return {"shipment": shipment.model_dump()}


class PreviewRFQRequest(BaseModel):
    origin_port: str
    destination_port: str
    size: str = ""
    commodity: str = ""
    mode: str = "sea_freight"
    weight_kg: Optional[float] = None
    agent: Optional[SelectedAgent] = None


@app.post("/preview-rfq")
def preview_rfq(payload: PreviewRFQRequest):
    """Generate ONE sample RFQ draft without sending anything. Lets the user
    see the email an agent would receive before committing to Send."""
    if not payload.origin_port.strip() or not payload.destination_port.strip():
        raise AppException(status_code=422, detail="Origin and destination ports are required")

    agent = payload.agent or SelectedAgent(agent_name="Sample Agent", email="agent@example.com")
    # Real-format reference so the preview matches what actually goes out.
    reference = f"RFQ-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:4]}"

    shipment_data = {
        "origin": payload.origin_port.strip(),
        "destination": payload.destination_port.strip(),
        "mode": payload.mode,
        "weight_kg": payload.weight_kg,
        "commodity": payload.commodity.strip(),
        "size": payload.size.strip(),
    }

    try:
        result = generate_rfq_drafts(
            shipment_data=shipment_data,
            agents=[{"agent_name": agent.agent_name, "email": agent.email}],
            reference=reference,
        )
        drafts = result.drafts if hasattr(result, "drafts") else result
        if not drafts:
            raise AppException(status_code=500, detail="LLM returned no draft")
        draft = drafts[0]
    except AppException:
        raise
    except Exception as e:
        raise AppException(status_code=500, detail=f"Draft generation failed: {e}")

    return {
        "reference": reference,
        "vendor_name": draft.vendor_name,
        "subject": draft.subject,
        "body": draft.body,
        "note": "Sample only — each agent gets its own unique reference at send time.",
    }


@app.post("/send-rfq")
def send_rfq(payload: SendRFQRequest):
    """Manual RFQ send from the dashboard. Each selected agent gets its OWN
    unique RFQ reference: one LLM draft, one email, and one rfq_jobs row per
    agent. Replies can then be matched to a specific agent via the reference
    in the subject line."""
    if not payload.agents:
        raise AppException(status_code=422, detail="Select at least one agent")
    if not payload.origin_port.strip() or not payload.destination_port.strip():
        raise AppException(status_code=422, detail="Origin and destination ports are required")

    shipment_data = {
        "origin": payload.origin_port.strip(),
        "destination": payload.destination_port.strip(),
        "mode": payload.mode,
        "weight_kg": payload.weight_kg,
        "commodity": payload.commodity.strip(),
        "size": payload.size.strip(),
    }

    # 1. Draft one email per agent, each with its own unique reference.
    #    Drafts run in parallel so latency ≈ one LLM call.
    from concurrent.futures import ThreadPoolExecutor

    def _draft_for_agent(agent: SelectedAgent) -> dict:
        reference = f"RFQ-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:4]}"
        entry = {
            "reference": reference,
            "agent_name": agent.agent_name,
            "email": agent.email,
            "draft": None,
            "error": "",
        }
        try:
            result = generate_rfq_drafts(
                shipment_data=shipment_data,
                agents=[{"agent_name": agent.agent_name, "email": agent.email}],
                reference=reference,
            )
            drafts = result.drafts if hasattr(result, "drafts") else result
            if drafts:
                entry["draft"] = drafts[0]
            else:
                entry["error"] = "LLM returned no draft"
        except Exception as e:
            entry["error"] = f"draft failed: {e}"
        return entry

    with ThreadPoolExecutor(max_workers=min(len(payload.agents), 10)) as pool:
        entries = list(pool.map(_draft_for_agent, payload.agents))

    # 2. Send the successful drafts as a batch
    drafts_as_dicts = [
        {
            "vendor_name": e["draft"].vendor_name,
            "vendor_email": e["draft"].vendor_email or e["email"],
            "subject": e["draft"].subject,
            "body": e["draft"].body,
        }
        for e in entries if e["draft"] is not None
    ]
    try:
        send_results = send_rfq_emails_batch(drafts_as_dicts)
    except Exception as e:
        logger.error("Batch send failed: %s", e)
        send_results = [{"vendor_name": d["vendor_name"], "status": f"batch_error: {e}"} for d in drafts_as_dicts]

    send_status = {r.get("vendor_name", ""): r.get("status", "") for r in send_results}

    # 3. Persist one job row per agent (RFQ_ID lives in rfq_jobs.reference)
    jobs = []
    for e in entries:
        status = e["error"] or send_status.get(e["agent_name"], "unknown")
        if e["draft"] is not None and not e["error"]:
            job_record = {
                "reference": e["reference"],
                "customer_email_sender": payload.customer_sender,
                "customer_email_subject": payload.customer_subject,
                "customer_email_body": payload.customer_body,
                "shipment_origin": shipment_data["origin"],
                "shipment_destination": shipment_data["destination"],
                "shipment_mode": payload.mode,
                "shipment_weight_kg": float(payload.weight_kg) if payload.weight_kg is not None else None,
                "shipment_commodity": payload.commodity,
                "shipment_size": payload.size,
                "status": "rfqs_sent",
                "agents_contacted": [e["agent_name"]],
            }
            try:
                supabase.table("rfq_jobs").insert(job_record).execute()
            except Exception as exc:
                logger.error("Failed to store job %s: %s", e["reference"], exc)
                status = f"sent but job persist failed: {exc}"
        jobs.append({
            "reference": e["reference"],
            "agent_name": e["agent_name"],
            "email": e["email"],
            "status": status,
        })

    return {"jobs": jobs, "shipment": shipment_data, "total_sent": len(drafts_as_dicts)}
