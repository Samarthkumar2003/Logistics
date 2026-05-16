"""
backfill_embeddings.py
----------------------
Backfills full_embedding for any shipments rows that have cargo_embedding
but no full_embedding (i.e. records seeded before dual-embedding was added).

Run once:
    python backfill_embeddings.py
"""
import logging
import os
from dotenv import load_dotenv
from supabase import create_client, Client
from openai import OpenAI

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

supabase: Client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
openai_client = OpenAI()


def get_embedding(text: str) -> list[float]:
    response = openai_client.embeddings.create(
        input=text,
        model="text-embedding-3-small",
    )
    return response.data[0].embedding


def build_full_text(row: dict) -> str:
    return (
        f"Origin: {row.get('origin', '')}. "
        f"Destination: {row.get('destination', '')}. "
        f"Mode: {str(row.get('mode', '')).replace('_', ' ')}. "
        f"Weight: {row.get('weight_kg', '')} kg. "
        f"Commodity: {row.get('commodity', '')}. "
        f"Agent: {row.get('agent_used', '')}. "
        f"Rate paid: USD {row.get('rate_paid', '')}. "
        f"Transit time: {row.get('transit_time_days', '')} days."
    )


def main() -> None:
    # Fetch rows missing full_embedding
    result = (
        supabase.table("shipments")
        .select("id, origin, destination, mode, weight_kg, commodity, agent_used, rate_paid, transit_time_days")
        .is_("full_embedding", "null")
        .execute()
    )
    rows = result.data or []

    if not rows:
        log.info("All rows already have full_embedding. Nothing to do.")
        return

    log.info("Found %d rows missing full_embedding.", len(rows))

    updated = 0
    failed = 0
    for row in rows:
        try:
            full_text = build_full_text(row)
            embedding = get_embedding(full_text)
            supabase.table("shipments").update({"full_embedding": embedding}).eq("id", row["id"]).execute()
            log.info("  Updated id=%s (%s → %s)", row["id"], row.get("origin"), row.get("destination"))
            updated += 1
        except Exception as e:
            log.error("  Failed id=%s: %s", row["id"], e)
            failed += 1

    log.info("Done. Updated=%d, Failed=%d", updated, failed)


if __name__ == "__main__":
    main()
