"""
build_training_data.py
----------------------
Step 1 of the fine-tuning pipeline.

Fetches a sample of emails from your inbox, auto-labels them using GPT-4o-mini
few-shot classification, and writes a JSONL training file ready for fine-tuning.

Usage:
    python build_training_data.py --sample 300 --output training_data.jsonl

The script:
1. Fetches emails in batches via IMAP (spread across the mailbox for variety)
2. Auto-labels each email using few-shot GPT-4o-mini
3. Writes JSONL in the exact format required by OpenAI fine-tuning
4. Prints a summary of label distribution
"""

import argparse
import json
import logging
import os
import random
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from email_connector import fetch_latest_emails

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

client = OpenAI()

# ---------------------------------------------------------------------------
# System prompt used for BOTH auto-labelling and in the final JSONL file
# (Must be identical in training and inference)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are an email classifier for a logistics and freight forwarding company. "
    "Classify the following email into exactly one of these categories:\n\n"
    "- customer_requirement: A customer requesting freight services, a shipment, "
    "transport booking, or asking for a rate quote for moving cargo\n"
    "- quotation_rate_card: A freight agent or vendor replying with rates, pricing, "
    "a rate card, or a formal quotation for cargo transport\n"
    "- general: Everything else — newsletters, spam, internal memos, tracking updates, "
    "delivery confirmations, invoices, or unrelated correspondence\n\n"
    "Reply with ONLY the category name, nothing else."
)

# Few-shot examples for auto-labelling (not written to JSONL — just for the labeller)
FEW_SHOT_EXAMPLES = [
    {
        "role": "user",
        "content": (
            "From: john.smith@acmecorp.com\n"
            "Subject: Need freight quote Mumbai to Hamburg\n\n"
            "Hi, we need to ship 500 kg of machine parts from Mumbai to Hamburg. "
            "Please provide rates for sea freight. Urgent."
        ),
    },
    {"role": "assistant", "content": "customer_requirement"},
    {
        "role": "user",
        "content": (
            "From: ops@dbschenker.example.com\n"
            "Subject: Re: RFQ-20260315-ab12 | Rate Quotation Hamburg–Mumbai\n\n"
            "Dear Team, please find our rates below:\n"
            "Sea Freight: USD 1,850 per shipment\nTransit: 24-26 days\nValidity: 30 days."
        ),
    },
    {"role": "assistant", "content": "quotation_rate_card"},
    {
        "role": "user",
        "content": (
            "From: newsletter@freightnews.com\n"
            "Subject: Weekly Logistics Industry Update\n\n"
            "Top stories this week: Container shipping rates continue to stabilize..."
        ),
    },
    {"role": "assistant", "content": "general"},
]


def auto_label(subject: str, body: str, sender: str) -> str:
    """Use GPT-4o-mini with few-shot examples to label a single email."""
    truncated_body = body[:1500]
    email_text = f"From: {sender}\nSubject: {subject}\n\n{truncated_body}"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *FEW_SHOT_EXAMPLES,
        {"role": "user", "content": email_text},
    ]

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0,
            max_tokens=20,
        )
        raw = response.choices[0].message.content.strip().lower()
        if "customer" in raw or "requirement" in raw:
            return "customer_requirement"
        elif "quotation" in raw or "rate" in raw:
            return "quotation_rate_card"
        else:
            return "general"
    except Exception as e:
        logger.warning("Auto-label failed for '%s': %s", subject[:50], e)
        return "general"


def build_training_file(sample_size: int, output_path: str):
    """Fetch emails, auto-label them, write JSONL training file."""
    logger.info("Fetching %d emails for training data...", sample_size)

    # Fetch the total count first
    probe = fetch_latest_emails(limit=1, offset=0)
    total = probe.get("total", 0)
    if total == 0:
        logger.error("No emails found in inbox. Check email credentials.")
        sys.exit(1)

    logger.info("Total emails available: %d", total)

    # Spread sampling across the mailbox for variety
    # We fetch in small batches at random offsets
    batch_size = 20
    emails_collected: list[dict] = []
    offsets_used: set[int] = set()

    while len(emails_collected) < sample_size:
        # Pick a random offset we haven't used
        max_offset = max(0, total - batch_size)
        offset = random.randint(0, max_offset)
        # Round to nearest batch_size to avoid tiny overlaps
        offset = (offset // batch_size) * batch_size
        if offset in offsets_used:
            continue
        offsets_used.add(offset)

        batch = fetch_latest_emails(limit=batch_size, offset=offset)
        batch_emails = batch.get("emails", [])
        emails_collected.extend(batch_emails)

        if len(offsets_used) > (total // batch_size) + 1:
            break  # Exhausted all offsets

    # Deduplicate by id and trim to requested sample_size
    seen_ids: set[str] = set()
    unique_emails = []
    for e in emails_collected:
        if e["id"] not in seen_ids:
            seen_ids.add(e["id"])
            unique_emails.append(e)
    unique_emails = unique_emails[:sample_size]

    logger.info("Collected %d unique emails. Auto-labelling...", len(unique_emails))

    # Auto-label and build JSONL records
    records = []
    label_counts: dict[str, int] = {"customer_requirement": 0, "quotation_rate_card": 0, "general": 0}

    for i, email in enumerate(unique_emails, 1):
        subject = email.get("subject", "")
        body = email.get("body", "")
        sender = email.get("sender", email.get("from", ""))

        label = auto_label(subject, body, sender)
        label_counts[label] = label_counts.get(label, 0) + 1

        # Build the JSONL record in OpenAI fine-tuning format
        truncated_body = body[:1500]
        email_text = f"From: {sender}\nSubject: {subject}\n\n{truncated_body}"

        record = {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": email_text},
                {"role": "assistant", "content": label},
            ]
        }
        records.append(record)

        if i % 20 == 0:
            logger.info("  %d/%d labelled — distribution so far: %s", i, len(unique_emails), label_counts)

    # Write JSONL file
    output = Path(output_path)
    with open(output, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    logger.info("\n=== Training data ready ===")
    logger.info("File: %s (%d examples)", output, len(records))
    logger.info("Label distribution:")
    for label, count in sorted(label_counts.items()):
        pct = count / len(records) * 100 if records else 0
        logger.info("  %-25s %4d  (%.1f%%)", label, count, pct)

    # Warn if class imbalance is severe
    values = [v for v in label_counts.values() if v > 0]
    if values and max(values) / min(values) > 5:
        logger.warning(
            "⚠ Severe class imbalance detected (ratio %.1f:1). "
            "Consider generating synthetic examples for minority classes or "
            "increasing the sample size.",
            max(values) / min(values),
        )

    return output_path, label_counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build fine-tuning training data from inbox emails")
    parser.add_argument("--sample", type=int, default=300, help="Number of emails to sample (default: 300)")
    parser.add_argument("--output", type=str, default="training_data.jsonl", help="Output JSONL file path")
    args = parser.parse_args()

    build_training_file(args.sample, args.output)
