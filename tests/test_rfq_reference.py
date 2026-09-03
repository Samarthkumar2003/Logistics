"""
The most important test file in this repository.

Every rate card's attribution runs through `extract_rfq_reference`. If it stops
matching a form an agent replies with, that reply silently attaches to nothing
and the operator never sees the quote. If it matches too loosely, a reply
attaches to the wrong shipment — which is worse, because it looks like it worked.

Two forms are live simultaneously and both must keep working:

    RFQId:20260101-a1b2c3d4    current subject token
    RFQ-20260101-a1b2          canonical / legacy, ~150 still out with agents

Two hex *widths* are also live. New references use 8 characters; every reference
issued before the widening used 4 and many are still awaiting a reply, so
dropping the 4-char form would strand them.
"""

import pytest

from backend.core.rfq_reference import (
    canonical,
    extract_rfq_reference,
    has_rfq_reference,
    inject_reference,
    subject_token,
)

REF = "RFQ-20260101-a1b2"


# ---------------------------------------------------------------------------
# extract_rfq_reference — always returns the canonical form, or None
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "RFQId:20260101-a1b2",
    "RFQ-20260101-a1b2",
    "Re: RFQId:20260101-a1b2 | Mumbai to Hamburg",
    "Fwd: Re: RFQId:20260101-a1b2",
    "Our rates for RFQId:20260101-a1b2 attached",
    "RFQId: 20260101-a1b2",          # space after the colon
    "RFQ Id : 20260101-a1b2",        # a human retyping it
    "RFQID:20260101-A1B2",           # shouting, uppercase hex
    "rfqid:20260101-a1b2",           # all lowercase
])
def test_extracts_canonical_form_from_every_live_variant(text):
    assert extract_rfq_reference(text) == REF


@pytest.mark.parametrize("text", [
    "",
    "Quotation for Mumbai to Hamburg",
    "RFQ-20260101-zzzz",          # zzzz is not hex
    "RFQ-2026011-a1b2",           # 7-digit date
    "RFQ-202601011-a1b2",         # 9-digit date
    "RFQ 20260101-a1b2",          # no separator: not a reference we ever issued
    "20260101-a1b2",              # bare core, no prefix
])
def test_returns_none_when_there_is_no_reference(text):
    assert extract_rfq_reference(text) is None


def test_none_input_does_not_raise():
    # Supabase hands back None for an unset column often enough that this is a
    # real code path, not a defensive nicety.
    assert extract_rfq_reference(None) is None
    assert has_rfq_reference(None) is False


def test_first_reference_wins_when_a_thread_quotes_several():
    subject = "Re: RFQId:20260101-a1b2 (was RFQId:20251231-c3d4)"
    assert extract_rfq_reference(subject) == REF


def test_extraction_is_case_insensitive_but_output_is_not():
    # The stored key in rfq_jobs.reference is lowercase. An agent SHOUTING the
    # reference back must still resolve to the same row.
    assert extract_rfq_reference("RFQID:20260101-A1B2") == REF
    # The hex core is what gets compared against rfq_jobs.reference.
    assert extract_rfq_reference("RFQID:20260101-A1B2").split("-", 1)[1].islower()


# ---------------------------------------------------------------------------
# canonical / subject_token / round-trip
# ---------------------------------------------------------------------------

def test_canonical_lowercases():
    assert canonical("20260101-A1B2") == REF


def test_subject_token_uses_the_labelled_form():
    assert subject_token(REF) == "RFQId:20260101-a1b2"


def test_subject_token_passes_through_anything_it_does_not_recognise():
    # A caller handing over something unexpected should still get a usable
    # subject rather than an empty one.
    assert subject_token("not-a-reference") == "not-a-reference"
    assert subject_token("") == ""


def test_round_trip_survives_the_subject_line():
    """canonical -> subject -> extracted must be the identity.

    This is the actual production loop: we store canonical, we send the token,
    the agent replies, we extract. Any asymmetry here loses the reply.
    """
    for core in ("20260101-a1b2", "20251231-ffff", "20260630-0000"):
        reference = canonical(core)
        assert extract_rfq_reference(subject_token(reference)) == reference


# ---------------------------------------------------------------------------
# has_rfq_reference — drives the classifier rule
# ---------------------------------------------------------------------------

def test_has_reference_agrees_with_extract():
    assert has_rfq_reference("Re: RFQId:20260101-a1b2") is True
    assert has_rfq_reference("Re: your quotation") is False


# ---------------------------------------------------------------------------
# inject_reference — every outgoing subject goes through this
# ---------------------------------------------------------------------------

def test_prepends_when_the_subject_has_no_reference():
    assert inject_reference("Rate request Mumbai-Hamburg", REF) == \
        "RFQId:20260101-a1b2 | Rate request Mumbai-Hamburg"


def test_replaces_a_reference_the_model_invented():
    # The LLM drafting path used to emit its own placeholder. Exactly one
    # reference must survive, and it must be ours.
    subject = "RFQ-20251231-c3d4 | Rate request"
    assert inject_reference(subject, REF) == "RFQId:20260101-a1b2 | Rate request"


def test_replaces_only_the_first_reference():
    subject = "RFQId:20251231-c3d4 re RFQId:20251230-e5f6"
    result = inject_reference(subject, REF)
    assert result.startswith("RFQId:20260101-a1b2")
    assert result.count("RFQId:") == 2  # the second is left alone


def test_converts_a_legacy_canonical_subject_to_the_token_form():
    assert inject_reference("RFQ-20260101-a1b2 | Rates", REF) == \
        "RFQId:20260101-a1b2 | Rates"


def test_empty_subject_yields_a_bare_token_with_no_dangling_separator():
    assert inject_reference("", REF) == "RFQId:20260101-a1b2"
    assert inject_reference("   ", REF) == "RFQId:20260101-a1b2"


def test_injected_subject_is_always_extractable():
    """The guarantee the whole reply pipeline rests on: whatever subject goes
    out, we can read the reference back out of it."""
    for subject in ("", "Rates please", "Re: RFQ-20251231-c3d4 | old", "  spaced  "):
        assert extract_rfq_reference(inject_reference(subject, REF)) == REF


# ---------------------------------------------------------------------------
# Hex width — 8 for new references, 4 still honoured for the ones in the wild
# ---------------------------------------------------------------------------

WIDE = "RFQ-20260101-a1b2c3d4"


@pytest.mark.parametrize("text", [
    "RFQId:20260101-a1b2c3d4",
    "RFQ-20260101-a1b2c3d4",
    "Re: RFQId:20260101-a1b2c3d4 | Mumbai to Hamburg",
    "RFQ Id : 20260101-a1b2c3d4",
    "RFQID:20260101-A1B2C3D4",
])
def test_extracts_the_8_char_form(text):
    assert extract_rfq_reference(text) == WIDE


def test_the_4_char_form_still_resolves():
    """~150 legacy references are still out with agents awaiting a reply."""
    assert extract_rfq_reference("Re: RFQId:20260101-a1b2") == REF


def test_both_widths_round_trip_through_the_subject_line():
    for core in ("20260101-a1b2", "20260101-a1b2c3d4", "20251231-ffffffff",
                 "20260630-00000000", "20251231-ffff"):
        reference = canonical(core)
        assert extract_rfq_reference(subject_token(reference)) == reference


def test_a_longer_hex_run_matches_nothing_rather_than_matching_wrongly():
    """Refusing to attribute is recoverable; attributing to the wrong shipment
    is not. A 12-char run is not a reference we ever issued, so the pattern must
    not shave a valid-looking 8- or 4-char core off the front of it."""
    assert extract_rfq_reference("RFQ-20260101-a1b2c3d4e5f6") is None
    assert has_rfq_reference("RFQ-20260101-a1b2c3d4e5f6") is False


def test_a_wide_reference_replaces_a_legacy_one_in_a_subject():
    assert inject_reference("RFQ-20260101-a1b2 | Rates", WIDE) == \
        "RFQId:20260101-a1b2c3d4 | Rates"


def test_generated_references_are_8_hex_and_extractable():
    """The generator and the matcher must not drift apart — a reference we can
    mint but cannot read back is an RFQ whose reply can never be attributed."""
    from backend.services.rfq_service import new_reference

    seen = set()
    for _ in range(200):
        reference = new_reference()
        core = reference.split("-", 2)[2]
        assert len(core) == 8, reference
        assert extract_rfq_reference(reference) == reference
        seen.add(reference)
    # Not a distribution test — just that the suffix varies at all. A constant
    # suffix would collide on the second RFQ of the day.
    assert len(seen) > 190
