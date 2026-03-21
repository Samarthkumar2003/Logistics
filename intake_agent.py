import os
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from openai import OpenAI
import json

load_dotenv()

client = OpenAI()


class ShipmentDetails(BaseModel):
    origin: str = Field(description="The city or country where the shipment originates.")
    destination: str = Field(description="The city or country where the shipment is heading.")
    weight_kg: float = Field(description="The total weight of the shipment in kilograms. Convert from pounds or tons if necessary.")
    commodity: str = Field(description="The type of goods or cargo being shipped.")
    mode: str = Field(description="The mode of transport, e.g., 'sea_freight', 'air_freight', 'road'. If not perfectly clear, deduce from context or label 'unknown'.")


def run_intake_agent(email_content: str) -> ShipmentDetails:
    completion = client.beta.chat.completions.parse(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are the Intake Agent for a logistics company. Your job is to extract shipment details from customer emails and strictly structure them into the provided schema."},
            {"role": "user", "content": "Please extract the details from this email:\n\n" + email_content}
        ],
        response_format=ShipmentDetails,
    )
    return completion.choices[0].message.parsed


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
