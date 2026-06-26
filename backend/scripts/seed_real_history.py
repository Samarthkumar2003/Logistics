"""
Seed real historical shipment data extracted from 10 client RFQ + vendor rate card email pairs.
Routes: India/UAE/Saudi Arabia corridors — the actual lanes this logistics copilot serves.
"""
import os
import logging
from dotenv import load_dotenv
from supabase import create_client, Client
from openai import OpenAI

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

url = os.environ["SUPABASE_URL"]
key = os.environ["SUPABASE_KEY"]
supabase: Client = create_client(url, key)
openai_client = OpenAI()

# ── Real shipment pairs extracted from client RFQ + vendor rate card emails ──
# Each record represents a completed shipment with known outcome.
# weight_kg estimated from container type (20GP≈18000kg, 40GP≈26000kg, air pallet≈500kg)
REAL_HISTORY: list[dict] = [
    # 1. Air import: Sharjah → Bangalore (water pump, 1 pallet)
    {
        "origin": "Sharjah, UAE",
        "destination": "Bangalore, India",
        "mode": "air_freight",
        "weight_kg": 520,
        "commodity": "industrial water pump, 1 pallet, import air freight",
        "agent_used": "Falcon Freight LLC",
        "rate_paid": 3200,
        "transit_time_days": 3,
    },
    # 2. Sea FCL export: India → Khorfakkan (general cargo, 1×20FT, EXW)
    {
        "origin": "Nhava Sheva, India",
        "destination": "Khorfakkan, UAE",
        "mode": "sea_freight",
        "weight_kg": 17500,
        "commodity": "general cargo, 1x20FT container, EXW, export to UAE",
        "agent_used": "Gulf Bridge Logistics",
        "rate_paid": 950,
        "transit_time_days": 12,
    },
    # 3. Sea FCL export: Mundra → Jeddah (foodstuff, 16×20GP, FOB)
    {
        "origin": "Mundra Port, India",
        "destination": "Jeddah, Saudi Arabia",
        "mode": "sea_freight",
        "weight_kg": 288000,
        "commodity": "foodstuff, 16x20GP containers, FOB Mundra, bulk food export to Saudi Arabia",
        "agent_used": "Arabian Freight Services",
        "rate_paid": 18400,
        "transit_time_days": 18,
    },
    # 4. Sea FCL export: Coimbatore → Jeddah (aluminum pipes, 1×20FT, EXW)
    {
        "origin": "Coimbatore, India",
        "destination": "Jeddah, Saudi Arabia",
        "mode": "sea_freight",
        "weight_kg": 16000,
        "commodity": "aluminum pipes and tubes, 1x20FT container, EXW Coimbatore",
        "agent_used": "Trans Arabia Cargo",
        "rate_paid": 1150,
        "transit_time_days": 20,
    },
    # 5. Sea FCL export: Mundra → Jebel Ali (general, 20FT or 40FT)
    {
        "origin": "Mundra Port, India",
        "destination": "Jebel Ali, UAE",
        "mode": "sea_freight",
        "weight_kg": 18000,
        "commodity": "general cargo, 20FT FCL, Mundra to Jebel Ali",
        "agent_used": "Falcon Freight LLC",
        "rate_paid": 750,
        "transit_time_days": 8,
    },
    # 6. Sea FCL export: Lucknow → Jebel Ali / Khorfakkan (1×20FT)
    {
        "origin": "Lucknow, India",
        "destination": "Jebel Ali, UAE",
        "mode": "sea_freight",
        "weight_kg": 15000,
        "commodity": "general cargo, 1x20FT, inland origin Lucknow to UAE port",
        "agent_used": "Skyways Freight India",
        "rate_paid": 1300,
        "transit_time_days": 14,
    },
    # 7. Sea OOG export: Tamil Nadu → UAE (2×40FR flat rack, EXW)
    {
        "origin": "Chennai, Tamil Nadu, India",
        "destination": "Dubai, UAE",
        "mode": "sea_freight",
        "weight_kg": 42000,
        "commodity": "out of gauge machinery, 2x40FR flat rack containers, EXW Tamil Nadu, OOG cargo",
        "agent_used": "Heavy Cargo Solutions UAE",
        "rate_paid": 8600,
        "transit_time_days": 16,
    },
    # 8. Sea FCL export: Mundra → Jeddah (rice, 1×20GP, FOB)
    {
        "origin": "Mundra Port, India",
        "destination": "Jeddah, Saudi Arabia",
        "mode": "sea_freight",
        "weight_kg": 20000,
        "commodity": "basmati rice, 1x20GP container, FOB Mundra, food grain export",
        "agent_used": "Arabian Freight Services",
        "rate_paid": 1050,
        "transit_time_days": 18,
    },
    # 9. Sea FCL export: Cochin → Jebel Ali (rice, 2×20GP, FOB)
    {
        "origin": "Cochin, India",
        "destination": "Jebel Ali, UAE",
        "mode": "sea_freight",
        "weight_kg": 40000,
        "commodity": "rice, 2x20GP containers, FOB Cochin, bulk grain export to UAE",
        "agent_used": "Gulf Bridge Logistics",
        "rate_paid": 1800,
        "transit_time_days": 10,
    },
    # 10. Sea LCL export: India ports → UAE / Saudi Arabia (mixed LCL)
    {
        "origin": "Nhava Sheva, India",
        "destination": "Dubai, UAE",
        "mode": "sea_freight",
        "weight_kg": 3500,
        "commodity": "mixed LCL consolidated cargo, various commodities, India to UAE and Saudi Arabia",
        "agent_used": "Consolidated Cargo India",
        "rate_paid": 1400,
        "transit_time_days": 15,
    },
]


def get_embedding(text: str) -> list[float]:
    response = openai_client.embeddings.create(
        input=text,
        model="text-embedding-3-small",
    )
    return response.data[0].embedding


def build_full_text(item: dict) -> str:
    """Build a rich text representation of a shipment for full embedding.

    Captures route, equipment, commodity, incoterm context, vendor, and rate
    so the vector encodes the complete shipment profile — not just cargo type.
    """
    return (
        f"Origin: {item['origin']}. "
        f"Destination: {item['destination']}. "
        f"Mode: {item['mode'].replace('_', ' ')}. "
        f"Weight: {item['weight_kg']} kg. "
        f"Commodity: {item['commodity']}. "
        f"Agent: {item['agent_used']}. "
        f"Rate paid: USD {item['rate_paid']}. "
        f"Transit time: {item['transit_time_days']} days."
    )


def main() -> None:
    log.info("Seeding %d real historical shipments into Supabase...", len(REAL_HISTORY))

    records: list[dict] = []
    for item in REAL_HISTORY:
        # ── Embedding 1: commodity-only (short, catches cargo-type similarity) ──
        log.info("Commodity embedding: %s", item["commodity"][:60])
        cargo_embedding = get_embedding(item["commodity"])

        # ── Embedding 2: full shipment text (route+weight+vendor+rate context) ──
        full_text = build_full_text(item)
        log.info("Full embedding: %s...", full_text[:80])
        full_embedding = get_embedding(full_text)

        records.append({
            **item,
            "cargo_embedding": cargo_embedding,
            "full_embedding": full_embedding,
        })

    log.info("Uploading %d records...", len(records))
    response = supabase.table("shipments").insert(records).execute()

    if hasattr(response, "data") and response.data:
        log.info("Seeded %d records successfully.", len(response.data))
    else:
        log.warning("Insert returned no data — check Supabase dashboard to verify.")


if __name__ == "__main__":
    main()
