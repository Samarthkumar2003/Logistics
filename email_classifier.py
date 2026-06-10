"""
email_classifier.py
-------------------
Hybrid email classifier: Rule-based → Fine-tuned GPT-4o-mini → Qwen+MLP fallback.

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
    method: str                   # rule | fine_tuned | mlp | few_shot
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

# Internal job reference: EN001103, EN000845 etc. — Bhatia Shipping's own job numbers
# Presence in subject means the email is about an existing job, never a new request
JOB_REF_PATTERN = re.compile(r"\bEN\d{5,}\b")

# Signals that the sender is ASKING for rates (even if they're a known agent acting as customer)
ASKING_FOR_RATES = re.compile(
    r"\b(rfp\b|r\.f\.p|enquir|enquier|kindly share|please share|request for (quote|quotation|rate|proposal)|"
    r"seeking (rate|quote)|require (rate|quote)|need (rate|quote)|logistics requirement|"
    r"freight requirement|shipping requirement)\b",
    re.IGNORECASE,
)


def _classify_by_rules(subject: str, body: str, sender: str) -> Optional[ClassificationResult]:
    _body = body
    """Tier 1: Hard rules only — high-certainty structural signals, no keyword counting.
    Anything not caught here goes to fine-tuned model → Qwen+MLP → GPT.
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

    # Hard rule 2: Known agent sender → quotation, UNLESS they're asking for rates themselves
    # (agents sometimes act as customers, forwarding their client's RFP to us)
    asking = ASKING_FOR_RATES.search(subject) or ASKING_FOR_RATES.search(_body[:500])
    if sender_email in agent_emails and not asking:
        return ClassificationResult(
            label="quotation_rate_card",
            confidence=0.92,
            method="rule",
            details=f"Sender is known agent ({sender_email})",
        )

    # Hard rule 3: RFQ reference pattern in subject (unknown sender) → reply to our RFQ
    if RFQ_PATTERN.search(subject) and not asking:
        return ClassificationResult(
            label="quotation_rate_card",
            confidence=0.85,
            method="rule",
            details="RFQ reference pattern in subject — agent reply",
        )

    # Hard rule 4: Internal Bhatia Shipping domain → always operational/general
    if sender_email.endswith("@bhatiashipping.com") or sender_email.endswith("@bhatiashippinggroup.com"):
        return ClassificationResult(
            label="general",
            confidence=0.97,
            method="rule",
            details=f"Internal sender ({sender_email}) — operational email",
        )

    # Hard rule 5: Bhatia Shipping's own job reference in subject → existing job, not a new request
    if JOB_REF_PATTERN.search(subject):
        return ClassificationResult(
            label="general",
            confidence=0.96,
            method="rule",
            details="Internal job reference (EN0XXXXX) in subject — operational thread",
        )

    return None  # Pass to Qwen+MLP → GPT


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
# TIER 3: Qwen3-Embedding-0.6B (instruction-aware) → sklearn MLP classifier
# Trains in-memory at first use from Supabase Qwen vectors (embedding_qwen col).
# Query side embeds with an instruction prefix encoding our empirical basis;
# training/document vectors are embedded plain (Qwen asymmetric convention).
# ---------------------------------------------------------------------------

# HyDE: only expand messages shorter than this (WhatsApp / terse emails)
HYDE_LENGTH_THRESHOLD = 200

# Confidence gate: MLP probability below this → skip to GPT
MLP_MIN_CONFIDENCE = 0.70

# Qwen model + the column holding its 1024-dim vectors
QWEN_MODEL_NAME = os.environ.get("QWEN_EMBED_MODEL", "Qwen/Qwen3-Embedding-0.6B")
QWEN_EMBED_COLUMN = "embedding_qwen"
# Apple MPS mis-sizes buffers for this model (attempts ~29GB alloc) → default CPU.
# Override with QWEN_DEVICE=mps|cuda once your hardware handles it.
QWEN_DEVICE = os.environ.get("QWEN_DEVICE", "cpu")
QWEN_BATCH_SIZE = int(os.environ.get("QWEN_BATCH_SIZE", "16"))
# fp16 halves memory + ~2x throughput on CUDA. CPU has no fp16 kernels (slower/unsupported),
# so default on only for cuda. Force with QWEN_FP16=1/0. Vectors stay comparable across
# devices/dtypes (tiny rounding only), so CPU-stored and GPU-stored embeddings interoperate.
_fp16_env = os.environ.get("QWEN_FP16")
QWEN_FP16 = (_fp16_env == "1") if _fp16_env is not None else (QWEN_DEVICE == "cuda")

# Instruction query — our empirical basis for classification. Prepended to the
# QUERY text only (Qwen "Instruct: {task}\nQuery: {text}" convention).
EMPIRICAL_BASIS = (
    "Classify a freight-forwarding email into one of three categories based on who is "
    "asking and who is pricing. "
    "customer_requirement: the sender is REQUESTING a freight quote, rate, or booking from us. "
    "They describe a shipment (origin/POL, destination/POD, cargo, weight, container type, "
    "incoterm, ready date) and want US to provide rates. An email with cargo specs but NO prices "
    "is customer_requirement. Signals: 'kindly share your best rate', 'request for quotation', "
    "'please quote', 'we require', filled POL/POD/cargo fields with empty price fields. "
    "quotation_rate_card: the sender is an agent or carrier PROVIDING rates/prices TO us, usually "
    "replying to our RFQ. They state the numbers. Signals: 'please find our rates', "
    "'USD X per container', 'all-in', transit time and validity filled in, RFQ reference in subject. "
    "general: everything else operational or non-deal — newsletters, spam, tracking updates, "
    "invoices, internal memos, existing-job threads. "
    "Decisive rule: ASKING for a price = customer_requirement; PROVIDING a price = quotation_rate_card."
)

import json as _json
import threading as _threading
import numpy as _np
from sklearn.neural_network import MLPClassifier as _MLPClassifier
from sklearn.preprocessing import LabelEncoder as _LabelEncoder

# Module-level caches — loaded/trained once, reused for every email
_qwen_encoder = None  # SentenceTransformer, lazily loaded (heavy)
_qwen_lock = _threading.Lock()       # guards one-time model load
_qwen_infer_lock = _threading.Lock()  # serializes encode() — torch model not thread-safe
_mlp_model: Optional[_MLPClassifier] = None
_mlp_label_encoder: Optional[_LabelEncoder] = None
_mlp_training_count: int = 0
_mlp_lock = _threading.Lock()  # prevent concurrent retraining from parallel classifier threads


def _get_qwen():
    """Lazily load the Qwen3 SentenceTransformer once. Heavy (~1.2GB), so cached."""
    global _qwen_encoder
    if _qwen_encoder is not None:
        return _qwen_encoder
    with _qwen_lock:
        if _qwen_encoder is not None:
            return _qwen_encoder
        from sentence_transformers import SentenceTransformer
        model_kwargs = {"torch_dtype": "float16"} if QWEN_FP16 else {}
        logger.info(
            "Loading Qwen embedding model: %s (device=%s, fp16=%s) ...",
            QWEN_MODEL_NAME, QWEN_DEVICE, QWEN_FP16,
        )
        _qwen_encoder = SentenceTransformer(
            QWEN_MODEL_NAME, device=QWEN_DEVICE, model_kwargs=model_kwargs,
        )
        logger.info("Qwen model loaded.")
    return _qwen_encoder


def _is_oom_error(exc: Exception) -> bool:
    """True for CUDA/MPS out-of-memory errors (across torch versions)."""
    msg = str(exc).lower()
    return "out of memory" in msg or "cuda oom" in msg or exc.__class__.__name__ == "OutOfMemoryError"


def _encode(texts: list[str], prompt: Optional[str] = None) -> "_np.ndarray":
    """Run model.encode with an auto-OOM fallback: on a CUDA/MPS OOM, free the
    cache, halve the batch size, and retry — down to batch=1 — before giving up.
    Lets large GPUs run big batches while small GPUs degrade gracefully."""
    model = _get_qwen()
    kwargs = {"normalize_embeddings": True, "convert_to_numpy": True}
    if prompt is not None:
        kwargs["prompt"] = prompt

    with _qwen_infer_lock:  # torch model not thread-safe under the batch ThreadPool
        batch = QWEN_BATCH_SIZE
        while True:
            try:
                return model.encode(texts, batch_size=batch, **kwargs)
            except Exception as e:
                if not _is_oom_error(e) or batch <= 1:
                    raise
                _free_accelerator_cache()
                new_batch = max(1, batch // 2)
                logger.warning("Qwen encode OOM at batch=%d → retrying at batch=%d", batch, new_batch)
                batch = new_batch


def _free_accelerator_cache() -> None:
    """Release cached GPU memory so the smaller-batch retry has room."""
    try:
        import torch
        if QWEN_DEVICE == "cuda" and torch.cuda.is_available():
            torch.cuda.empty_cache()
        elif QWEN_DEVICE == "mps" and torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:
        pass


def _embed_documents(texts: list[str]) -> "_np.ndarray":
    """Embed training/document texts WITHOUT instruction (Qwen asymmetric convention).
    Returns an L2-normalized (n, 1024) array."""
    return _encode([t[:8000] for t in texts])


def _embed_queries(texts: list[str]) -> "_np.ndarray":
    """Batch QUERY embedding with the empirical-basis instruction prefix.
    Use for storage/training so vectors match the runtime query path.
    Returns an L2-normalized (n, 1024) array."""
    return _encode([t[:8000] for t in texts], prompt=f"Instruct: {EMPIRICAL_BASIS}\nQuery: ")


def _embed_query(text: str) -> "_np.ndarray":
    """Embed a single QUERY text with the empirical-basis instruction prefix.
    Returns an L2-normalized (1, 1024) array."""
    return _embed_queries([text])


def _get_embedding(text: str, is_query: bool = True) -> list[float]:
    """Public embedding helper used by feedback/training writers.
    is_query=True (default) → QUERY embedding with the instruction prefix.
    The classifier head requires train/inference consistency: runtime embeds
    incoming emails as queries, so stored training vectors must match."""
    if is_query:
        return _embed_query(text)[0].tolist()
    return _embed_documents([text])[0].tolist()


def _load_mlp() -> tuple[Optional[_MLPClassifier], Optional[_LabelEncoder], int]:
    """Fetch Qwen training vectors from Supabase and train an MLP. Returns (model, encoder, count)."""
    try:
        rows = supabase.table("email_training_data").select(f"label, {QWEN_EMBED_COLUMN}").execute().data
        rows = [r for r in rows if r.get(QWEN_EMBED_COLUMN)]
        if len(rows) < 10:
            logger.warning("Only %d rows with %s — need >=10 to train MLP.", len(rows), QWEN_EMBED_COLUMN)
            return None, None, 0

        X = _np.array([
            _json.loads(r[QWEN_EMBED_COLUMN]) if isinstance(r[QWEN_EMBED_COLUMN], str) else r[QWEN_EMBED_COLUMN]
            for r in rows
        ])
        le = _LabelEncoder()
        y = le.fit_transform([r["label"] for r in rows])

        model = _build_mlp()
        model.fit(X, y)
        logger.info("MLP trained on %d examples, classes: %s", len(rows), list(le.classes_))
        return model, le, len(rows)
    except Exception as e:
        logger.warning("MLP training failed: %s", e)
        return None, None, 0


def _build_mlp() -> _MLPClassifier:
    """MLP head over 1024-dim Qwen vectors. Shared by runtime + eval script."""
    return _MLPClassifier(
        hidden_layer_sizes=(256, 64),
        activation="relu",
        alpha=1e-4,
        max_iter=500,
        early_stopping=True,
        n_iter_no_change=15,
        random_state=42,
    )


def _get_mlp() -> tuple[Optional[_MLPClassifier], Optional[_LabelEncoder]]:
    """Return cached MLP. Lock ensures only one thread trains at a time."""
    global _mlp_model, _mlp_label_encoder, _mlp_training_count

    # Fast path: model loaded and likely fresh — skip Supabase count check
    if _mlp_model is not None:
        return _mlp_model, _mlp_label_encoder

    with _mlp_lock:
        # Re-check inside lock in case another thread just trained it
        if _mlp_model is not None:
            return _mlp_model, _mlp_label_encoder
        try:
            current_count = supabase.table("email_training_data").select("id", count="exact").limit(1).execute().count or 0
        except Exception:
            current_count = _mlp_training_count
        if _mlp_model is None or current_count != _mlp_training_count:
            _mlp_model, _mlp_label_encoder, _mlp_training_count = _load_mlp()

    return _mlp_model, _mlp_label_encoder


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
    no_gpt_fallback: bool = False,
) -> ClassificationResult:
    """Classify an email using the hybrid approach:
    Tier 1: Rules → Tier 2: Fine-tuned model → Tier 3: Qwen+MLP → Tier 4: Few-shot

    rules_only=True  : only hard rules, instant, zero network calls.
    no_gpt_fallback=True : rules + Qwen+MLP embedding only; skips HyDE + few-shot GPT.
                           Low-confidence MLP defaults to 'general'. Use for inbox display.
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

    # Tier 3: Qwen embedding + MLP (local inference; skips HyDE when no_gpt_fallback)
    raw_text = f"Subject: {subject}\n\n{body[:2000]}"
    hyde_used = not no_gpt_fallback and len(raw_text.strip()) < HYDE_LENGTH_THRESHOLD
    embed_text = _hyde_expand(subject, body) if hyde_used else raw_text

    model, encoder = _get_mlp()
    if model is not None:
        try:
            vec = _embed_query(embed_text)
            proba = model.predict_proba(vec)[0]
            best_idx = int(_np.argmax(proba))
            confidence = float(proba[best_idx])
            label = encoder.classes_[best_idx]

            if confidence >= MLP_MIN_CONFIDENCE:
                proba_str = ", ".join(f"{encoder.classes_[i]}={proba[i]:.2f}" for i in range(len(proba)))
                mlp_result = ClassificationResult(
                    label=label,
                    confidence=confidence,
                    method="mlp+hyde" if hyde_used else "mlp",
                    details=f"Qwen+MLP probabilities: [{proba_str}], hyde={hyde_used}",
                )
                logger.info("Classified by MLP: %s (%.0f%%)", mlp_result.label, mlp_result.confidence * 100)
                return mlp_result
            else:
                logger.info("MLP gated out: confidence %.2f < %.2f for label '%s'", confidence, MLP_MIN_CONFIDENCE, label)
        except Exception as e:
            logger.warning("MLP classification failed: %s", e)

    if no_gpt_fallback:
        return ClassificationResult(
            label="general",
            confidence=0.45,
            method="mlp",
            details="MLP inconclusive; no_gpt_fallback=True so defaulting to general",
        )

    # Tier 4: Few-shot fallback (always works, uses GPT)
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

    # 2. Generate Qwen document embedding and add to training data
    try:
        email_text = f"Subject: {email_subject}\n\n{email_body[:2000]}"
        embedding = _get_embedding(email_text)  # plain document embedding (no instruction)

        supabase.table("email_training_data").insert({
            "content": email_text,
            "subject": email_subject,
            "sender": email_sender,
            "label": corrected_label,
            QWEN_EMBED_COLUMN: embedding,
        }).execute()
    except Exception as e:
        logger.error("Failed to add corrected email to training data: %s", e)
        return {"status": "partial", "detail": f"Feedback stored but training data failed: {e}"}

    return {"status": "ok", "detail": f"Feedback stored and added to training as '{corrected_label}'"}


# ---------------------------------------------------------------------------
# BATCH: Classify multiple emails
# ---------------------------------------------------------------------------

def classify_emails_batch(
    emails: list[dict],
    rules_only: bool = False,
    _no_gpt_fallback: bool = False,
) -> list[dict]:
    """Classify a list of emails in parallel. Each dict must have: subject, body, sender.

    rules_only=True   : hard rules only, instant, zero network.
    _no_gpt_fallback  : deprecated, kept for call-site compatibility — parallel full pipeline used.
    All API calls (embeddings + GPT few-shot) run concurrently so total time ≈ slowest single email.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _classify_one(email: dict) -> dict:
        result = classify_email(
            subject=email.get("subject", ""),
            body=email.get("body", ""),
            sender=email.get("sender", email.get("from", "")),
            rules_only=rules_only,
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
    with ThreadPoolExecutor(max_workers=min(len(emails), 20)) as pool:
        future_to_idx = {pool.submit(_classify_one, e): i for i, e in enumerate(emails)}
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                email = emails[idx]
                logger.warning("classify_emails_batch failed for email %s: %s", email.get("id"), exc)
                results[idx] = {
                    "id": email.get("id", ""),
                    "subject": email.get("subject", ""),
                    "label": "general",
                    "confidence": 0.0,
                    "method": "error",
                    "details": str(exc),
                }
    return results
