import os
import logging
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from openai import OpenAI
import json
from port_normalizer import normalize_port
from retry_utils import with_retry

log = logging.getLogger(__name__)

VALID_MODES = {"sea_freight", "air_freight", "road"}


class ExtractionError(ValueError):
    """Raised when extracted shipment data fails confidence checks."""

load_dotenv()

client = OpenAI()


class ShipmentDetails(BaseModel):
    origin: str = Field(description="The city or country where the shipment originates.")
    destination: str = Field(description="The city or country where the shipment is heading.")
    weight_kg: float = Field(description="The total weight of the shipment in kilograms. Convert from pounds or tons if necessary.")
    commodity: str = Field(description="The type of goods or cargo being shipped.")
    mode: str = Field(description="The mode of transport, e.g., 'sea_freight', 'air_freight', 'road'. If not perfectly clear, deduce from context or label 'unknown'.")
    destination_country: str = Field(description="The country of the destination. Infer from the destination city/port name if not explicitly stated.")


@with_retry(max_attempts=3, base_delay=1.0)
def run_intake_agent(email_content: str) -> ShipmentDetails:
    completion = client.beta.chat.completions.parse(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are the Intake Agent for a logistics company. Your job is to extract shipment details from customer emails and strictly structure them into the provided schema."},
            {"role": "user", "content": "Please extract the details from this email:\n\n" + email_content}
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
    """Confidence gate — reject extractions with missing or invalid fields."""
    errors: list[str] = []

    if not s.origin or len(s.origin.strip()) < 2:
        errors.append("origin is missing or too short")

    if not s.destination or len(s.destination.strip()) < 2:
        errors.append("destination is missing or too short")

    if s.origin.strip().lower() == s.destination.strip().lower():
        errors.append(f"origin and destination are identical: '{s.origin}'")

    if s.weight_kg <= 0:
        errors.append(f"weight_kg is invalid: {s.weight_kg}")

    if s.mode.lower() not in VALID_MODES:
        log.warning("Unrecognized mode '%s' — defaulting to 'sea_freight'", s.mode)
        s.mode = "sea_freight"

    if not s.commodity or len(s.commodity.strip()) < 3:
        errors.append("commodity is missing or too vague")

    if errors:
        raise ExtractionError(f"Extraction failed validation: {'; '.join(errors)}")


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
