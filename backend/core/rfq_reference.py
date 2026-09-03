"""
rfq_reference.py
----------------
The RFQ reference this system puts in every outgoing subject line, and reads
back out of agent replies. It is the only link between a reply and the shipment
it answers, so it lives in core rather than inside any one agent.

Two forms, deliberately:

    canonical   RFQ-20260101-a1b2c3d4     what rfq_jobs.reference stores
    subject     RFQId:20260101-a1b2c3d4   what goes out in the subject line

The subject form is labelled so it survives a human retyping it and is obvious
to an agent skimming their inbox. The canonical form stays as it was because 172
jobs already use it and ~150 RFQs are out in the wild awaiting replies — so the
pattern below matches BOTH, and extraction always returns the canonical form.

The date component is kept in the subject token. The hex suffix alone would
collide across days, and a reference that is only unique within one day cannot
attribute a reply that arrives a week later.

The hex suffix is 8 characters. It was 4, which is 65,536 values scoped to one
day: at ~150 RFQs a day that is a ~16% chance per day of drawing a reference
that already exists. `rfq_jobs.reference` is unique, so a collision does not
misattribute anything — the insert fails *after* the mail has gone out, leaving
an RFQ with an agent and no job row to attach the reply to. Both widths match
below, because shortening the pattern to 8 only would orphan every reference
already in the wild.
"""

import re
from typing import Optional

# The unique part: YYYYMMDD-xxxx, where xxxx is 8 hex (current) or 4 (legacy).
#
# The trailing lookahead is what makes accepting two widths safe. Without it a
# 4-char match could be taken out of the front of a longer hex run and resolve
# to a real-but-wrong job. With it, anything longer than 8 matches nothing at
# all — refusing to attribute is recoverable, attributing to the wrong shipment
# is not.
_CORE = r"(\d{8}-[a-f0-9]{4,8})(?![a-f0-9])"

# Matches the current subject token AND the legacy bare form. Case-insensitive
# because agents retype references by hand; whitespace around the colon is
# tolerated because mail clients wrap subject lines.
RFQ_PATTERN = re.compile(rf"(?:RFQ\s*Id\s*:\s*|RFQ-){_CORE}", re.I)


def canonical(core: str) -> str:
    """The stored form of a reference, from its unique part."""
    return f"RFQ-{core.lower()}"


def subject_token(reference: str) -> str:
    """The form that goes into an outgoing subject line.

    Falls back to the reference verbatim if it isn't one of ours, so a caller
    passing something unexpected still produces a usable subject.
    """
    match = RFQ_PATTERN.search(reference or "")
    return f"RFQId:{match.group(1).lower()}" if match else (reference or "")


def extract_rfq_reference(text: str) -> Optional[str]:
    """Pull an RFQ reference out of a subject line or body.

    Returns the canonical `RFQ-YYYYMMDD-xxxx` form — directly comparable with
    `rfq_jobs.reference` — or None. Matching is case-insensitive but the lookup
    key is not, so an agent replying `RFQID:20260101-A1B2` still resolves.
    """
    match = RFQ_PATTERN.search(text or "")
    return canonical(match.group(1)) if match else None


def has_rfq_reference(text: str) -> bool:
    """True when this text carries one of our references.

    Used by the classifier: a reply quoting a reference we issued is, by
    construction, an agent responding to an RFQ we sent.
    """
    return RFQ_PATTERN.search(text or "") is not None


def inject_reference(subject: str, reference: str) -> str:
    """Ensure `reference` is the RFQ token in this subject.

    Replaces any existing token — including the placeholder carried over from a
    preview draft, or a legacy `RFQ-...` the model produced — so exactly one
    reference is present and it is the right one. Prepends when there is none.

    Every outgoing subject goes through this. Asking a model to include the
    reference is not enough: when it deviates, the RFQ leaves unmatchable and
    the reply can never be attributed.
    """
    subject = (subject or "").strip()
    token = subject_token(reference)
    if RFQ_PATTERN.search(subject):
        return RFQ_PATTERN.sub(token, subject, count=1)
    return f"{token} | {subject}".strip(" |")
