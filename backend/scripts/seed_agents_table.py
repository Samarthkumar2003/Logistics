"""
seed_agents_table.py
--------------------
One-off: seed the Supabase `agents` table from data/agents_database.csv.
Idempotent — upserts on (agent_name, email).

Run: python -m backend.scripts.seed_agents_table
"""
import csv
import logging
import os

from dotenv import load_dotenv
from supabase import create_client

from backend.core.logging_config import configure_logging, default_log_file
from backend.core.paths import PROJECT_ROOT

load_dotenv(PROJECT_ROOT / ".env")
configure_logging(log_file=default_log_file())  # writes agents: keep a record
logger = logging.getLogger(__name__)

AGENTS_CSV = PROJECT_ROOT / "data" / "agents_database.csv"


def seed_agents() -> None:
    supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

    with open(AGENTS_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    records = []
    for r in rows:
        email = (r.get("email") or "").strip()
        if not email:
            logger.warning("Skipping %s — no email", r.get("agent_name"))
            continue
        records.append({
            "agent_name": (r.get("agent_name") or "").strip(),
            "location": (r.get("location") or "").strip(),
            "country": (r.get("country") or "").strip(),
            "email": email,
            "contact_person": (r.get("contact_person") or "").strip(),
            "phone": (r.get("phone") or "").strip(),
            "specialty": (r.get("specialty") or "").strip(),
            "modes": [m.strip() for m in (r.get("modes") or "").split(",") if m.strip()],
            "category": (r.get("category") or "").strip().upper(),
        })

    result = supabase.table("agents").upsert(records, on_conflict="agent_name,email").execute()
    logger.info("Seeded %d agents into Supabase", len(result.data or []))


if __name__ == "__main__":
    seed_agents()
