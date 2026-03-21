from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field
from typing import List

load_dotenv()
client = OpenAI()


class DraftEmail(BaseModel):
    vendor_name: str = Field(description="The name of the vendor this email is addressed to.")
    subject: str = Field(description="A professional subject line for the email.")
    body: str = Field(description="The drafted email body text.")


class RFQResponse(BaseModel):
    drafts: List[DraftEmail]


def generate_rfq_drafts(shipment_data: dict, history_matches: list) -> RFQResponse:
    unique_agents = list(set([m['agent_used'] for m in history_matches if m.get('agent_used')]))
    if not unique_agents:
        unique_agents = ["General Freight Forwarder (Fallback)"]

    agents_str = ", ".join(unique_agents)

    system_prompt = (
        "You are the RFQ (Request for Quotation) Agent for a logistics company. "
        "Your job is to draft professional, concise emails to freight forwarding vendors asking for a rate quote. "
        "The user will provide SHIPMENT DETAILS and HISTORICAL CONTEXT. "
        f"Draft a separate email for each of these vendors: {agents_str}. "
        "Keep the tone professional. Do NOT mention the historical price in the email to the vendor."
    )

    user_prompt = (
        f"SHIPMENT DETAILS:\n"
        f"Origin: {shipment_data.get('origin', 'Unknown')}\n"
        f"Destination: {shipment_data.get('destination', 'Unknown')}\n"
        f"Mode: {shipment_data.get('mode', 'Unknown')}\n"
        f"Weight: {shipment_data.get('weight_kg', 'Unknown')} kg\n"
        f"Commodity: {shipment_data.get('commodity', 'Unknown')}\n\n"
        f"HISTORICAL MATCHES (context only):\n{history_matches}"
    )

    completion = client.beta.chat.completions.parse(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        response_format=RFQResponse,
    )
    return completion.choices[0].message.parsed


if __name__ == "__main__":
    mock_intake = {"origin": "Hamburg port", "destination": "Mumbai", "mode": "sea_freight", "weight_kg": 1500.0, "commodity": "spare automotive parts"}
    mock_history = [
        {"commodity": "automotive engine blocks", "agent_used": "Kuehne+Nagel", "rate_paid": 2100.0, "transit_time_days": 28},
        {"commodity": "rubber tires and suspension parts", "agent_used": "DB Schenker", "rate_paid": 1200.0, "transit_time_days": 30}
    ]
    result = generate_rfq_drafts(mock_intake, mock_history)
    for i, d in enumerate(result.drafts, 1):
        print(f"Draft {i} (To: {d.vendor_name})\nSubject: {d.subject}\n{d.body}\n---")
