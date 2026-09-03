"""
Classifier rules and LLM-reply parsing.

Only the rule tiers are tested — they short-circuit before any network call.
The LLM path is not covered here and should not be: a test that needs an API key
is a test that gets skipped.
"""

import pytest

from backend.classifier.email_classifier import (
    _COVER_NOTE_RE,
    _RC_SUBJ_RE,
    _extract_email_domain,
    _parse_llm_label,
    classify_email,
)


# ---------------------------------------------------------------------------
# _extract_email_domain
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sender,expected", [
    ("agent@example.com", "example.com"),
    ("Sales Team <sales@Agent.CO.UK>", "agent.co.uk"),
    ("  spaced@example.com  ", "example.com"),
    ("no-at-sign", ""),
    ("", ""),
])
def test_extracts_domain(sender, expected):
    assert _extract_email_domain(sender) == expected


# ---------------------------------------------------------------------------
# Rule tier 1 — internal senders
# ---------------------------------------------------------------------------

def test_internal_sender_is_general():
    result = classify_email("Anything at all", "body", "ops@bhatiashipping.com")
    assert result.label == "general"
    assert result.method == "rule:internal_domain"


def test_internal_domain_match_ignores_case_and_display_name():
    result = classify_email("x", "y", "Ops Desk <Ops@BhatiaShipping.com>")
    assert result.method == "rule:internal_domain"


def test_internal_rule_beats_the_rfq_rule():
    """Our own outgoing RFQ carries an RFQId in its subject. Without this
    precedence the system would classify its own sent mail as a rate card and
    try to link it to the job it created."""
    result = classify_email(
        "RFQId:20260101-a1b2 | Rate request", "body", "desk@bhatiashipping.com"
    )
    assert result.label == "general"


# ---------------------------------------------------------------------------
# Rule tier 2 — an external sender quoting our reference back
# ---------------------------------------------------------------------------

def test_external_reply_carrying_our_reference_is_a_rate_card():
    result = classify_email(
        "Re: RFQId:20260101-a1b2 | Rate request", "Rates attached", "agent@example.com"
    )
    assert result.label == "quotation_rate_card"
    assert result.method == "rule:rfq_reference"
    assert result.confidence == 1.0
    assert "RFQ-20260101-a1b2" in result.details


def test_legacy_canonical_reference_in_subject_also_triggers_the_rule():
    # ~150 RFQs went out with the old form and are still awaiting replies.
    result = classify_email("Re: RFQ-20260101-a1b2", "rates", "agent@example.com")
    assert result.method == "rule:rfq_reference"


def test_reference_in_the_body_alone_does_not_trigger_the_rule():
    """Subject-only, deliberately. The reference appears in the quoted original
    of every later message in a thread, so matching the body would relabel
    operational follow-ups as rate cards months after the fact.

    The subject carries a job reference so a later rule catches it — otherwise
    the email would fall through to the LLM, which this suite forbids.
    """
    result = classify_email(
        "BSPL123456 container update",
        "See RFQId:20260101-a1b2 below\n> original",
        "agent@example.com",
    )
    assert result.method == "rule:job_ref_no_rate_signal"


# ---------------------------------------------------------------------------
# Rule tier 3 — job reference with no rate signal
# ---------------------------------------------------------------------------

def test_job_reference_subject_without_a_rate_word_is_general():
    result = classify_email("BSPL123456 shipping documents", "docs", "agent@example.com")
    assert result.label == "general"
    assert result.method == "rule:job_ref_no_rate_signal"


# ---------------------------------------------------------------------------
# Rule tier 4 — rate-card subject with a cover-note body
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    "Dear Sir,\n\nKindly find attached our latest rates.\n\nRegards",
    "Please find attached the revised tariff.",
    "PFA rates for your reference",
    "Attached herewith our rate sheet",
    "Rates as attached.",
    "Dear Sir, find attached.",
])
def test_rate_card_subject_with_cover_note_body_skips_the_llm(body):
    """The rate table is in the attachment, so the body is one line. The LLM
    reads that as general; this rule is why those are not lost.

    The first two phrasings did NOT match until this test was written — see
    _COVER_NOTE_RE. They are the two most common cover notes in the inbox.
    """
    result = classify_email("Rate Card - Nhava Sheva to Rotterdam", body,
                            "agent@example.com")
    assert result.label == "quotation_rate_card"
    assert result.method == "rule:rc_subject_cover_note"


def test_a_cover_note_without_a_rate_card_subject_is_not_enough():
    """Both halves are required. "Please find attached" appears on invoices,
    packing lists, and delivery orders — matching it alone would label most of
    the operational inbox as rate cards.

    Asserted on the two predicates rather than through classify_email, because
    an email that matches neither rule reaches the LLM, and this suite is
    offline.
    """
    assert _COVER_NOTE_RE.search("Please find attached.")
    assert not _RC_SUBJ_RE.search("Invoice INV-2026-004")


# ---------------------------------------------------------------------------
# _parse_llm_label
# ---------------------------------------------------------------------------

def test_parses_clean_json():
    assert _parse_llm_label('{"label": "customer_requirement", "confidence": 0.9}') == \
        ("customer_requirement", 0.9)


def test_parses_json_inside_a_markdown_fence():
    raw = '```json\n{"label": "quotation_rate_card", "confidence": 0.88}\n```'
    assert _parse_llm_label(raw) == ("quotation_rate_card", 0.88)


def test_parses_json_with_prose_around_it():
    raw = 'Sure! Here is the result:\n{"label": "general", "confidence": 0.6}\nHope that helps.'
    assert _parse_llm_label(raw) == ("general", 0.6)


@pytest.mark.parametrize("confidence,expected", [(1.7, 1.0), (-0.5, 0.0), (0.5, 0.5)])
def test_confidence_is_clamped(confidence, expected):
    raw = '{"label": "general", "confidence": %s}' % confidence
    assert _parse_llm_label(raw)[1] == expected


def test_missing_confidence_defaults_rather_than_failing():
    assert _parse_llm_label('{"label": "general"}') == ("general", 0.8)


def test_unknown_label_in_valid_json_falls_through_to_the_text_scan():
    # "urgent" is not a label we accept, and the text contains none of the
    # fallback keywords either.
    assert _parse_llm_label('{"label": "urgent", "confidence": 0.9}')[0] == "general"


def test_unparseable_output_is_general():
    assert _parse_llm_label("I'm sorry, I can't help with that.") == ("general", 0.5)
    assert _parse_llm_label("") == ("general", 0.5)


# --- P2-5: the fallback is too loose. These pin the current behaviour so the
# --- fix is a deliberate change with a failing test, not a surprise.

def test_p2_5_bare_word_rate_in_prose_becomes_a_rate_card():
    """KNOWN ISSUE (P2-5). Any reply mentioning 'rate' — including a refusal or
    an explanation — is read as a rate card at 0.7 confidence.

    In 17,241 classified rows this has never fired, because the providers in use
    return clean JSON. It is a latent trap, not a live bug: change providers and
    it starts mislabelling.
    """
    assert _parse_llm_label("The rate of change was unclear") == ("quotation_rate_card", 0.7)


def test_p2_5_the_word_customer_alone_becomes_a_customer_requirement():
    """KNOWN ISSUE (P2-5). Same trap on the other branch, and it is checked
    first — so 'the customer asked about rates' becomes customer_requirement."""
    assert _parse_llm_label("the customer asked about rates") == ("customer_requirement", 0.7)
