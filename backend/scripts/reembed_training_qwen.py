"""
reembed_training_qwen.py
------------------------
Backfills the `embedding_qwen` column on email_training_data using the local
Qwen3-Embedding-0.6B model in QUERY mode (instruction prefix), so stored
training vectors match the runtime query path the MLP classifier consumes.

The legacy OpenAI `embedding` column (1536-dim) is left untouched so the old
SVM path stays rollback-able. Qwen vectors are 1024-dim.

Schema prerequisite (run once in Supabase SQL editor):
    ALTER TABLE email_training_data ADD COLUMN IF NOT EXISTS embedding_qwen vector(1024);

Usage:
    python reembed_training_qwen.py            # only rows missing embedding_qwen
    python reembed_training_qwen.py --all      # re-embed every row (overwrite)
    python reembed_training_qwen.py --batch 32 # encode batch size
"""

import argparse
import logging
import os

from dotenv import load_dotenv
from supabase import create_client, Client

from backend.classifier.email_classifier import _embed_queries, QWEN_EMBED_COLUMN

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

supabase: Client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def fetch_rows(only_missing: bool) -> list[dict]:
    """Fetch ALL training rows needing a Qwen embedding, paginating past
    Supabase's 1000-row default cap."""
    all_rows: list[dict] = []
    page = 0
    page_size = 1000
    while True:
        query = supabase.table("email_training_data").select("id, content")
        if only_missing:
            query = query.is_(QWEN_EMBED_COLUMN, "null")
        chunk = query.range(page * page_size, page * page_size + page_size - 1).execute().data or []
        all_rows.extend(chunk)
        if len(chunk) < page_size:
            break
        page += 1
    return [r for r in all_rows if r.get("content")]


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill Qwen embeddings for email_training_data")
    parser.add_argument("--all", action="store_true", help="Re-embed every row (overwrite existing)")
    parser.add_argument("--batch", type=int, default=32, help="Encode batch size")
    args = parser.parse_args()

    rows = fetch_rows(only_missing=not args.all)
    if not rows:
        log.info("No rows to embed. Nothing to do.")
        return

    log.info("Embedding %d rows with Qwen (batch=%d)...", len(rows), args.batch)

    updated = 0
    failed = 0
    for start in range(0, len(rows), args.batch):
        chunk = rows[start:start + args.batch]
        try:
            vectors = _embed_queries([r["content"] for r in chunk])
        except Exception as e:
            log.error("  Batch embed failed at offset %d: %s", start, e)
            failed += len(chunk)
            continue

        for row, vec in zip(chunk, vectors):
            try:
                supabase.table("email_training_data").update(
                    {QWEN_EMBED_COLUMN: vec.tolist()}
                ).eq("id", row["id"]).execute()
                updated += 1
            except Exception as e:
                log.error("  Update failed id=%s: %s", row["id"], e)
                failed += 1

        log.info("  Progress: %d/%d", min(start + args.batch, len(rows)), len(rows))

    log.info("Done. Updated=%d, Failed=%d", updated, failed)


if __name__ == "__main__":
    main()
