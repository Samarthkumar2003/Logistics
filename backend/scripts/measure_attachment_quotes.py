"""
measure_attachment_quotes.py
-----------------------------
Phase 1.5 gate: decide whether vision (Phase 2) is worth building.

Of persisted rate-card emails, how many have NO usable text body but DO carry
an attachment — i.e. their rates would be silently lost by the text-only parser
today. A high ratio justifies building the image/PDF vision path; a low ratio
means defer it (attachments are already stored, so nothing is lost either way).

Run:  python -m backend.scripts.measure_attachment_quotes
"""

import os
import logging

from dotenv import load_dotenv
from supabase import create_client

from backend.core.logging_config import configure_logging

load_dotenv()
configure_logging()  # read-only measurement: console is enough
logger = logging.getLogger(__name__)

supabase = create_client(
    os.environ.get("SUPABASE_URL", ""),
    os.environ.get("SUPABASE_KEY", ""),
)

_BODY_PLACEHOLDER = "[No plain-text body"


def _is_attachment_only(email: dict) -> bool:
    body = (email.get("body") or "").strip()
    empty = (not body) or body.startswith(_BODY_PLACEHOLDER)
    return bool(email.get("has_attachments")) and empty


def main() -> None:
    rows = (
        supabase.table("emails")
        .select("subject, body, has_attachments")
        .eq("classification", "quotation_rate_card")
        .execute()
        .data
        or []
    )

    total = len(rows)
    if total == 0:
        logger.info("No rate-card emails persisted yet. Run ingestion first.")
        return

    attachment_only = [r for r in rows if _is_attachment_only(r)]
    ratio = len(attachment_only) / total

    logger.info("=== Rate-card attachment gate ===")
    logger.info("rate_card_total        : %d", total)
    logger.info("attachment_only_quotes : %d", len(attachment_only))
    logger.info("ratio                  : %.1f%%", ratio * 100)
    logger.info("")
    logger.info("Examples (would be lost by text-only parsing today):")
    for r in attachment_only[:5]:
        logger.info("  - %s", (r.get("subject") or "(no subject)")[:90])
    logger.info("")
    if ratio >= 0.15:
        logger.info("VERDICT: ratio >= 15%% → BUILD vision (Phase 2). These rates are lost today.")
    else:
        logger.info("VERDICT: ratio < 15%% → DEFER vision. Most rate cards parse from text; "
                    "attachments are stored and vision can be added later with zero re-ingest.")


if __name__ == "__main__":
    main()
