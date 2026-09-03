from openai import OpenAI
from pydantic import BaseModel, Field
from typing import List, Optional

from backend.core.rfq_reference import subject_token

_client: Optional[OpenAI] = None


def _get_client() -> OpenAI:
    """Lazy singleton — credential failures surface per-request, not at import."""
    global _client
    if _client is None:
        _client = OpenAI()
    return _client


class DraftEmail(BaseModel):
    vendor_name: str = Field(description="The name of the vendor this email is addressed to.")
    vendor_email: str = Field(default="", description="The vendor email address.")
    subject: str = Field(description="A professional subject line for the email.")
    body: str = Field(description="The drafted email body text.")


class RFQResponse(BaseModel):
    drafts: List[DraftEmail]


def generate_rfq_drafts(shipment_data: dict, agents: list[dict], reference: str) -> RFQResponse:
    agent_names = [a["agent_name"] for a in agents]
    if not agent_names:
        agent_names = ["General Freight Forwarder (Fallback)"]

    agents_str = ", ".join(agent_names)

    # .get(key, default) still returns None when the key holds None — use `or`
    origin = shipment_data.get("origin") or "Unknown"
    destination = shipment_data.get("destination") or "Unknown"
    mode = shipment_data.get("mode") or "sea_freight"

    system_prompt = (
        "You are the RFQ (Request for Quotation) Agent for a logistics company. "
        "Your job is to draft professional, concise emails to freight forwarding vendors asking for a rate quote. "
        "The user will provide SHIPMENT DETAILS and VENDOR CONTEXT. "
        f"Draft a separate email for each of these vendors: {agents_str}. "
        f"Each email subject line MUST follow this format: \"{subject_token(reference)} | Request for Quotation - {mode} {origin} to {destination}\". "
        "Keep the RFQId token exactly as written — it is how the reply gets matched back to this shipment. "
        "Keep the tone professional. Do NOT mention any historical pricing in the email to the vendor."
    )

    agents_context = [
        f"- {a['agent_name']} (specialty: {a.get('specialty', 'N/A')})"
        for a in agents
    ]

    weight = shipment_data.get("weight_kg")
    size = shipment_data.get("size", "")
    commodity = shipment_data.get("commodity") or ""
    user_prompt = (
        f"SHIPMENT DETAILS:\n"
        f"Reference: {reference}\n"
        f"Origin: {origin}\n"
        f"Destination: {destination}\n"
        f"Mode: {mode}\n"
        + (f"Container/Size: {size}\n" if size else "")
        + (f"Weight: {weight} kg\n" if weight is not None else "")
        + (f"Commodity: {commodity}\n" if commodity else "")
        + "\n"
        f"VENDORS TO DRAFT FOR (context only):\n"
        + "\n".join(agents_context)
    )

    completion = _get_client().beta.chat.completions.parse(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        response_format=RFQResponse,
    )
    result = completion.choices[0].message.parsed

    # Programmatically set vendor_email from the agents list by matching vendor_name
    agent_email_map = {a["agent_name"]: a.get("email", "") for a in agents}
    for draft in result.drafts:
        draft.vendor_email = agent_email_map.get(draft.vendor_name, "")

    return result


if __name__ == "__main__":
    mock_intake = {"origin": "Hamburg port", "destination": "Mumbai", "mode": "sea_freight", "weight_kg": 1500.0, "commodity": "spare automotive parts"}
    mock_agents = [
        {"agent_name": "Kuehne+Nagel", "email": "quotes@kuehne-nagel.com", "specialty": "sea_freight"},
        {"agent_name": "DB Schenker", "email": "rfq@dbschenker.com", "specialty": "sea_freight"}
    ]
    mock_reference = "SHP-2026-0042"
    result = generate_rfq_drafts(mock_intake, mock_agents, mock_reference)
    for i, d in enumerate(result.drafts, 1):
        print(f"Draft {i} (To: {d.vendor_name} <{d.vendor_email}>)\nSubject: {d.subject}\n{d.body}\n---")
