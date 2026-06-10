"""
eval_classifier.py
------------------
Evaluate the SVM classifier on the Supabase training data using
stratified 5-fold cross-validation.

Reports:
  - Per-class precision, recall, F1
  - Overall accuracy
  - Confusion matrix
  - Which folds used OpenAI embeddings vs Qwen embeddings

Usage:
    python eval_classifier.py
    python eval_classifier.py --qwen          # use embedding_qwen column
    python eval_classifier.py --folds 10      # change fold count
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
from sklearn.model_selection import StratifiedKFold
from sklearn.preprocessing import LabelEncoder
from sklearn.svm import SVC
from supabase import create_client

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

LABEL_ORDER = ["customer_requirement", "quotation_rate_card", "general"]


def fetch_data(use_qwen: bool) -> tuple[np.ndarray, list[str]]:
    supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    col = "embedding_qwen" if use_qwen else "embedding"
    logger.info("Fetching training data (embedding column: %s)…", col)
    rows = supabase.table("email_training_data").select(f"label, {col}").execute().data
    rows = [r for r in rows if r.get(col)]
    logger.info("Loaded %d rows with embeddings.", len(rows))

    X = np.array([
        json.loads(r[col]) if isinstance(r[col], str) else r[col]
        for r in rows
    ])
    y = [r["label"] for r in rows]
    return X, y


def run_eval(X: np.ndarray, y: list[str], n_folds: int) -> None:
    le = LabelEncoder()
    y_enc = le.fit_transform(y)
    classes = le.classes_

    skf = StratifiedKFold(n_splits=n_folds, shuffle=True, random_state=42)
    y_true_all: list[int] = []
    y_pred_all: list[int] = []

    for fold, (train_idx, val_idx) in enumerate(skf.split(X, y_enc), 1):
        X_train, X_val = X[train_idx], X[val_idx]
        y_train, y_val = y_enc[train_idx], y_enc[val_idx]

        model = SVC(kernel="rbf", C=10.0, gamma="scale", probability=True)
        model.fit(X_train, y_train)
        preds = model.predict(X_val)

        fold_acc = accuracy_score(y_val, preds)
        logger.info("Fold %d/%d — accuracy: %.1f%%", fold, n_folds, fold_acc * 100)

        y_true_all.extend(y_val.tolist())
        y_pred_all.extend(preds.tolist())

    print("\n" + "=" * 60)
    print(f"CROSS-VALIDATION RESULTS  ({n_folds}-fold stratified)")
    print("=" * 60)
    print(f"\nOverall accuracy: {accuracy_score(y_true_all, y_pred_all) * 100:.2f}%")
    print(f"Total samples:    {len(y_true_all)}")

    print("\nPer-class report:")
    print(classification_report(
        y_true_all, y_pred_all,
        labels=list(range(len(classes))),
        target_names=classes,
        digits=3,
    ))

    cm = confusion_matrix(y_true_all, y_pred_all)
    print("Confusion matrix (rows=actual, cols=predicted):")
    header = "".join(f"{c[:8]:>10}" for c in classes)
    print(f"{'':>24}{header}")
    for i, row in enumerate(cm):
        row_str = "".join(f"{v:>10}" for v in row)
        print(f"{classes[i]:>24}{row_str}")

    print("\nLabel distribution in training set:")
    for label in LABEL_ORDER:
        count = y.count(label)
        pct = count / len(y) * 100
        print(f"  {label:<30} {count:>4}  ({pct:.1f}%)")

    print("\nMisclassification breakdown:")
    y_true_arr = np.array(y_true_all)
    y_pred_arr = np.array(y_pred_all)
    for i, true_label in enumerate(classes):
        mask = y_true_arr == i
        wrong = (y_pred_arr[mask] != i).sum()
        if wrong:
            print(f"  {true_label}: {wrong} misclassified out of {mask.sum()}")
            for j, pred_label in enumerate(classes):
                if j != i:
                    n = ((y_true_arr == i) & (y_pred_arr == j)).sum()
                    if n:
                        print(f"      → predicted as {pred_label}: {n}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qwen", action="store_true", help="Use Qwen embeddings (embedding_qwen column)")
    parser.add_argument("--folds", type=int, default=5, help="Number of CV folds (default 5)")
    args = parser.parse_args()

    embedding_type = "Qwen (local GPU)" if args.qwen else "OpenAI text-embedding-3-small"
    logger.info("Embedding type: %s | Folds: %d", embedding_type, args.folds)

    X, y = fetch_data(use_qwen=args.qwen)
    run_eval(X, y, n_folds=args.folds)


if __name__ == "__main__":
    main()
