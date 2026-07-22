"""
email_classifier.py
-------------------
Email classifier: a single LLM call through a swappable provider (see
``llm_provider.py``). The model is chosen at runtime via the ``LLM_PROVIDER``
env var — swapping OpenAI for Gemini (or any future provider) needs no code
change here.

Labels:
  - customer_requirement : Customer asking for a shipment / freight quote
  - quotation_rate_card  : Agent replying with rates / pricing
  - general              : Everything else (newsletters, spam, internal, etc.)
"""

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from supabase import create_client

from backend.classifier.llm_provider import get_provider

load_dotenv()
logger = logging.getLogger(__name__)

supabase = create_client(
    os.environ.get("SUPABASE_URL", ""),
    os.environ.get("SUPABASE_KEY", ""),
)


@dataclass
class ClassificationResult:
    label: str        # customer_requirement | quotation_rate_card | general
    confidence: float
    method: str
    details: str = ""


CLASSIFY_SYSTEM_PROMPT = """\
You are an email classifier for Bhatia Shipping, a Mumbai-based international freight forwarder.

CRITICAL RULE: Classify based on the LATEST message ONLY. Ignore everything below "From:", "-----Original Message-----", "> ", or "On [date] wrote:" — that is quoted history. A thread subject may say "Quote Request" but if the latest reply is asking for shipment status, it is general.

════════════════════════════════════════════════════════════
LABEL: customer_requirement
════════════════════════════════════════════════════════════
The sender is asking Bhatia Shipping to provide a freight rate, cost estimate, or quotation for a specific shipment. The sender wants pricing FROM us.

✓ INCLUDE when the latest message body contains ANY of:
  • "Kindly provide / share / quote your best rate / quotation / freight"
  • "Please quote", "Kindly quote", "awaiting rates", "awaiting quotation"
  • "Request for quotation / quote / freight / costing / budgetary quote"
  • "Looking for freight / rates", "need a rate", "require freight"
  • "Send us below rate" + port pair + cargo details
  • "Kindly advise [DO / delivery / port / destination] charges" when the primary question is about cost
  • A cargo description (weight/CBM/container type/commodity) + route (POL/POD) + a cost question
  • Any language in any language asking for freight pricing (e.g. Chinese 询价 = rate inquiry)

✓ Sender can be a direct shipper OR an intermediary agent / NVOCC forwarding the enquiry to us (e.g. CSS Group, ALS Line, Intertrans, RWC Shipping, Shepherd Shipping, Kryfs, Hemisphere Freight, Paragon Dubai, CMG, Best Co Logistics). Agent-as-customer is still customer_requirement.

✓ Applies to NEW threads AND continuations of ongoing quote negotiations where the customer is still pushing on price, revising cargo details, or requesting a revised rate.

✗ DO NOT label customer_requirement when the latest message is:
  • Sharing / attaching documents (BL draft, invoice, packing list, certificates, MM copy, P INV)
  • Asking for shipment status, vessel ETA/ETD, SOB update, or cargo departure confirmation
  • Confirming receipt ("well received", "noted", "many thanks", "keep us posted")
  • Asking "check and revert", sharing booking confirmation, or reporting a problem on an active shipment
  • Asking for cost of stuffing / container clearance for an EXISTING booking (booking number present → general)
  • Rate comparison within an existing dispute or unhappy shipper thread
  • Asking for feedback on rates already provided (not a new rate request)

════════════════════════════════════════════════════════════
LABEL: quotation_rate_card
════════════════════════════════════════════════════════════
A shipping line, carrier, or freight agent is PROACTIVELY pushing supply-side pricing to us — rates we can use when quoting our customers.

✓ INCLUDE when:
  • Subject contains: "Rate Sheet [Month Year]", "Special Rates Ex [port]", "Tariff Revision", "Local Charges Effective / Revision", "General Rate Increase", "GRI", "Rate Card", "Rate Update", "Rate Circular", "Peak Season Surcharge", "PSS Announcement", "Revised Freight Rates Effective"
  • Body contains a rate table (port pairs + USD amounts per 20'/40'/TEU/FEU) with validity dates or "effective from [date]"
  • Carrier is announcing tariff changes or new surcharges (GRI, PSS, war risk, bunker surcharge revision)
  • Carrier replies to Bhatia's booking/rate request with confirmed ocean freight rates in the body (e.g. MSC replying "RE: REQUEST FOR BOOKING // RATE" with USD rate table or confirmed freight)
  • Container leasing or used-container sale offers with pricing (e.g. "Sale Price: USD 1750/40'HC valid till [date]")
  • Cover-note emails where subject contains "Rate Sheet", "GRI", "PSS", "Special Rates", "Tariff Revision" etc. and body says "please find attached" — rates are in the attachment, not the plain-text body; still quotation_rate_card

✗ DO NOT label quotation_rate_card when:
  • Carrier confirms a booking WITHOUT providing rates (vessel/slot confirmation, booking copy)
  • Carrier sends vessel schedule, booking status report, routing guide, or planning confirmation WITHOUT rate data
  • Bunker surcharge is mentioned only incidentally within a booking confirmation (the primary purpose is operational)

════════════════════════════════════════════════════════════
LABEL: general
════════════════════════════════════════════════════════════
Everything else. DEFAULT label when neither above applies.

Includes:
  • Operational: BL drafts, SOB notices, pre-alerts, arrival notices, delivery orders, booking confirmations, container load plans, Form 13, customs clearance, vessel schedules
  • Financial: invoices, debit notes, SOA, payment confirmations, stuffing costs for existing bookings
  • Documents: scan copies, packing lists, certificates, export declarations, MM copies
  • Status requests / acknowledgements: "please update status", "keep us posted", "noted / well received", "check and revert"
  • All senders from @bhatiashipping.com or @bhatiashippinggroup.com (always general)
  • Company introduction / profile / marketing emails offering logistics services TO us
  • Thread replies that have pivoted to document exchange or status follow-up, even if the original subject was a quote request

════════════════════════════════════════════════════════════
DECISION ORDER (stop at first match)
════════════════════════════════════════════════════════════
1. Sender is @bhatiashipping.com / @bhatiashippinggroup.com → general
2. Latest body explicitly asks us for freight rates / costs / quotation → customer_requirement
3. Sender is proactively pushing supply-side pricing or tariff announcement → quotation_rate_card
4. Otherwise → general

Respond ONLY with valid JSON, no markdown:
{"label": "customer_requirement" | "quotation_rate_card" | "general", "confidence": <0.0-1.0>}
"""

_VALID_LABELS = {"customer_requirement", "quotation_rate_card", "general"}
MAX_BODY_CHARS = 8000


def _parse_llm_label(raw: str) -> tuple[str, float]:
    """Parse provider reply into (label, confidence). Tolerates JSON in code fences."""
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            label = str(data.get("label", "")).strip().lower()
            if label in _VALID_LABELS:
                return label, max(0.0, min(1.0, float(data.get("confidence", 0.8))))
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    lowered = text.lower()
    if "customer_requirement" in lowered or "customer" in lowered:
        return "customer_requirement", 0.7
    if "quotation_rate_card" in lowered or "quotation" in lowered or "rate" in lowered:
        return "quotation_rate_card", 0.7
    return "general", 0.5


_INTERNAL_DOMAINS = {"bhatiashipping.com"}

# Matches job/reference numbers common in freight forwarding subject lines
_JOB_REF_RE = re.compile(r"\b[A-Z]{2,6}\d{5,}\b")

# Rate-card subject signals — strong enough to bypass LLM when body is a cover note
_RC_SUBJ_RE = re.compile(
    r"\b(rate\s*sheet|special\s*rates?\s*(ex|from)|tariff\s*(revision|update|circular|notice|change)|"
    r"local\s*charges?\s*(revision|update|effective|change)|rate\s*card|buy\s*rates?|"
    r"rate\s*update|rate\s*circular|new\s*rates?\s*(for|eff)|rates?\s*effective|"
    r"rate\s*announcement|general\s*rate\s*increase|gri\b|peak\s*season\s*surcharge|"
    r"pss\s*(announcement|notice)|revised\s*freight\s*rates?\s*(effective|from))\b",
    re.I,
)

# Cover-note body: sender says "find attached" with no rate data in plain text
_COVER_NOTE_RE = re.compile(
    r"\b(kindly\s+find\s+attach|please\s+find\s+attach|find\s+attach|pfa\b|"
    r"attached\s+herewith|herewith\s+(?:pl\s+)?find|as\s+attached|"
    r"kindly\s+find\s+the\s+(?:revised\s+)?rate|please\s+find\s+the\s+(?:revised\s+)?rate)\b",
    re.I,
)

def _strip_quoted(body: str) -> str:
    """Return only the latest message — strip quoted reply history."""
    # Split on common reply delimiters and take everything before the first one
    for delimiter in ("-----Original Message-----", "________________________________",
                      "\nFrom:", "\n> ", "\nOn "):
        idx = body.find(delimiter)
        if idx > 100:  # ignore if delimiter is right at the top (no new content above it)
            body = body[:idx]
    return body.strip()


def _extract_email_domain(sender: str) -> str:
    """Extract domain from 'Name <addr@domain.com>' or 'addr@domain.com'."""
    m = re.search(r"<([^>]+)>", sender)
    addr = m.group(1) if m else sender
    return addr.split("@")[-1].strip().lower() if "@" in addr else ""


def classify_email(subject: str, body: str, sender: str = "") -> ClassificationResult:
    """Classify one email. Internal-domain senders are marked general without an LLM call."""
    if _extract_email_domain(sender) in _INTERNAL_DOMAINS:
        logger.info("Rule: internal sender (%s) → general", sender)
        return ClassificationResult(
            label="general",
            confidence=1.0,
            method="rule:internal_domain",
            details=f"Sender domain is internal ({sender})",
        )

    subj_lower = subject.lower().strip()
    # Only skip to general on job-ref subjects that also lack any rate/quote signal.
    # Re:/Fw: alone is NOT enough — customer enquiries frequently arrive as reply threads.
    _RATE_SIGNAL_RE = re.compile(
        r'\b(quote|quotation|rfq|rate|inquiry|enquiry|freight|fcl|lcl|air freight|sea freight)\b', re.I
    )
    if _JOB_REF_RE.search(subject) and not _RATE_SIGNAL_RE.search(subject):
        logger.info("Rule: job-ref subject with no rate signal (%r) → general", subject)
        return ClassificationResult(
            label="general",
            confidence=0.95,
            method="rule:job_ref_no_rate_signal",
            details=f"Job reference subject with no quote/rate keyword: {subject}",
        )

    latest = _strip_quoted(body)

    # Rate sheet sent as attachment: subject has RC signal, body is just a cover note.
    # No rate table in plain text — LLM would call it general. Catch it here.
    if _RC_SUBJ_RE.search(subject) and _COVER_NOTE_RE.search(latest[:400]):
        logger.info("Rule: RC subject + cover-note body (%r) → quotation_rate_card", subject)
        return ClassificationResult(
            label="quotation_rate_card",
            confidence=0.93,
            method="rule:rc_subject_cover_note",
            details=f"Rate-card subject with attachment cover note: {subject}",
        )

    logger.debug("Classify input — subject=%r body_chars=%d stripped_chars=%d",
                 subject, len(body), len(latest))
    user_prompt = f"From: {sender}\nSubject: {subject}\n\n{latest[:MAX_BODY_CHARS]}"
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            provider = get_provider()
            raw = provider.complete(CLASSIFY_SYSTEM_PROMPT, user_prompt, temperature=0.0, max_tokens=60)
            label, confidence = _parse_llm_label(raw)
            logger.info("Classified by %s: %s (%.0f%%)", provider.name, label, confidence * 100)
            return ClassificationResult(
                label=label,
                confidence=confidence,
                method=f"llm:{provider.name}",
                details=f"{provider.name} output: {raw[:200]}",
            )
        except Exception as e:
            last_exc = e
            err_str = str(e)
            # insufficient_quota is also a 429, but retrying can never succeed —
            # the account is out of credit. Fail fast instead of burning ~40s of
            # backoff per email on a permanently failing call.
            if "insufficient_quota" in err_str:
                logger.error("LLM quota exhausted — classification unavailable: %s", e)
                break
            if "429" in err_str or "rate_limit" in err_str.lower():
                wait = 2 ** attempt * 5 + random.uniform(0, 2)  # 5-7s, 10-12s, 20-22s
                logger.warning("Rate limit on attempt %d, retrying in %ds: %s", attempt + 1, wait, e)
                time.sleep(wait)
            else:
                break  # non-retryable error
    logger.error("LLM classification failed: %s", last_exc)
    return ClassificationResult(label="general", confidence=0.0, method="error", details=str(last_exc))


def classify_emails_batch(emails: list[dict]) -> list[dict]:
    """Classify multiple emails concurrently. Each dict needs: id, subject, body, sender."""
    def _one(email: dict) -> dict:
        result = classify_email(
            subject=email.get("subject", ""),
            body=email.get("body", ""),
            sender=email.get("sender", email.get("from", "")),
        )
        return {
            "id": email.get("id", ""),
            "subject": email.get("subject", ""),
            "label": result.label,
            "confidence": result.confidence,
            "method": result.method,
            "details": result.details,
        }

    results: list[dict] = [{}] * len(emails)
    with ThreadPoolExecutor(max_workers=min(len(emails), 5)) as pool:
        future_to_idx = {pool.submit(_one, e): i for i, e in enumerate(emails)}
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                e = emails[idx]
                logger.warning("classify_emails_batch failed for %s: %s", e.get("id"), exc)
                results[idx] = {
                    "id": e.get("id", ""), "subject": e.get("subject", ""),
                    "label": "general", "confidence": 0.0, "method": "error", "details": str(exc),
                }
    return results


def submit_feedback(
    email_subject: str,
    email_body: str,
    email_sender: str,
    predicted_label: str,
    corrected_label: str,
    confidence: float = 0.0,
) -> dict:
    """Store a human correction in the feedback table."""
    try:
        supabase.table("classification_feedback").insert({
            "email_subject": email_subject,
            "email_body": email_body[:5000],
            "email_sender": email_sender,
            "predicted_label": predicted_label,
            "corrected_label": corrected_label,
            "confidence": confidence,
            "added_to_training": False,
        }).execute()
    except Exception as e:
        logger.error("Failed to store feedback: %s", e)
        return {"status": "error", "detail": str(e)}

    return {"status": "ok", "detail": f"Feedback stored as '{corrected_label}'"}
