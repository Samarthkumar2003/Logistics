"""
reconcile_labels.py
-------------------
One-off cache cleanup + label reconciliation.

Problem: labels live in two stores (`emails.classification` and the
`email_classifications` cache) under two id key spaces (provider_msg_id and
Message-ID), and they have drifted. The UI reads cache-first; the pipeline
reads the table.

This script:
  1. Computes the EFFECTIVE label per email (cache-first, like /fetch-inbox);
     human feedback corrections in the cache win automatically.
  2. Applies the thread rule: earliest customer_requirement per thread keeps
     the label, every later one in the thread becomes general.
  3. Writes the final label to emails.classification.
  4. WIPES the cache and rebuilds it — one row per email, keyed ONLY by
     provider_msg_id — so both stores agree and the dual-key mess is gone.
     Backfill on startup then finds every email cached: no LLM re-spend.

Run: python -m backend.scripts.reconcile_labels
"""
import logging
import os
from collections import defaultdict

from dotenv import load_dotenv
from supabase import create_client

from backend.core.paths import PROJECT_ROOT

load_dotenv(PROJECT_ROOT / ".env")
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

CR = "customer_requirement"


def _fetch_all(supabase, table: str, columns: str, page_size: int = 1000) -> list[dict]:
    rows: list[dict] = []
    page = 0
    while True:
        chunk = (
            supabase.table(table).select(columns)
            .range(page * page_size, page * page_size + page_size - 1)
            .execute().data or []
        )
        rows.extend(chunk)
        if len(chunk) < page_size:
            return rows
        page += 1


def reconcile() -> None:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise SystemExit("SUPABASE_URL and SUPABASE_KEY must be set in .env")
    supabase = create_client(url, key)

    emails = _fetch_all(
        supabase, "emails",
        "id, provider_msg_id, message_id, thread_id, received_at, subject, classification",
    )
    cache_rows = _fetch_all(supabase, "email_classifications", "email_id, label, confidence, method")
    cache = {r["email_id"]: r for r in cache_rows}
    logger.info("Loaded %d emails, %d cache rows", len(emails), len(cache_rows))

    # 1. Effective label per email (cache-first, matching the UI)
    final: dict[str, dict] = {}  # email row id -> {label, confidence, method}
    for e in emails:
        c = cache.get(e.get("provider_msg_id")) or cache.get(e.get("message_id") or "")
        if c and c.get("method") != "error":
            final[e["id"]] = {"label": c["label"], "confidence": c.get("confidence", 0.0),
                              "method": c.get("method", "")}
        else:
            final[e["id"]] = {"label": e.get("classification") or "general",
                              "confidence": 0.0, "method": "table"}

    # 2. Thread rule — earliest CR per thread wins
    by_thread: dict[str, list[dict]] = defaultdict(list)
    for e in emails:
        if final[e["id"]]["label"] == CR:
            by_thread[e.get("thread_id") or e["id"]].append(e)

    downgraded = 0
    for thread_id, cr_emails in by_thread.items():
        cr_emails.sort(key=lambda e: e.get("received_at") or "")
        for reply in cr_emails[1:]:
            final[reply["id"]] = {"label": "general", "confidence": 1.0, "method": "thread_rule"}
            downgraded += 1
            logger.info("Thread %s: downgraded reply %s", thread_id, (reply.get("subject") or "")[:55])
    logger.info("Thread rule: %d replies downgraded across %d customer threads",
                downgraded, len(by_thread))

    # 3. Write final labels to emails.classification (only where changed)
    table_updates = 0
    for e in emails:
        new_label = final[e["id"]]["label"]
        if (e.get("classification") or "") != new_label:
            supabase.table("emails").update({"classification": new_label}).eq("id", e["id"]).execute()
            table_updates += 1
    logger.info("emails.classification updated on %d rows", table_updates)

    # 4. Wipe + rebuild cache keyed only by provider_msg_id
    supabase.table("email_classifications").delete().neq("email_id", "").execute()
    logger.info("Cache wiped")

    rebuild = []
    for e in emails:
        if not e.get("provider_msg_id"):
            continue
        f = final[e["id"]]
        rebuild.append({
            "email_id": e["provider_msg_id"],
            "subject": (e.get("subject") or "")[:500],
            "label": f["label"],
            "confidence": f["confidence"],
            "method": f["method"] or "rebuild",
        })
    for i in range(0, len(rebuild), 500):
        supabase.table("email_classifications").upsert(
            rebuild[i:i + 500], on_conflict="email_id"
        ).execute()
    logger.info("Cache rebuilt: %d rows, single key space (provider_msg_id)", len(rebuild))


if __name__ == "__main__":
    reconcile()
