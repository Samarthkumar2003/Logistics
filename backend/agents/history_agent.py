"""
History Agent — dual-embedding semantic search over historical shipments.

Two search strategies run in parallel and are merged by weighted score:
  1. cargo_embedding  — commodity-only vector (catches cargo-type similarity)
  2. full_embedding   — full shipment text (route + equipment + vendor + rate context)

Full embedding gets higher weight (0.6) because it encodes route context.
Commodity embedding (0.4) catches cases where route differs but cargo type
is a useful benchmark anyway.
"""
import logging
from typing import Any
from dotenv import load_dotenv
import os
from supabase import create_client, Client
from openai import OpenAI
from backend.core.port_normalizer import normalize_port
from backend.core.retry_utils import with_retry

load_dotenv()
log = logging.getLogger(__name__)

# Lazy singletons — credential failures surface per-request, not at import
# (an import-time raise here would take down the whole API).
_supabase: Client | None = None
_openai: OpenAI | None = None


def _get_supabase() -> Client:
    global _supabase
    if _supabase is None:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_KEY")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set in .env")
        _supabase = create_client(url, key)
    return _supabase


def _get_openai() -> OpenAI:
    global _openai
    if _openai is None:
        _openai = OpenAI()
    return _openai

# Weight applied to each search strategy when merging results
COMMODITY_WEIGHT = 0.4
FULL_WEIGHT = 0.6


@with_retry(max_attempts=3, base_delay=1.0)
def _get_embedding(text: str) -> list[float]:
    response = _get_openai().embeddings.create(
        input=text,
        model="text-embedding-3-small",
    )
    return response.data[0].embedding


def _build_full_query_text(
    origin: str,
    destination: str,
    mode: str,
    commodity_desc: str,
    weight_kg: float | None = None,
) -> str:
    """Build the same rich text format used in seed_real_history.py."""
    parts = [
        f"Origin: {origin}.",
        f"Destination: {destination}.",
        f"Mode: {mode.replace('_', ' ')}.",
    ]
    if weight_kg:
        parts.append(f"Weight: {weight_kg} kg.")
    parts.append(f"Commodity: {commodity_desc}.")
    return " ".join(parts)


def _merge_results(
    commodity_rows: list[dict],
    full_rows: list[dict],
    limit: int,
) -> list[dict]:
    """Merge two result lists by weighted similarity score, deduplicate by id."""
    scores: dict[int, dict] = {}

    for row in commodity_rows:
        rid = row["id"]
        weighted = row["similarity"] * COMMODITY_WEIGHT
        if rid not in scores:
            scores[rid] = {**row, "score": 0.0, "commodity_sim": 0.0, "full_sim": 0.0}
        scores[rid]["score"] += weighted
        scores[rid]["commodity_sim"] = row["similarity"]

    for row in full_rows:
        rid = row["id"]
        weighted = row["similarity"] * FULL_WEIGHT
        if rid not in scores:
            scores[rid] = {**row, "score": 0.0, "commodity_sim": 0.0, "full_sim": 0.0}
        scores[rid]["score"] += weighted
        scores[rid]["full_sim"] = row["similarity"]

    ranked = sorted(scores.values(), key=lambda r: r["score"], reverse=True)

    # Expose merged score as `similarity` so callers need no changes
    for r in ranked:
        r["similarity"] = r["score"]

    return ranked[:limit]


def find_similar_shipments(
    origin: str,
    destination: str,
    mode: str,
    commodity_desc: str,
    weight_kg: float | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """
    Run dual-embedding search and return top-N merged results.

    Args:
        origin: Port/city of origin (e.g. "Mundra Port, India")
        destination: Port/city of destination (e.g. "Jebel Ali, UAE")
        mode: "sea_freight" | "air_freight" | "road"
        commodity_desc: Free-text description of the cargo
        weight_kg: Optional cargo weight for richer full query text
        limit: Max results to return

    Returns:
        List of shipment dicts with a `similarity` field (merged weighted score).
    """
    # ── Normalize inputs before querying ──
    # All intake fields are optional; substitute neutral stand-ins so the
    # search still runs. Empty commodity would crash the embedding call.
    commodity_desc = (commodity_desc or "").strip() or "general cargo"
    mode = (mode or "").strip() or "sea_freight"
    origin = normalize_port(origin or "")
    destination = normalize_port(destination or "")
    log.debug("Normalized: %s → %s", origin, destination)

    # ── Generate both query embeddings ──
    log.debug("Generating commodity embedding for: %s", commodity_desc[:60])
    commodity_vec = _get_embedding(commodity_desc)

    full_query = _build_full_query_text(origin, destination, mode, commodity_desc, weight_kg)
    log.debug("Generating full embedding for: %s...", full_query[:80])
    full_vec = _get_embedding(full_query)

    # ── Search 1: commodity embedding (with structured filters) ──
    commodity_rows: list[dict] = []
    try:
        result = _get_supabase().rpc("match_shipments", {
            "p_origin": origin,
            "p_destination": destination,
            "p_mode": mode,
            "p_embedding": commodity_vec,
            "match_count": limit * 2,  # fetch more before merging
        }).execute()
        commodity_rows = result.data or []
    except Exception as e:
        log.warning("match_shipments RPC failed: %s", e)

    # ── Search 2: full embedding (no structured filters — embedding encodes route) ──
    full_rows: list[dict] = []
    try:
        result = _get_supabase().rpc("match_shipments_full", {
            "p_embedding": full_vec,
            "match_count": limit * 2,
        }).execute()
        full_rows = result.data or []
    except Exception as e:
        log.warning("match_shipments_full RPC failed: %s", e)

    # ── Merge and rank ──
    merged = _merge_results(commodity_rows, full_rows, limit)
    log.info(
        "Dual search: commodity=%d results, full=%d results → merged top %d",
        len(commodity_rows), len(full_rows), len(merged),
    )
    return merged


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    matches = find_similar_shipments(
        origin="Mundra Port, India",
        destination="Jebel Ali, UAE",
        mode="sea_freight",
        commodity_desc="general cargo 20FT FCL",
        weight_kg=18000,
    )
    if not matches:
        print("No historical shipments found.")
    else:
        print("Top matches:")
        for i, m in enumerate(matches, 1):
            print(
                f"  [{i}] {m['commodity']} | Agent: {m['agent_used']} "
                f"| Rate: ${m['rate_paid']} | {m['transit_time_days']}d "
                f"| Score: {m['similarity']:.3f} "
                f"(commodity={m.get('commodity_sim', 0):.2f}, full={m.get('full_sim', 0):.2f})"
            )
