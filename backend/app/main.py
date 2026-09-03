"""
main.py
-------
One-shot CLI walk of the pipeline over the most recent inbox mail, for local
debugging. Read-only: it prints drafts and never sends anything.

    python -m backend.app.main
"""

import json
import uuid
from datetime import datetime

from backend.connectors.email_connector import fetch_latest_emails
from backend.agents.intake_agent import run_intake_agent, ShipmentDetails
from backend.agents.rfq_agent import generate_rfq_drafts, RFQResponse
from backend.repositories import agent_repo

# How many agents to draft against. In the real flow the operator chooses from
# the full list on the send-request page; this CLI just takes the first few so
# there is something to draft to.
DRAFT_AGENT_LIMIT = 2


def process_inbox(limit: int = 4) -> None:
    print("🚀 Starting the Logistics Copilot Pipeline...\n")
    try:
        # fetch_latest_emails returns {"emails": [...], "total": n}
        emails = fetch_latest_emails(limit=limit).get("emails", [])

        if not emails:
            print("📭 Inbox is empty or no emails found.")
            return

        print(f"\n📥 Retrieved {len(emails)} emails. Processing via OpenAI...\n")
        print("=" * 60)

        for i, email_data in enumerate(emails, 1):
            print(f"\n--- 📦 Processing Shipment Request {i} ---")
            print(f"From: {email_data['from']}")
            print(f"Subject: {email_data['subject']}")
            print("-" * 30)

            # Step 1: Intake Agent — parse email into structured data
            full_email_content = f"Subject: {email_data['subject']}\n\nBody:\n{email_data['body']}"
            extracted: ShipmentDetails = run_intake_agent(full_email_content)

            print("\n✅ Intake Agent Extracted:")
            print(json.dumps(extracted.model_dump(), indent=4))

            if not extracted.origin or not extracted.destination:
                print("\n⏭  No origin/destination extracted — skipping RFQ draft.")
                print("\n" + "=" * 60)
                continue

            # Step 2: Agents — the same source the send-request page reads.
            # There is no automatic destination/mode filtering in the live flow;
            # a human picks. This CLI takes the first few.
            agents = agent_repo.list_all()[:DRAFT_AGENT_LIMIT]
            print(f"\n🔎 Drafting to {len(agents)} agent(s): "
                  f"{', '.join(a.agent_name for a in agents) or '—'}")
            if not agents:
                print("\n" + "=" * 60)
                continue

            # Step 3: RFQ Agent — draft vendor emails (not sent)
            reference = f"RFQ-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:4]}"
            rfq_response: RFQResponse = generate_rfq_drafts(
                shipment_data=extracted.model_dump(),
                agents=[
                    {"agent_name": a.agent_name, "email": a.email, "specialty": a.specialty}
                    for a in agents
                ],
                reference=reference,
            )

            print(f"\n✉️  RFQ Agent Drafted {len(rfq_response.drafts)} Email(s) [{reference}]:")
            for draft in rfq_response.drafts:
                print(f"\n  📧 To: {draft.vendor_name}")
                print(f"  Subject: {draft.subject}")
                print(f"  Body preview: {draft.body[:120].strip()}...")

            print("\n" + "=" * 60)

    except Exception as e:
        print(f"\n❌ Pipeline Error: {e}")


if __name__ == "__main__":
    process_inbox()
