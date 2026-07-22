"""
automation.py
-------------
Full end-to-end automation loop. Runs every 5 minutes via APScheduler
(started from the FastAPI lifespan handler — see backend/app/api.py).

State lives in Supabase, NOT on disk:
  - emails.processed_at      which emails the pipeline has handled. The scan
                             claims a row atomically (UPDATE ... WHERE
                             processed_at IS NULL) so duplicate RFQ sends are
                             impossible across restarts, threads, or workers.
  - automation_state (1 row) enabled flag + last-run stats.

Pipeline per run:
  1. Select unprocessed emails from `emails` (already ingested + classified
     by the ingest job — no Gmail call, no re-classification)
  2. Atomically claim each row
  3. customer_requirement -> intake -> lookup agents -> RFQ drafts -> send
       -> store job. Replies in a thread that already produced a
       customer_requirement are downgraded to general.
  4. quotation_rate_card  -> find matching open jobs -> parse rates -> store
"""

import logging
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("logistics_copilot.automation")
SCAN_BATCH = 50

# Prevents the scheduler tick and POST /automation/run-now from scanning
# concurrently in the same process. Cross-process safety comes from the
# atomic claim on emails.processed_at.
_scan_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@dataclass
class ScanStats:
    run_at: str = ""
    emails_scanned: int = 0
    new_emails: int = 0
    customer_requirements: int = 0
    quotation_rate_cards: int = 0
    general: int = 0
    errors: int = 0
    duration_seconds: float = 0.0
    jobs_created: List[str] = field(default_factory=list)
    quotations_stored: int = 0
    # Detected customer-requirement emails shown in the dashboard's last-run panel
    customer_emails: List[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# automation_state (single row, id=1)
# ---------------------------------------------------------------------------

def _get_state_row(supabase) -> dict:
    try:
        rows = supabase.table("automation_state").select("*").eq("id", 1).execute().data or []
        if rows:
            return rows[0]
    except Exception as e:
        logger.warning("automation_state read failed (run sql/setup_scan_state.sql?): %s", e)
    return {"enabled": True, "last_stats": None}


def _save_stats(supabase, stats: ScanStats) -> None:
    try:
        supabase.table("automation_state").upsert({
            "id": 1,
            "last_stats": asdict(stats),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="id").execute()
    except Exception as e:
        logger.warning("Failed to save scan stats: %s", e)


def get_status(supabase) -> dict:
    row = _get_state_row(supabase)
    try:
        processed_total = (
            supabase.table("emails")
            .select("id", count="exact")
            .not_.is_("processed_at", "null")
            .limit(1)
            .execute()
            .count or 0
        )
    except Exception:
        processed_total = 0
    email_redirect = (os.getenv("EMAIL_REDIRECT") or "").strip()
    return {
        "enabled": row.get("enabled", True),
        "schedule": "Every 5 minutes",
        "last_run": row.get("last_stats"),
        "processed_total": processed_total,
        # Safety: when set, ALL outgoing mail goes here instead of vendors
        "email_redirect": email_redirect or None,
        "safe_mode": bool(email_redirect),
    }


def set_enabled(supabase, enabled: bool) -> None:
    try:
        supabase.table("automation_state").upsert({
            "id": 1,
            "enabled": enabled,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="id").execute()
        logger.info("Automation %s", "enabled" if enabled else "disabled")
    except Exception as e:
        logger.error("Failed to toggle automation: %s", e)
        raise


# ---------------------------------------------------------------------------
# Claim + thread helpers
# ---------------------------------------------------------------------------

def _claim_email(supabase, row_id: str) -> bool:
    """Atomically mark one email as processed. Returns True only for the
    caller that wins the claim — everyone else sees zero updated rows."""
    try:
        res = (
            supabase.table("emails")
            .update({"processed_at": datetime.now(timezone.utc).isoformat()})
            .eq("id", row_id)
            .is_("processed_at", "null")
            .execute()
        )
        return bool(res.data)
    except Exception as e:
        logger.error("Failed to claim email %s: %s", row_id, e)
        return False


def _thread_already_customer(supabase, thread_id: str, current_msg_id: str) -> bool:
    """True if another email in this thread was already processed as a
    customer_requirement — replies then get downgraded to general."""
    if not thread_id:
        return False
    try:
        rows = (
            supabase.table("emails")
            .select("id")
            .eq("thread_id", thread_id)
            .eq("classification", "customer_requirement")
            .not_.is_("processed_at", "null")
            .neq("provider_msg_id", current_msg_id)
            .limit(1)
            .execute()
            .data
        )
        return bool(rows)
    except Exception as e:
        logger.warning("Thread lookup failed for %s: %s", thread_id, e)
        return False


# ---------------------------------------------------------------------------
# Customer requirement -> create RFQ job
# ---------------------------------------------------------------------------

def _process_customer_email(email: dict, supabase) -> Optional[str]:
    """Run full RFQ pipeline for one customer email. Returns RFQ reference or None."""
    from backend.agents.intake_agent import run_intake_agent
    from backend.agents.agents_lookup import lookup_agents
    from backend.agents.rfq_agent import generate_rfq_drafts
    from backend.connectors.email_sender import send_rfq_emails_batch

    subject = email.get("subject", "")
    body = email.get("body", "")
    sender = email.get("from", "")
    full_content = f"Subject: {subject}\n\nBody:\n{body}"

    # 1. Extract shipment details
    try:
        shipment = run_intake_agent(full_content)
    except Exception as e:
        logger.error("Intake agent failed for email %s: %s", email.get("id"), e)
        return None

    # All fields are optional, but auto-sending an RFQ needs at least a route
    if not shipment.origin or not shipment.destination:
        logger.warning(
            "Email %s missing origin/destination (origin=%r, destination=%r) — skipping auto-RFQ",
            email.get("id"), shipment.origin, shipment.destination,
        )
        return None

    # 2. Lookup agents
    try:
        agents = lookup_agents(
            destination=shipment.destination,
            destination_country=shipment.destination_country,
            mode=shipment.mode,
            commodity_desc=shipment.commodity,
            origin=shipment.origin,
        )
    except Exception as e:
        logger.error("Agent lookup failed: %s", e)
        return None

    if not agents:
        logger.warning("No agents found for shipment to %s", shipment.destination)
        return None

    # 3. Generate RFQ reference + drafts
    reference = f"RFQ-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:4]}"
    agents_as_dicts = [
        {"agent_name": a.agent_name, "email": a.email, "specialty": a.specialty,
         "historical_rate": a.historical_rate, "historical_transit_days": a.historical_transit_days}
        for a in agents
    ]

    try:
        drafts_result = generate_rfq_drafts(shipment_data=shipment.model_dump(), agents=agents_as_dicts, reference=reference)
        drafts_list = drafts_result.drafts if hasattr(drafts_result, "drafts") else drafts_result
    except Exception as e:
        logger.error("RFQ draft generation failed: %s", e)
        return None

    drafts_as_dicts = [
        {"vendor_name": d.vendor_name, "vendor_email": d.vendor_email, "subject": d.subject, "body": d.body}
        for d in (drafts_list if isinstance(drafts_list, list) else [])
    ]

    # 4. Send RFQs (EMAIL_REDIRECT routes to test inbox)
    try:
        send_rfq_emails_batch(drafts_as_dicts)
    except Exception as e:
        logger.error("Batch send failed: %s", e)

    # 5. Store job in Supabase
    job_record = {
        "reference": reference,
        "customer_email_sender": sender,
        "customer_email_subject": subject,
        "customer_email_body": body,
        "shipment_origin": shipment.origin,
        "shipment_destination": shipment.destination,
        "shipment_mode": shipment.mode,
        "shipment_weight_kg": float(shipment.weight_kg) if shipment.weight_kg is not None else None,
        "shipment_commodity": shipment.commodity,
        "status": "rfqs_sent",
        "agents_contacted": [a.agent_name for a in agents],
    }
    try:
        supabase.table("rfq_jobs").insert(job_record).execute()
        logger.info("Job created: %s → %d agents contacted", reference, len(agents))
    except Exception as e:
        logger.error("Failed to store job %s: %s", reference, e)
        return None

    return reference


# ---------------------------------------------------------------------------
# Quotation rate card -> parse and store
# ---------------------------------------------------------------------------

def _process_rate_card(email: dict, supabase) -> int:
    """Parse a rate card email and store quotations against open jobs. Returns count stored."""
    from backend.agents.quotation_agent import parse_quotation_email
    from backend.classifier.price_predictor import predict_price, assess_quotation
    from backend.agents.history_agent import find_similar_shipments

    subject = email.get("subject", "")
    body = email.get("body", "")
    sender_raw = email.get("from", "")
    m = re.search(r'<([^>]+)>', sender_raw)
    sender_email = (m.group(1) if m else sender_raw).strip().lower()

    # Find open jobs that contacted this agent — newest first so the fallback
    # below genuinely targets the latest job.
    try:
        open_jobs = (
            supabase.table("rfq_jobs")
            .select("*")
            .in_("status", ["rfqs_sent", "quotes_received"])
            .order("created_at", desc=True)
            .execute()
            .data
        )
    except Exception as e:
        logger.error("Failed to fetch open jobs: %s", e)
        return 0

    from backend.agents.agents_lookup import _load_agents_csv
    all_agents = _load_agents_csv()
    agent_email_to_name = {a.get("email", "").lower(): a.get("agent_name", "") for a in all_agents}
    agent_name = agent_email_to_name.get(sender_email, sender_email)

    # Find jobs where this agent was contacted
    matching_jobs = [
        j for j in open_jobs
        if agent_name in (j.get("agents_contacted") or [])
        or sender_email in [a.get("email", "").lower() for a in all_agents
                           if a.get("agent_name") in (j.get("agents_contacted") or [])]
    ]

    if not matching_jobs:
        logger.info("Rate card from %s — no matching open job, storing against latest job", sender_email)
        matching_jobs = open_jobs[:1] if open_jobs else []

    if not matching_jobs:
        logger.warning("Rate card from %s — no open jobs at all, skipping", sender_email)
        return 0

    try:
        parsed_rates = parse_quotation_email(body, subject)
    except Exception as e:
        logger.warning("Failed to parse rates from %s: %s", sender_email, e)
        return 0

    if not parsed_rates:
        return 0

    stored = 0
    for job in matching_jobs[:1]:  # match to most recent relevant job
        reference = job["reference"]
        shipment = {
            "origin": job.get("shipment_origin", ""),
            "destination": job.get("shipment_destination", ""),
            "mode": job.get("shipment_mode", ""),
            "weight_kg": job.get("shipment_weight_kg", 0),
            "commodity": job.get("shipment_commodity", ""),
        }

        try:
            history = find_similar_shipments(origin=shipment["origin"], destination=shipment["destination"],
                                             mode=shipment["mode"], commodity_desc=shipment["commodity"])
            prediction = predict_price(shipment, history)
        except Exception:
            prediction = None

        # Load existing dedup keys
        try:
            existing = supabase.table("quotations").select("agent_email, rate, rate_label").eq("rfq_reference", reference).execute().data
            existing_keys = {(r["agent_email"], r["rate"], r.get("rate_label", "")) for r in existing}
        except Exception:
            existing_keys = set()

        for parsed in parsed_rates:
            dedup_key = (sender_email, parsed.rate, parsed.rate_label)
            if dedup_key in existing_keys:
                continue
            existing_keys.add(dedup_key)

            assessment = None
            pred_low = pred_high = None
            if prediction and parsed.rate is not None:
                try:
                    assessment = assess_quotation(parsed.rate, prediction)
                    pred_low = prediction.predicted_low
                    pred_high = prediction.predicted_high
                except Exception:
                    pass

            record = {
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
                supabase.table("quotations").insert(record).execute()
                stored += 1
            except Exception as e:
                logger.error("Failed to store quotation: %s", e)

        if stored > 0:
            try:
                supabase.table("rfq_jobs").update({"status": "quotes_received"}).eq("reference", reference).execute()
            except Exception:
                pass

    return stored


# ---------------------------------------------------------------------------
# Main scan job
# ---------------------------------------------------------------------------

def run_scan(supabase) -> ScanStats:
    """Process unclaimed emails from the `emails` table. Called every 5 min
    by the scheduler and on demand via POST /automation/run-now."""
    stats = ScanStats(run_at=datetime.now(timezone.utc).isoformat())

    if not _scan_lock.acquire(blocking=False):
        logger.info("Scan already running — skipping this trigger")
        return stats

    try:
        state = _get_state_row(supabase)
        if not state.get("enabled", True):
            logger.info("Automation disabled — skipping scan")
            return stats

        start = time.time()
        logger.info("Starting inbox scan...")

        try:
            rows = (
                supabase.table("emails")
                .select("id, provider_msg_id, thread_id, sender, subject, body, classification")
                .is_("processed_at", "null")
                .order("received_at", desc=False)  # oldest first — FIFO
                .limit(SCAN_BATCH)
                .execute()
                .data or []
            )
        except Exception as e:
            logger.error("Scan failed to fetch unprocessed emails: %s", e)
            stats.errors += 1
            _save_stats(supabase, stats)
            return stats

        stats.emails_scanned = len(rows)
        stats.new_emails = len(rows)
        logger.info("Found %d unprocessed emails", len(rows))

        for r in rows:
            if not _claim_email(supabase, r["id"]):
                continue  # another worker claimed it — not ours

            msg_id = r.get("provider_msg_id", "")
            label = (r.get("classification") or "").strip()

            # Ingest normally classifies at persist time; classify here only
            # if that step was skipped or failed.
            if not label:
                try:
                    from backend.classifier.email_classifier import classify_email
                    label = classify_email(
                        subject=r.get("subject", ""), body=r.get("body", ""),
                        sender=r.get("sender", ""),
                    ).label
                except Exception as e:
                    logger.warning("Fallback classification failed for %s: %s", msg_id, e)
                    label = "general"

            # Replies in an already-processed customer thread -> general
            if label == "customer_requirement" and _thread_already_customer(
                supabase, r.get("thread_id", ""), msg_id
            ):
                logger.info("Email %s is a reply in an already-processed customer thread — treating as general", msg_id)
                label = "general"

            email = {
                "id": msg_id,
                "subject": r.get("subject") or "",
                "body": r.get("body") or "",
                "from": r.get("sender") or "",
            }

            try:
                if label == "customer_requirement":
                    stats.customer_requirements += 1
                    stats.customer_emails.append({
                        "id": msg_id,
                        "subject": email["subject"],
                        "sender": email["from"],
                    })
                    reference = _process_customer_email(email, supabase)
                    if reference:
                        stats.jobs_created.append(reference)

                elif label == "quotation_rate_card":
                    stats.quotation_rate_cards += 1
                    n = _process_rate_card(email, supabase)
                    stats.quotations_stored += n

                else:
                    stats.general += 1

            except Exception as e:
                logger.error("Failed to process email %s (%s): %s", msg_id, label, e)
                stats.errors += 1

        stats.duration_seconds = round(time.time() - start, 2)
        logger.info(
            "Scan done in %.1fs — %d new, %d customer→%d jobs, %d rate cards→%d quotations, %d errors",
            stats.duration_seconds, stats.new_emails, stats.customer_requirements,
            len(stats.jobs_created), stats.quotation_rate_cards, stats.quotations_stored, stats.errors,
        )
        _save_stats(supabase, stats)
        return stats

    finally:
        _scan_lock.release()
