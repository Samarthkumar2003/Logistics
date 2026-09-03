"""
backfill_3months.py
-------------------
Fetch ALL inbox emails from the last 3 months via Gmail API, classify each
with the production LLM prompt, and upsert into Supabase (emails +
email_classifications). Idempotent — re-running only classifies new arrivals.

Usage:
    python -m backend.scripts.backfill_3months              # full run
    python -m backend.scripts.backfill_3months --dry-run    # classify + print, no DB writes
    python -m backend.scripts.backfill_3months --days 90    # override lookback window
    python -m backend.scripts.backfill_3months --max 3000   # override email cap
"""

import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from supabase import create_client

from backend.core.logging_config import configure_logging, default_log_file
from backend.core.logging_context import carry_context, email_context

load_dotenv()
configure_logging(log_file=default_log_file())  # bulk run: keep a record
logger = logging.getLogger("backfill_3months")

supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_KEY"],
)

# ---------------------------------------------------------------------------
# Config from argv
# ---------------------------------------------------------------------------

DRY_RUN  = "--dry-run" in sys.argv
DAYS     = next((int(sys.argv[sys.argv.index("--days") + 1])
                 for i, a in enumerate(sys.argv) if a == "--days"), 91)
MAX_MAIL = next((int(sys.argv[sys.argv.index("--max") + 1])
                 for i, a in enumerate(sys.argv) if a == "--max"), 3000)

CUTOFF_DT    = datetime.now(tz=timezone.utc) - timedelta(days=DAYS)
CUTOFF_EPOCH = int(CUTOFF_DT.timestamp())

logger.info("Backfill window: last %d days (since %s)", DAYS, CUTOFF_DT.strftime("%Y-%m-%d"))
logger.info("Max emails cap: %d | Dry-run: %s", MAX_MAIL, DRY_RUN)


# ---------------------------------------------------------------------------
# Gmail fetch (parallel, full format)
# ---------------------------------------------------------------------------

from backend.connectors.gmail_connector import fetch_messages_since   # noqa: E402


def fetch_3months() -> list[dict]:
    logger.info("Fetching Gmail messages since epoch %d …", CUTOFF_EPOCH)
    records = fetch_messages_since(after_epoch_s=CUTOFF_EPOCH, max_results=MAX_MAIL)
    logger.info("Fetched %d messages", len(records))
    return records


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def _existing_message_ids(batch: list[str]) -> set[str]:
    """Return message_ids already in the emails table (dedup before insert)."""
    if not batch:
        return set()
    try:
        rows = (
            supabase.table("emails")
            .select("message_id, provider_msg_id")
            .in_("message_id", [m for m in batch if m])
            .execute()
            .data or []
        )
        return {r["message_id"] for r in rows if r.get("message_id")}
    except Exception as e:
        logger.warning("Dedup check failed: %s", e)
        return set()


def _existing_classified_ids(batch: list[str]) -> set[str]:
    """Return email_ids already in email_classifications."""
    if not batch:
        return set()
    try:
        rows = (
            supabase.table("email_classifications")
            .select("email_id")
            .in_("email_id", batch)
            .neq("method", "error")
            .execute()
            .data or []
        )
        return {r["email_id"] for r in rows}
    except Exception as e:
        logger.warning("Classification dedup check failed: %s", e)
        return set()


def upsert_email(r: dict, label: str, confidence: float, method: str) -> str | None:
    """Upsert one record into emails + email_classifications. Returns emails.id UUID."""
    try:
        res = supabase.table("emails").upsert({
            "message_id":            r.get("message_id") or None,
            "provider":              r["provider"],
            "provider_msg_id":       r["provider_msg_id"],
            "thread_id":             r.get("thread_id", ""),
            "sender":                r.get("sender", ""),
            "subject":               r.get("subject", ""),
            "body":                  r.get("body", ""),
            "has_attachments":       r.get("has_attachments", False),
            "received_at":           r.get("received_at"),
            "classification":        label,
            "classification_status": "classified",
        }, on_conflict="message_id").execute()
        email_uuid = res.data[0]["id"] if res.data else None
    except Exception as e:
        logger.warning("emails upsert failed for %s: %s", r.get("provider_msg_id"), e)
        return None

    try:
        key = r.get("message_id") or r["provider_msg_id"]
        supabase.table("email_classifications").upsert({
            "email_id":   key,
            "subject":    (r.get("subject") or "")[:500],
            "sender":     (r.get("sender") or "")[:300],
            "label":      label,
            "confidence": confidence,
            "method":     method,
            "updated_at": "now()",
        }, on_conflict="email_id").execute()
    except Exception as e:
        logger.warning("email_classifications upsert failed for %s: %s", key, e)

    return email_uuid


# ---------------------------------------------------------------------------
# Classification (parallel, using the production prompt via email_classifier)
# ---------------------------------------------------------------------------

from backend.classifier.email_classifier import classify_email   # noqa: E402


def _classify_one(r: dict) -> tuple[dict, str, float, str]:
    # A three-month backfill is thousands of classifications; without the id the
    # rule-tier lines it emits cannot be traced to a message afterwards.
    with email_context(r.get("provider_msg_id", "") or r.get("id", "")):
        result = classify_email(
            subject=r.get("subject", ""),
            body=r.get("body", ""),
            sender=r.get("sender", ""),
        )
        return r, result.label, result.confidence, result.method


def classify_parallel(records: list[dict], workers: int = 5) -> list[tuple]:
    """Classify all records in parallel. Returns list of (record, label, conf, method)."""
    results = []
    total = len(records)
    classify = carry_context(_classify_one)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(classify, r): r for r in records}
        done = 0
        for future in as_completed(futures):
            done += 1
            try:
                results.append(future.result())
            except Exception as e:
                r = futures[future]
                logger.warning("Classify failed for %s: %s", r.get("provider_msg_id"), e)
                results.append((r, "general", 0.0, "error"))
            if done % 25 == 0:
                logger.info("Classified %d/%d …", done, total)
    return results


# ---------------------------------------------------------------------------
# Analysis report
# ---------------------------------------------------------------------------

def print_analysis(classified: list[tuple]) -> None:
    label_counter: Counter = Counter()
    conf_by_label: dict[str, list[float]] = {}
    by_label: dict[str, list[dict]] = {}

    for r, label, conf, method in classified:
        label_counter[label] += 1
        conf_by_label.setdefault(label, []).append(conf)
        by_label.setdefault(label, []).append(r)

    total = len(classified)
    print("\n" + "=" * 70)
    print(f"  EMAIL ANALYSIS — last {DAYS} days  ({total} emails total)")
    print("=" * 70)

    for lbl in ["customer_requirement", "quotation_rate_card", "general"]:
        count = label_counter.get(lbl, 0)
        confs = conf_by_label.get(lbl, [])
        avg_conf = sum(confs) / len(confs) if confs else 0.0
        print(f"\n{'─'*70}")
        print(f"  {lbl.upper()}  — {count} emails ({count/total*100:.1f}%)  avg conf {avg_conf:.2f}")
        print(f"{'─'*70}")
        samples = by_label.get(lbl, [])[:8]
        for r in samples:
            recv = (r.get("received_at") or "")[:10]
            sender_short = r.get("sender", "")[:40]
            print(f"  [{recv}] {r.get('subject','')[:55]}")
            print(f"           {sender_short}")

    # Top senders per category
    print(f"\n{'─'*70}")
    print("  TOP SENDERS by category")
    print(f"{'─'*70}")
    import re
    def domain(s: str) -> str:
        m = re.search(r"@([\w.]+)", s)
        return m.group(1).lower() if m else "unknown"

    for lbl in ["customer_requirement", "quotation_rate_card"]:
        domains = Counter(domain(r.get("sender","")) for r in by_label.get(lbl, []))
        print(f"\n  {lbl}:")
        for d, c in domains.most_common(5):
            print(f"    {d:<35} {c}")

    print(f"\n{'='*70}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    records = fetch_3months()
    if not records:
        logger.error("No emails fetched — check GMAIL_MAILBOX and service_account.json")
        sys.exit(1)

    # Determine which are new (not yet in emails table)
    msg_ids = [r.get("message_id", "") for r in records]
    already_in_db = _existing_message_ids([m for m in msg_ids if m])
    new_records   = [r for r in records if r.get("message_id", "") not in already_in_db]
    existing_rec  = [r for r in records if r.get("message_id", "") in already_in_db]
    logger.info(
        "Records: %d total / %d new / %d already in DB",
        len(records), len(new_records), len(existing_rec),
    )

    # Classify ALL (new + existing that may lack a classification label)
    existing_email_keys = _existing_classified_ids(
        [r.get("message_id") or r["provider_msg_id"] for r in existing_rec]
    )
    needs_classify = new_records + [
        r for r in existing_rec
        if (r.get("message_id") or r["provider_msg_id"]) not in existing_email_keys
    ]

    logger.info("Classifying %d emails (LLM calls) …", len(needs_classify))
    t0 = time.time()
    classified_new = classify_parallel(needs_classify, workers=5)
    elapsed = time.time() - t0
    logger.info("Classification done in %.1fs", elapsed)

    # Build full classified list (new + previously-classified existing) for analysis
    # For existing-already-classified, fetch labels from cache
    from backend.classifier.classification_cache import get_cached  # noqa: E402
    cached_keys = [r.get("message_id") or r["provider_msg_id"] for r in existing_rec
                   if (r.get("message_id") or r["provider_msg_id"]) in existing_email_keys]
    cache_map = get_cached(cached_keys)
    classified_existing = [
        (r, cache_map.get(r.get("message_id") or r["provider_msg_id"], {}).get("label", "general"),
         cache_map.get(r.get("message_id") or r["provider_msg_id"], {}).get("confidence", 0.0),
         cache_map.get(r.get("message_id") or r["provider_msg_id"], {}).get("method", "cache"))
        for r in existing_rec
        if (r.get("message_id") or r["provider_msg_id"]) in existing_email_keys
    ]
    all_classified = classified_new + classified_existing

    # Print analysis
    print_analysis(all_classified)

    if DRY_RUN:
        logger.info("Dry-run: no DB writes.")
        return

    # Upsert to Supabase
    inserted = updated = failed = 0
    for r, label, conf, method in classified_new:
        uid = upsert_email(r, label, conf, method)
        if uid:
            if r.get("message_id", "") in already_in_db:
                updated += 1
            else:
                inserted += 1
        else:
            failed += 1

    # Advance sync watermark to newest received_at seen
    from backend.connectors.email_store import _advance_watermark, _parse_dt  # noqa: E402
    newest = max(
        (_parse_dt(r.get("received_at")) for r in records if r.get("received_at")),
        default=None,
    )
    if newest:
        _advance_watermark("gmail", newest)
        logger.info("Watermark advanced to %s", newest.isoformat())

    print(f"\n{'='*70}")
    print(f"  SUPABASE WRITE SUMMARY")
    print(f"{'─'*70}")
    print(f"  Inserted (new)    : {inserted}")
    print(f"  Updated (existing): {updated}")
    print(f"  Failed            : {failed}")
    print(f"  Skipped (cached)  : {len(classified_existing)}")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
