"""
Secret redaction.

This filter exists because OPENAI_API_KEY's first characters were being logged
on every daily_report run, into GitHub Actions output. It is a security control,
and an untested security control is a guess.

The other half of the job is not over-redacting: a filter that masks RFQ
references or port codes makes the logs useless and gets switched off.
"""

import logging

from backend.core.logging_config import SecretRedactingFilter, collect_secrets

SECRET = "super-secret-value-12345"


def _apply(filter_: SecretRedactingFilter, message: str, *args) -> str:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, message, args or None, None)
    filter_.filter(record)
    return record.getMessage()


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
