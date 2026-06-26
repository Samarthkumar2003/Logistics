"""
email_classifier.py
-------------------
Email classifier: a single LLM call through a swappable provider (see
``llm_provider.py``). The model is chosen at runtime via the ``LLM_PROVIDER``
env var — swapping OpenAI for Gemini (or any future provider) needs no code
change here (Open/Closed Principle).

Labels:
  - customer_requirement : Customer asking for a shipment / freight quote
  - quotation_rate_card  : Agent replying with rates / pricing
  - general              : Everything else (newsletters, spam, internal, etc.)

The Qwen embedding + SVM helpers below are retained as tooling — eval and
ingest scripts import them — but are no longer part of the classification flow.
"""

import os
import json
import logging
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv
from supabase import create_client

from backend.classifier.llm_provider import get_provider

load_dotenv()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
supabase = create_client(
    os.environ.get("SUPABASE_URL", ""),
    os.environ.get("SUPABASE_KEY", ""),
)


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
# LLM classification prompt — the asking-vs-providing empirical basis
# ---------------------------------------------------------------------------

CLASSIFY_SYSTEM_PROMPT = (
    "You are an email classifier for a freight-forwarding company. Classify each "
    "email into exactly one of three labels based on who is asking and who is pricing:\n\n"
    "- customer_requirement: the sender is REQUESTING a freight quote, rate, or booking. "
    "They describe a shipment (origin/POL, destination/POD, cargo, weight, container type, "
    "incoterm, ready date) and want US to provide rates. An email with cargo specs but NO "
    "prices is customer_requirement. Signals: 'kindly share your best rate', 'request for "
    "quotation', 'please quote', 'we require', filled POL/POD/cargo fields with empty prices.\n"
    "- quotation_rate_card: the sender is an agent or carrier PROVIDING rates/prices TO us, "
    "usually replying to our RFQ. They state the numbers. Signals: 'please find our rates', "
    "'USD X per container', 'all-in', transit time and validity filled in, RFQ reference in subject.\n"
    "- general: everything else operational or non-deal — newsletters, spam, tracking updates, "
    "invoices, internal memos, existing-job threads.\n\n"
    "Decisive rule: ASKING for a price = customer_requirement; PROVIDING a price = quotation_rate_card.\n\n"
    'Respond ONLY with JSON: {"label": "<one of the three labels>", "confidence": <0.0-1.0>}'
)


# ---------------------------------------------------------------------------
# Qwen3-Embedding-0.6B (instruction-aware) → sklearn SVM (RBF) classifier
# Retained as tooling for eval/ingest scripts — NOT used by classify_email.
# Trains in-memory at first use from Supabase Qwen vectors (embedding_qwen col).
# Query side embeds with an instruction prefix encoding our empirical basis;
# training/document vectors are embedded plain (Qwen asymmetric convention).
# SVM RBF beat MLP on the full 1112-row set: 0.796 vs 0.761 overall, 0.918 vs
# 0.853 gated, and rate-card recall 0.69 vs 0.48 (the costliest error class).
# ---------------------------------------------------------------------------

# Confidence gate retained for eval scripts that import it.
CLF_MIN_CONFIDENCE = 0.70
MLP_MIN_CONFIDENCE = CLF_MIN_CONFIDENCE  # back-compat alias for eval scripts

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
from sklearn.svm import SVC as _SVC
from sklearn.preprocessing import LabelEncoder as _LabelEncoder

# Module-level caches — loaded/trained once, reused for every email
_qwen_encoder = None  # SentenceTransformer, lazily loaded (heavy)
_qwen_lock = _threading.Lock()       # guards one-time model load
_qwen_infer_lock = _threading.Lock()  # serializes encode() — torch model not thread-safe
_clf_model: Optional[_SVC] = None
_clf_label_encoder: Optional[_LabelEncoder] = None
_clf_training_count: int = 0
_clf_lock = _threading.Lock()  # prevent concurrent retraining from parallel classifier threads


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


def _fetch_all_training_rows() -> list[dict]:
    """Page past Supabase's 1000-row cap so the head trains on the FULL table.
    A single .execute() silently truncates at 1000; this loops until a short page."""
    rows: list[dict] = []
    page, size = 0, 1000
    while True:
        chunk = (
            supabase.table("email_training_data")
            .select(f"label, {QWEN_EMBED_COLUMN}")
            .range(page * size, page * size + size - 1)
            .execute()
            .data
            or []
        )
        rows.extend(chunk)
        if len(chunk) < size:
            return rows
        page += 1


def _load_classifier() -> tuple[Optional[_SVC], Optional[_LabelEncoder], int]:
    """Fetch Qwen training vectors from Supabase and train the SVM. Returns (model, encoder, count)."""
    try:
        rows = _fetch_all_training_rows()
        rows = [r for r in rows if r.get(QWEN_EMBED_COLUMN)]
        if len(rows) < 10:
            logger.warning("Only %d rows with %s — need >=10 to train SVM.", len(rows), QWEN_EMBED_COLUMN)
            return None, None, 0

        X = _np.array([
            _json.loads(r[QWEN_EMBED_COLUMN]) if isinstance(r[QWEN_EMBED_COLUMN], str) else r[QWEN_EMBED_COLUMN]
            for r in rows
        ])
        le = _LabelEncoder()
        y = le.fit_transform([r["label"] for r in rows])

        model = _build_classifier()
        model.fit(X, y)
        logger.info("SVM trained on %d examples, classes: %s", len(rows), list(le.classes_))
        return model, le, len(rows)
    except Exception as e:
        logger.warning("SVM training failed: %s", e)
        return None, None, 0


def _build_classifier() -> _SVC:
    """SVM RBF head over 1024-dim Qwen vectors. Shared by runtime + eval scripts.
    probability=True is required: Tier 3's confidence gate needs predict_proba."""
    return _SVC(kernel="rbf", C=10.0, gamma="scale", probability=True, random_state=42)


def _get_classifier() -> tuple[Optional[_SVC], Optional[_LabelEncoder]]:
    """Return cached SVM. Lock ensures only one thread trains at a time."""
    global _clf_model, _clf_label_encoder, _clf_training_count

    # Fast path: model loaded and likely fresh — skip Supabase count check
    if _clf_model is not None:
        return _clf_model, _clf_label_encoder

    with _clf_lock:
        # Re-check inside lock in case another thread just trained it
        if _clf_model is not None:
            return _clf_model, _clf_label_encoder
        try:
            current_count = supabase.table("email_training_data").select("id", count="exact").limit(1).execute().count or 0
        except Exception:
            current_count = _clf_training_count
        if _clf_model is None or current_count != _clf_training_count:
            _clf_model, _clf_label_encoder, _clf_training_count = _load_classifier()

    return _clf_model, _clf_label_encoder


# Back-compat aliases for eval scripts that import the old names
_build_mlp = _build_classifier
_get_mlp = _get_classifier


# ---------------------------------------------------------------------------
# MAIN ENTRY POINT: single LLM call through the swappable provider
# ---------------------------------------------------------------------------

_VALID_LABELS = {"customer_requirement", "quotation_rate_card", "general"}

# Max chars of body sent to the LLM. ~2K tokens — trivial for gpt-4o-mini (128K)
# and Gemini flash-lite (1M). Bigger window so the rate-request signal isn't cut
# off when long flattened-HTML-table threads push it down.
MAX_BODY_CHARS = 8000


def _parse_llm_label(raw: str) -> tuple[str, float]:
    """Parse the provider reply into (label, confidence). Tolerates JSON wrapped
    in code fences or surrounding prose; falls back to keyword matching."""
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            label = str(data.get("label", "")).strip().lower()
            if label in _VALID_LABELS:
                conf = float(data.get("confidence", 0.8))
                return label, max(0.0, min(1.0, conf))
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    lowered = text.lower()
    if "customer_requirement" in lowered or "customer" in lowered:
        return "customer_requirement", 0.7
    if "quotation_rate_card" in lowered or "quotation" in lowered or "rate" in lowered:
        return "quotation_rate_card", 0.7
    return "general", 0.5


def classify_email(
    subject: str,
    body: str,
    sender: str = "",
    rules_only: bool = False,
    no_gpt_fallback: bool = False,
) -> ClassificationResult:
    """Classify an email with a single LLM call through the active provider
    (``LLM_PROVIDER`` env: openai | gemini | ...).

    rules_only / no_gpt_fallback are retained for call-site compatibility but
    no longer change behavior — classification is one provider call.
    """
    truncated_body = body[:MAX_BODY_CHARS]
    user_prompt = f"From: {sender}\nSubject: {subject}\n\n{truncated_body}"

    try:
        provider = get_provider()
        raw = provider.complete(CLASSIFY_SYSTEM_PROMPT, user_prompt, temperature=0.0, max_tokens=60)
        label, confidence = _parse_llm_label(raw)
        result = ClassificationResult(
            label=label,
            confidence=confidence,
            method=f"llm:{provider.name}",
            details=f"{provider.name} output: {raw[:200]}",
        )
        logger.info("Classified by %s: %s (%.0f%%)", provider.name, result.label, result.confidence * 100)
        return result
    except Exception as e:
        logger.error("LLM classification failed: %s", e)
        return ClassificationResult(
            label="general",
            confidence=0.0,
            method="error",
            details=f"Classification failed: {e}",
        )


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
