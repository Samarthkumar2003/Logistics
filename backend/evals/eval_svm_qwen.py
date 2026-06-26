"""
eval_svm_qwen.py
----------------
Ablation: legacy SVM RBF head on the NEW Qwen query-mode vectors
(embedding_qwen, 1024-dim). Compares against MLP + Qwen (eval_classifier.py)
to isolate the head (SVM vs MLP) on identical embeddings.

Paginates past Supabase's 1000-row cap so the full table is used (the other
evals silently truncate at 1000).

Usage:
    python eval_svm_qwen.py
    python eval_svm_qwen.py --test-size 0.15 --seed 42
"""

import argparse
import json
import logging
import os

import numpy as np
from dotenv import load_dotenv
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder
from sklearn.svm import SVC
from supabase import create_client, Client

from backend.classifier.email_classifier import MLP_MIN_CONFIDENCE, QWEN_EMBED_COLUMN

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

supabase: Client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

VALID_LABELS = {"customer_requirement", "quotation_rate_card", "general"}


def load_data() -> tuple[np.ndarray, list[str]]:
    """Load ALL Qwen vectors + labels, paginating past the 1000-row cap."""
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
            break
        page += 1

    rows = [r for r in rows if r.get(QWEN_EMBED_COLUMN) and r.get("label") in VALID_LABELS]
    if len(rows) < 10:
        raise SystemExit(f"Only {len(rows)} usable rows.")
    X = np.array([
        json.loads(r[QWEN_EMBED_COLUMN]) if isinstance(r[QWEN_EMBED_COLUMN], str) else r[QWEN_EMBED_COLUMN]
        for r in rows
    ])
    return X, [r["label"] for r in rows]


def print_confusion(cm: np.ndarray, classes: list[str]) -> None:
    width = max(len(c) for c in classes) + 2
    print("\nConfusion matrix (rows=true, cols=pred):")
    print(" " * width + "".join(f"{c[:10]:>12}" for c in classes))
    for i, c in enumerate(classes):
        print(f"{c:>{width}}" + "".join(f"{cm[i, j]:>12}" for j in range(len(classes))))


def main() -> None:
    parser = argparse.ArgumentParser(description="SVM head on Qwen query-mode vectors (85/15)")
    parser.add_argument("--test-size", type=float, default=0.15)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    X, y_labels = load_data()
    le = LabelEncoder()
    y = le.fit_transform(y_labels)
    classes = list(le.classes_)

    log.info("Loaded %d examples across %d classes: %s", len(y), len(classes), classes)
    for c in classes:
        log.info("  %-22s %d", c, y_labels.count(c))

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=args.test_size, random_state=args.seed, stratify=y,
    )
    log.info("Split: %d train / %d test", len(y_train), len(y_test))

    model = SVC(kernel="rbf", C=10.0, gamma="scale", probability=True)
    model.fit(X_train, y_train)

    proba = model.predict_proba(X_test)
    y_pred = proba.argmax(axis=1)
    confidences = proba.max(axis=1)

    acc = accuracy_score(y_test, y_pred)
    print("\n" + "=" * 60)
    print(f"[SVM + Qwen query 1024d] OVERALL ACCURACY (test): {acc:.4f}  ({int(acc * len(y_test))}/{len(y_test)})")
    print("=" * 60)

    print("\nClassification report:")
    print(classification_report(
        y_test, y_pred, labels=range(len(classes)),
        target_names=classes, digits=4, zero_division=0,
    ))

    print_confusion(confusion_matrix(y_test, y_pred, labels=range(len(classes))), classes)

    gated_mask = confidences >= MLP_MIN_CONFIDENCE
    n_gated = int(gated_mask.sum())
    print(f"\nConfidence gate ({MLP_MIN_CONFIDENCE:.2f}):")
    if n_gated:
        print(f"  Coverage: {n_gated}/{len(y_test)} ({n_gated / len(y_test):.1%}) pass the gate")
        print(f"  Accuracy on gated: {accuracy_score(y_test[gated_mask], y_pred[gated_mask]):.4f}")
        print(f"  {len(y_test) - n_gated} test emails fall through to GPT few-shot")
    else:
        print("  No test predictions cleared the gate.")

    correct = y_pred == y_test
    if correct.any():
        print(f"\nMean confidence — correct: {confidences[correct].mean():.4f}", end="")
    if (~correct).any():
        print(f" | wrong: {confidences[~correct].mean():.4f}", end="")
    print()


if __name__ == "__main__":
    main()
