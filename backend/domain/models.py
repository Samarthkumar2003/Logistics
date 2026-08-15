"""
models.py
---------
The shapes this backend passes around internally.

Repositories return these; services operate on them; only the HTTP layer turns
them into Pydantic responses. The point is that `job.weight_kg` either exists or
doesn't — where previously every caller wrote `r.get("shipment_weight_kg", 0)`
and re-guessed the schema, sometimes with a different default.

Every `from_row` is tolerant: Supabase returns partial rows whenever a query
selects a subset of columns, so a missing key is normal, not an error.
"""

from dataclasses import dataclass, field
from typing import Any, Optional

# Labels the classifier can assign. `pending` is not one of them — it is a
# processing state, surfaced separately.
LABEL_CUSTOMER_REQUIREMENT = "customer_requirement"
LABEL_RATE_CARD = "quotation_rate_card"
LABEL_GENERAL = "general"

# Job statuses that can still receive a reply.
OPEN_JOB_STATUSES = ("rfqs_sent", "quotes_received")


def _s(row: dict, key: str, default: str = "") -> str:
    value = row.get(key)
    return default if value is None else str(value)


@dataclass
class Email:
    """One ingested message. `id` is the Gmail message id (provider_msg_id) —
    the key everything else in the system joins on."""
    id: str
    sender: str = ""
    subject: str = ""
    body: str = ""
    received_at: Optional[str] = None
    thread_id: str = ""
    classification: str = ""
    classification_status: str = ""
    rfq_reference: Optional[str] = None
    has_attachments: bool = False
    row_id: Optional[str] = None  # the Postgres uuid; only the scan needs it

    @classmethod
    def from_row(cls, row: dict) -> "Email":
        return cls(
            id=_s(row, "provider_msg_id"),
            sender=_s(row, "sender"),
            subject=_s(row, "subject"),
            body=_s(row, "body"),
            received_at=row.get("received_at"),
            thread_id=_s(row, "thread_id"),
            classification=_s(row, "classification"),
            classification_status=_s(row, "classification_status"),
            rfq_reference=row.get("rfq_reference"),
            has_attachments=bool(row.get("has_attachments")),
            row_id=row.get("id"),
        )

    @property
    def is_rate_card(self) -> bool:
        return self.classification == LABEL_RATE_CARD

    @property
    def is_linked(self) -> bool:
        return bool(self.rfq_reference)


@dataclass
class RfqJob:
    """One RFQ sent to one agent. A customer request fans out into several of
    these, grouped by `customer_email_id`."""
    reference: str
    status: str = ""
    agents_contacted: list[str] = field(default_factory=list)
    customer_email_id: Optional[str] = None
    customer_thread_id: Optional[str] = None
    customer_sender: str = ""
    customer_subject: str = ""
    customer_body: str = ""
    origin: str = ""
    destination: str = ""
    mode: str = ""
    commodity: str = ""
    size: str = ""
    weight_kg: Optional[float] = None
    created_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: dict) -> "RfqJob":
        return cls(
            reference=_s(row, "reference"),
            status=_s(row, "status"),
            agents_contacted=list(row.get("agents_contacted") or []),
            customer_email_id=row.get("customer_email_id"),
            customer_thread_id=row.get("customer_thread_id"),
            customer_sender=_s(row, "customer_email_sender"),
            customer_subject=_s(row, "customer_email_subject"),
            customer_body=_s(row, "customer_email_body"),
            origin=_s(row, "shipment_origin"),
            destination=_s(row, "shipment_destination"),
            mode=_s(row, "shipment_mode"),
            commodity=_s(row, "shipment_commodity"),
            size=_s(row, "shipment_size"),
            weight_kg=row.get("shipment_weight_kg"),
            created_at=row.get("created_at"),
        )

    @property
    def agent_name(self) -> str:
        """One job is one agent — the list is a legacy of the old fan-out shape."""
        return self.agents_contacted[0] if self.agents_contacted else ""

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_JOB_STATUSES

    def shipment_dict(self) -> dict[str, Any]:
        """The loose shape the LLM agents still expect."""
        return {
            "origin": self.origin,
            "destination": self.destination,
            "mode": self.mode,
            "weight_kg": self.weight_kg,
            "commodity": self.commodity,
            "size": self.size,
        }


@dataclass
class AgentContact:
    """A freight agent's office. The CSV is one row *per office*, so the
    identity is (name, email) — never the name alone."""
    agent_name: str
    email: str
    location: str = ""
    country: str = ""
    specialty: str = ""
    category: str = ""
    contact_person: str = ""

    @classmethod
    def from_row(cls, row: dict) -> "AgentContact":
        return cls(
            agent_name=_s(row, "agent_name"),
            email=_s(row, "email").lower(),
            location=_s(row, "location"),
            country=_s(row, "country"),
            specialty=_s(row, "specialty"),
            category=_s(row, "category"),
            contact_person=_s(row, "contact_person"),
        )

    @property
    def key(self) -> tuple[str, str]:
        return (self.agent_name, self.email)


@dataclass
class Attachment:
    id: str
    file_name: str = ""
    mime_type: str = ""
    size_bytes: Optional[int] = None
    storage_path: str = ""
    url: str = ""

    @classmethod
    def from_row(cls, row: dict) -> "Attachment":
        return cls(
            id=_s(row, "id"),
            file_name=_s(row, "file_name"),
            mime_type=_s(row, "mime_type"),
            size_bytes=row.get("size_bytes"),
            storage_path=_s(row, "storage_path"),
        )
