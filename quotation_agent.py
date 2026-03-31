"""
quotation_agent.py
------------------
Parses incoming quotation/rate emails from freight agents into structured data.
"""
import re
import os
import logging
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field
from typing import Optional

load_dotenv()
logger = logging.getLogger(__name__)
client = OpenAI()

RFQ_PATTERN = re.compile(r"RFQ-(\d{8}-[a-f0-9]{4})")


class QuotationDetails(BaseModel):
    rate: float = Field(description="The quoted rate/price for the shipment in the stated currency")
    currency: str = Field(default="USD", description="Currency code, e.g. USD, EUR, INR")
    transit_time_days: int = Field(description="Estimated transit time in days")
    validity: str = Field(default="", description="How long the quote is valid, e.g. '30 days' or a specific date")
    terms: str = Field(default="", description="Any special terms, conditions, incoterms, or notes")


def extract_rfq_reference(subject: str) -> Optional[str]:
    """Extract RFQ reference from email subject line.
    Returns the full reference like 'RFQ-20260323-7f3a' or None.
    """
    match = RFQ_PATTERN.search(subject)
    return match.group(0) if match else None


def parse_quotation_email(email_body: str, email_subject: str = "") -> QuotationDetails:
    """Parse a quotation email into structured data using GPT-4o-mini."""
    full_content = f"Subject: {email_subject}\n\nBody:\n{email_body}"

    completion = client.beta.chat.completions.parse(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": (
                "You are a logistics quotation parser. Extract the rate, currency, "
                "transit time, validity period, and any special terms from the "
                "vendor's quotation email. If a field is not mentioned, use reasonable defaults."
            )},
            {"role": "user", "content": f"Extract quotation details from this email:\n\n{full_content}"}
        ],
        response_format=QuotationDetails,
    )
    return completion.choices[0].message.parsed


if __name__ == "__main__":
    sample = """
    Dear Logistics Team,

    Thank you for your RFQ-20260323-7f3a inquiry.

    We are pleased to quote the following for sea freight Hamburg to Mumbai:

    Rate: USD 2,250 per shipment
    Transit Time: 26-28 days
    Validity: 30 days from date of quotation
    Terms: FOB Hamburg, subject to space availability

    Please let us know if you wish to proceed.

    Best regards,
    Hans Mueller
    Kuehne+Nagel
    """
    result = parse_quotation_email(sample, "Re: RFQ-20260323-7f3a | Request for Quotation")
    print(f"Rate: {result.currency} {result.rate}")
    print(f"Transit: {result.transit_time_days} days")
    print(f"Validity: {result.validity}")
    print(f"Terms: {result.terms}")
