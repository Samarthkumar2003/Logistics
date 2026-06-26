"""
eval_mlp_openai.py
------------------
Ablation: the NEW MLP head on the LEGACY OpenAI `embedding` column (1536-dim).

Isolates the head change (SVM -> MLP) from the embedding change (OpenAI -> Qwen)
by holding the embeddings fixed and only swapping the classifier. Same 85/15
stratified split, seed, and confidence gate as the other evals, so it is directly
comparable to eval_svm_baseline.py and eval_classifier.py.

Usage:
    python eval_mlp_openai.py
    python eval_mlp_openai.py --test-size 0.15 --seed 42
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
from supabase import create_client, Client

from backend.classifier.email_classifier import _build_mlp, MLP_MIN_CONFIDENCE  # same head + gate

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

supabase: Client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

VALID_LABELS = {"customer_requirement", "quotation_rate_card", "general"}
OPENAI_COLUMN = "embedding"  # OpenAI text-embedding-3-small, 1536-dim


def load_data() -> tuple[np.ndarray, list[str]]:
    rows = supabase.table("email_training_data").select(f"label, {OPENAI_COLUMN}").execute().data
    rows = [r for r in rows if r.get(OPENAI_COLUMN) and r.get("label") in VALID_LABELS]
    if len(rows) < 10:
        raise SystemExit(f"Only {len(rows)} rows have {OPENAI_COLUMN}. Nothing to train.")
    X = np.array([
        json.loads(r[OPENAI_COLUMN]) if isinstance(r[OPENAI_COLUMN], str) else r[OPENAI_COLUMN]
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
    parser = argparse.ArgumentParser(description="MLP head on OpenAI embeddings (85/15)")
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

    model = _build_mlp()
    model.fit(X_train, y_train)

    proba = model.predict_proba(X_test)
    y_pred = proba.argmax(axis=1)
    confidences = proba.max(axis=1)

    acc = accuracy_score(y_test, y_pred)
    print("\n" + "=" * 60)
    print(f"[MLP + OpenAI 1536d] OVERALL ACCURACY (test): {acc:.4f}  ({int(acc * len(y_test))}/{len(y_test)})")
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
