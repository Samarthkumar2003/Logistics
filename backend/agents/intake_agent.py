import os
import logging
from dotenv import load_dotenv
from typing import Optional
from pydantic import BaseModel, Field
from openai import OpenAI
import json
from backend.core.port_normalizer import normalize_port
from backend.core.retry_utils import with_retry

log = logging.getLogger(__name__)

VALID_MODES = {"sea_freight", "air_freight", "road"}


class ExtractionError(ValueError):
    """Raised when extracted shipment data fails confidence checks."""

load_dotenv()

_client: Optional[OpenAI] = None


def _get_client() -> OpenAI:
    """Lazy singleton — a missing OPENAI_API_KEY fails the request that needs
    it (clean 4xx/5xx), not the module import (dead server)."""
    global _client
    if _client is None:
        _client = OpenAI()
    return _client


class ShipmentDetails(BaseModel):
    origin: Optional[str] = Field(default=None, description="The exact port, city, or location stated in the email as the shipment origin. Do NOT substitute, normalize, or replace with a nearby city. Copy verbatim. If not stated, return null.")
    destination: Optional[str] = Field(default=None, description="The exact port, city, or location stated in the email as the shipment destination. Do NOT substitute, normalize, or replace with a nearby city. Copy verbatim. If not stated, return null.")
    weight_kg: Optional[float] = Field(default=None, description="Total shipment weight in kg. Convert from lbs/tons if a unit is explicitly stated. If weight is NOT mentioned anywhere in the email, return null — do NOT invent or estimate a value.")
    commodity: Optional[str] = Field(default=None, description="The type of goods or cargo being shipped. If the email does not state what the cargo is, return null — do NOT guess.")
    mode: Optional[str] = Field(default=None, description="The mode of transport: 'sea_freight', 'air_freight', or 'road'. Deduce from context (e.g. containers/POL imply sea). If genuinely unclear, return null — do NOT guess.")
    destination_country: Optional[str] = Field(default=None, description="The country of the destination. Infer from the destination city/port name if not explicitly stated. If unknown, return null.")


@with_retry(max_attempts=3, base_delay=1.0)
def run_intake_agent(email_content: str) -> ShipmentDetails:
    completion = _get_client().beta.chat.completions.parse(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are the Intake Agent for a logistics company. "
                    "Extract shipment details from customer emails and structure them into the provided schema. "
                    "STRICT RULES — violations cause downstream errors:\n"
                    "1. LOCATIONS: Copy origin and destination EXACTLY as written in the email. "
                    "Never substitute a nearby city, canonical port name, or assumed location. "
                    "If the email says 'Tema', output 'Tema' — not 'Accra' or 'Tema Port, Ghana'.\n"
                    "2. WEIGHT: Only populate weight_kg if a weight figure with a unit is explicitly stated in the email. "
                    "Do NOT estimate weight from container count, cargo type, or any other inference. "
                    "If weight is not mentioned, return null.\n"
                    "3. MISSING FIELDS: Never invent, estimate, or infer values for fields not present in the email. "
                    "Return null for optional fields that are absent."
                ),
            },
            {"role": "user", "content": "Extract the shipment details from this email:\n\n" + email_content}
        ],
        response_format=ShipmentDetails,
    )
    result = completion.choices[0].message.parsed
    # Normalize port names immediately after extraction
    result.origin = normalize_port(result.origin)
    result.destination = normalize_port(result.destination)
    _validate_extraction(result)
    return result


def _validate_extraction(s: ShipmentDetails) -> None:
    """Sanitize extracted fields. All fields are optional — invalid or noisy
    values are coerced to None instead of raising. Callers that require
    specific fields (e.g. the RFQ pipeline needs origin/destination) enforce
    that at their own boundary."""
    if s.origin is not None and len(s.origin.strip()) < 2:
        s.origin = None

    if s.destination is not None and len(s.destination.strip()) < 2:
        s.destination = None

    if (s.origin and s.destination
            and s.origin.strip().lower() == s.destination.strip().lower()):
        log.warning("Origin and destination identical (%r) — clearing destination", s.origin)
        s.destination = None

    if s.weight_kg is not None and s.weight_kg <= 0:
        s.weight_kg = None

    if s.mode is not None and s.mode.lower() not in VALID_MODES:
        log.warning("Unrecognized mode %r — clearing", s.mode)
        s.mode = None

    if s.commodity is not None and len(s.commodity.strip()) < 3:
        s.commodity = None

    if s.destination_country is not None and not s.destination_country.strip():
        s.destination_country = None


if __name__ == "__main__":
    sample_email = """
    Hi team,
    We need a quotation for an upcoming shipment next Tuesday.
    We are moving 12 pallets of spare automotive parts. The total weight is roughly 1500 kg.
    Please quote us for sea freight from Hamburg port to Mumbai.
    Thanks, Logistics Manager
    """
    if not os.getenv("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY not found in .env file.")
        exit(1)
    result = run_intake_agent(sample_email)
    print(json.dumps(result.model_dump(), indent=4))
