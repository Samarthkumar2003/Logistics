"""
parse_whatsapp_export.py
------------------------
Parses WhatsApp .txt exports from whatsapp_exports/,
labels each message block with Claude Haiku,
and ingests confirmed examples into Supabase RAG.

Modes:
    python parse_whatsapp_export.py --dry-run   # label everything, print table, DO NOT ingest
    python parse_whatsapp_export.py --review    # show each label, press y/n/edit before ingesting
    python parse_whatsapp_export.py             # auto-ingest high-confidence, skip low-confidence

Labels: customer_requirement | quotation_rate_card | general | skip
"""

import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from supabase import create_client

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

openai_client = OpenAI()
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

EXPORTS_DIR = Path(__file__).parent / "whatsapp_exports"
AUTO_INGEST_THRESHOLD = 0.80
VALID_LABELS = {"customer_requirement", "quotation_rate_card", "general", "skip"}

_SKIP_PATTERNS = re.compile(
    r"omitted|end-to-end encrypted|Missed (?:voice|video) call|"
    r"Voice call,|is a contact\.|<Media omitted>",
    re.IGNORECASE,
)
_TIMESTAMP_RE = re.compile(
    r"^\[(\d{1,2}/\d{1,2}/\d{2,4}),\s*(\d{1,2}:\d{2}:\d{2}\s*[AP]M)\]\s*([^:]+):\s*(.*)",
    re.DOTALL,
)
_GREETING_RE = re.compile(
    r"^(?:hi+|hello|hey|ok|okay|sure|thanks|thank you|noted|fine|good|great|"
    r"yes|no|done|understood|please|pls|regards|see you|bye|morning|"
    r"good morning|good afternoon|good evening|call me|call back|\.+|👍+|🙏+)[\s!?.]*$",
    re.IGNORECASE,
)

LABEL_DESCRIPTIONS = {
    "customer_requirement": "customer asking for shipping rates, booking, or freight quote",
    "quotation_rate_card":  "agent/carrier providing freight rates or price breakdown",
    "general":              "shipment status, documents, operational coordination",
    "skip":                 "too short, greeting-only, or not useful for training",
}


@dataclass
class WaMessage:
    timestamp: datetime
    sender: str
    text: str
    chat_file: str


@dataclass
class LabeledBlock:
    block: dict
    label: str
    confidence: float
    reasoning: str = ""


def _parse_file(path: Path) -> list[WaMessage]:
    messages: list[WaMessage] = []
    current: WaMessage | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _TIMESTAMP_RE.match(line)
        if m:
            if current:
                messages.append(current)
            date_str, time_str, sender, text = m.groups()
            try:
                ts = datetime.strptime(f"{date_str} {time_str.strip()}", "%d/%m/%y %I:%M:%S %p")
            except ValueError:
                try:
                    ts = datetime.strptime(f"{date_str} {time_str.strip()}", "%d/%m/%Y %I:%M:%S %p")
                except ValueError:
                    ts = datetime.min
            current = WaMessage(ts, sender.strip(), text.strip(), path.stem)
        elif current:
            current.text += "\n" + line.strip()
    if current:
        messages.append(current)
    return messages


def _should_skip(text: str) -> bool:
    t = text.strip()
    if not t or len(t) < 12:
        return True
    if _SKIP_PATTERNS.search(t):
        return True
    if _GREETING_RE.match(t):
        return True
    return False


def _group_messages(messages: list[WaMessage], gap_minutes: int = 5) -> list[dict]:
    blocks: list[dict] = []
    if not messages:
        return blocks
    current_sender = messages[0].sender
    current_texts: list[str] = []
    current_ts = messages[0].timestamp
    current_file = messages[0].chat_file
    for msg in messages:
        gap = (msg.timestamp - current_ts).total_seconds() / 60
        if msg.sender == current_sender and gap <= gap_minutes:
            if not _should_skip(msg.text):
                current_texts.append(msg.text)
            current_ts = msg.timestamp
        else:
            if current_texts:
                blocks.append({"sender": current_sender, "text": "\n".join(current_texts),
                               "timestamp": current_ts.isoformat(), "chat_file": current_file})
            current_sender = msg.sender
            current_texts = [] if _should_skip(msg.text) else [msg.text]
            current_ts = msg.timestamp
            current_file = msg.chat_file
    if current_texts:
        blocks.append({"sender": current_sender, "text": "\n".join(current_texts),
                       "timestamp": current_ts.isoformat(), "chat_file": current_file})
    return blocks


def _label_block(block: dict) -> LabeledBlock:
    """Label a message block using Claude Haiku."""
    label_list = "\n".join(f"  - {k}: {v}" for k, v in LABEL_DESCRIPTIONS.items())
    prompt = (
        f"You are classifying a WhatsApp message from a freight/logistics chat.\n\n"
        f"Chat: {block['chat_file']}  |  Sender: {block['sender']}\n"
        f"Message:\n{block['text']}\n\n"
        f"Labels:\n{label_list}\n\n"
        f"Reply ONLY with JSON (no markdown):\n"
        f'{{\"label\": \"...\", \"confidence\": 0.0, \"reason\": \"one sentence\"}}'
    )
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=120,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        label = data.get("label", "skip")
        confidence = float(data.get("confidence", 0.0))
        reason = data.get("reason", "")
        if label not in VALID_LABELS:
            label = "skip"
        return LabeledBlock(block, label, confidence, reason)
    except Exception as e:
        logger.warning("Claude label failed (%s): %s", block["text"][:40], e)
        return LabeledBlock(block, "skip", 0.0, f"error: {e}")


def _embed(text: str) -> list[float]:
    resp = openai_client.embeddings.create(model="text-embedding-3-small", input=text[:8000])
    return resp.data[0].embedding


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _fetch_existing_hashes() -> set[str]:
    rows = supabase.table("email_training_data").select("content").execute()
    return {_content_hash(r["content"]) for r in rows.data if r.get("content")}


def _ingest_block(block: dict, label: str, existing_hashes: set[str]) -> str:
    """Returns 'inserted', 'duplicate', or 'failed'."""
    content = f"[WhatsApp message]\n{block['text']}"
    chash = _content_hash(content)
    if chash in existing_hashes:
        return "duplicate"
    try:
        vec = _embed(content)
        subject = f"[WhatsApp] {block['chat_file']} — {block['text'][:50].replace(chr(10), ' ')}"
        supabase.table("email_training_data").insert({
            "content": content, "subject": subject, "sender": block["sender"],
            "label": label, "source": "whatsapp", "embedding": vec,
        }).execute()
        existing_hashes.add(chash)
        return "inserted"
    except Exception as e:
        logger.error("Insert failed: %s", e)
        return "failed"


def _print_dry_run_table(results: list[LabeledBlock]) -> None:
    """Print a full table of every labeled block for review."""
    label_order = ["customer_requirement", "quotation_rate_card", "general", "skip"]
    for label in label_order:
        group = [r for r in results if r.label == label]
        if not group:
            continue
        print(f"\n{'═'*70}")
        print(f"  {label.upper()}  ({len(group)} blocks)")
        print(f"{'═'*70}")
        for i, r in enumerate(group, 1):
            snippet = r.block["text"][:120].replace("\n", " | ")
            print(f"  [{i:3d}] conf={r.confidence:.2f}  chat={r.block['chat_file']}")
            print(f"        sender={r.block['sender']}")
            print(f"        text: {snippet}")
            print(f"        why:  {r.reasoning}")
            print()


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    review_mode = "--review" in sys.argv

    txt_files = sorted(EXPORTS_DIR.glob("*.txt"))
    if not txt_files:
        logger.error("No .txt files found in %s", EXPORTS_DIR)
        sys.exit(1)

    logger.info("Parsing %d chat file(s)...", len(txt_files))
    all_blocks: list[dict] = []
    for f in txt_files:
        msgs = _parse_file(f)
        blocks = _group_messages(msgs)
        logger.info("  %-30s → %3d messages → %3d blocks", f.name, len(msgs), len(blocks))
        all_blocks.extend(blocks)
    logger.info("Total blocks: %d  |  Labeling with Claude Haiku...\n", len(all_blocks))

    # Label all blocks
    results: list[LabeledBlock] = []
    for i, block in enumerate(all_blocks, 1):
        r = _label_block(block)
        results.append(r)
        snippet = block["text"][:60].replace("\n", " ")
        logger.info("[%3d/%d] %-26s conf=%.2f  %s", i, len(all_blocks), r.label, r.confidence, snippet)

    if dry_run:
        print("\n\n" + "="*70)
        print("  DRY RUN — nothing ingested. Full label breakdown:")
        print("="*70)
        _print_dry_run_table(results)
        counts = {l: sum(1 for r in results if r.label == l) for l in VALID_LABELS}
        print("\nSummary:")
        for l, c in counts.items():
            print(f"  {l:<28} {c}")
        print("\nRun without --dry-run to ingest, or --review for interactive mode.")
        return

    # Ingest
    existing_hashes = _fetch_existing_hashes()
    logger.info("Existing hashes in DB: %d", len(existing_hashes))

    counts: dict[str, int] = {l: 0 for l in [*VALID_LABELS, "inserted", "duplicate", "failed", "low_conf_skip"]}

    for r in results:
        if r.label == "skip":
            counts["skip"] += 1
            continue

        snippet = r.block["text"][:65].replace("\n", " ")

        if review_mode:
            print(f"\n{'─'*65}")
            print(f"Chat: {r.block['chat_file']}  |  Sender: {r.block['sender']}")
            print(f"Label: {r.label}  |  Confidence: {r.confidence:.2f}")
            print(f"Why: {r.reasoning}")
            print(f"Text:\n{r.block['text'][:300]}")
            print(f"{'─'*65}")
            ans = input(
                f"[y=accept '{r.label}' / n=skip / cr=customer_requirement / "
                f"q=quotation_rate_card / g=general / x=quit]: "
            ).strip().lower()
            if ans == "x":
                break
            label_map = {"cr": "customer_requirement", "q": "quotation_rate_card", "g": "general"}
            final_label = label_map.get(ans, r.label if ans == "y" else None)
            if not final_label:
                counts["low_conf_skip"] += 1
                continue
            result = _ingest_block(r.block, final_label, existing_hashes)
            counts[result] += 1
            logger.info("  → %s as %s", result, final_label)

        elif r.confidence >= AUTO_INGEST_THRESHOLD:
            result = _ingest_block(r.block, r.label, existing_hashes)
            counts[result] += 1
            logger.info("AUTO [%.2f] %-26s %s — %s", r.confidence, r.label, result, snippet)

        else:
            counts["low_conf_skip"] += 1
            logger.info("LOW  [%.2f] %-26s skipped (run --review to decide) — %s",
                        r.confidence, r.label, snippet)

    print("\n=== DONE ===")
    print(f"  inserted into Supabase        : {counts['inserted']}")
    print(f"  duplicate (already in DB)     : {counts['duplicate']}")
    print(f"  skipped (label=skip)          : {counts['skip']}")
    print(f"  skipped (label=general)       : {counts['general']}")
    print(f"  skipped (low confidence)      : {counts['low_conf_skip']}")
    print(f"  failed                        : {counts['failed']}")


if __name__ == "__main__":
    main()
