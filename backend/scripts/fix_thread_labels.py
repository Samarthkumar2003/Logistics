"""
fix_thread_labels.py
--------------------
One-off cleanup: within each thread, keep only the EARLIEST email whose
EFFECTIVE label is customer_requirement; downgrade every later one to general.

Effective label = email_classifications cache (what /fetch-inbox shows,
keyed by provider_msg_id) falling back to emails.classification. Both stores
are updated so the UI and the pipeline agree afterwards.

Run: python -m backend.scripts.fix_thread_labels
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
    """Page past Supabase's 1000-row cap."""
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


def _set_label(supabase, email_row: dict, label: str) -> None:
    """Write one label into BOTH stores (emails + classification cache,
    under both id key spaces) so UI and pipeline can't disagree."""
    supabase.table("emails").update({"classification": label}).eq("id", email_row["id"]).execute()
    for cache_key in (email_row.get("provider_msg_id"), email_row.get("message_id")):
        if cache_key:
            supabase.table("email_classifications").upsert({
                "email_id": cache_key,
                "label": label,
                "confidence": 1.0,
                "method": "thread_rule",
            }, on_conflict="email_id").execute()


def fix_thread_labels() -> None:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise SystemExit("SUPABASE_URL and SUPABASE_KEY must be set in .env")
    supabase = create_client(url, key)

    emails = _fetch_all(
        supabase, "emails",
        "id, provider_msg_id, message_id, thread_id, received_at, subject, classification",
    )
    cache_rows = _fetch_all(supabase, "email_classifications", "email_id, label")
    cache = {r["email_id"]: r["label"] for r in cache_rows}
    logger.info("Loaded %d emails, %d cached labels", len(emails), len(cache))

    def effective_label(e: dict) -> str:
        return (
            cache.get(e.get("provider_msg_id"))
            or cache.get(e.get("message_id"))
            or e.get("classification")
            or "general"
        )

    by_thread: dict[str, list[dict]] = defaultdict(list)
    for e in emails:
        if effective_label(e) == CR:
            by_thread[e.get("thread_id") or e["id"]].append(e)

    downgraded = kept = 0
    for thread_id, cr_emails in by_thread.items():
        cr_emails.sort(key=lambda e: e.get("received_at") or "")
        # Earliest request keeps the label — make both stores agree on it
        _set_label(supabase, cr_emails[0], CR)
        kept += 1
        for reply in cr_emails[1:]:
            _set_label(supabase, reply, "general")
            downgraded += 1
            logger.info("Downgraded reply in thread %s: %s", thread_id, (reply.get("subject") or "")[:60])

    logger.info("Done — %d replies downgraded, %d threads kept their original request", downgraded, kept)


if __name__ == "__main__":
    fix_thread_labels()
