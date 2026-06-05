"""
automation.py
-------------
Full end-to-end automation loop. Runs every 5 minutes via APScheduler.

Pipeline per run:
  1. Fetch latest inbox emails (metadata only, fast)
  2. Skip already-processed IDs
  3. Classify new emails (parallel, snippet as body proxy)
  4. customer_requirement → fetch full body → intake → lookup agents →
       generate RFQ drafts → send emails → store job in Supabase
  5. quotation_rate_card  → fetch full body → find matching open jobs →
       parse rates → store quotations → update job status
  6. Persist processed IDs to avoid re-processing across runs
"""

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("logistics_copilot.automation")

STATE_FILE = Path(__file__).parent / "automation_state.json"
SCAN_BATCH = 50


# ---------------------------------------------------------------------------
# State
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


@dataclass
class AutomationState:
    enabled: bool = True
    last_stats: Optional[dict] = None
    processed_ids: List[str] = field(default_factory=list)


def _load_state() -> AutomationState:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return AutomationState(**{k: v for k, v in data.items() if k in AutomationState.__dataclass_fields__})
        except Exception as e:
            logger.warning("Failed to load state: %s — using defaults", e)
    return AutomationState()


def _save_state(state: AutomationState) -> None:
    STATE_FILE.write_text(json.dumps(asdict(state), indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Customer requirement → create RFQ job
# ---------------------------------------------------------------------------

def _process_customer_email(email: dict, supabase) -> Optional[str]:
    """Run full RFQ pipeline for one customer email. Returns RFQ reference or None."""
    from intake_agent import run_intake_agent
    from agents_lookup import lookup_agents
    from rfq_agent import generate_rfq_drafts
    from email_sender import send_rfq_emails_batch

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
        "shipment_weight_kg": float(shipment.weight_kg),
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
# Quotation rate card → parse and store
# ---------------------------------------------------------------------------

def _process_rate_card(email: dict, supabase) -> int:
    """Parse a rate card email and store quotations against open jobs. Returns count stored."""
    from quotation_agent import parse_quotation_email
    from price_predictor import predict_price, assess_quotation
    from history_agent import find_similar_shipments
    import re

    subject = email.get("subject", "")
    body = email.get("body", "")
    sender_raw = email.get("from", "")
    m = re.search(r'<([^>]+)>', sender_raw)
    sender_email = (m.group(1) if m else sender_raw).strip().lower()

    # Find open jobs that contacted this agent
    try:
        open_jobs = supabase.table("rfq_jobs").select("*").in_("status", ["rfqs_sent", "quotes_received"]).execute().data
    except Exception as e:
        logger.error("Failed to fetch open jobs: %s", e)
        return 0

    from agents_lookup import _load_agents_csv
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
    """Fetch recent emails, classify new ones, run full pipeline. Called every 5 min."""
    from gmail_connector import fetch_latest_emails, fetch_full_message
    from email_classifier import classify_emails_batch

    state = _load_state()
    if not state.enabled:
        logger.info("Automation disabled — skipping scan")
        return ScanStats(run_at=datetime.now(timezone.utc).isoformat())

    stats = ScanStats(run_at=datetime.now(timezone.utc).isoformat())
    start = time.time()
    logger.info("Starting inbox scan...")

    try:
        result = fetch_latest_emails(limit=SCAN_BATCH, offset=0)
        emails = result.get("emails", [])
        stats.emails_scanned = len(emails)
        processed_set = set(state.processed_ids)

        # Filter to only new emails
        new_emails = [e for e in emails if e.get("id") not in processed_set]
        stats.new_emails = len(new_emails)
        logger.info("Scanned %d emails, %d new", stats.emails_scanned, stats.new_emails)

        if not new_emails:
            state.last_stats = asdict(stats)
            _save_state(state)
            return stats

        # Batch classify (snippet as body proxy — fast)
        batch = [{"id": e["id"], "subject": e["subject"],
                  "body": e.get("snippet", "") or e.get("body", ""),
                  "sender": e["from"]} for e in new_emails]
        classified = classify_emails_batch(batch)
        label_map = {c["id"]: c["label"] for c in classified}

        for email in new_emails:
            email_id = email.get("id", "")
            label = label_map.get(email_id, "general")

            try:
                if label == "customer_requirement":
                    stats.customer_requirements += 1
                    # Fetch full body before processing
                    full = fetch_full_message(email_id)
                    email["body"] = full.get("body", email.get("snippet", ""))
                    reference = _process_customer_email(email, supabase)
                    if reference:
                        stats.jobs_created.append(reference)

                elif label == "quotation_rate_card":
                    stats.quotation_rate_cards += 1
                    full = fetch_full_message(email_id)
                    email["body"] = full.get("body", email.get("snippet", ""))
                    n = _process_rate_card(email, supabase)
                    stats.quotations_stored += n

                else:
                    stats.general += 1

            except Exception as e:
                logger.error("Failed to process email %s (%s): %s", email_id, label, e)
                stats.errors += 1

            processed_set.add(email_id)

    except Exception as e:
        logger.error("Scan failed: %s", e)
        stats.errors += 1

    stats.duration_seconds = round(time.time() - start, 2)
    logger.info(
        "Scan done in %.1fs — %d new, %d customer→%d jobs, %d rate cards→%d quotations, %d errors",
        stats.duration_seconds, stats.new_emails, stats.customer_requirements,
        len(stats.jobs_created), stats.quotation_rate_cards, stats.quotations_stored, stats.errors,
    )

    state.processed_ids = list(processed_set)[-10_000:]
    state.last_stats = asdict(stats)
    _save_state(state)
    return stats


# ---------------------------------------------------------------------------
# Status / control
# ---------------------------------------------------------------------------

def get_status() -> dict:
    state = _load_state()
    return {
        "enabled": state.enabled,
        "schedule": "Every 5 minutes",
        "last_run": state.last_stats,
        "processed_total": len(state.processed_ids),
    }


def set_enabled(enabled: bool) -> None:
    state = _load_state()
    state.enabled = enabled
    _save_state(state)
    logger.info("Automation %s", "enabled" if enabled else "disabled")
