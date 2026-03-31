"""
email_classifier.py
-------------------
Hybrid email classifier: Rule-based → Fine-tuned GPT-4o-mini → KNN fallback.

Labels:
  - customer_requirement : Customer asking for a shipment / freight quote
  - quotation_rate_card  : Agent replying with rates / pricing
  - general              : Everything else (newsletters, spam, internal, etc.)
"""

import csv
import json
import os
import re
import logging
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv
from openai import OpenAI
from supabase import create_client

load_dotenv()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
client = OpenAI()
supabase = create_client(
    os.environ.get("SUPABASE_URL", ""),
    os.environ.get("SUPABASE_KEY", ""),
)

AGENTS_CSV = os.path.join(os.path.dirname(__file__), "agents_database.csv")

# Fine-tuned model ID — set after training via .env or directly here
FINE_TUNED_MODEL = os.environ.get("CLASSIFIER_MODEL_ID", "")


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass
class ClassificationResult:
    label: str                    # customer_requirement | quotation_rate_card | general
    confidence: float             # 0.0 - 1.0
    method: str                   # rule | fine_tuned | knn | few_shot
    details: str = ""             # human-readable explanation


# ---------------------------------------------------------------------------
# TIER 1: Rule-based classification
# ---------------------------------------------------------------------------

# Known agent emails loaded from CSV
_AGENT_EMAILS: set[str] = set()

def _load_agent_emails() -> set[str]:
    global _AGENT_EMAILS
    if _AGENT_EMAILS:
        return _AGENT_EMAILS
    try:
        with open(AGENTS_CSV, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                email = row.get("email", "").strip().lower()
                if email:
                    _AGENT_EMAILS.add(email)
    except Exception as e:
        logger.warning("Could not load agent emails from CSV: %s", e)
    return _AGENT_EMAILS

# RFQ reference pattern
RFQ_PATTERN = re.compile(r"RFQ-\d{8}-[a-f0-9]{4}", re.IGNORECASE)

# Keyword sets for rule matching
QUOTATION_KEYWORDS = [
    r"\brate\b.*\b(usd|eur|inr|gbp|\$|€|₹)\b",
    r"\b(usd|eur|inr|gbp|\$|€|₹)\s*[\d,]+",
    r"\bper\s+(cbm|kg|ton|container|teu|feu)\b",
    r"\btransit\s+time\b.*\bdays?\b",
    r"\bvalidity\b.*\b(days?|week|month)\b",
    r"\bfreight\s+charges?\b",
    r"\ball[\s-]?in\s+rate\b",
    r"\bocean\s+freight\b.*\b\d+\b",
    r"\bair\s+freight\b.*\b\d+\b",
    r"\bquot(e|ation)\b.*\b(attached|below|follows)\b",
    r"\brate\s+card\b",
    r"\bcharges?\s+(are|as follows|below)\b",
]

CUSTOMER_REQUIREMENT_KEYWORDS = [
    r"\b(need|require|looking\s+for)\b.*\b(ship|transport|freight|move|send)\b",
    r"\bshipment\s+(from|required|needed)\b",
    r"\b(please|kindly)\s+(arrange|book|quote|provide)\b",
    r"\bcargo\s+(ready|available|needs?\s+to)\b",
    r"\b(origin|pickup)\b.*\b(destination|delivery)\b",
    r"\b\d+\s*(kg|tons?|cbm|containers?|pallets?)\b.*\b(from|to)\b",
    r"\b(urgent|asap)\b.*\b(ship|deliver|transport)\b",
    r"\bRFQ\b(?!.*RFQ-\d{8})",  # RFQ mention without a reference number (new request)
    r"\brequest\s+for\s+(quotation|proposal|rate)\b",
]

COMPILED_QUOTATION = [re.compile(p, re.IGNORECASE) for p in QUOTATION_KEYWORDS]
COMPILED_CUSTOMER = [re.compile(p, re.IGNORECASE) for p in CUSTOMER_REQUIREMENT_KEYWORDS]


def _classify_by_rules(subject: str, body: str, sender: str) -> Optional[ClassificationResult]:
    """Tier 1: Fast rule-based classification."""
    text = f"{subject} {body}"
    sender_lower = sender.strip().lower()

    # Extract email address from "Name <email>" format
    email_match = re.search(r"<([^>]+)>", sender_lower)
    sender_email = email_match.group(1) if email_match else sender_lower

    # Rule 1: RFQ reference in subject + sender is a known agent → quotation reply
    agent_emails = _load_agent_emails()
    if RFQ_PATTERN.search(subject) and sender_email in agent_emails:
        return ClassificationResult(
            label="quotation_rate_card",
            confidence=0.98,
            method="rule",
            details=f"RFQ reference in subject + sender is known agent ({sender_email})",
        )

    # Rule 2: Sender is a known agent + pricing keywords → quotation
    if sender_email in agent_emails:
        quote_hits = sum(1 for p in COMPILED_QUOTATION if p.search(text))
        if quote_hits >= 2:
            return ClassificationResult(
                label="quotation_rate_card",
                confidence=0.92,
                method="rule",
                details=f"Known agent sender + {quote_hits} pricing keywords matched",
            )

    # Rule 3: Strong quotation keywords (even from unknown sender)
    quote_hits = sum(1 for p in COMPILED_QUOTATION if p.search(text))
    if quote_hits >= 4:
        return ClassificationResult(
            label="quotation_rate_card",
            confidence=0.85,
            method="rule",
            details=f"{quote_hits} pricing keywords matched (strong signal)",
        )

    # Rule 4: Strong customer requirement keywords
    cust_hits = sum(1 for p in COMPILED_CUSTOMER if p.search(text))
    if cust_hits >= 3:
        return ClassificationResult(
            label="customer_requirement",
            confidence=0.88,
            method="rule",
            details=f"{cust_hits} customer requirement keywords matched",
        )

    # Rule 5: RFQ reference in subject (reply to our RFQ) but unknown sender
    if RFQ_PATTERN.search(subject):
        return ClassificationResult(
            label="quotation_rate_card",
            confidence=0.80,
            method="rule",
            details="RFQ reference in subject (likely agent reply)",
        )

    return None  # Rules could not classify — pass to Tier 2


# ---------------------------------------------------------------------------
# TIER 2: Fine-tuned GPT-4o-mini
# ---------------------------------------------------------------------------

def _classify_by_fine_tuned(subject: str, body: str, sender: str) -> Optional[ClassificationResult]:
    """Tier 2: Fine-tuned GPT-4o-mini classification."""
    if not FINE_TUNED_MODEL:
        return None  # No fine-tuned model available yet

    # Truncate body to avoid token limits
    truncated_body = body[:2000] if len(body) > 2000 else body
    email_text = f"From: {sender}\nSubject: {subject}\n\n{truncated_body}"

    try:
        response = client.chat.completions.create(
            model=FINE_TUNED_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "Classify the following email into exactly one category: customer_requirement, quotation_rate_card, or general",
                },
                {"role": "user", "content": email_text},
            ],
            temperature=0,
            max_tokens=20,
        )
        raw_label = response.choices[0].message.content.strip().lower()

        # Normalize the label
        if "customer" in raw_label or "requirement" in raw_label:
            label = "customer_requirement"
        elif "quotation" in raw_label or "rate" in raw_label:
            label = "quotation_rate_card"
        else:
            label = "general"

        return ClassificationResult(
            label=label,
            confidence=0.90,
            method="fine_tuned",
            details=f"Fine-tuned model ({FINE_TUNED_MODEL}) raw output: {raw_label}",
        )
    except Exception as e:
        logger.warning("Fine-tuned classification failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# TIER 3: KNN fallback via pgvector
# ---------------------------------------------------------------------------

def _get_embedding(text: str) -> list[float]:
    """Generate an embedding using text-embedding-3-small."""
    response = client.embeddings.create(
        model="text-embedding-3-small",
        input=text[:8000],  # Stay within token limits
    )
    return response.data[0].embedding


def _classify_by_knn(subject: str, body: str) -> Optional[ClassificationResult]:
    """Tier 3: KNN classification using pgvector embeddings."""
    try:
        # Check if we have any training data
        count_result = supabase.table("email_training_data").select("id", count="exact").limit(1).execute()
        if not count_result.count or count_result.count < 10:
            return None  # Not enough training data for KNN

        email_text = f"Subject: {subject}\n\n{body[:2000]}"
        embedding = _get_embedding(email_text)

        result = supabase.rpc("classify_email", {
            "query_embedding": embedding,
            "k": 7,
        }).execute()

        if result.data and len(result.data) > 0:
            row = result.data[0]
            return ClassificationResult(
                label=row["predicted_label"],
                confidence=row["confidence"],
                method="knn",
                details=f"KNN vote: {row['vote_count']}/7, avg similarity: {row['avg_similarity']:.3f}",
            )
    except Exception as e:
        logger.warning("KNN classification failed: %s", e)

    return None


# ---------------------------------------------------------------------------
# FEW-SHOT fallback (last resort — when no fine-tuned model and no KNN data)
# ---------------------------------------------------------------------------

def _classify_by_few_shot(subject: str, body: str, sender: str) -> ClassificationResult:
    """Last resort: GPT-4o-mini few-shot classification."""
    truncated_body = body[:2000] if len(body) > 2000 else body
    email_text = f"From: {sender}\nSubject: {subject}\n\n{truncated_body}"

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an email classifier for a logistics/freight forwarding company. "
                        "Classify emails into exactly one of these categories:\n\n"
                        "- customer_requirement: A customer requesting a shipment, freight booking, or transport service\n"
                        "- quotation_rate_card: A freight agent/vendor replying with rates, pricing, or a quotation\n"
                        "- general: Everything else (newsletters, internal memos, spam, tracking updates, etc.)\n\n"
                        "Reply with ONLY the category name, nothing else."
                    ),
                },
                {"role": "user", "content": email_text},
            ],
            temperature=0,
            max_tokens=20,
        )
        raw_label = response.choices[0].message.content.strip().lower()

        if "customer" in raw_label or "requirement" in raw_label:
            label = "customer_requirement"
        elif "quotation" in raw_label or "rate" in raw_label:
            label = "quotation_rate_card"
        else:
            label = "general"

        return ClassificationResult(
            label=label,
            confidence=0.75,
            method="few_shot",
            details=f"GPT-4o-mini few-shot output: {raw_label}",
        )
    except Exception as e:
        logger.error("Few-shot classification failed: %s", e)
        return ClassificationResult(
            label="general",
            confidence=0.0,
            method="few_shot",
            details=f"Classification failed: {e}",
        )


# ---------------------------------------------------------------------------
# MAIN ENTRY POINT: Hybrid classifier
# ---------------------------------------------------------------------------

def classify_email(subject: str, body: str, sender: str = "") -> ClassificationResult:
    """Classify an email using the hybrid approach:
    Tier 1: Rules → Tier 2: Fine-tuned model → Tier 3: KNN → Tier 4: Few-shot
    """
    # Tier 1: Rules (instant, free)
    result = _classify_by_rules(subject, body, sender)
    if result:
        logger.info("Classified by rules: %s (%.0f%%)", result.label, result.confidence * 100)
        return result

    # Tier 2: Fine-tuned model (fast, cheap)
    result = _classify_by_fine_tuned(subject, body, sender)
    if result:
        logger.info("Classified by fine-tuned model: %s (%.0f%%)", result.label, result.confidence * 100)
        return result

    # Tier 3: KNN via pgvector (needs training data)
    result = _classify_by_knn(subject, body)
    if result:
        logger.info("Classified by KNN: %s (%.0f%%)", result.label, result.confidence * 100)
        return result

    # Tier 4: Few-shot fallback (always works)
    result = _classify_by_few_shot(subject, body, sender)
    logger.info("Classified by few-shot: %s (%.0f%%)", result.label, result.confidence * 100)
    return result


# ---------------------------------------------------------------------------
# FEEDBACK LOOP: Store corrections and add to training data
# ---------------------------------------------------------------------------

def submit_feedback(
    email_subject: str,
    email_body: str,
    email_sender: str,
    predicted_label: str,
    corrected_label: str,
    confidence: float = 0.0,
) -> dict:
    """Store a human correction and add the corrected email to training data."""
    # 1. Store the feedback record
    try:
        supabase.table("classification_feedback").insert({
            "email_subject": email_subject,
            "email_body": email_body[:5000],
            "email_sender": email_sender,
            "predicted_label": predicted_label,
            "corrected_label": corrected_label,
            "confidence": confidence,
            "added_to_training": True,
        }).execute()
    except Exception as e:
        logger.error("Failed to store feedback: %s", e)
        return {"status": "error", "detail": str(e)}

    # 2. Generate embedding and add to training data
    try:
        email_text = f"Subject: {email_subject}\n\n{email_body[:2000]}"
        embedding = _get_embedding(email_text)

        supabase.table("email_training_data").insert({
            "content": email_text,
            "subject": email_subject,
            "sender": email_sender,
            "label": corrected_label,
            "embedding": embedding,
        }).execute()
    except Exception as e:
        logger.error("Failed to add corrected email to training data: %s", e)
        return {"status": "partial", "detail": f"Feedback stored but training data failed: {e}"}

    return {"status": "ok", "detail": f"Feedback stored and added to training as '{corrected_label}'"}


# ---------------------------------------------------------------------------
# BATCH: Classify multiple emails
# ---------------------------------------------------------------------------

def classify_emails_batch(emails: list[dict]) -> list[dict]:
    """Classify a list of emails. Each dict should have: subject, body, sender (optional)."""
    results = []
    for email in emails:
        result = classify_email(
            subject=email.get("subject", ""),
            body=email.get("body", ""),
            sender=email.get("sender", email.get("from", "")),
        )
        results.append({
            "id": email.get("id", ""),
            "subject": email.get("subject", ""),
            "label": result.label,
            "confidence": result.confidence,
            "method": result.method,
            "details": result.details,
        })
    return results
