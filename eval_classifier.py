"""
eval_classifier.py
------------------
Evaluates the Qwen+MLP email classifier head with an 85/15 stratified
train/test split. Reports full metrics: accuracy, per-class precision/recall/F1,
macro & weighted averages, confusion matrix, and gated accuracy at the runtime
confidence threshold.

Reads pre-computed Qwen vectors from email_training_data.embedding_qwen, so run
reembed_training_qwen.py first.

Usage:
    python eval_classifier.py
    python eval_classifier.py --test-size 0.15 --seed 42
"""

import argparse
import json
import logging
import os

import numpy as np
from dotenv import load_dotenv
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
)
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder
from supabase import create_client, Client

from email_classifier import _build_mlp, MLP_MIN_CONFIDENCE, QWEN_EMBED_COLUMN

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

supabase: Client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


VALID_LABELS = {"customer_requirement", "quotation_rate_card", "general"}


def load_data(embed_live: bool) -> tuple[np.ndarray, list[str]]:
    """Load labels + vectors. Reads embedding_qwen from Supabase, or embeds
    `content` in-memory with Qwen when embed_live=True (no DB column needed)."""
    if embed_live:
        from email_classifier import _embed_queries
        rows = supabase.table("email_training_data").select("label, content").execute().data
        rows = [r for r in rows if r.get("content") and r.get("label") in VALID_LABELS]
        if len(rows) < 10:
            raise SystemExit(f"Only {len(rows)} usable rows. Need >=10.")
        log.info("Embedding %d rows live with Qwen (QUERY mode)...", len(rows))
        # Query mode (instruction prefix) — matches runtime + stored training vectors.
        # Chunk so progress is visible and memory stays bounded.
        texts = [r["content"] for r in rows]
        chunks = []
        step = 64
        for start in range(0, len(texts), step):
            chunks.append(_embed_queries(texts[start:start + step]))
            log.info("  embedded %d/%d", min(start + step, len(texts)), len(texts))
        X = np.vstack(chunks)
        y = [r["label"] for r in rows]
        return X, y

    rows = supabase.table("email_training_data").select(
        f"label, {QWEN_EMBED_COLUMN}"
    ).execute().data
    rows = [r for r in rows if r.get(QWEN_EMBED_COLUMN) and r.get("label") in VALID_LABELS]
    if len(rows) < 10:
        raise SystemExit(
            f"Only {len(rows)} rows have {QWEN_EMBED_COLUMN}. "
            "Run reembed_training_qwen.py first, or pass --embed-live."
        )
    X = np.array([
        json.loads(r[QWEN_EMBED_COLUMN]) if isinstance(r[QWEN_EMBED_COLUMN], str)
        else r[QWEN_EMBED_COLUMN]
        for r in rows
    ])
    y = [r["label"] for r in rows]
    return X, y


def print_confusion(cm: np.ndarray, classes: list[str]) -> None:
    width = max(len(c) for c in classes) + 2
    header = " " * width + "".join(f"{c[:10]:>12}" for c in classes)
    print("\nConfusion matrix (rows=true, cols=pred):")
    print(header)
    for i, c in enumerate(classes):
        row = f"{c:>{width}}" + "".join(f"{cm[i, j]:>12}" for j in range(len(classes)))
        print(row)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Qwen+MLP classifier (85/15 split)")
    parser.add_argument("--test-size", type=float, default=0.15, help="Test fraction (default 0.15)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--embed-live", action="store_true",
                        help="Embed content in-memory with Qwen (no embedding_qwen column needed)")
    args = parser.parse_args()

    X, y_labels = load_data(embed_live=args.embed_live)
    le = LabelEncoder()
    y = le.fit_transform(y_labels)
    classes = list(le.classes_)

    log.info("Loaded %d examples across %d classes: %s", len(y), len(classes), classes)
    for c in classes:
        log.info("  %-22s %d", c, y_labels.count(c))

    # Stratified 85/15 split keeps class balance in both sides
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=args.test_size, random_state=args.seed, stratify=y,
    )
    log.info("Split: %d train / %d test", len(y_train), len(y_test))

    model = _build_mlp()
    model.fit(X_train, y_train)

    # --- Predictions + probabilities ---
    proba = model.predict_proba(X_test)
    y_pred = proba.argmax(axis=1)
    confidences = proba.max(axis=1)

    # --- Headline metrics ---
    acc = accuracy_score(y_test, y_pred)
    print("\n" + "=" * 60)
    print(f"OVERALL ACCURACY (test): {acc:.4f}  ({int(acc * len(y_test))}/{len(y_test)})")
    print("=" * 60)

    # --- Per-class precision / recall / F1, macro + weighted ---
    print("\nClassification report:")
    print(classification_report(
        y_test, y_pred, labels=range(len(classes)),
        target_names=classes, digits=4, zero_division=0,
    ))

    # --- Confusion matrix ---
    cm = confusion_matrix(y_test, y_pred, labels=range(len(classes)))
    print_confusion(cm, classes)

    # --- Confidence-gated accuracy (mirrors runtime MLP_MIN_CONFIDENCE) ---
    gated_mask = confidences >= MLP_MIN_CONFIDENCE
    n_gated = int(gated_mask.sum())
    print(f"\nConfidence gate ({MLP_MIN_CONFIDENCE:.2f}):")
    if n_gated:
        gated_acc = accuracy_score(y_test[gated_mask], y_pred[gated_mask])
        print(f"  Coverage: {n_gated}/{len(y_test)} ({n_gated / len(y_test):.1%}) pass the gate")
        print(f"  Accuracy on gated: {gated_acc:.4f}")
        print(f"  {len(y_test) - n_gated} test emails fall through to GPT few-shot")
    else:
        print("  No test predictions cleared the gate.")

    # --- Mean confidence by correctness ---
    correct = y_pred == y_test
    if correct.any():
        print(f"\nMean confidence — correct: {confidences[correct].mean():.4f}", end="")
    if (~correct).any():
        print(f" | wrong: {confidences[~correct].mean():.4f}", end="")
    print()


if __name__ == "__main__":
    main()
