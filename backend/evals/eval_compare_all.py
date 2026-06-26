"""
eval_compare_all.py
-------------------
Single-split benchmark of all stored head x embedding combos, on the FULL table
(paginates past Supabase's 1000-row cap). Same 85/15 stratified split + seed +
confidence gate for every config, so the comparison is apples-to-apples.

Configs (4): {SVM, MLP} x {OpenAI 1536d `embedding`, Qwen query 1024d `embedding_qwen`}.
Document-mode Qwen is not stored (it was only ever embedded in-memory), so it is
not included here.

Usage:
    python eval_compare_all.py
    python eval_compare_all.py --test-size 0.15 --seed 42
"""

import argparse
import json
import logging
import os

import numpy as np
from dotenv import load_dotenv
from sklearn.metrics import accuracy_score, f1_score
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder
from sklearn.svm import SVC
from supabase import create_client, Client

from backend.classifier.email_classifier import _build_mlp, MLP_MIN_CONFIDENCE, QWEN_EMBED_COLUMN

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

supabase: Client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

VALID_LABELS = {"customer_requirement", "quotation_rate_card", "general"}
OPENAI_COLUMN = "embedding"


def fetch_all(columns: str) -> list[dict]:
    """Fetch every row for the given select string, paginating past 1000."""
    rows: list[dict] = []
    page, size = 0, 1000
    while True:
        chunk = (
            supabase.table("email_training_data").select(columns)
            .range(page * size, page * size + size - 1).execute().data or []
        )
        rows.extend(chunk)
        if len(chunk) < size:
            break
        page += 1
    return rows


def to_matrix(rows: list[dict], col: str) -> tuple[np.ndarray, list[str], list[str]]:
    """Build (X, labels, ids) from rows that have both a label and the column."""
    keep = [r for r in rows if r.get(col) and r.get("label") in VALID_LABELS]
    X = np.array([
        json.loads(r[col]) if isinstance(r[col], str) else r[col] for r in keep
    ])
    return X, [r["label"] for r in keep], [r["id"] for r in keep]


def build_head(name: str):
    if name == "SVM":
        return SVC(kernel="rbf", C=10.0, gamma="scale", probability=True)
    return _build_mlp()


def evaluate(head_name: str, X, y, seed: float, test_size: float) -> dict:
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=test_size, random_state=seed, stratify=y,
    )
    model = build_head(head_name)
    model.fit(X_train, y_train)
    proba = model.predict_proba(X_test)
    y_pred = proba.argmax(axis=1)
    conf = proba.max(axis=1)

    gated = conf >= MLP_MIN_CONFIDENCE
    n_gated = int(gated.sum())
    return {
        "n_test": len(y_test),
        "accuracy": accuracy_score(y_test, y_pred),
        "macro_f1": f1_score(y_test, y_pred, average="macro"),
        "coverage": n_gated / len(y_test),
        "gated_acc": accuracy_score(y_test[gated], y_pred[gated]) if n_gated else float("nan"),
        "n_to_gpt": len(y_test) - n_gated,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare all head x embedding combos (full 1112)")
    parser.add_argument("--test-size", type=float, default=0.15)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    # One fetch per embedding column (paginated).
    openai_rows = fetch_all(f"id, label, {OPENAI_COLUMN}")
    qwen_rows = fetch_all(f"id, label, {QWEN_EMBED_COLUMN}")

    datasets = {
        "OpenAI 1536d": to_matrix(openai_rows, OPENAI_COLUMN),
        "Qwen-query 1024d": to_matrix(qwen_rows, QWEN_EMBED_COLUMN),
    }
    for name, (X, y_lab, _) in datasets.items():
        log.info("%s: %d rows usable", name, len(y_lab))

    results = []
    for emb_name, (X, y_lab, _) in datasets.items():
        le = LabelEncoder()
        y = le.fit_transform(y_lab)
        for head in ("SVM", "MLP"):
            r = evaluate(head, X, y, args.seed, args.test_size)
            r["config"] = f"{head} + {emb_name}"
            r["n_rows"] = len(y_lab)
            results.append(r)
            log.info("done: %s", r["config"])

    # --- comparison table ---
    print("\n" + "=" * 88)
    print(f"BENCHMARK — 85/15 split, seed={args.seed}, gate={MLP_MIN_CONFIDENCE:.2f}, FULL table (paginated)")
    print("=" * 88)
    hdr = f"{'config':<26}{'rows':>6}{'overall':>9}{'macroF1':>9}{'gatedAcc':>10}{'coverage':>10}{'->GPT':>7}"
    print(hdr)
    print("-" * len(hdr))
    for r in sorted(results, key=lambda d: d["gated_acc"], reverse=True):
        print(
            f"{r['config']:<26}{r['n_rows']:>6}{r['accuracy']:>9.4f}{r['macro_f1']:>9.4f}"
            f"{r['gated_acc']:>10.4f}{r['coverage']:>9.1%}{r['n_to_gpt']:>7}"
        )
    print("\n(sorted by gated accuracy)")


if __name__ == "__main__":
    main()
