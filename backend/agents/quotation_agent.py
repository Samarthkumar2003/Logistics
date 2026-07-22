"""
quotation_agent.py
------------------
Parses incoming quotation/rate emails from freight agents into structured data.
Extracts ALL rates from a single email — one QuotationDetails per container type,
service level, or route variant.
"""
import re
import logging
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field
from typing import Optional

load_dotenv()
logger = logging.getLogger(__name__)

_client: Optional[OpenAI] = None


def _get_client() -> OpenAI:
    """Lazy singleton — credential failures surface per-request, not at import."""
    global _client
    if _client is None:
        _client = OpenAI()
    return _client

RFQ_PATTERN = re.compile(r"RFQ-(\d{8}-[a-f0-9]{4})")


class QuotationDetails(BaseModel):
    rate: float = Field(description="The quoted rate/price for this line item in the stated currency")
    currency: str = Field(default="USD", description="Currency code, e.g. USD, EUR, INR")
    transit_time_days: int = Field(description="Estimated transit time in days")
    validity: str = Field(default="", description="How long the quote is valid, e.g. '30 days' or a specific date")
    terms: str = Field(default="", description="Any special terms, conditions, incoterms, or notes")
    rate_label: str = Field(
        default="",
        description=(
            "Short label identifying this rate line, e.g. '20FT FCL', '40FT HC', "
            "'Air Freight', 'LCL per CBM', 'Base Rate'. Use empty string if only one rate."
        ),
    )


class QuotationList(BaseModel):
    quotations: list[QuotationDetails] = Field(
        description=(
            "All distinct rate line items from the email. "
            "One entry per container type, service level, or route variant. "
            "If only one rate is mentioned, return a list with one item."
        )
    )


def extract_rfq_reference(subject: str) -> Optional[str]:
    """Extract RFQ reference from email subject line.
    Returns the full reference like 'RFQ-20260323-7f3a' or None.
    """
    match = RFQ_PATTERN.search(subject)
    return match.group(0) if match else None


def parse_quotation_email(email_body: str, email_subject: str = "") -> list[QuotationDetails]:
    """Parse a quotation email into a list of structured rate records.

    Returns one QuotationDetails per distinct rate line item in the email.
    Single-rate emails return a list with one item.
    """
    full_content = f"Subject: {email_subject}\n\nBody:\n{email_body}"

    completion = _get_client().beta.chat.completions.parse(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a logistics quotation parser. "
                    "Extract EVERY distinct rate from the vendor email as a separate item. "
                    "Common patterns: multiple container types (20FT, 40FT, 40HC), "
                    "LCL per CBM vs FCL, air vs sea, different service levels. "
                    "For each rate line, extract: rate amount, currency, transit time, "
                    "validity, terms, and a short label identifying the rate type. "
                    "If only one rate is present, return a list with one item. "
                    "Use reasonable defaults for missing fields."
                ),
            },
            {
                "role": "user",
                "content": f"Extract all quotation rates from this email:\n\n{full_content}",
            },
        ],
        response_format=QuotationList,
    )
    result = completion.choices[0].message.parsed
    return result.quotations if result and result.quotations else []


if __name__ == "__main__":
    sample = """
    Dear Logistics Team,

    Thank you for your RFQ-20260323-7f3a inquiry.

    We are pleased to quote the following for sea freight Hamburg to Mumbai:

    20FT FCL: USD 2,250 per container
    40FT FCL: USD 3,800 per container
    40FT HC:  USD 4,100 per container

    Transit Time: 26-28 days
    Validity: 30 days from date of quotation
    Terms: FOB Hamburg, subject to space availability

    Please let us know if you wish to proceed.

    Best regards,
    Hans Mueller
    Kuehne+Nagel
    """
    rates = parse_quotation_email(sample, "Re: RFQ-20260323-7f3a | Request for Quotation")
    print(f"Extracted {len(rates)} rate(s):")
    for r in rates:
        print(f"  [{r.rate_label}] {r.currency} {r.rate} | {r.transit_time_days}d | {r.validity}")
