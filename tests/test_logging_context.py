"""
Correlation ids.

Two failure modes worth guarding: an id that leaks past its block (the next
email's log lines get the previous email's id — wrong data, worse than none),
and a filter that quietly stops stamping (ids silently absent, discovered during
the next incident).
"""

import logging

import pytest

from backend.core.logging_context import (
    CorrelationFilter,
    current_ids,
    email_context,
    new_id,
    request_context,
    scan_context,
)


def _record(message: str = "hello") -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, __file__, 1, message, None, None)


def _stamp(record: logging.LogRecord) -> logging.LogRecord:
    CorrelationFilter().filter(record)
    return record


# ---------------------------------------------------------------------------
# new_id
# ---------------------------------------------------------------------------

def test_ids_are_short_and_unique():
    assert len(new_id()) == 8
    assert new_id() != new_id()
    assert new_id("scan-").startswith("scan-")


# ---------------------------------------------------------------------------
# Scoping
# ---------------------------------------------------------------------------

def test_no_context_means_empty_fields_and_an_empty_fragment():
    record = _stamp(_record())
    assert (record.request_id, record.scan_id, record.email_id) == ("", "", "")
    assert record.ctx == "", "startup lines must not be littered with empty brackets"


def test_ids_appear_only_inside_their_block():
    with request_context("req-1"):
        assert _stamp(_record()).ctx == " [req=req-1]"
    assert _stamp(_record()).ctx == ""


def test_scan_and_email_ids_nest():
    with scan_context("scan-1"):
        assert _stamp(_record()).ctx == " [scan=scan-1]"
        with email_context("msg-42"):
            assert _stamp(_record()).ctx == " [scan=scan-1 email=msg-42]"
        # back to scan level only — the email id must not linger onto the next
        # email in the batch
        assert _stamp(_record()).ctx == " [scan=scan-1]"


def test_all_three_can_be_active_at_once():
    with request_context("r"), scan_context("s"), email_context("e"):
        assert _stamp(_record()).ctx == " [req=r scan=s email=e]"


def test_context_is_released_when_the_body_raises():
    """The reason these are context managers and not set/clear calls. An
    exception mid-scan used to leave the id set, so every later line in the
    process carried a scan id for a scan that had already died."""
    with pytest.raises(ValueError):
        with scan_context("scan-boom"):
            raise ValueError("boom")
    assert current_ids()["scan_id"] == ""


def test_nested_contexts_of_the_same_kind_restore_the_outer_value():
    with request_context("outer"):
        with request_context("inner"):
            assert current_ids()["request_id"] == "inner"
        assert current_ids()["request_id"] == "outer"


def test_a_generated_id_is_returned_so_it_can_be_echoed_to_the_client():
    with request_context() as rid:
        assert rid and current_ids()["request_id"] == rid


def test_an_explicit_id_is_honoured():
    # An inbound X-Request-ID lets one trace span the frontend and the backend.
    with request_context("trace-abc123") as rid:
        assert rid == "trace-abc123"


def test_empty_email_id_does_not_add_a_fragment():
    # provider_msg_id is occasionally missing; that should read as "no id",
    # not as "email=".
    with email_context(""):
        assert _stamp(_record()).ctx == ""


# ---------------------------------------------------------------------------
# The filter contract
# ---------------------------------------------------------------------------

def test_filter_always_passes_the_record_through():
    """A filter returning False DROPS the log line. This one only annotates —
    if it ever returns falsy, logging goes silent."""
    assert CorrelationFilter().filter(_record()) is True
    with scan_context("s"):
        assert CorrelationFilter().filter(_record()) is True


def test_every_field_the_json_formatter_names_is_set():
    # _JSON_FORMAT references these by name; a missing attribute raises inside
    # the formatter, at which point logging swallows it and the line is lost.
    record = _stamp(_record())
    for attribute in ("request_id", "scan_id", "email_id", "ctx"):
        assert hasattr(record, attribute)
