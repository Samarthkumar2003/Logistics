import json
from email_connector import fetch_latest_emails
from intake_agent import run_intake_agent, ShipmentDetails
from history_agent import find_similar_shipments
from rfq_agent import generate_rfq_drafts, RFQResponse

def process_inbox():
    print("🚀 Starting the Logistics Copilot Pipeline...\n")
    try:
        # Fetch the 4 most recent emails
        emails = fetch_latest_emails(limit=4)

        if not emails:
            print("📭 Inbox is empty or no emails found.")
            return

        print(f"\n📥 Retrieved {len(emails)} emails. Processing via OpenAI...\n")
        print("="*60)

        for i, email_data in enumerate(emails, 1):
            print(f"\n--- 📦 Processing Shipment Request {i} ---")
            print(f"From: {email_data['from']}")
            print(f"Subject: {email_data['subject']}")
            print("-" * 30)

            # Step 1: Intake Agent — parse email into structured data
            full_email_content = f"Subject: {email_data['subject']}\n\nBody:\n{email_data['body']}"
            extracted_data: ShipmentDetails = run_intake_agent(full_email_content)

            print("\n✅ Intake Agent Extracted:")
            print(json.dumps(extracted_data.model_dump(), indent=4))

            # Step 2: History Agent — find similar past shipments via vector search
            history_matches = find_similar_shipments(
                origin=extracted_data.origin,
                destination=extracted_data.destination,
                mode=extracted_data.mode,
                commodity_desc=extracted_data.commodity
            )

            if history_matches:
                print(f"\n📚 History Agent Found {len(history_matches)} Match(es):")
                for m in history_matches:
                    print(f"  - {m['commodity']} | Agent: {m['agent_used']} | Rate: ${m['rate_paid']} | {m['transit_time_days']} days | Similarity: {m['similarity']:.3f}")
            else:
                print("\n📭 No historical matches found. Using fallback vendor.")

            # Step 3: RFQ Agent — draft vendor emails
            rfq_response: RFQResponse = generate_rfq_drafts(
                shipment_data=extracted_data.model_dump(),
                history_matches=history_matches
            )

            print(f"\n✉️  RFQ Agent Drafted {len(rfq_response.drafts)} Email(s):")
            for draft in rfq_response.drafts:
                print(f"\n  📧 To: {draft.vendor_name}")
                print(f"  Subject: {draft.subject}")
                print(f"  Body preview: {draft.body[:120].strip()}...")

            print("\n" + "="*60)

    except Exception as e:
        print(f"\n❌ Pipeline Error: {e}")

if __name__ == "__main__":
    process_inbox()
