import csv
import os
import logging
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv
from history_agent import find_similar_shipments

load_dotenv()
logger = logging.getLogger(__name__)

AGENTS_CSV = os.path.join(os.path.dirname(__file__), "agents_database.csv")

# Mapping of well-known logistics cities to their countries.
CITY_TO_COUNTRY = {
    "mumbai": "India",
    "delhi": "India",
    "new delhi": "India",
    "chennai": "India",
    "kolkata": "India",
    "bangalore": "India",
    "nhava sheva": "India",
    "hamburg": "Germany",
    "frankfurt": "Germany",
    "berlin": "Germany",
    "munich": "Germany",
    "bremerhaven": "Germany",
    "shenzhen": "China",
    "shanghai": "China",
    "beijing": "China",
    "guangzhou": "China",
    "hong kong": "China",
    "ningbo": "China",
    "yantian": "China",
    "dubai": "UAE",
    "abu dhabi": "UAE",
    "jebel ali": "UAE",
    "paris": "France",
    "marseille": "France",
    "le havre": "France",
    "lyon": "France",
    "los angeles": "United States",
    "new york": "United States",
    "chicago": "United States",
    "houston": "United States",
    "seattle": "United States",
    "san francisco": "United States",
    "long beach": "United States",
    "savannah": "United States",
    "copenhagen": "Denmark",
    "aarhus": "Denmark",
    "london": "UK",
    "felixstowe": "UK",
    "southampton": "UK",
    "manchester": "UK",
    "singapore": "Singapore",
    "tokyo": "Japan",
    "yokohama": "Japan",
    "osaka": "Japan",
    "nagoya": "Japan",
    "sydney": "Australia",
    "melbourne": "Australia",
    "rotterdam": "Netherlands",
    "amsterdam": "Netherlands",
    "antwerp": "Belgium",
    "busan": "South Korea",
    "seoul": "South Korea",
    "bangkok": "Thailand",
    "ho chi minh city": "Vietnam",
    "jakarta": "Indonesia",
    "kuala lumpur": "Malaysia",
    "port klang": "Malaysia",
    "colombo": "Sri Lanka",
    "karachi": "Pakistan",
    "jeddah": "Saudi Arabia",
    "riyadh": "Saudi Arabia",
    "istanbul": "Turkey",
    "santos": "Brazil",
    "sao paulo": "Brazil",
    "lagos": "Nigeria",
    "mombasa": "Kenya",
    "durban": "South Africa",
    "cape town": "South Africa",
}


@dataclass
class AgentMatch:
    agent_name: str
    email: str
    country: str
    specialty: str
    source: str  # "csv", "history", or "both"
    historical_rate: Optional[float] = None
    historical_transit_days: Optional[int] = None


def _load_agents_csv() -> list[dict]:
    """Load all agents from the CSV file."""
    agents = []
    try:
        with open(AGENTS_CSV, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                agents.append(row)
    except FileNotFoundError:
        logger.error("Agents CSV not found at %s", AGENTS_CSV)
    except Exception as exc:
        logger.error("Failed to load agents CSV: %s", exc)
    return agents


def _extract_country(destination: str) -> str:
    """Extract the country name from a destination string.

    Simple heuristic: take the last comma-separated part and strip it.
    Falls back to the full string if no comma.
    Examples:
      "Mumbai" -> "India" (via mapping)
      "Los Angeles, CA" -> "United States" (via mapping on first part)
      "Paris" -> "France"
    """
    if not destination:
        return ""

    # Try the full string first (lowered) against the mapping.
    lowered = destination.strip().lower()
    if lowered in CITY_TO_COUNTRY:
        return CITY_TO_COUNTRY[lowered]

    # If the string has commas, try the first part (city name).
    parts = [p.strip() for p in destination.split(",")]
    city_candidate = parts[0].lower()
    if city_candidate in CITY_TO_COUNTRY:
        return CITY_TO_COUNTRY[city_candidate]

    # Try the last part as a possible country name.
    last_part = parts[-1].strip()
    if last_part:
        return last_part

    return destination.strip()


def lookup_agents(
    destination: str,
    destination_country: str,
    mode: str,
    commodity_desc: str,
    origin: str,
) -> list[AgentMatch]:
    """Find agents for a shipment from CSV + history.

    1. Load CSV agents, filter by country matching destination_country and mode.
    2. Query history for similar shipments.
    3. Merge: if an agent appears in both, mark as "both" and attach historical data.
    4. For history-only agents, look up their email from CSV.
    5. Return merged list sorted by source quality (both > history > csv).
    """
    # Resolve the country -- the intake agent's destination_country takes priority.
    country = destination_country.strip() if destination_country else ""
    if not country:
        country = _extract_country(destination)
    logger.info(
        "Looking up agents for destination=%s, country=%s, mode=%s",
        destination,
        country,
        mode,
    )

    # -- Step 1: CSV agents filtered by country and mode -----------------------
    all_csv_agents = _load_agents_csv()
    csv_matches: dict[str, dict] = {}
    for agent in all_csv_agents:
        agent_country = agent.get("country", "").strip()
        agent_modes = [m.strip() for m in agent.get("modes", "").split(",")]
        country_match = agent_country.lower() == country.lower() if country else False
        mode_match = mode.lower() in [m.lower() for m in agent_modes] if mode else True
        if country_match and mode_match:
            csv_matches[agent["agent_name"]] = agent

    logger.info("CSV matches for %s / %s: %d", country, mode, len(csv_matches))

    # -- Step 2: Historical similar shipments ----------------------------------
    history_agents: dict[str, dict] = {}
    try:
        similar = find_similar_shipments(
            origin=origin,
            destination=destination,
            mode=mode,
            commodity_desc=commodity_desc,
        )
        for record in similar:
            agent_name = record.get("agent_used", "")
            if agent_name:
                history_agents[agent_name] = {
                    "rate": record.get("rate_paid"),
                    "transit_days": record.get("transit_time_days"),
                }
    except Exception as exc:
        logger.warning("History lookup failed, proceeding with CSV only: %s", exc)

    logger.info("History matches: %d", len(history_agents))

    # -- Step 3 & 4: Merge results ---------------------------------------------
    # Build a quick lookup of all CSV agents (not just filtered) for email resolution.
    all_csv_by_name: dict[str, dict] = {}
    for agent in all_csv_agents:
        # Keep the first entry per name (could have multiple country offices).
        if agent["agent_name"] not in all_csv_by_name:
            all_csv_by_name[agent["agent_name"]] = agent

    merged: dict[str, AgentMatch] = {}

    # Add CSV-filtered agents first.
    for name, agent in csv_matches.items():
        source = "both" if name in history_agents else "csv"
        hist = history_agents.get(name, {})
        merged[name] = AgentMatch(
            agent_name=name,
            email=agent.get("email", ""),
            country=agent.get("country", ""),
            specialty=agent.get("specialty", ""),
            source=source,
            historical_rate=hist.get("rate"),
            historical_transit_days=hist.get("transit_days"),
        )

    # Add history-only agents (not already in CSV-filtered set).
    for name, hist in history_agents.items():
        if name in merged:
            continue
        csv_info = all_csv_by_name.get(name, {})
        merged[name] = AgentMatch(
            agent_name=name,
            email=csv_info.get("email", ""),
            country=csv_info.get("country", country),
            specialty=csv_info.get("specialty", ""),
            source="history",
            historical_rate=hist.get("rate"),
            historical_transit_days=hist.get("transit_days"),
        )

    # -- Step 5: Sort by quality and return ------------------------------------
    source_priority = {"both": 0, "history": 1, "csv": 2}
    results = sorted(
        merged.values(),
        key=lambda m: source_priority.get(m.source, 3),
    )

    logger.info("Total agents matched: %d", len(results))
    for match in results:
        logger.debug(
            "  %s [%s] source=%s rate=%s transit=%s",
            match.agent_name,
            match.country,
            match.source,
            match.historical_rate,
            match.historical_transit_days,
        )

    return results
