"""
fetch_gmail_history.py
----------------------
Fetches ALL emails from the client Gmail inbox (via DWD service account),
labels each with GPT-4o-mini, and ingests confirmed examples into Supabase RAG.

Modes:
    python fetch_gmail_history.py --dry-run   # label + print table, no ingest
    python fetch_gmail_history.py             # auto-ingest high confidence

Requires: service_account.json + GMAIL_MAILBOX in .env
"""

import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from openai import OpenAI
from supabase import create_client
import base64
from email.header import decode_header

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

openai_client = OpenAI()
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

SERVICE_ACCOUNT_FILE = Path(__file__).parent / "service_account.json"
GMAIL_MAILBOX = os.environ["GMAIL_MAILBOX"]
AUTO_INGEST_THRESHOLD = 0.80
VALID_LABELS = {"customer_requirement", "quotation_rate_card", "general", "skip"}

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]


def _get_service():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    ).with_subject(GMAIL_MAILBOX)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _decode_header_value(raw: str) -> str:
    parts = decode_header(raw or "")
    result = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            result += part.decode(enc or "utf-8", errors="replace")
        else:
            result += part
    return result


def _extract_body(payload: dict) -> str:
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    if mime.startswith("multipart/"):
        for part in payload.get("parts", []):
            text = _extract_body(part)
            if text:
                return text
    return ""


MAX_EMAILS = 2000

# Broad query: catches RFQs, rate cards, quotations, bookings, enquiries
# Date range: Jan 2025 → now. Excludes internal Bhatia-to-Bhatia threads.
GMAIL_QUERY = (
    "after:2025/01/01 "
    "("
    "subject:RFQ OR subject:quotation OR subject:\"rate request\" OR "
    "subject:\"rate card\" OR subject:\"spot rate\" OR subject:\"rate offer\" OR "
    "subject:\"rate sheet\" OR subject:\"rate update\" OR subject:\"freight rate\" OR "
    "subject:\"ocean freight\" OR subject:\"air freight\" OR subject:\"sea freight\" OR "
    "subject:enquiry OR subject:inquiry OR subject:\"quote request\" OR "
    "subject:\"booking request\" OR subject:\"rate req\" OR subject:FCL OR "
    "subject:LCL OR subject:\"20ft\" OR subject:\"40ft\" OR subject:\"20GP\" OR "
    "subject:\"40HC\" OR subject:cargo OR subject:shipment OR subject:\"rate valid\" OR "
    "subject:\"best rate\" OR subject:costing OR subject:tariff"
    ")"
)


def fetch_all_emails(service) -> list[dict]:
    """Fetch relevant emails using subject query + pagination."""
    all_refs = []
    page_token = None
    logger.info("Query: %s", GMAIL_QUERY[:120])
    logger.info("Fetching up to %d relevant emails...", MAX_EMAILS)
    while len(all_refs) < MAX_EMAILS:
        kwargs = {
            "userId": "me",
            "maxResults": min(500, MAX_EMAILS - len(all_refs)),
            "q": GMAIL_QUERY,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().messages().list(**kwargs).execute()
        refs = result.get("messages", [])
        all_refs.extend(refs)
        page_token = result.get("nextPageToken")
        logger.info("  fetched %d emails so far...", len(all_refs))
        if not page_token or len(all_refs) >= MAX_EMAILS:
            break

    logger.info("Total messages in inbox: %d — fetching full content...", len(all_refs))

    emails = []
    for i, ref in enumerate(all_refs, 1):
        try:
            msg = service.users().messages().get(
                userId="me", id=ref["id"], format="full"
            ).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            body = _extract_body(msg.get("payload", {})).strip()
            if not body or len(body) < 20:
                continue
            emails.append({
                "id": msg["id"],
                "from": headers.get("From", ""),
                "subject": _decode_header_value(headers.get("Subject", "")),
                "body": body,
            })
            if i % 50 == 0:
                logger.info("  processed %d/%d emails", i, len(all_refs))
        except HttpError as e:
            logger.warning("Skipping %s: %s", ref["id"], e)

    logger.info("Emails with usable body: %d", len(emails))
    return emails


def _label_email(email: dict) -> tuple[str, float, str]:
    """Label one email with GPT-4o-mini. Returns (label, confidence, reason)."""
    prompt = (
        "You are classifying an email from a freight/logistics inbox.\n\n"
        f"From: {email['from']}\n"
        f"Subject: {email['subject']}\n"
        f"Body (first 600 chars):\n{email['body'][:600]}\n\n"
        "Classify into exactly one label:\n"
        "- customer_requirement: customer asking for shipping rates, booking, or freight quote\n"
        "- quotation_rate_card: agent/carrier providing freight rates or price breakdown\n"
        "- general: shipment status, tracking, documents, invoices, newsletters, operational\n"
        "- skip: spam, unsubscribe, out-of-office, or not logistics-related\n\n"
        'Reply ONLY with JSON: {"label": "...", "confidence": 0.0, "reason": "one sentence"}'
    )
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=100,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        label = data.get("label", "skip")
        confidence = float(data.get("confidence", 0.0))
        reason = data.get("reason", "")
        if label not in VALID_LABELS:
            label = "skip"
        return label, confidence, reason
    except Exception as e:
        logger.warning("GPT label failed for '%s': %s", email.get("subject", "")[:40], e)
        return "skip", 0.0, str(e)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _fetch_existing_hashes() -> set[str]:
    rows = supabase.table("email_training_data").select("content").execute()
    return {_content_hash(r["content"]) for r in rows.data if r.get("content")}


def _embed(text: str) -> list[float]:
    # Qwen document embedding (no instruction) — matches email_training_data.embedding_qwen
    from email_classifier import _get_embedding
    return _get_embedding(text)


def _ingest_email(email: dict, label: str, existing_hashes: set[str]) -> str:
    content = f"Subject: {email['subject']}\nFrom: {email['from']}\n\n{email['body']}"
    chash = _content_hash(content)
    if chash in existing_hashes:
        return "duplicate"
    try:
        vec = _embed(content)
        supabase.table("email_training_data").insert({
            "content": content,
            "subject": email["subject"],
            "sender": email["from"],
            "label": label,
            "source": "gmail_history",
            "embedding_qwen": vec,
        }).execute()
        existing_hashes.add(chash)
        return "inserted"
    except Exception as e:
        logger.error("Insert failed: %s", e)
        return "failed"


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    service = _get_service()
    emails = fetch_all_emails(service)

    if not emails:
        logger.error("No emails fetched.")
        sys.exit(1)

    logger.info("\nLabeling %d emails with GPT-4o-mini...\n", len(emails))

    labeled: list[dict] = []
    for i, email in enumerate(emails, 1):
        label, conf, reason = _label_email(email)
        labeled.append({"email": email, "label": label, "confidence": conf, "reason": reason})
        snippet = email["subject"][:60]
        logger.info("[%3d/%d] %-26s conf=%.2f  %s", i, len(emails), label, conf, snippet)

    if dry_run:
        print("\n\n" + "="*70)
        print("  DRY RUN — nothing ingested. Label breakdown:")
        print("="*70)
        for lbl in ["customer_requirement", "quotation_rate_card", "general", "skip"]:
            group = [r for r in labeled if r["label"] == lbl]
            if not group:
                continue
            print(f"\n{'─'*70}")
            print(f"  {lbl.upper()}  ({len(group)} emails)")
            print(f"{'─'*70}")
            for r in group:
                print(f"  [{r['confidence']:.2f}] {r['email']['subject'][:65]}")
                print(f"         From: {r['email']['from'][:60]}")
                print(f"         Why:  {r['reason']}")
        counts = {l: sum(1 for r in labeled if r["label"] == l) for l in VALID_LABELS}
        print("\nSummary:")
        for l, c in counts.items():
            print(f"  {l:<28} {c}")
        print("\nRun without --dry-run to ingest.")
        return

    existing_hashes = _fetch_existing_hashes()
    logger.info("Existing hashes in DB: %d", len(existing_hashes))

    counts = {l: 0 for l in [*VALID_LABELS, "inserted", "duplicate", "failed", "low_conf_skip"]}

    for r in labeled:
        if r["label"] in ("skip", "general"):
            counts[r["label"]] += 1
            continue
        if r["confidence"] >= AUTO_INGEST_THRESHOLD:
            result = _ingest_email(r["email"], r["label"], existing_hashes)
            counts[result] += 1
            logger.info("AUTO [%.2f] %-26s %s — %s", r["confidence"], r["label"], result,
                        r["email"]["subject"][:50])
        else:
            counts["low_conf_skip"] += 1
            logger.info("LOW  [%.2f] %-26s — %s", r["confidence"], r["label"],
                        r["email"]["subject"][:50])

    print("\n=== DONE ===")
    print(f"  inserted into Supabase        : {counts['inserted']}")
    print(f"  duplicate (already in DB)     : {counts['duplicate']}")
    print(f"  skipped (general)             : {counts['general']}")
    print(f"  skipped (spam/skip)           : {counts['skip']}")
    print(f"  skipped (low confidence)      : {counts['low_conf_skip']}")
    print(f"  failed                        : {counts['failed']}")


if __name__ == "__main__":
    main()
