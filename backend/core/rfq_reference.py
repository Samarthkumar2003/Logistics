"""
rfq_reference.py
----------------
The RFQ reference this system puts in every outgoing subject line, and reads
back out of agent replies. It is the only link between a reply and the shipment
it answers, so it lives in core rather than inside any one agent.

Three forms are live simultaneously and all three must keep working:

    counter     RFQ-001042                 what rfq_jobs.reference stores now
    subject     RFQId:001042               what goes out in the subject line now
    legacy      RFQ-20260101-a1b2c3d4      every reference issued before this,
                RFQId:20260101-a1b2        in both widths and both prefixes

The counter comes from a Postgres sequence (sql/add_rfq_reference_sequence.sql)
so an operator can read a reference aloud, which a random hex suffix made
impossible. The legacy form is not migrated and never will be: references issued
under it are sitting in vendors' inboxes awaiting replies, and rewriting our own
rows would not change what those vendors quote back.

WHY THE COUNTER IS ACCEPTED ONLY UNDER THE `RFQId:` LABEL
---------------------------------------------------------
The legacy form is unmistakably ours. No freight agent coincidentally types
`RFQ-20260101-a1b2c3d4`. `RFQ-001042` is the opposite: it is exactly what a
vendor writes about their OWN paperwork, and vendors do have their own RFQ
numbering. Accepting a bare `RFQ-NNNNNN` from an inbound reply would let
"our RFQ-100234 refers" file that vendor's quote against whatever shipment
happens to hold reference 100234.

So there are two patterns, and the difference between them is the whole point:

    _INBOUND  reading a vendor's reply. Legacy under either prefix; the counter
              only under the `RFQId:` label, which is what we actually send and
              therefore what a replying agent quotes back.
    _OWN      finding our own token in a subject we are composing, where a bare
              `RFQ-NNNNNN` does need to be recognised so inject_reference
              replaces it instead of prepending a second reference.

Strict when believing someone else, permissive when editing our own text.

WHY THE COUNTER IS EXACTLY SIX DIGITS
-------------------------------------
Not four-to-six. A variable width lets a truncated or retyped `RFQ-001042`
match as `RFQ-0010` and resolve to reference 10 — a different, real shipment.
Fixing the width means a short or long digit run matches nothing at all, and
refusing to attribute is recoverable in a way that misattributing is not. That
is the same reasoning as the legacy lookahead below, applied to digits.

Six digits is 999,999 references. At the observed ~150/day that is roughly
eighteen years. rfq_service refuses to issue past it rather than emit a
seven-digit reference this module would silently match nothing of.
"""

import re
from typing import Optional

# Six digits is 999,999 references. rfq_service refuses to issue past this
# rather than emit a seven-digit reference the patterns below match nothing of.
MAX_REFERENCE_NUMBER = 999_999

# The reference a draft preview shows. A preview is sent to nobody, so it must
# not consume a sequence number — an operator opening the editor five times would
# otherwise leave five permanent gaps in the numbering. The sequence starts at
# 1000, so 000000 is never issued to a real RFQ, and a reply somehow quoting it
# finds no job row and is left unlinked rather than misfiled.
RESERVED_PREVIEW_REFERENCE = "RFQ-000000"

# Legacy core: YYYYMMDD-xxxx, where xxxx is 8 hex (post-widening) or 4 (before).
#
# The trailing lookahead is what makes accepting two widths safe. Without it a
# 4-char match could be taken out of the front of a longer hex run and resolve
# to a real-but-wrong job. With it, anything longer than 8 matches nothing at
# all — refusing to attribute is recoverable, attributing to the wrong shipment
# is not.
_LEGACY = r"\d{8}-[a-f0-9]{4,8}(?![a-f0-9])"

# Counter core: exactly six digits, and not the leading six of a longer run.
_COUNTER = r"\d{6}(?![0-9])"

# `RFQId:` — the label we actually send. Case-insensitive and tolerant of
# whitespace around the colon because agents retype it and clients wrap subjects.
_LABEL = r"RFQ\s*Id\s*:\s*"
# `RFQ-` — the bare canonical prefix.
_BARE = r"RFQ-"

# Reading someone else's mail: the counter requires the label.
_INBOUND = re.compile(
    rf"(?:{_LABEL}|{_BARE})(?P<legacy>{_LEGACY})"
    rf"|{_LABEL}(?P<counter>{_COUNTER})",
    re.I,
)

# Editing our own subject: a bare counter counts too.
_OWN = re.compile(
    rf"(?:{_LABEL}|{_BARE})(?P<legacy>{_LEGACY})"
    rf"|(?:{_LABEL}|{_BARE})(?P<counter>{_COUNTER})",
    re.I,
)

# Kept under its historical name for anything that reaches in for the inbound
# matcher. New code should call the functions below.
RFQ_PATTERN = _INBOUND


def _core(match: re.Match) -> str:
    """The unique part of whichever form matched.

    Both patterns have exactly two named groups and exactly one of them is ever
    set, so this is the only correct way to read either match. Reading
    `group(1)` — as this module did when there was a single form — returns None
    for a counter, and a None core silently produces the string "RFQ-None".
    """
    return match.group("legacy") or match.group("counter")


def canonical(core: str) -> str:
    """The stored form of a reference, from its unique part."""
    return f"RFQ-{core.lower()}"


def subject_token(reference: str) -> str:
    """The form that goes into an outgoing subject line.

    Falls back to the reference verbatim if it isn't one of ours, so a caller
    passing something unexpected still produces a usable subject.
    """
    match = _OWN.search(reference or "")
    return f"RFQId:{_core(match).lower()}" if match else (reference or "")


def extract_rfq_reference(text: str) -> Optional[str]:
    """Pull an RFQ reference out of a subject line or body.

    Returns the canonical form — directly comparable with `rfq_jobs.reference` —
    or None. Matching is case-insensitive but the lookup key is not, so an agent
    replying `RFQID:20260101-A1B2` still resolves.
    """
    match = _INBOUND.search(text or "")
    return canonical(_core(match)) if match else None


def has_rfq_reference(text: str) -> bool:
    """True when this text carries one of our references.

    Used by the classifier: a reply quoting a reference we issued is, by
    construction, an agent responding to an RFQ we sent.
    """
    return _INBOUND.search(text or "") is not None


def inject_reference(subject: str, reference: str) -> str:
    """Ensure `reference` is the RFQ token in this subject.

    Replaces any existing token — including the placeholder carried over from a
    preview draft, or a legacy `RFQ-...` the model produced — so exactly one
    reference is present and it is the right one. Prepends when there is none.

    Matches on `_OWN`, not `_INBOUND`: this is our own draft, and a bare
    `RFQ-001042` sitting in it must be replaced rather than left in place while a
    second reference is prepended in front of it.

    Every outgoing subject goes through this. Asking a model to include the
    reference is not enough: when it deviates, the RFQ leaves unmatchable and
    the reply can never be attributed.
    """
    subject = (subject or "").strip()
    token = subject_token(reference)
    if _OWN.search(subject):
        return _OWN.sub(token, subject, count=1)
    return f"{token} | {subject}".strip(" |")
