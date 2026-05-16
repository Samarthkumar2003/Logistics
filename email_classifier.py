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

# RFQ reference pattern — our system generates these, so presence = agent reply
RFQ_PATTERN = re.compile(r"RFQ-\d{8}-[a-f0-9]{4}", re.IGNORECASE)


def _classify_by_rules(subject: str, _body: str, sender: str) -> Optional[ClassificationResult]:
    """Tier 1: Hard rules only — high-certainty structural signals, no keyword counting.
    Anything not caught here goes to KNN then GPT.
    """
    sender_lower = sender.strip().lower()
    email_match = re.search(r"<([^>]+)>", sender_lower)
    sender_email = email_match.group(1) if email_match else sender_lower
    agent_emails = _load_agent_emails()

    # Hard rule 1: RFQ reference in subject + known agent sender → reply to our RFQ
    if RFQ_PATTERN.search(subject) and sender_email in agent_emails:
        return ClassificationResult(
            label="quotation_rate_card",
            confidence=0.98,
            method="rule",
            details=f"RFQ reference in subject + known agent sender ({sender_email})",
        )

    # Hard rule 2: Known agent sender → quotation (agent database is ground truth)
    if sender_email in agent_emails:
        return ClassificationResult(
            label="quotation_rate_card",
            confidence=0.92,
            method="rule",
            details=f"Sender is known agent ({sender_email})",
        )

    # Hard rule 3: RFQ reference pattern in subject (unknown sender) → reply to our RFQ
    if RFQ_PATTERN.search(subject):
        return ClassificationResult(
            label="quotation_rate_card",
            confidence=0.85,
            method="rule",
            details="RFQ reference pattern in subject — agent reply",
        )

    return None  # Pass to KNN → GPT


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
                    "content": (
                        "Classify the following email into exactly one category: "
                        "customer_requirement (asking us for freight rates/booking), "
                        "quotation_rate_card (agent providing rates to us), "
                        "or general (everything else). "
                        "An email with cargo specs but no prices = customer_requirement."
                    ),
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
# TIER 3: SVM RBF classifier (replaces KNN)
# Trains in-memory at first use from Supabase vectors.
# Benchmarked at 98.2% LOOCV accuracy vs KNN 69.6% on same data.
# ---------------------------------------------------------------------------

# HyDE: only expand messages shorter than this (WhatsApp / terse emails)
HYDE_LENGTH_THRESHOLD = 200

# Confidence gate: SVM probability below this → skip to GPT
SVM_MIN_CONFIDENCE = 0.70

import json as _json
import numpy as _np
from sklearn.svm import SVC as _SVC
from sklearn.preprocessing import LabelEncoder as _LabelEncoder

# Module-level model cache — trained once, reused for every email
_svm_model: Optional[_SVC] = None
_svm_encoder: Optional[_LabelEncoder] = None
_svm_training_count: int = 0   # track when to retrain after new examples added


def _get_embedding(text: str) -> list[float]:
    """Generate an embedding using text-embedding-3-small."""
    response = client.embeddings.create(
        model="text-embedding-3-small",
        input=text[:8000],
    )
    return response.data[0].embedding


def _load_svm() -> tuple[Optional[_SVC], Optional[_LabelEncoder], int]:
    """Fetch training vectors from Supabase and train SVM RBF. Returns (model, encoder, count)."""
    try:
        rows = supabase.table("email_training_data").select("label, embedding").execute().data
        rows = [r for r in rows if r.get("embedding")]
        if len(rows) < 10:
            return None, None, 0

        X = _np.array([
            _json.loads(r["embedding"]) if isinstance(r["embedding"], str) else r["embedding"]
            for r in rows
        ])
        le = _LabelEncoder()
        y = le.fit_transform([r["label"] for r in rows])

        model = _SVC(kernel="rbf", C=10.0, gamma="scale", probability=True)
        model.fit(X, y)
        logger.info("SVM trained on %d examples, classes: %s", len(rows), list(le.classes_))
        return model, le, len(rows)
    except Exception as e:
        logger.warning("SVM training failed: %s", e)
        return None, None, 0


def _get_svm() -> tuple[Optional[_SVC], Optional[_LabelEncoder]]:
    """Return cached SVM, retraining if Supabase has new examples since last train."""
    global _svm_model, _svm_encoder, _svm_training_count

    try:
        current_count = supabase.table("email_training_data").select("id", count="exact").limit(1).execute().count or 0
    except Exception:
        current_count = _svm_training_count

    if _svm_model is None or current_count != _svm_training_count:
        _svm_model, _svm_encoder, _svm_training_count = _load_svm()

    return _svm_model, _svm_encoder


def _hyde_expand(subject: str, body: str) -> str:
    """HyDE: expand a short/terse message into a full hypothetical logistics email.
    Closes the vocabulary gap between terse WhatsApp messages and formal training examples.
    Only called for short messages to avoid unnecessary API cost.
    """
    short_text = f"{subject} {body}".strip()
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a logistics email expander. Given a short or terse logistics message, "
                        "rewrite it as a full professional email a freight forwarder might send or receive. "
                        "Preserve all facts (ports, container types, cargo, quantities). "
                        "Do not add prices or details not implied by the original. "
                        "Output only the expanded email text, no commentary."
                    ),
                },
                {"role": "user", "content": short_text},
            ],
            temperature=0.2,
            max_tokens=300,
        )
        expanded = response.choices[0].message.content.strip()
        logger.info("HyDE expanded %d chars → %d chars", len(short_text), len(expanded))
        return expanded
    except Exception as e:
        logger.warning("HyDE expansion failed, using raw text: %s", e)
        return short_text


def _classify_by_svm(subject: str, body: str) -> Optional[ClassificationResult]:
    """Tier 3: SVM RBF classifier with HyDE expansion and confidence gating.

    SVM trains in-memory from Supabase vectors at first call, then caches.
    Retrains automatically when new training examples are added (feedback loop).
    HyDE expands short messages before embedding to bridge vocabulary gap.
    Confidence gate: low-probability predictions fall through to GPT.
    """
    model, encoder = _get_svm()
    if model is None:
        return None

    raw_text = f"Subject: {subject}\n\n{body[:2000]}"
    hyde_used = len(raw_text.strip()) < HYDE_LENGTH_THRESHOLD

    embed_text = _hyde_expand(subject, body) if hyde_used else raw_text

    try:
        vec = _np.array([_get_embedding(embed_text)])
        proba = model.predict_proba(vec)[0]
        best_idx = int(_np.argmax(proba))
        confidence = float(proba[best_idx])
        label = encoder.classes_[best_idx]

        if confidence < SVM_MIN_CONFIDENCE:
            logger.info(
                "SVM gated out: confidence %.2f < %.2f for label '%s' — routing to GPT",
                confidence, SVM_MIN_CONFIDENCE, label,
            )
            return None

        proba_str = ", ".join(f"{encoder.classes_[i]}={proba[i]:.2f}" for i in range(len(proba)))
        return ClassificationResult(
            label=label,
            confidence=confidence,
            method="svm+hyde" if hyde_used else "svm",
            details=f"SVM RBF probabilities: [{proba_str}], hyde={hyde_used}",
        )
    except Exception as e:
        logger.warning("SVM classification failed: %s", e)
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
                        "You are an email classifier for a logistics/freight forwarding company.\n\n"
                        "Classify emails into exactly one of these 3 categories:\n\n"
                        "- customer_requirement: A customer ASKING us for a freight quote, booking, or shipping service. "
                        "They describe their shipment (origin, destination, cargo, weight, container type) and want us to provide rates. "
                        "Key signals: 'kindly provide your best quote', 'please send rates', 'POL/POD fields', 'request for quotation', cargo specs.\n\n"
                        "- quotation_rate_card: A freight agent or vendor PROVIDING rates/prices TO us in response to our RFQ. "
                        "They are the ones quoting prices. "
                        "Key signals: 'please find our rates', 'we offer USD X per container', 'our quote is', rates with currency figures, transit time + validity fields filled in.\n\n"
                        "- general: Everything else — newsletters, spam, tracking updates, invoices, internal memos.\n\n"
                        "CRITICAL DISTINCTION: If the email is ASKING for rates → customer_requirement. "
                        "If the email is PROVIDING rates → quotation_rate_card. "
                        "An email with POL/POD/cargo specs but NO prices is almost always customer_requirement.\n\n"
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

def classify_email(
    subject: str,
    body: str,
    sender: str = "",
    rules_only: bool = False,
) -> ClassificationResult:
    """Classify an email using the hybrid approach:
    Tier 1: Rules → Tier 2: Fine-tuned model → Tier 3: KNN → Tier 4: Few-shot

    Pass rules_only=True to skip all network/API calls (instant, free).
    Emails that can't be classified by rules return label='general'.
    """
    # Tier 1: Rules (instant, free, no network)
    result = _classify_by_rules(subject, body, sender)
    if result:
        logger.info("Classified by rules: %s (%.0f%%)", result.label, result.confidence * 100)
        return result

    if rules_only:
        return ClassificationResult(
            label="general",
            confidence=0.5,
            method="rule",
            details="Rules inconclusive; rules_only=True so defaulting to general",
        )

    # Tier 2: Fine-tuned model (fast, cheap)
    result = _classify_by_fine_tuned(subject, body, sender)
    if result:
        logger.info("Classified by fine-tuned model: %s (%.0f%%)", result.label, result.confidence * 100)
        return result

    # Tier 3: KNN via pgvector (needs training data)
    result = _classify_by_svm(subject, body)
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

def classify_emails_batch(emails: list[dict], rules_only: bool = False) -> list[dict]:
    """Classify a list of emails. Each dict should have: subject, body, sender (optional).

    Pass rules_only=True to use only regex rules — instant, no API calls, no network.
    """
    results = []
    for email in emails:
        result = classify_email(
            subject=email.get("subject", ""),
            body=email.get("body", ""),
            sender=email.get("sender", email.get("from", "")),
            rules_only=rules_only,
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
