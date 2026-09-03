"""
Secret redaction.

This filter exists because OPENAI_API_KEY's first characters were being logged
on every daily_report run, into GitHub Actions output. It is a security control,
and an untested security control is a guess.

The other half of the job is not over-redacting: a filter that masks RFQ
references or port codes makes the logs useless and gets switched off.

The formatter tests at the foot of this file cover P1-2: a filter runs before
`Formatter.formatException`, so it cannot see a traceback, and every
`logger.exception` call used to write its frames verbatim past both matchers.
"""

import json
import logging
import sys

from backend.core.logging_config import (
    RedactingFormatter,
    SecretRedactingFilter,
    _json_formatter,
    collect_secrets,
    scrub,
)

SECRET = "super-secret-value-12345"


def _apply(filter_: SecretRedactingFilter, message: str, *args) -> str:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, message, args or None, None)
    filter_.filter(record)
    return record.getMessage()


def _record(msg: str = "operation failed", *, exc: BaseException | None = None,
            sinfo: str | None = None, **extra) -> logging.LogRecord:
    """A record carrying whatever a real `logger.exception` call would carry."""
    exc_info = None
    if exc is not None:
        try:
            raise exc
        except BaseException:
            exc_info = sys.exc_info()
    record = logging.LogRecord(
        "test", logging.ERROR, __file__, 1, msg, None, exc_info, None, sinfo
    )
    # Supplied by CorrelationFilter in the real stack; the JSON format string
    # names them, so they have to exist before a formatter sees the record.
    for field in ("ctx", "request_id", "scan_id", "job_id", "email_id"):
        setattr(record, field, "")
    for key, value in extra.items():
        setattr(record, key, value)
    return record


# ---------------------------------------------------------------------------
# collect_secrets
# ---------------------------------------------------------------------------

def test_collects_values_of_credential_shaped_names():
    found = collect_secrets({
        "OPENAI_API_KEY": SECRET,
        "SUPABASE_KEY": "another-long-secret",
        "EMAIL_PASSWORD": "hunter2-but-longer",
        "GMAIL_REFRESH_TOKEN": "1//refresh-token-value",
        "GOOGLE_OAUTH_CLIENT_SECRET": "client-secret-value",
    })
    assert SECRET in found
    assert len(found) == 5


def test_ignores_names_that_are_not_credentials():
    found = collect_secrets({"SMTP_SERVER": "smtp.gmail.com", "LOG_LEVEL": "INFO"})
    assert found == []


def test_ignores_numeric_values():
    """SMTP_PORT matches the name pattern. Redacting every '587' in the logs
    would be worse than the problem being solved."""
    assert collect_secrets({"SMTP_PORT": "587", "TIMEOUT_KEY": "30000"}) == []


def test_ignores_short_values():
    # Too short to be a credential, long enough to appear inside ordinary words.
    assert collect_secrets({"API_KEY": "abc"}) == []


def test_ignores_empty_values():
    # An unset optional credential must not turn into a filter that matches "".
    assert collect_secrets({"EMAIL_PASSWORD": ""}) == []


def test_longest_first_so_a_nested_secret_is_masked_whole():
    found = collect_secrets({"A_KEY": "secret-value", "B_KEY": "secret-value-longer"})
    assert found[0] == "secret-value-longer"


# ---------------------------------------------------------------------------
# The filter
# ---------------------------------------------------------------------------

def test_redacts_a_secret_passed_as_an_argument():
    f = SecretRedactingFilter([SECRET])
    assert _apply(f, "key is %s", SECRET) == "key is ***REDACTED***"


def test_redacts_a_secret_embedded_in_the_message():
    f = SecretRedactingFilter([SECRET])
    assert SECRET not in _apply(f, f"connecting with {SECRET} now")


def test_clears_args_so_the_formatter_cannot_re_expand_the_secret():
    """The redaction has to survive formatting. If args were left in place, the
    handler would re-run %-substitution and put the secret straight back."""
    f = SecretRedactingFilter([SECRET])
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "key %s", (SECRET,), None)
    f.filter(record)
    assert record.args == ()
    assert SECRET not in record.getMessage()


def test_redacts_recognisable_credential_shapes_it_was_never_told_about():
    # An error string echoing a header, from a provider we never configured.
    f = SecretRedactingFilter([])
    masked = _apply(f, "rejected sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789")
    assert "sk-proj" not in masked
    assert "***REDACTED***" in masked


def test_leaves_ordinary_lines_completely_untouched():
    f = SecretRedactingFilter([SECRET])
    line = "linked RFQ-20260101-a1b2 to job, port INNSA, 587 bytes"
    assert _apply(f, line) == line


def test_does_not_mask_business_identifiers_that_look_like_tokens():
    f = SecretRedactingFilter([])
    for value in ("RFQ-20260101-a1b2", "RFQId:20260101-a1b2", "BSPL123456", "19fe71005df6e2f4"):
        assert _apply(f, f"processing {value}") == f"processing {value}"


def test_an_empty_secret_list_is_not_a_wildcard():
    assert _apply(SecretRedactingFilter([]), "nothing to hide") == "nothing to hide"


def test_a_broken_format_string_passes_through_rather_than_exploding():
    """Logging must never be the thing that crashes a request. A bad format
    string is the formatter's problem to report, not this filter's."""
    f = SecretRedactingFilter([SECRET])
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "%d items", ("not-a-number",), None)
    assert f.filter(record) is True


def test_the_filter_returns_true_so_lines_are_never_dropped():
    assert SecretRedactingFilter([SECRET]).filter(
        logging.LogRecord("t", logging.INFO, __file__, 1, SECRET, None, None)
    ) is True


# ---------------------------------------------------------------------------
# The formatter — P1-2. A filter cannot reach a traceback; this can.
# ---------------------------------------------------------------------------

def test_a_raised_secret_does_not_survive_into_the_traceback():
    """The case BUGS.md P1-2 names. Reverting the formatter to a plain
    logging.Formatter fails this and the five below it."""
    out = RedactingFormatter("%(message)s", secrets=[]).format(
        _record(exc=RuntimeError("sk-live-AbCdEfGhIjKlMnOpQrStUvWx0123456789"))
    )
    assert "sk-live-" not in out
    assert "***REDACTED***" in out
    assert "RuntimeError" in out  # the diagnostic value is kept


def test_an_env_secret_in_a_traceback_is_masked():
    out = RedactingFormatter("%(message)s", secrets=[SECRET]).format(
        _record(exc=ValueError(f"connect failed for {SECRET}"))
    )
    assert SECRET not in out


def test_a_supabase_url_with_an_embedded_key_is_masked():
    """The concrete leak: the service_role JWT lands in a frame's locals or the
    URL of a failing request."""
    key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.abcdef123456"
    out = RedactingFormatter("%(message)s", secrets=[]).format(
        _record(exc=RuntimeError(f"POST https://proj.supabase.co/rest/v1/emails?apikey={key}"))
    )
    assert key not in out
    assert "eyJ" not in out


def test_stack_info_is_masked_too():
    out = RedactingFormatter("%(message)s", secrets=[SECRET]).format(
        _record(sinfo=f'  File "x.py", line 1\n    login(password="{SECRET}")')
    )
    assert SECRET not in out


def test_the_message_is_still_masked_when_there_is_no_exception():
    out = RedactingFormatter("%(message)s", secrets=[SECRET]).format(
        _record(msg=f"using {SECRET}")
    )
    assert SECRET not in out


def test_scrubbing_is_idempotent_because_two_handlers_share_one_formatter():
    """`Formatter.format` caches its rendered traceback on `record.exc_text`, so
    the second handler re-renders the already-scrubbed text. Masking a mask must
    not corrupt the line."""
    formatter = RedactingFormatter("%(message)s", secrets=[SECRET])
    record = _record(exc=RuntimeError(f"boom {SECRET}"))
    first = formatter.format(record)
    assert formatter.format(record) == first
    assert SECRET not in first


def test_the_formatter_leaves_an_ordinary_traceback_intact():
    out = RedactingFormatter("%(message)s", secrets=[SECRET]).format(
        _record(exc=KeyError("RFQ-20260101-a1b2"))
    )
    assert "RFQ-20260101-a1b2" in out
    assert "***REDACTED***" not in out


def test_scrub_handles_empty_and_untouched_text():
    assert scrub("", [SECRET]) == ""
    assert scrub("linked RFQ-20260101-a1b2, port INNSA", [SECRET]) == \
        "linked RFQ-20260101-a1b2, port INNSA"


# ---------------------------------------------------------------------------
# The JSON formatter — the production path (LOG_JSON=1 in the Dockerfile)
# ---------------------------------------------------------------------------

def test_json_formatter_masks_a_traceback():
    out = _json_formatter([SECRET]).format(_record(exc=RuntimeError(f"boom {SECRET}")))
    assert SECRET not in out
    assert json.loads(out)  # still one valid JSON object per line


def test_json_formatter_masks_extra_fields():
    """`extra=` was only ever message-redacted, so a secret handed to a
    structured field went out untouched."""
    out = _json_formatter([SECRET]).format(_record(upstream_auth=SECRET))
    assert SECRET not in out


def test_json_formatter_masks_a_secret_that_json_would_escape():
    """Scrubbing only the rendered line misses this: the quote and backslash come
    back as \\" and \\\\, so the raw substring no longer matches. The
    pre-serialisation pass is what catches it."""
    awkward = 'pa"ss\\word-long-enough'
    out = _json_formatter([awkward]).format(_record(msg=f"login {awkward} failed"))
    assert awkward not in out
    assert json.loads(out)["message"] == "login ***REDACTED*** failed"


def test_json_formatter_keeps_ordinary_content():
    out = json.loads(_json_formatter([SECRET]).format(_record(msg="linked RFQ-20260101-a1b2")))
    assert out["message"] == "linked RFQ-20260101-a1b2"
